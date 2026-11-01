"""MCP server for the Zoom G6: patches, live editing, rhythm, tuner and SD card information.

Every tool opens the USB MIDI port for the duration of the call, sends only messages from the verified list
(protocol/COMMANDS.md) and never resends a message automatically.
"""

from __future__ import annotations

import base64
import binascii
import functools
import json
import os
import re
import time
from pathlib import Path

from . import protocol as P
from .device import (DeviceError, G6Device, enumerate_effects, live_session, pc_session, read_active_patch,
                     read_active_slot, read_display, read_stored_patch)
from .effects import EffectCatalog

CONFIRM_TIMEOUT = 1.0
WAV_LOAD_TIMEOUT = 10.0   # the device copies the file before answering; no maximum time is known
WAV_LOAD_LINGER = 1.5     # keep listening after the reply for late notifications
LOOPER_LISTEN = 1.0       # listen for Looper notifications after play/stop (nothing is waited for)
DEFAULT_BACKUP_DIR = '~/.g6-control/backups'
PATCH_FORMAT_GUIDE = Path(__file__).parent / 'data' / 'patch_format.md'


def _ranges(numbers: list[int]) -> str:
    out, start = [], None
    for k, n in enumerate(numbers):
        if start is None:
            start = n
        if k == len(numbers) - 1 or numbers[k + 1] != n + 1:
            out.append(f'{start}' if start == n else f'{start}-{n}')
            start = None
    return ', '.join(out) or 'none'


def _pattern_label(index: int) -> str:
    name = P.RHYTHM_PATTERNS[index] if 0 <= index < len(P.RHYTHM_PATTERNS) else '?'
    return f'No. {index + 1} {name}'


def _safe_name(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9_.-]', '_', name) or 'patch'


class G6Tools:
    """The tool implementations. Each public method is registered as an MCP tool by build_server()."""

    def __init__(self, device: G6Device | None = None, catalog: EffectCatalog | None = None,
                 backup_dir: str | os.PathLike | None = None):
        self.device = device or G6Device()
        self.catalog = catalog or EffectCatalog()
        self.backup_dir = Path(os.path.expanduser(str(backup_dir or os.environ.get('G6_BACKUP_DIR')
                                                      or DEFAULT_BACKUP_DIR)))
        self.wav_count: int | None = None   # last WAV count read by this process (guards storage_wav_list)
        self.wav_load_state_file = self.backup_dir / '.wav-load-uncertain.json'
        self.wav_load_uncertain: dict | None = self._read_uncertain()
        self.looper_state_file = self.backup_dir / '.looper-state.json'
        self.looper: dict = self._read_looper()

    # ---------------- patches (stored) ----------------

    def patch_list(self) -> str:
        """List the names of the 240 stored patches (slots 1-240, footswitch numbering) and the free slots.
        Read-only; the G6 shows "PC Mode" for a few seconds."""
        with self.device.conversation() as conv, pc_session(conv):
            lines, free = [], []
            for slot in range(P.SLOT_MIN, P.SLOT_MAX + 1):
                name = P.ptcf_name(read_stored_patch(conv, slot))
                if name == P.EMPTY_PATCH_NAME:
                    free.append(slot)
                else:
                    lines.append(f'{slot}: {name}')
        return '\n'.join(lines + [f'Free slots: {_ranges(free)}'])

    def patch_read(self, slot: int) -> str:
        """Read the patch stored in a slot (1-240) and return its name and the .zptc file encoded in base64
        (the same format Guitar Lab exports). Read-only."""
        if not P.is_valid_slot(slot):
            return 'Slot out of range (1-240).'
        with self.device.conversation() as conv, pc_session(conv):
            ptcf = read_stored_patch(conv, slot)
        return f'name: {P.ptcf_name(ptcf)}\nzptc_base64: {base64.b64encode(ptcf).decode()}'

    def patch_write(self, zptc_base64: str, slot: int, overwrite: bool = False) -> str:
        """Write a .zptc patch (base64) to a slot (1-240) and read it back to verify it.
        An occupied slot is never written unless overwrite=True; in that case the existing patch is first
        saved as a .zptc file in the backup folder (G6_BACKUP_DIR). This changes the stored patch."""
        try:
            ptcf = base64.b64decode(zptc_base64, validate=True)
        except (binascii.Error, ValueError):
            return 'Error: zptc_base64 is not valid base64.'
        try:
            P.check_ptcf(ptcf)
        except P.ProtocolError as err:
            return f'Error: {err}. Nothing written.'
        if not P.is_valid_slot(slot):
            return 'Error: slot out of range (1-240). Nothing written.'
        message = P.write_stored_patch_msg(slot, ptcf)
        notes = []
        with self.device.conversation() as conv, pc_session(conv):
            old = read_stored_patch(conv, slot)
            old_name = P.ptcf_name(old)
            if old_name != P.EMPTY_PATCH_NAME:
                if not overwrite:
                    return f'Slot {slot} contains "{old_name}": not written (overwrite=True is required).'
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                path = self.backup_dir / f'slot{slot}_{_safe_name(old_name)}_{time.strftime("%Y%m%d-%H%M%S")}.zptc'
                path.write_bytes(old)
                notes.append(f'previous patch "{old_name}" saved to {path}')
            steps = [conv.request(P.MSG['write_begin'], P.is_ok) is not None]
            steps.append(steps[0] and conv.request(message, P.is_ok, 2.0) is not None)
            steps.append(steps[1] and conv.request(P.MSG['write_end'], P.is_ok) is not None)
            new = read_stored_patch(conv, slot)
        notes.append(f'confirmations begin/data/end: {steps}')
        notes.append(f'slot {slot} now contains "{P.ptcf_name(new)}", identical to the file: {new == ptcf}')
        return '\n'.join(notes)

    def patch_format_guide(self) -> str:
        """How to build a .zptc patch for patch_write: file layout, how effects and parameter values are stored,
        foot switch assignments, Python helpers, and the effect table (ids, processing load, parameters and ranges)
        for firmware 2.00. Read it before creating or editing a patch. Sends nothing to the G6."""
        return PATCH_FORMAT_GUIDE.read_text(encoding='utf-8')

    # ---------------- active patch (live) ----------------

    def patch_status(self) -> str:
        """Show the active patch: slot, name and, for each block (0-8), effect, ON/OFF and the parameters
        as shown on the display, each with its number (to use with effect_set_param).
        An amp uses two blocks: the second is shown as 'page 2' and has no on/off of its own."""
        with self.device.conversation() as conv:
            ptcf = live_session(conv)
            slot = read_active_slot(conv)
            blocks = read_display(conv)
        name = P.ptcf_name(ptcf) if ptcf else '?'
        lines = [f'Slot {slot or "?"}: "{name}"']
        for b in sorted(blocks):
            params = blocks[b]
            effect = P.effect_name(blocks, b)
            if not effect:
                continue
            if P.is_amp_page2(blocks, b):
                state = '(page 2)'
            else:
                state = 'ON' if params.get(0, {}).get('value') == '1' else 'OFF'
            detail = ', '.join(f'[{p}] {v.get("label", "")}={v.get("value", "")}'
                               for p, v in sorted(params.items()) if p >= 2 and v.get('label'))
            lines.append(f'block {b}: {effect} {state} — {detail}')
        return '\n'.join(lines)

    def _select(self, conv, slot: int) -> str:
        for message in P.patch_select_messages(slot):
            conv.send_channel(message)
        time.sleep(0.4)
        ptcf = read_active_patch(conv)
        return f'Slot {slot} active: "{P.ptcf_name(ptcf) if ptcf else "?"}"'

    def patch_select(self, slot: int) -> str:
        """Activate a slot (1-240) with Bank Select + Program Change and return its name."""
        if not P.is_valid_slot(slot):
            return 'Slot out of range (1-240).'
        with self.device.conversation() as conv:
            live_session(conv)
            return self._select(conv, slot)

    def _step(self, delta: int) -> str:
        with self.device.conversation() as conv:
            live_session(conv)
            slot = read_active_slot(conv)
            if not slot:
                return 'Cannot read the active slot.'
            return self._select(conv, (slot - 1 + delta) % P.SLOT_MAX + 1)

    def patch_next(self) -> str:
        """Activate the slot after the active one (240 wraps to 1)."""
        return self._step(+1)

    def patch_previous(self) -> str:
        """Activate the slot before the active one (1 wraps to 240)."""
        return self._step(-1)

    def effect_set_enabled(self, block: int, enabled: bool) -> str:
        """Switch a block (0-8) of the active patch on (enabled=True) or off, like the on/off button in
        Guitar Lab. Reads the state first and acts only if needed. Does not save the patch."""
        if not P.is_valid_block(block):
            return 'Block out of range (0-8).'
        with self.device.conversation() as conv:
            live_session(conv)
            blocks = read_display(conv)
            effect = P.effect_name(blocks, block)
            if not effect:
                return f'Block {block} is empty.'
            if P.is_amp_page2(blocks, block):
                return f'Block {block} is page 2 of {effect}: use block {block - 1}.'
            current = blocks[block].get(0, {}).get('value') == '1'
            if current == enabled:
                return f'{effect} is already {"on" if enabled else "off"}.'
            conv.drain()
            conv.send(P.select_block_msg(block))
            conv.send(P.MSG['toggle_press'])
            time.sleep(0.05)
            conv.send(P.MSG['toggle_release'])
            reply = conv.wait_body(lambda b: len(b) >= 6 and b[:5] == [0x64, 0x20, 0x01, block, 0x00], 1.5)
        if reply is None:
            return f'No confirmation from the G6 for {effect} (not resent).'
        return f'{effect} is now {"on" if reply[5] == 1 else "off"}.'

    def effect_set_param(self, block: int, param: int, value: int) -> str:
        """Set a parameter (number >= 2, see patch_status) of a block (0-8) of the active patch.
        The value is the raw value: for many parameters it equals the display, for some it does not
        (lists are indices; a range that does not start at 0 is offset, e.g. Time 1-4000 is raw 0-3999). It is checked against the
        maximum the G6 reports for that effect module (identified by the effect id stored in the active patch),
        and refused if larger. For page 2 of an amp use the numbers
        shown by patch_status: the conversion is automatic. Returns the value shown afterwards on the display.
        Does not save the patch. The first call reads the effect list (PC Mode for a few seconds)."""
        if not P.is_valid_block(block):
            return 'Block out of range (0-8).'
        if not isinstance(param, int) or param < P.EFFECT_PARAM_MIN:
            return 'Parameters 0 (on/off) and 1 (effect type) cannot be set here: use effect_set_enabled.'
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return 'Value must be a non-negative integer (raw value).'
        with self.device.conversation() as conv:
            if not self.catalog.loaded:
                with pc_session(conv):
                    self.catalog.store(enumerate_effects(conv))
            ptcf = live_session(conv)
            blocks = read_display(conv)
            effect = P.effect_name(blocks, block)
            if not effect:
                return f'Block {block} is empty.'
            target_block, target_param = block, param
            if P.is_amp_page2(blocks, block):
                target_block, target_param = block - 1, param + 4
            if target_param > P.EFFECT_PARAM_MAX:
                return f'{effect}: parameter {param} out of range. Nothing sent.'
            file, problem = self._module_of(ptcf, target_block, effect)
            if problem:
                return f'{effect}: {problem} Nothing sent.'
            rng = self.catalog.range_of(file, target_param)
            if rng is None:
                return f'{effect} ({file}) has no parameter {param} here. Nothing sent.'
            if value > rng['max']:
                return f'{effect}: parameter {param} accepts raw values 0-{rng["max"]}; {value} refused. Nothing sent.'
            conv.drain()
            conv.send(P.select_block_msg(target_block))
            reply = conv.request(P.effect_param_msg(target_block, target_param, value),
                                 lambda b: P.is_effect_confirmation(b, target_block, target_param), 1.5)
            shown = read_display(conv, block, block).get(block, {}).get(param, {})
        text = f'{effect}: {shown.get("label", "")} = {shown.get("value", "")}'
        if reply is None:
            return 'No confirmation from the G6 (this happens when the value was already set). Now: ' + text
        return text

    def _module_of(self, ptcf, block: int, effect: str) -> tuple[str | None, str | None]:
        """Module file of a chain block: from the effect id stored in the active patch (exact); by display name
        only when the active patch could not be read. Returns (file, None) or (None, reason)."""
        if ptcf is None:
            file = self.catalog.resolve(effect)
            return (file, None) if file else (None, 'the active patch could not be read and the name is not in the table.')
        try:
            ids = P.ptcf_effect_ids(ptcf)
        except P.ProtocolError as err:
            return None, f'{err}.'
        effect_id = ids[block] if block < len(ids) else 0
        if effect_id in (0, P.AMP_SECOND_HALF_ID):
            return None, f'the active patch has no effect in block {block}, but the display shows one.'
        file = self.catalog.resolve_id(effect_id)
        if file is None:
            return None, f'effect id 0x{effect_id:08x} is not in the module table, so the value cannot be checked.'
        return file, None

    def effects_list(self, search: str = '', include_ir: bool = False) -> str:
        """Effects installed on the G6, read from the device: module file and, for each parameter in order
        ([2], [3], ...: same numbering as effect_set_param, amps included), raw maximum and default.
        Parameter names are not part of it (see patch_status or the Zoom effect list).
        'search' filters by effect or file name (e.g. 'MUFF'); impulse responses are skipped unless include_ir=True.
        Also refreshes the ranges used by effect_set_param. PC Mode for a few seconds."""
        with self.device.conversation() as conv, pc_session(conv):
            records = enumerate_effects(conv)
        self.catalog.store(records)
        by_file = {f: n for n, f in self.catalog.file_table.items()}
        lines, ir_count = [], 0
        for r in records:
            if P.is_ir_file(r['file']):
                ir_count += 1
                if not include_ir:
                    continue
            label = by_file.get(r['file'], '')
            if search and search.upper() not in r['file'].upper() and search.upper() not in label.upper():
                continue
            params = ', '.join(f'[{n}] max {p["max"]} def {p["default"]}' for n, p in sorted(r['params'].items()))
            head = f'{label} ({r["file"]})' if label else r['file']
            lines.append(f'{head}: {params or "no parameters"}')
        lines.append(f'(impulse responses installed: {ir_count})')
        return '\n'.join(lines)

    # ---------------- rhythm (drum machine) ----------------

    def rhythm_status(self) -> str:
        """Read the rhythm settings stored on the G6: BPM, pattern, volume and COUNT (looper precount).
        Whether the drum machine is playing cannot be read over USB and is reported as unknown."""
        with self.device.conversation() as conv:
            conv.drain()
            frame = conv.request_frame(P.MSG['request_rhythm_state'], P.is_rhythm_state_frame, 1.0)
        if frame is None:
            return 'No reply to the rhythm state request (not resent).'
        s = P.parse_rhythm_state(frame)
        return (f'BPM {s["bpm"]}, pattern {_pattern_label(s["pattern"])}, volume {s["volume"]}, '
                f'COUNT {"on" if s["count"] else "off"}; playing: unknown (not readable over USB)')

    def _send_once(self, body, done: str) -> str:
        with self.device.conversation() as conv:
            conv.send(body)
            time.sleep(0.05)
        return done

    def rhythm_start(self) -> str:
        """Start the drum machine. Sent once; whether it is playing cannot be read back."""
        return self._send_once(P.MSG['rhythm_start'], 'Rhythm start sent.')

    def rhythm_stop(self) -> str:
        """Stop the drum machine. Sent once; whether it stopped cannot be read back."""
        return self._send_once(P.MSG['rhythm_stop'], 'Rhythm stop sent.')

    def _set_rhythm(self, name: str, value: int, shown: str) -> str:
        if not P.is_valid_rhythm_value(name, value):
            spec = P.RHYTHM_PARAMS[name]
            return f'{name} out of range ({spec["min"]}-{spec["max"]}): nothing sent.'
        with self.device.conversation() as conv:
            conv.drain()
            reply = conv.request(P.set_rhythm_param_msg(name, value),
                                 lambda b: P.is_rhythm_confirmation(b, name), CONFIRM_TIMEOUT)
        if reply is None:
            return f'{shown}: sent, not confirmed by the G6 (not resent).'
        confirmed = P.parse_rhythm_param(reply)['value']
        return f'{shown}: confirmed.' if confirmed == value else f'{shown}: the G6 confirmed {confirmed}.'

    def rhythm_set_bpm(self, bpm: int) -> str:
        """Set the global tempo (40-250 BPM), shared by the drum machine, effects and looper. Not saved to the patch."""
        return self._set_rhythm('bpm', bpm, f'Tempo {bpm} BPM')

    def rhythm_set_pattern(self, number: int) -> str:
        """Select the drum pattern by its number in the owner's manual (1-68, e.g. 33 = IndiePop).
        Changing pattern while playing may realign the drum machine."""
        if not isinstance(number, int) or not 1 <= number <= len(P.RHYTHM_PATTERNS):
            return f'Pattern number out of range (1-{len(P.RHYTHM_PATTERNS)}): nothing sent.'
        return self._set_rhythm('pattern', number - 1, f'Pattern {_pattern_label(number - 1)}')

    def rhythm_set_volume(self, volume: int) -> str:
        """Set the drum machine volume (0-100)."""
        return self._set_rhythm('volume', volume, f'Rhythm volume {volume}')

    def rhythm_set_count(self, enabled: bool) -> str:
        """Switch COUNT (the looper precount) on or off. It does not start anything by itself."""
        if not isinstance(enabled, bool):
            return 'enabled must be true or false: nothing sent.'
        return self._set_rhythm('count', int(enabled), f'COUNT {"on" if enabled else "off"}')

    # ---------------- tuner ----------------

    def _tuner(self, on: bool) -> str:
        with self.device.conversation() as conv:
            conv.drain()
            reply = conv.request(P.MSG['tuner_on' if on else 'tuner_off'], lambda b: P.is_tuner_echo(b, on), CONFIRM_TIMEOUT)
        state = 'on' if on else 'off'
        return f'Tuner {state}.' if reply is not None else f'Tuner {state}: sent, no echo from the G6 (not resent).'

    def tuner_on(self) -> str:
        """Switch the G6 tuner on (a distinct command, not a toggle)."""
        return self._tuner(True)

    def tuner_off(self) -> str:
        """Switch the G6 tuner off."""
        return self._tuner(False)

    # ---------------- SD card (read-only) ----------------

    def storage_sd_space(self) -> str:
        """Total and free space of the SD card, in bytes and GiB. Read-only."""
        with self.device.conversation() as conv:
            conv.drain()
            frame = conv.request_frame(P.MSG['request_sd_space'], P.is_sd_space_frame, 1.5)
        if frame is None:
            return 'No reply to the SD space request (not resent).'
        s = P.parse_sd_space(frame)
        gib = 1024 ** 3
        return (f'SD card: {s["freeBytes"] / gib:.2f} GiB free of {s["totalBytes"] / gib:.2f} GiB '
                f'({s["freeBytes"]} / {s["totalBytes"]} bytes)')

    def storage_wav_count(self) -> str:
        """Number of WAV files on the SD card, from the device's file cache (its freshness is unknown).
        Required before storage_wav_list. Read-only."""
        self.wav_count = None
        with self.device.conversation() as conv:
            conv.drain()
            frame = conv.request_frame(P.MSG['request_wav_count'], P.is_wav_count_frame, 1.5)
        if frame is None:
            return 'No reply to the WAV count request (not resent).'
        self.wav_count = P.parse_wav_count(frame)
        return f'WAV files in the device cache: {self.wav_count}'

    def storage_wav_list(self, start: int = 0, end: int | None = None) -> str:
        """Names of WAV files start..end (inclusive, 0-based, at most 50 per call) from the device's file cache.
        Needs a positive count from storage_wav_count in this session. Indices are not permanent identifiers.
        Read-only."""
        count = self.wav_count
        if not count:
            return 'Run storage_wav_count first (a positive count is required): nothing sent.'
        if end is None:
            end = min(start + P.WAV_LIST_MAX_RECORDS, count) - 1
        try:
            message = P.wav_list_msg(start, end, count)
        except P.ProtocolError as err:
            return f'{err} (count {count}): nothing sent.'
        with self.device.conversation() as conv:
            conv.drain()
            frame = conv.request_frame(message, P.is_wav_list_frame, 3.0)
        if frame is None:
            return 'No reply to the WAV list request (not resent).'
        try:
            names = P.parse_wav_list(frame, start, end)
        except P.ProtocolError as err:
            self.wav_count = None
            return f'{err}. Run storage_wav_count again.'
        return '\n'.join(f'{start + i}: {n}' for i, n in enumerate(names))

    def _read_all_wav_names(self, conv) -> list[str]:
        """WAV count, then every name in pages of at most 50. Any inconsistency stops the sequence."""
        frame = conv.request_frame(P.MSG['request_wav_count'], P.is_wav_count_frame, 1.5)
        if frame is None:
            raise DeviceError('No reply to the WAV count request.')
        count = P.parse_wav_count(frame)
        if count == 0:
            raise DeviceError('The device reports no WAV files.')
        names = []
        for start in range(0, count, P.WAV_LIST_MAX_RECORDS):
            end = min(start + P.WAV_LIST_MAX_RECORDS, count) - 1
            frame = conv.request_frame(P.wav_list_msg(start, end, count), P.is_wav_list_frame, 3.0)
            if frame is None:
                raise DeviceError(f'No reply to the WAV list request ({start}-{end}).')
            names += P.parse_wav_list(frame, start, end)
        return names

    def _rhythm_bpm(self, conv) -> int | None:
        frame = conv.request_frame(P.MSG['request_rhythm_state'], P.is_rhythm_state_frame, 1.0)
        return P.parse_rhythm_state(frame)['bpm'] if frame else None

    # The generic reply 00 RR has no opcode, index or transaction id. A reply that arrives after the listening
    # window cannot be told apart from the reply to a later request: after an uncertain load, further loads are
    # blocked until the user has checked the Looper and cleared the state explicitly (it survives restarts).

    def _read_uncertain(self) -> dict | None:
        try:
            return json.loads(self.wav_load_state_file.read_text())
        except (OSError, ValueError):
            return None

    def _set_uncertain(self, state: dict | None) -> None:
        self.wav_load_uncertain = state
        try:
            if state is None:
                self.wav_load_state_file.unlink(missing_ok=True)
            else:
                self.wav_load_state_file.parent.mkdir(parents=True, exist_ok=True)
                self.wav_load_state_file.write_text(json.dumps(state))
        except OSError:
            pass   # the state still holds for this process

    def storage_wav_load_clear_uncertain(self, looper_checked_confirmed: bool = False) -> str:
        """Allow WAV loads again after a load whose outcome was uncertain (no reply in time, or more than one reply).
        Sends nothing to the G6. Call it only after the user has looked at the Looper screen, knows which file is
        loaded and that the Looper is stopped, and confirms it (looper_checked_confirmed=True). A late reply to the
        earlier load can still arrive afterwards: the protocol cannot rule it out."""
        if self.wav_load_uncertain is None:
            return 'No uncertain WAV load is recorded: nothing to clear.'
        if looper_checked_confirmed is not True:
            return ('Not cleared: ask the user to check the Looper screen (loaded file, stopped) and confirm, then call '
                    'again with looper_checked_confirmed=True.')
        previous = self.wav_load_uncertain
        self._set_uncertain(None)
        return (f'Cleared the uncertain load of "{previous.get("name")}" ({previous.get("reason")}). WAV loads are '
                'allowed again. A late reply to that load may still arrive and cannot be told apart from a new one.')

    # ---------------- Looper play / stop (gated) ----------------
    # Block 5C, parameter 0 is the Looper's main foot-switch gesture: it plays a loaded, stopped loop, but can record
    # (empty Looper) or overdub (while playing). The flow: a WAV load by this server with a non-uncertain outcome,
    # a manual confirmation on the display (looper_arm_play), one play (the permission is consumed before sending),
    # then one stop. The state is written to disk before every send, so a crash or restart never re-enables play.

    def _read_looper(self) -> dict:
        try:
            state = json.loads(self.looper_state_file.read_text())
            return {'loaded': state.get('loaded'), 'armed': state.get('armed') is True,
                    'stop_available': state.get('stop_available') is True}
        except (OSError, ValueError, AttributeError):
            return {'loaded': None, 'armed': False, 'stop_available': False}

    def _set_looper(self, **changes) -> None:
        self.looper = {**self.looper, **changes}
        try:
            self.looper_state_file.parent.mkdir(parents=True, exist_ok=True)
            self.looper_state_file.write_text(json.dumps(self.looper))
        except OSError:
            pass   # the state still holds for this process

    def _looper_events(self, conv, seconds: float) -> list[str]:
        names = []
        for message in conv.collect(seconds):
            b = self._body(message)
            name = P.parse_looper_file_event(b) if b is not None else None
            if name is not None:
                names.append(name)
        return names

    def looper_arm_play(self, wav_loaded_and_looper_stopped_confirmed: bool = False) -> str:
        """Allow ONE looper_play. Sends nothing. Requires a WAV load done by this server whose outcome was not
        uncertain. Ask the user to look at the G6 display and confirm that the WAV is loaded and the Looper is
        stopped (not empty, not playing, not recording); only then set wav_loaded_and_looper_stopped_confirmed=True.
        Needed again before every play."""
        if self.wav_load_uncertain is not None:
            return 'Not armed: the last WAV load has an uncertain outcome (see storage_wav_load_clear_uncertain).'
        if not self.looper.get('loaded'):
            return 'Not armed: load a WAV with storage_wav_load first (its outcome must not be uncertain).'
        if wav_loaded_and_looper_stopped_confirmed is not True:
            return ('Not armed: ask the user to check the G6 display (WAV loaded, Looper stopped) and confirm, then call '
                    'again with wav_loaded_and_looper_stopped_confirmed=True.')
        self._set_looper(armed=True, stop_available=False)
        return (f'Play armed for "{self.looper["loaded"]["name"]}": the next looper_play sends one play. '
                'Nothing was sent.')

    def looper_play(self) -> str:
        """Start Looper playback: one send, only after looper_arm_play. The permission is used up before sending, so
        a second play needs a new confirmation. If the Looper is not in the confirmed state the same gesture can
        record or overdub: the arming step exists to prevent that. Afterwards looper_stop is available."""
        if not self.looper.get('armed'):
            return 'Not sent: play is not armed. Load a WAV, then call looper_arm_play after the user confirms the display.'
        name = (self.looper.get('loaded') or {}).get('name', '?')
        self._set_looper(armed=False, stop_available=True)   # consumed before the port is even opened
        with self.device.conversation() as conv:
            conv.drain()
            conv.send(P.MSG['looper_play'])
            events = self._looper_events(conv, LOOPER_LISTEN)
        lines = [f'Play sent once (Looper with "{name}"). Play is now disabled; looper_stop is available.']
        lines.append(f'Looper notifications: {", ".join(events)}.' if events else 'No Looper notification received.')
        lines.append('Check the display and listen: the device state cannot be read over USB.')
        return '\n'.join(lines)

    def looper_stop(self) -> str:
        """Stop Looper playback started by looper_play: one send. If the loop does not stop, stop it from the foot
        switch (do not repeat). Play stays disabled until a new looper_arm_play."""
        if not self.looper.get('stop_available'):
            return 'Not sent: stop is available only after a looper_play sent by this server (use the foot switch).'
        self._set_looper(stop_available=False)
        with self.device.conversation() as conv:
            conv.drain()
            conv.send(P.MSG['looper_stop'])
            events = self._looper_events(conv, LOOPER_LISTEN)
        lines = ['Stop sent once. If the loop is still playing, stop it from the foot switch: stop is not resent.']
        lines.append(f'Looper notifications: {", ".join(events)}.' if events else 'No Looper notification received.')
        lines.append('Play stays disabled until a new confirmation (looper_arm_play).')
        return '\n'.join(lines)

    # ---------------- Looper settings ----------------

    @staticmethod
    def _describe_looper_settings(s: dict) -> str:
        return (f'volume {s["volume"]}, STOP MODE {s["stopModeName"]}, POSITION {s["positionName"]}, '
                f'LOOP MODE {s["loopModeName"]}')

    def looper_settings(self) -> str:
        """Read the Looper settings stored on the G6 (one 64 0A read): volume (0-100), STOP MODE
        (INSTANT / FINISH / FADE OUT), POSITION (PRE-EFFECTS / POST-EFFECTS) and LOOP MODE. These are settings, not
        the transport state: whether the Looper is playing cannot be read. LOOP MODE is the saved MONO/STEREO
        preference (0 = MONO verified, 1 = STEREO not verified): with an SD card inserted the Looper always records in
        STEREO, whatever it says. Read-only."""
        with self.device.conversation() as conv:
            conv.drain()
            frame = conv.request_frame(P.MSG['request_rhythm_state'], P.is_rhythm_state_frame, 1.0)
        if frame is None:
            return 'No reply to the settings request (not resent).'
        return self._describe_looper_settings(P.parse_looper_settings(frame)) + \
            '. Settings only: the Looper transport state is not readable over USB.'

    def looper_set_stop_mode(self, mode: int) -> str:
        """Set the Looper STOP MODE: 0 INSTANT, 1 FINISH, 2 FADE OUT (no other values). Sent once, never retried;
        then the settings are read back. This changes a setting only (it may be saved by the device): it does not
        stop anything. Refused while a play sent by this server has not been stopped."""
        if not isinstance(mode, int) or isinstance(mode, bool) or mode not in P.STOP_MODES:
            return 'STOP MODE must be 0 (INSTANT), 1 (FINISH) or 2 (FADE OUT): nothing sent.'
        if self.looper.get('stop_available'):
            return 'Not sent: a play sent by this server has not been stopped (looper_stop or the foot switch first).'
        with self.device.conversation() as conv:
            conv.drain()
            reply = conv.request(P.set_stop_mode_msg(mode),
                                 lambda b: (P.parse_stop_mode_change(b) or {}).get('confirmed') is True,
                                 CONFIRM_TIMEOUT)
            frame = conv.request_frame(P.MSG['request_rhythm_state'], P.is_rhythm_state_frame, 1.0)
        wanted = P.STOP_MODES[mode]
        if reply is None:
            lines = [f'STOP MODE {wanted}: sent, not confirmed by the G6 (not resent).']
        else:
            got = P.parse_stop_mode_change(reply)['value']
            lines = [f'STOP MODE {wanted}: confirmed.' if got == mode
                     else f'STOP MODE {wanted}: the G6 confirmed {P.STOP_MODES.get(got, got)}.']
        if frame is None:
            lines.append('No reply to the read-back (not resent).')
        else:
            settings = P.parse_looper_settings(frame)
            lines.append('Read back: ' + self._describe_looper_settings(settings) + '.')
        lines.append('This is a setting, not the Looper transport state.')
        return '\n'.join(lines)

    def looper_set_position(self, position: int, looper_reset_confirmed: bool = False) -> str:
        """Set the Looper POSITION: 0 PRE-EFFECTS (the loop goes through the patch), 1 POST-EFFECTS. Changing it
        STOPS AND RESETS THE LOOPER (a loop or a loaded WAV is dropped from the Looper; the files on the SD card stay).
        Ask the user first and set looper_reset_confirmed=True only after they confirm the Looper is stopped and holds
        nothing they need. The current value is read first: if it already matches, nothing is sent. Sent once, never
        retried; then read back. Refused while a play sent by this server has not been stopped."""
        if not isinstance(position, int) or isinstance(position, bool) or position not in P.LOOPER_POSITIONS:
            return 'POSITION must be 0 (PRE-EFFECTS) or 1 (POST-EFFECTS): nothing sent.'
        if self.looper.get('stop_available'):
            return 'Not sent: a play sent by this server has not been stopped (looper_stop or the foot switch first).'
        if looper_reset_confirmed is not True:
            return ('Not sent: changing POSITION stops and resets the Looper. Ask the user to confirm that the Looper is '
                    'stopped and holds nothing they need, then call again with looper_reset_confirmed=True.')
        wanted = P.LOOPER_POSITIONS[position]
        with self.device.conversation() as conv:
            conv.drain()
            before = conv.request_frame(P.MSG['request_rhythm_state'], P.is_rhythm_state_frame, 1.0)
            if before is None:
                return 'No reply to the read of the current POSITION: nothing sent (not resent).'
            if P.parse_looper_settings(before)['position'] == position:
                return f'POSITION is already {wanted}: nothing sent (resending it would reset the Looper).'
            # The Looper is reset by this setter: the loaded WAV and any play permission are gone.
            self._set_looper(loaded=None, armed=False, stop_available=False)
            reply = conv.request(P.set_position_msg(position),
                                 lambda b: (P.parse_position_change(b) or {}).get('confirmed') is True,
                                 CONFIRM_TIMEOUT)
            frame = conv.request_frame(P.MSG['request_rhythm_state'], P.is_rhythm_state_frame, 1.0)
        if reply is None:
            lines = [f'POSITION {wanted}: sent, not confirmed by the G6 (not resent).']
        else:
            got = P.parse_position_change(reply)['value']
            lines = [f'POSITION {wanted}: confirmed.' if got == position
                     else f'POSITION {wanted}: the G6 confirmed {P.LOOPER_POSITIONS.get(got, got)}.']
        if frame is None:
            lines.append('No reply to the read-back (not resent).')
        else:
            lines.append('Read back: ' + self._describe_looper_settings(P.parse_looper_settings(frame)) + '.')
        lines.append('The Looper was reset: load a WAV again before a play.')
        return '\n'.join(lines)

    def looper_set_volume(self, volume: int) -> str:
        """Set the Looper volume (0-100). Sent once, never retried; then the settings are read back. A setting
        (the device keeps it after a power cycle); it does not start or stop anything."""
        if not isinstance(volume, int) or isinstance(volume, bool) or not 0 <= volume <= P.LOOPER_VOLUME_MAX:
            return f'Looper volume must be 0-{P.LOOPER_VOLUME_MAX}: nothing sent.'
        with self.device.conversation() as conv:
            conv.drain()
            reply = conv.request(P.set_looper_volume_msg(volume),
                                 lambda b: (P.parse_looper_volume_change(b) or {}).get('confirmed') is True,
                                 CONFIRM_TIMEOUT)
            frame = conv.request_frame(P.MSG['request_rhythm_state'], P.is_rhythm_state_frame, 1.0)
        if reply is None:
            lines = [f'Looper volume {volume}: sent, not confirmed by the G6 (not resent).']
        else:
            got = P.parse_looper_volume_change(reply)['value']
            lines = [f'Looper volume {volume}: confirmed.' if got == volume
                     else f'Looper volume {volume}: the G6 confirmed {got}.']
        if frame is None:
            lines.append('No reply to the read-back (not resent).')
        else:
            lines.append('Read back: ' + self._describe_looper_settings(P.parse_looper_settings(frame)) + '.')
        return '\n'.join(lines)

    def storage_wav_load(self, name: str, looper_stopped_confirmed: bool = False) -> str:
        """Load a WAV file from the SD card into the Looper, chosen by file name (as shown by storage_wav_list).
        This is NOT a read: it replaces the Looper session (it may finalise or discard what the Looper holds)
        and may change the global BPM from the file's metadata. It never plays, records or deletes anything.
        Ask the user first, and set looper_stopped_confirmed=True only after they confirm that the Looper is
        stopped and holds nothing they need to keep. The index is resolved from a fresh WAV list; the G6 then
        loads from its own folder scan, so check the file name it announces. Sent once, never retried; the reply
        alone does not prove the file was loaded: check the Looper screen."""
        if self.wav_load_uncertain is not None:
            u = self.wav_load_uncertain
            return (f'Not sent: the previous WAV load ("{u.get("name")}", {u.get("at")}) has an uncertain outcome '
                    f'({u.get("reason")}). Ask the user to check the Looper screen, then call '
                    'storage_wav_load_clear_uncertain(looper_checked_confirmed=True) before loading again.')
        if looper_stopped_confirmed is not True:
            return ('Not sent: ask the user to confirm that the Looper is stopped and holds nothing to keep, '
                    'then call again with looper_stopped_confirmed=True.')
        if not isinstance(name, str) or not name.strip() or '\0' in name or len(name) > 63:
            return 'Not sent: invalid file name.'
        self.wav_count = None   # the cache may change: any later list needs a new count
        with self.device.conversation() as conv:
            live_session(conv)
            bpm_before = self._rhythm_bpm(conv)
            names = self._read_all_wav_names(conv)
            try:
                index = P.find_wav_index(names, name)
            except P.ProtocolError as err:
                return f'Not sent: {err}.'
            count = len(names)
            frame = conv.request_frame(P.wav_list_msg(index, index, count), P.is_wav_list_frame, 3.0)
            if frame is None:
                return 'Not sent: no reply when re-reading the selected entry.'
            try:
                again = P.parse_wav_list(frame, index, index)[0]
            except P.ProtocolError as err:
                return f'Not sent: {err}.'
            if again.casefold() != names[index].casefold():
                return f'Not sent: index {index} now reads "{again}" instead of "{names[index]}": the list changed.'
            conv.drain()
            # Recorded before sending: if anything fails from here on, the outcome stays uncertain.
            self._set_uncertain({'name': names[index], 'index': index, 'reason': 'sent, outcome not evaluated',
                                 'at': time.strftime('%Y-%m-%d %H:%M:%S')})
            self._set_looper(loaded=None, armed=False, stop_available=False)
            conv.send(P.wav_load_msg(index, count))
            received = conv.collect(WAV_LOAD_TIMEOUT, done=lambda m: self._is_ack(m), linger=WAV_LOAD_LINGER)
            bpm_after = self._rhythm_bpm(conv)
        return self._wav_load_report(names[index], index, received, bpm_before, bpm_after)

    @staticmethod
    def _body(message):
        if message.type != 'sysex':
            return None
        return P.body_of([0xF0, *message.data, 0xF7])

    def _is_ack(self, message) -> bool:
        b = self._body(message)
        return b is not None and P.ack_result(b) is not None

    def _wav_load_report(self, requested: str, index: int, received: list, bpm_before, bpm_after) -> str:
        bodies = [b for b in (self._body(m) for m in received) if b is not None]
        results = [P.ack_result(b) for b in bodies if P.ack_result(b) is not None]
        announced = [n for n in (P.parse_looper_file_event(b) for b in bodies) if n is not None]
        updates = sum(1 for b in bodies if b[:2] == [0x64, 0x20])
        lines = [f'WAV load sent once for "{requested}" (index {index}).']
        uncertain = None
        if not results:
            uncertain = f'no reply within {WAV_LOAD_TIMEOUT:g} s'
            lines.append(f'No generic reply within {WAV_LOAD_TIMEOUT:g} s. Not resent. The load may still be '
                         'running or may have happened: a reply can arrive later.')
        elif len(results) > 1:
            uncertain = f'{len(results)} generic replies in the listening window'
            codes = ', '.join(f'0x{r:02X}' for r in results)
            lines.append(f'{len(results)} generic replies arrived ({codes}): at least one belongs to something else '
                         '(for example a late reply to an earlier request). The outcome is uncertain.')
        else:
            code = results[0]
            meaning = P.WAV_LOAD_RESULTS.get(code, 'undocumented code')
            lines.append(f'One generic reply arrived: 0x{code:02X} ({meaning}). '
                         'The reply has no opcode or index, so it cannot be tied to this load with certainty, and '
                         '00 does not prove the copy is correct.')
        other = [n for n in announced if n.casefold() != requested.casefold() and n != 'New Loop File']
        if other:
            lines.append(f'WARNING: the G6 announced a different file: "{other[-1]}". Its own folder scan does not '
                         'match the list: check the Looper screen and do not play before checking.')
        elif any(n.casefold() == requested.casefold() for n in announced):
            lines.append(f'The G6 announced the file "{requested}".')
        else:
            lines.append('No file name announced by the G6 (no 64 3B notification).')
        lines.append(f'Other notifications: {updates} parameter update(s).')
        if bpm_before is not None and bpm_after is not None:
            changed = 'unchanged' if bpm_before == bpm_after else 'changed by the file'
            lines.append(f'BPM {bpm_before} -> {bpm_after} ({changed}).')
        if uncertain:
            self._set_uncertain({'name': requested, 'index': index, 'reason': uncertain,
                                 'at': time.strftime('%Y-%m-%d %H:%M:%S')})
            lines.append('Further WAV loads are blocked until the user checks the Looper screen and '
                         'storage_wav_load_clear_uncertain(looper_checked_confirmed=True) is called.')
        else:
            self._set_uncertain(None)
            self._set_looper(loaded={'name': requested, 'at': time.strftime('%Y-%m-%d %H:%M:%S')},
                             armed=False, stop_available=False)
        lines.append('Confirm on the Looper screen that the expected file is loaded and stopped.')
        return '\n'.join(lines)


TOOL_NAMES = (
    'patch_list', 'patch_read', 'patch_write', 'patch_format_guide', 'patch_status', 'patch_select', 'patch_next',
    'patch_previous',
    'effect_set_enabled', 'effect_set_param', 'effects_list',
    'rhythm_status', 'rhythm_start', 'rhythm_stop', 'rhythm_set_bpm', 'rhythm_set_pattern', 'rhythm_set_volume',
    'rhythm_set_count', 'tuner_on', 'tuner_off', 'storage_sd_space', 'storage_wav_count', 'storage_wav_list',
    'storage_wav_load', 'storage_wav_load_clear_uncertain', 'looper_arm_play', 'looper_play', 'looper_stop',
    'looper_settings', 'looper_set_stop_mode', 'looper_set_volume', 'looper_set_position',
)


def _guard(tools: G6Tools, method):
    """Turn device and protocol errors into a readable tool result."""
    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (DeviceError, P.ProtocolError, P.BlockedMessage) as err:
            tools.wav_count = None
            return f'Error: {err}'
    return wrapper


def build_server(tools: G6Tools | None = None):
    from mcp.server.fastmcp import FastMCP

    tools = tools or G6Tools()
    server = FastMCP('zoom-g6', instructions=(
        'Controls a Zoom G6 guitar multi-effects processor connected over USB: stored patches (.zptc), the active '
        'patch and its effects, the drum machine, the tuner, SD card information and WAV loading into the Looper '
        '(only after the user confirms the Looper is stopped), and a gated Looper play/stop (load, confirmation on the '
        'display, one play, then stop), and the Looper settings (volume, STOP MODE, POSITION). Live changes are not saved '
        'to the patch. Only verified messages are sent and nothing is resent automatically. Before building or '
        'editing a patch, read patch_format_guide.'))
    for name in TOOL_NAMES:
        server.tool(name=name)(_guard(tools, getattr(tools, name)))
    return server


def main() -> None:
    build_server().run()


if __name__ == '__main__':
    main()
