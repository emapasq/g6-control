"""Tool behaviour against a simulated G6 (no MIDI port is opened): `python -m unittest discover -s tests`."""

import base64
import json
import tempfile
import unittest
from pathlib import Path

from g6_mcp import protocol as P
from g6_mcp.device import G6Device
from g6_mcp.effects import EffectCatalog
from g6_mcp import server as S
from g6_mcp.server import TOOL_NAMES, G6Tools

VECTORS = json.loads((Path(__file__).resolve().parents[2] / 'protocol' / 'test-vectors.json').read_text())


def frame(hex_text):
    return [int(x, 16) for x in hex_text.split()]


def make_ptcf(name: str, ids: list[int] = (), size: int = 120) -> bytes:
    data = bytearray(b'PTCF' + size.to_bytes(4, 'little') + bytes(size - 8))
    data[12:16] = len(ids).to_bytes(4, 'little')
    data[26:36] = name.ljust(10).encode()
    for k, effect_id in enumerate(ids):
        data[0x24 + 4 * k:0x28 + 4 * k] = effect_id.to_bytes(4, 'little')
    return bytes(data)


# ZNR, empty, FD DLXR (two entries), Tremolo: the chain of the simulated active patch
CHAIN_IDS = [0x01000040, 0, 0x0400002E, 0x00000001, 0x06000010]


def effect_record(file: str, maxima: list[int]) -> list[int]:
    """64 06 frame: parameter 0 record, then one record per parameter 2.. with the given maxima."""
    recs = []
    for mx in [1] + maxima:
        recs += [mx & 0x7F, mx >> 7, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    n = len(maxima) + 2
    name = list(file.encode().ljust(13, b'\0'))
    return [0xF0, *P.HEADER, 0x64, 0x06, *name, 0, *[0] * 16, n, *recs, 0xF7]


class Msg:
    def __init__(self, type, **kw):
        self.type = type
        self.__dict__.update(kw)


class FakeG6:
    """Answers the verified messages like a G6 would. Records every message received."""

    def __init__(self):
        self.received = []          # bodies (SysEx) and channel tuples
        self.out = []               # pending messages towards the host
        self.pc_mode = False
        self.slots = {n: make_ptcf('Empty') for n in range(1, 241)}
        self.slots[104] = make_ptcf('Surf Clean', CHAIN_IDS)
        self.active = 104
        self.bank = None
        self.selected_block = None
        # block 0 ZNR (on), blocks 2-3 FD DLXR (amp: page 2 repeats the name), block 4 Tremolo (off)
        self.chain = {0: ('ZNR', 1, ['DETCT', 'Depth', 'THRSH', 'Decay'], [0, 70, 25, 10]),
                      2: ('FD DLXR', 1, ['GAIN', 'BASS', 'MIDDLE', 'TREBLE'], [30, 55, 40, 65]),
                      3: ('FD DLXR', 0, ['PRESENCE', 'VOLUME', 'INPUT', 'SPEED'], [70, 75, 0, 0]),
                      4: ('Tremolo', 0, ['Wave', 'Depth', 'Rate', 'VOL'], [0, 35, 35, 100])}
        self.effects = [effect_record('ZNR.ZD2', [1, 100, 100, 100]),
                        effect_record('FDDLXRAU.ZD2', [100, 100, 100, 100, 100, 100, 1, 100]),
                        effect_record('TREMOLO.ZD2', [2, 100, 100, 100]),
                        effect_record('IR01.ZD2', [100])]
        self.effect_index = 0
        self.rhythm = {'bpm': 55, 'pattern': 32, 'volume': 50, 'count': 0}
        self.tuner = False
        self.stop_mode = 0
        self.looper_volume = 60
        self.position = 1
        self.silent = set()         # message names that get no reply
        self.wav_cache = ['LOOPER_000.WAV', 'LOOPER_001.WAV', 'LOOPER_002.WAV']   # what 64 35 / 64 37 report
        self.wav_scan = None        # what the loader finds (its own folder scan); None = same as the cache
        self.load_result = 0x00     # 00 RR reply to 64 39; None = no reply
        self.load_bpm = None        # BPM imported from the file's metadata
        self.loaded = None
        self.announce = True        # send 64 3B with the loaded file name
        self.late_replies = []      # 00 RR replies held back by a timed-out load, delivered at the next 64 39
        self.hold_reply = None      # with load_result None: a reply that will arrive late

    # ----- helpers -----

    def sysex(self, body):
        self.out.append(Msg('sysex', data=tuple(P.HEADER + list(body))))

    def ok(self):
        self.sysex([0x00, 0x00])

    def dump(self, prefix, ptcf):
        buf = ptcf + bytes(P.PATCH_SIZE - len(ptcf))
        return prefix + [0x28, 0x06] + P.pack7(buf) + P.crc5(buf)

    def display(self, first, last):
        data = [0x64, 0x01]
        for b in range(first, last + 1):
            if b not in self.chain:
                continue
            name, on, labels, values = self.chain[b]
            entries = [(0, 0, str(on)), (1, 0, name)]
            for k, (lab, val) in enumerate(zip(labels, values)):
                entries += [(k + 2, 1, lab), (k + 2, 0, str(val))]
            for p, kind, text in entries:
                data += [b, p, kind, 0] + list(text.encode()[:10].ljust(10, b'\0'))
        self.sysex(data)

    # ----- incoming -----

    def receive(self, m):
        if m.type != 'sysex':
            self.received.append(tuple(m.bytes()))
            if m.type == 'control_change' and m.control == 32:
                self.bank = m.value
            elif m.type == 'program_change':
                self.active = P.bank_pos_to_slot(self.bank, m.program)
            return
        b = list(m.data[3:])
        self.received.append(b)
        if b == [0x52]:
            self.pc_mode = True; self.ok()
        elif b == [0x50]:
            self.ok()
        elif b == [0x53]:
            self.pc_mode = False
        elif b == [0x00, 0x00]:
            pass
        elif b[0] == 0x46:
            slot = P.bank_pos_to_slot(b[3], b[5])
            self.sysex(self.dump([0x45] + b[1:7], self.slots[slot]))
        elif b in ([0x56], [0x57]):
            self.ok()
        elif b[0] == 0x45:
            slot = P.bank_pos_to_slot(b[3], b[5])
            self.slots[slot] = P.parse_stored_patch(b)
            self.ok()
        elif b == [0x64, 0x13]:
            self.sysex(self.dump([0x64, 0x12, 0x01], self.slots[self.active]))
        elif b == [0x33]:
            bank, pos = P.slot_to_bank_pos(self.active)
            self.out += [Msg('control_change', control=0, value=0), Msg('control_change', control=32, value=bank),
                         Msg('program_change', program=pos)]
        elif b[:2] == [0x64, 0x02]:
            self.display(b[2], b[3])
        elif b[:5] == [0x64, 0x20, 0x00, 0x64, 0x01]:
            self.selected_block = b[5]
        elif b[:4] == [0x64, 0x20, 0x00, 0x5A]:
            if b[5] == 0:  # release
                name, on, labels, values = self.chain[self.selected_block]
                self.chain[self.selected_block] = (name, 1 - on, labels, values)
                self.sysex([0x64, 0x20, 0x01, self.selected_block, 0x00, 1 - on, 0, 0, 0, 0])
        elif b[:4] == [0x64, 0x20, 0x00, 0x5B]:
            pass  # rhythm start/stop: no reply
        elif b in (P.MSG['looper_play'], P.MSG['looper_stop']):
            self.looper_playing = b == P.MSG['looper_play']
            if self.loaded:
                self.sysex([0x64, 0x3B] + list(self.loaded.encode().ljust(64, b'\0')))
        elif b[:5] == [0x64, 0x20, 0x00, 0x64, P.LOOPER_VOLUME_PARAM]:
            if 'looper_volume' in self.silent:
                return
            self.looper_volume = P.decode7(b[5:])
            self.sysex([0x64, 0x20, 0x01] + b[3:])
        elif b[:5] == [0x64, 0x20, 0x00, 0x64, P.POSITION_PARAM]:
            if 'position' in self.silent:
                return
            self.position = P.decode7(b[5:])
            self.sysex([0x64, 0x20, 0x01] + b[3:])
        elif b[:5] == [0x64, 0x20, 0x00, 0x64, P.STOP_MODE_PARAM]:
            if 'stop_mode' in self.silent:
                return
            self.stop_mode = P.decode7(b[5:])
            self.sysex([0x64, 0x20, 0x01] + b[3:])
        elif b[:4] == [0x64, 0x20, 0x00, 0x64]:
            name = P.parse_rhythm_param(b)['name']
            if name in self.silent:
                return
            self.rhythm[name] = P.decode7(b[5:])
            self.sysex([0x64, 0x20, 0x01] + b[3:])
        elif b[:3] == [0x64, 0x20, 0x00] and b[3] <= 8:
            block, param = b[3], b[4]
            if param >= 6:
                block, param = block + 1, param - 4
            name, on, labels, values = self.chain[block]
            values = list(values); values[param - 2] = P.decode7(b[5:])
            self.chain[block] = (name, on, labels, values)
            self.sysex([0x64, 0x20, 0x01] + b[3:])
        elif b == [0x64, 0x0A]:
            f = frame(VECTORS['rhythm_states'][0]['frame'])
            f[41], f[42], f[43] = self.rhythm['pattern'], self.rhythm['volume'], self.rhythm['count']
            f[45] = self.stop_mode
            f[46] = self.looper_volume
            f[48] = self.position
            f[6], f[7] = self.rhythm['bpm'] & 0x7F, self.rhythm['bpm'] >> 7
            self.out.append(Msg('sysex', data=tuple(f[1:-1])))
        elif b == [0x64, 0x04]:
            self.effect_index = 0; self.ok()
        elif b == [0x64, 0x05]:
            if self.effect_index < len(self.effects):
                self.out.append(Msg('sysex', data=tuple(self.effects[self.effect_index][1:-1])))
                self.effect_index += 1
            else:
                self.sysex([0x64, 0x07])
        elif b in ([0x64, 0x0B], [0x64, 0x0C]):
            self.tuner = b[1] == 0x0B
            self.sysex(b)
        elif b == [0x64, 0x3C]:
            self.out.append(Msg('sysex', data=tuple(frame(VECTORS['storage']['sd_space']['frame'])[1:-1])))
        elif b == [0x64, 0x35]:
            n = len(self.wav_cache)
            self.sysex([0x64, 0x36, n & 0x7F, n >> 7])
        elif b[:2] == [0x64, 0x37]:
            start, end = b[2] | b[3] << 7, b[4] | b[5] << 7
            data = [0x64, 0x38, end - start + 1]
            for i in range(start, end + 1):
                data += list(self.wav_cache[i].encode().ljust(64, b'\0'))
            self.sysex(data)
        elif b[:2] == [0x64, 0x39]:
            index = b[2] | b[3] << 7
            scan = self.wav_scan if self.wav_scan is not None else self.wav_cache
            while self.late_replies:   # the late reply of an earlier load arrives first
                self.sysex([0x00, self.late_replies.pop(0)])
            if self.load_result is None:
                if self.hold_reply is not None:
                    self.late_replies.append(self.hold_reply)
                return
            if self.load_result == 0:
                self.loaded = scan[index]
                if self.announce:
                    self.sysex([0x64, 0x3B] + list(self.loaded.encode().ljust(64, b'\0')))
                self.sysex([0x64, 0x20, 0x00, 0x5C, 0x00, 0x04, 0, 0, 0, 0])
                if self.load_bpm is not None:
                    self.rhythm['bpm'] = self.load_bpm
            self.sysex([0x00, self.load_result])
        else:
            raise AssertionError(f'unexpected message {P.hexs(b)}')


class FakePort:
    def __init__(self, g6):
        self.g6 = g6

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, m):
        self.g6.receive(m)

    def iter_pending(self):
        while self.g6.out:
            yield self.g6.out.pop(0)


class FakeMessage:
    def __init__(self, type, data=()):
        self.type, self.data = type, tuple(data)

    @staticmethod
    def from_bytes(data):
        if data[0] & 0xF0 == 0xB0:
            m = Msg('control_change', control=data[1], value=data[2])
        else:
            m = Msg('program_change', program=data[1])
        m.bytes = lambda: list(data)
        return m


class FakeMido:
    Message = FakeMessage

    def __init__(self, g6, present=True):
        self.g6, self.present = g6, present

    def _open(self, name, **kw):
        if not self.present:
            raise OSError('unknown port')
        return FakePort(self.g6)

    open_input = _open
    open_output = _open


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.g6 = FakeG6()
        self.tmp = tempfile.TemporaryDirectory()
        backend = FakeMido(self.g6)
        self.tools = G6Tools(device=G6Device(backend_factory=lambda: backend), catalog=EffectCatalog(),
                             backup_dir=self.tmp.name)

        # short listening windows keep the suite fast; the logic does not depend on the length
        self._timeouts = (S.WAV_LOAD_TIMEOUT, S.WAV_LOAD_LINGER, S.LOOPER_LISTEN)
        S.WAV_LOAD_TIMEOUT, S.WAV_LOAD_LINGER, S.LOOPER_LISTEN = 0.3, 0.1, 0.05

    def tearDown(self):
        S.WAV_LOAD_TIMEOUT, S.WAV_LOAD_LINGER, S.LOOPER_LISTEN = self._timeouts
        self.tmp.cleanup()

    def sent(self):
        return [b for b in self.g6.received if isinstance(b, list)]

    # ----- patches -----

    def test_patch_list(self):
        out = self.tools.patch_list()
        self.assertIn('104: Surf Clean', out)
        self.assertIn('Free slots: 1-103, 105-240', out)
        self.assertFalse(self.g6.pc_mode)

    def test_patch_read_write_with_backup(self):
        out = self.tools.patch_read(104)
        z = out.split('zptc_base64: ')[1]
        self.assertEqual(base64.b64decode(z), self.g6.slots[104])
        new = base64.b64encode(make_ptcf('New One')).decode()
        refused = self.tools.patch_write(new, 104)
        self.assertIn('not written', refused)
        self.assertEqual(P.ptcf_name(self.g6.slots[104]), 'Surf Clean')
        done = self.tools.patch_write(new, 104, overwrite=True)
        self.assertIn('identical to the file: True', done)
        self.assertEqual(P.ptcf_name(self.g6.slots[104]), 'New One')
        backups = list(Path(self.tmp.name).glob('slot104_Surf_Clean_*.zptc'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(P.ptcf_name(backups[0].read_bytes()), 'Surf Clean')
        self.assertFalse(self.g6.pc_mode)

    def test_patch_write_rejects_bad_input(self):
        self.assertIn('not valid base64', self.tools.patch_write('***', 1))
        self.assertIn('not a PTCF', self.tools.patch_write(base64.b64encode(b'XXXX1234').decode(), 1))
        self.assertIn('out of range', self.tools.patch_write(base64.b64encode(make_ptcf('A')).decode(), 0))
        self.assertEqual(self.g6.received, [])

    # ----- live -----

    def test_patch_status(self):
        out = self.tools.patch_status()
        self.assertIn('Slot 104: "Surf Clean"', out)
        self.assertIn('block 0: ZNR ON — [2] DETCT=0', out)
        self.assertIn('block 3: FD DLXR (page 2) — [2] PRESENCE=70', out)
        self.assertIn('block 4: Tremolo OFF', out)

    def test_patch_select_next_previous(self):
        self.assertIn('Slot 1 active', self.tools.patch_select(1))
        self.assertIn('Slot 2 active', self.tools.patch_next())
        self.assertIn('Slot 1 active', self.tools.patch_previous())
        self.assertIn('Slot 240 active', self.tools.patch_previous())
        self.assertIn('out of range', self.tools.patch_select(241))

    def test_effect_set_enabled(self):
        self.assertEqual(self.tools.effect_set_enabled(4, True), 'Tremolo is now on.')
        self.assertEqual(self.tools.effect_set_enabled(4, True), 'Tremolo is already on.')
        self.assertIn('page 2', self.tools.effect_set_enabled(3, False))
        self.assertIn('empty', self.tools.effect_set_enabled(7, True))

    def test_effect_set_param_validates_against_device_maximum(self):
        self.assertEqual(self.tools.effect_set_param(4, 3, 60), 'Tremolo: Depth = 60')
        self.assertTrue(self.tools.catalog.loaded)
        before = len(self.sent())
        refused = self.tools.effect_set_param(4, 2, 3)   # Wave: 0-2
        self.assertIn('0-2; 3 refused', refused)
        self.assertFalse(any(b[:4] == [0x64, 0x20, 0x00, 4] for b in self.sent()[before:]))

    def test_effect_set_param_amp_page2(self):
        self.assertEqual(self.tools.effect_set_param(3, 2, 80), 'FD DLXR: PRESENCE = 80')
        self.assertIn([0x64, 0x20, 0x00, 2, 6, 80, 0, 0, 0, 0], self.sent())   # set on the first block, param + 4
        self.assertIn('0-1; 2 refused', self.tools.effect_set_param(3, 4, 2))  # INPUT: 0-1

    def test_effect_set_param_unknown_effect_id(self):
        self.g6.slots[104] = make_ptcf('Surf Clean', CHAIN_IDS + [0x0F000010])
        self.g6.chain[5] = ('Mystery', 1, ['A', 'B', 'C', 'D'], [0, 0, 0, 0])
        self.assertIn('0x0f000010 is not in the module table', self.tools.effect_set_param(5, 2, 1))

    def test_effect_set_param_uses_the_id_not_the_display_name(self):
        name, on, labels, values = self.g6.chain[4]
        self.g6.chain[4] = ('TremRenamd', on, labels, values)   # a display name that is not in the table
        self.assertEqual(self.tools.effect_set_param(4, 3, 60), 'TremRenamd: Depth = 60')
        self.assertIn('0-2; 3 refused', self.tools.effect_set_param(4, 2, 3))

    def test_effect_set_param_patch_and_display_disagree(self):
        self.g6.chain[5] = ('Ghost', 1, ['A', 'B', 'C', 'D'], [0, 0, 0, 0])   # beyond the patch's 5 entries
        self.assertIn('has no effect in block 5', self.tools.effect_set_param(5, 2, 1))

    def test_effect_set_param_input_checks(self):
        self.assertIn('cannot be set here', self.tools.effect_set_param(0, 0, 1))
        self.assertIn('non-negative', self.tools.effect_set_param(0, 2, -1))
        self.assertIn('out of range', self.tools.effect_set_param(9, 2, 1))
        self.assertEqual(self.g6.received, [])

    def test_effects_list(self):
        out = self.tools.effects_list()
        self.assertIn('ZNR (ZNR.ZD2): [2] max 1 def 0', out)
        self.assertIn('FD DLXR (FDDLXRAU.ZD2)', out)
        self.assertNotIn('IR01', out)
        self.assertIn('(impulse responses installed: 1)', out)
        self.assertIn('TREMOLO', self.tools.effects_list(search='trem'))
        self.assertNotIn('ZNR', self.tools.effects_list(search='trem'))

    # ----- rhythm, tuner, storage -----

    def test_rhythm(self):
        self.assertIn('BPM 55, pattern No. 33 IndiePop, volume 50, COUNT off; playing: unknown',
                      self.tools.rhythm_status())
        self.assertEqual(self.tools.rhythm_set_bpm(120), 'Tempo 120 BPM: confirmed.')
        self.assertEqual(self.tools.rhythm_set_pattern(2), 'Pattern No. 2 8Beats1: confirmed.')
        self.assertEqual(self.tools.rhythm_set_volume(70), 'Rhythm volume 70: confirmed.')
        self.assertEqual(self.tools.rhythm_set_count(True), 'COUNT on: confirmed.')
        self.assertIn('BPM 120, pattern No. 2 8Beats1, volume 70, COUNT on', self.tools.rhythm_status())
        self.assertEqual(self.tools.rhythm_start(), 'Rhythm start sent.')
        self.assertEqual(self.tools.rhythm_stop(), 'Rhythm stop sent.')

    def test_rhythm_ranges_send_nothing(self):
        for result in (self.tools.rhythm_set_bpm(39), self.tools.rhythm_set_bpm(251), self.tools.rhythm_set_pattern(0),
                       self.tools.rhythm_set_pattern(69), self.tools.rhythm_set_volume(101),
                       self.tools.rhythm_set_count(2)):
            self.assertIn('nothing sent', result)
        self.assertEqual(self.g6.received, [])

    def test_rhythm_setter_not_confirmed_is_not_resent(self):
        self.g6.silent.add('volume')
        self.assertIn('not confirmed', self.tools.rhythm_set_volume(10))
        setters = [b for b in self.sent() if b[:5] == [0x64, 0x20, 0x00, 0x64, 0x5B]]
        self.assertEqual(len(setters), 1)

    def test_rhythm_and_storage_need_no_session(self):
        self.tools.rhythm_status(); self.tools.rhythm_start(); self.tools.storage_sd_space(); self.tools.tuner_on()
        self.assertEqual(self.sent(), [[0x64, 0x0A], P.MSG['rhythm_start'], [0x64, 0x3C], [0x64, 0x0B]])

    def test_tuner(self):
        self.assertEqual(self.tools.tuner_on(), 'Tuner on.')
        self.assertTrue(self.g6.tuner)
        self.assertEqual(self.tools.tuner_off(), 'Tuner off.')
        self.assertFalse(self.g6.tuner)

    def test_storage(self):
        self.assertIn('59.35 GiB free of 59.45 GiB', self.tools.storage_sd_space())
        self.assertIn('Run storage_wav_count first', self.tools.storage_wav_list())
        self.assertNotIn([0x64, 0x37], [b[:2] for b in self.sent()])
        self.assertEqual(self.tools.storage_wav_count(), 'WAV files in the device cache: 3')
        self.assertEqual(self.tools.storage_wav_list(), '0: LOOPER_000.WAV\n1: LOOPER_001.WAV\n2: LOOPER_002.WAV')
        self.assertIn('nothing sent', self.tools.storage_wav_list(0, 3))

    # ----- WAV load -----

    def load(self, name='LOOPER_001.WAV', confirmed=True):
        return self.tools.storage_wav_load(name, looper_stopped_confirmed=confirmed)

    def loads_sent(self):
        return [b for b in self.sent() if b[:2] == [0x64, 0x39]]

    def test_wav_load_by_name(self):
        out = self.load('looper_001.wav')
        self.assertIn('WAV load sent once for "LOOPER_001.WAV" (index 1)', out)
        self.assertIn('One generic reply arrived: 0x00 (completed', out)
        self.assertIn('cannot be tied to this load with certainty', out)
        self.assertIn('announced the file "LOOPER_001.WAV"', out)
        self.assertNotIn('blocked', out)
        self.assertIsNone(self.tools.wav_load_uncertain)
        self.assertIn('BPM 55 -> 55 (unchanged)', out)
        self.assertEqual(self.loads_sent(), [[0x64, 0x39, 1, 0]])
        self.assertEqual(self.g6.loaded, 'LOOPER_001.WAV')

    def test_wav_load_needs_confirmation(self):
        for confirmed in (False, 'yes', 1):
            self.assertIn('Not sent', self.load(confirmed=confirmed))
        self.assertEqual(self.g6.received, [])

    def test_wav_load_rejects_missing_ambiguous_and_invalid_names(self):
        self.assertIn('is not in the WAV list', self.load('nothing.wav'))
        self.g6.wav_cache = ['A.WAV', 'a.wav', 'B.WAV']
        self.assertIn('ambiguous', self.load('A.WAV'))
        for bad in ('', '   ', 'x\0y', 'x' * 64, None):
            self.assertIn('Not sent', self.tools.storage_wav_load(bad, looper_stopped_confirmed=True))
        self.assertEqual(self.loads_sent(), [])

    def test_wav_load_reports_a_different_file(self):
        self.g6.wav_scan = ['LOOPER_000.WAV', 'NEW_FILE.WAV', 'LOOPER_001.WAV']   # stale cache
        out = self.load('LOOPER_001.WAV')
        self.assertIn('WARNING: the G6 announced a different file: "NEW_FILE.WAV"', out)
        self.assertEqual(len(self.loads_sent()), 1)

    def test_wav_load_error_code_and_bpm_change(self):
        self.g6.load_result = 0x13
        out = self.load()
        self.assertIn('One generic reply arrived: 0x13 (undocumented code)', out)
        self.assertNotIn('refused', out.lower())
        self.g6.load_result = 0
        self.g6.load_bpm = 120
        self.assertIn('BPM 55 -> 120 (changed by the file)', self.load())

    def test_wav_load_no_reply_is_not_resent(self):
        self.g6.load_result = None
        out = self.load()
        self.assertIn('No generic reply within 0.3 s. Not resent.', out)
        self.assertIn('blocked', out)
        self.assertEqual(len(self.loads_sent()), 1)

    def test_timeout_blocks_further_loads_until_explicit_recovery(self):
        self.g6.load_result = None
        self.load()
        before = len(self.g6.received)
        out = self.load('LOOPER_002.WAV')
        self.assertIn('Not sent: the previous WAV load ("LOOPER_001.WAV"', out)
        self.assertEqual(len(self.g6.received), before)                       # nothing at all was sent
        self.assertIn('Not cleared', self.tools.storage_wav_load_clear_uncertain())
        self.assertIn('Not cleared', self.tools.storage_wav_load_clear_uncertain(looper_checked_confirmed='yes'))
        self.assertIn('Cleared', self.tools.storage_wav_load_clear_uncertain(looper_checked_confirmed=True))
        self.assertEqual(len(self.g6.received), before)                       # recovery sends nothing
        self.g6.load_result = 0
        self.assertIn('One generic reply arrived', self.load('LOOPER_002.WAV'))
        self.assertEqual(len(self.loads_sent()), 2)

    def test_late_reply_after_timeout_is_not_taken_as_the_next_result(self):
        self.g6.load_result = None
        self.g6.hold_reply = 0x16                  # the first load answers only after the window closed
        self.load('LOOPER_001.WAV')
        self.tools.storage_wav_load_clear_uncertain(looper_checked_confirmed=True)
        self.g6.load_result = 0
        out = self.load('LOOPER_002.WAV')          # receives the late 0x16, then its own 0x00
        self.assertIn('2 generic replies arrived (0x16, 0x00)', out)
        self.assertIn('outcome is uncertain', out)
        self.assertNotIn('One generic reply', out)
        self.assertIsNotNone(self.tools.wav_load_uncertain)
        self.assertIn('Not sent: the previous WAV load ("LOOPER_002.WAV"', self.load('LOOPER_000.WAV'))

    def test_uncertain_state_survives_a_restart(self):
        self.g6.load_result = None
        self.load()
        backend = FakeMido(self.g6)
        restarted = G6Tools(device=G6Device(backend_factory=lambda: backend), catalog=EffectCatalog(),
                            backup_dir=self.tmp.name)
        self.assertIn('Not sent: the previous WAV load', restarted.storage_wav_load('LOOPER_002.WAV', True))
        restarted.storage_wav_load_clear_uncertain(looper_checked_confirmed=True)
        self.assertFalse((Path(self.tmp.name) / '.wav-load-uncertain.json').exists())

    def test_uncertain_until_evaluated_if_the_call_fails_after_sending(self):
        original = self.tools._wav_load_report
        self.tools._wav_load_report = lambda *a: (_ for _ in ()).throw(RuntimeError('crash after sending'))
        with self.assertRaises(RuntimeError):
            self.load()
        self.tools._wav_load_report = original
        self.assertIn('sent, outcome not evaluated', self.load('LOOPER_002.WAV'))

    def test_missing_file_announcement_is_reported(self):
        self.g6.announce = False
        out = self.load()
        self.assertIn('No file name announced by the G6 (no 64 3B notification).', out)

    def test_wav_load_with_more_than_50_files(self):
        self.g6.wav_cache = [f'F{i:03d}.WAV' for i in range(120)]
        out = self.load('F105.WAV')
        self.assertIn('(index 105)', out)
        self.assertEqual(self.loads_sent(), [[0x64, 0x39, 105, 0]])
        lists = [b for b in self.sent() if b[:2] == [0x64, 0x37]]
        self.assertEqual(lists, [[0x64, 0x37, 0, 0, 49, 0], [0x64, 0x37, 50, 0, 99, 0], [0x64, 0x37, 100, 0, 119, 0],
                                 [0x64, 0x37, 105, 0, 105, 0]])

    def test_wav_load_invalidates_the_cached_count(self):
        self.tools.storage_wav_count()
        self.load()
        self.assertIn('Run storage_wav_count first', self.tools.storage_wav_list())

    def test_wav_load_never_sends_transport_or_clear(self):
        self.load()
        for b in self.sent():
            self.assertTrue(P.is_safe_to_send(b), P.hexs(b))
            self.assertNotEqual(b[:4], [0x64, 0x20, 0x00, 0x5C])   # looper block: play/stop/record/clear
        self.assertEqual(len(self.loads_sent()), 1)

    # ----- Looper play / stop -----

    def looper_sent(self):
        return [b for b in self.sent() if b[:4] == [0x64, 0x20, 0x00, 0x5C]]

    def test_play_needs_a_load_and_a_confirmation(self):
        self.assertIn('Not sent: play is not armed', self.tools.looper_play())
        self.assertIn('load a WAV with storage_wav_load first', self.tools.looper_arm_play(True))
        self.load()
        self.assertIn('Not armed: ask the user', self.tools.looper_arm_play())
        self.assertIn('Not armed: ask the user', self.tools.looper_arm_play('yes'))
        self.assertIn('Not sent: stop is available only after', self.tools.looper_stop())
        self.assertEqual(self.looper_sent(), [])

    def test_one_play_then_stop_then_new_confirmation(self):
        self.load('LOOPER_002.WAV')
        self.assertIn('Play armed for "LOOPER_002.WAV"', self.tools.looper_arm_play(True))
        out = self.tools.looper_play()
        self.assertIn('Play sent once', out)
        self.assertIn('Looper notifications: LOOPER_002.WAV', out)
        self.assertTrue(self.g6.looper_playing)
        self.assertIn('Not sent: play is not armed', self.tools.looper_play())      # one shot
        self.assertIn('Stop sent once', self.tools.looper_stop())
        self.assertFalse(self.g6.looper_playing)
        self.assertIn('Not sent: stop is available only after', self.tools.looper_stop())   # one shot
        self.assertIn('Not sent: play is not armed', self.tools.looper_play())      # no automatic re-enable
        self.tools.looper_arm_play(True)
        self.tools.looper_play()
        self.assertEqual(self.looper_sent(), [P.MSG['looper_play'], P.MSG['looper_stop'], P.MSG['looper_play']])

    def test_uncertain_load_cannot_be_armed(self):
        self.load()
        self.g6.load_result = None
        self.load('LOOPER_002.WAV')
        self.assertIn('uncertain outcome', self.tools.looper_arm_play(True))
        self.tools.storage_wav_load_clear_uncertain(looper_checked_confirmed=True)
        self.assertIn('load a WAV with storage_wav_load first', self.tools.looper_arm_play(True))   # needs a new load

    def test_a_new_load_withdraws_the_permission(self):
        self.load()
        self.tools.looper_arm_play(True)
        self.load('LOOPER_002.WAV')
        self.assertIn('Not sent: play is not armed', self.tools.looper_play())

    def test_play_permission_is_consumed_before_sending_and_survives_restart(self):
        self.load()
        self.tools.looper_arm_play(True)
        broken = G6Tools(device=G6Device(backend_factory=lambda: FakeMido(self.g6, present=False)),
                         catalog=EffectCatalog(), backup_dir=self.tmp.name)
        self.assertTrue(broken.looper['armed'])                   # read from disk
        with self.assertRaises(Exception):
            broken.looper_play()                                  # the port cannot be opened
        restarted = G6Tools(device=G6Device(backend_factory=lambda: FakeMido(self.g6)), catalog=EffectCatalog(),
                            backup_dir=self.tmp.name)
        self.assertIn('Not sent: play is not armed', restarted.looper_play())
        self.assertIn('Stop sent once', restarted.looper_stop())  # stop stays available after a restart
        self.assertEqual(self.looper_sent(), [P.MSG['looper_stop']])

    def test_confirming_a_stopped_looper_replaces_a_pending_stop(self):
        self.load()
        self.tools.looper_arm_play(True)
        self.tools.looper_play()                                   # then stopped from the foot switch
        self.tools.looper_arm_play(True)
        self.assertIn('Not sent: stop is available only after', self.tools.looper_stop())

    # ----- Looper settings -----

    def test_looper_settings_read_only(self):
        out = self.tools.looper_settings()
        self.assertIn('volume 60, STOP MODE INSTANT, POSITION POST-EFFECTS, LOOP MODE MONO', out)
        self.assertIn('not readable over USB', out)
        self.assertEqual(self.sent(), [[0x64, 0x0A]])

    def test_set_stop_mode_once_with_read_back(self):
        out = self.tools.looper_set_stop_mode(2)
        self.assertIn('STOP MODE FADE OUT: confirmed.', out)
        self.assertIn('Read back: volume 60, STOP MODE FADE OUT', out)
        self.assertIn('not the Looper transport state', out)
        setters = [b for b in self.sent() if b[:5] == [0x64, 0x20, 0x00, 0x64, 0x5E]]
        self.assertEqual(setters, [[0x64, 0x20, 0x00, 0x64, 0x5E, 2, 0, 0, 0, 0]])
        self.assertEqual(self.looper_sent(), [])                  # never a stop or play

    def test_stop_mode_values_and_no_retry(self):
        for bad in (3, -1, True, '0', 1.5):
            self.assertIn('nothing sent', self.tools.looper_set_stop_mode(bad))
        self.assertEqual(self.g6.received, [])
        self.g6.silent.add('stop_mode')
        self.assertIn('not confirmed by the G6 (not resent)', self.tools.looper_set_stop_mode(1))
        self.assertEqual(len([b for b in self.sent() if b[:5] == [0x64, 0x20, 0x00, 0x64, 0x5E]]), 1)

    def test_looper_volume_once_with_read_back(self):
        out = self.tools.looper_set_volume(50)
        self.assertIn('Looper volume 50: confirmed.', out)
        self.assertIn('Read back: volume 50', out)
        setters = [b for b in self.sent() if b[:5] == [0x64, 0x20, 0x00, 0x64, 0x5F]]
        self.assertEqual(setters, [[0x64, 0x20, 0x00, 0x64, 0x5F, 50, 0, 0, 0, 0]])
        for bad in (101, -1, True, '60', 1.5):
            self.assertIn('nothing sent', self.tools.looper_set_volume(bad))
        self.g6.silent.add('looper_volume')
        self.assertIn('not confirmed by the G6 (not resent)', self.tools.looper_set_volume(60))
        self.assertEqual(len([b for b in self.sent() if b[:5] == [0x64, 0x20, 0x00, 0x64, 0x5F]]), 2)
        self.assertEqual(self.looper_sent(), [])

    def test_stop_mode_not_changed_while_our_play_is_running(self):
        self.load()
        self.tools.looper_arm_play(True)
        self.tools.looper_play()
        self.assertIn('Not sent: a play sent by this server has not been stopped', self.tools.looper_set_stop_mode(0))
        self.tools.looper_stop()
        self.assertIn('confirmed', self.tools.looper_set_stop_mode(0))

    def position_setters(self):
        return [b for b in self.sent() if b[:5] == [0x64, 0x20, 0x00, 0x64, 0x61]]

    def test_position_needs_confirmation_and_valid_values(self):
        for bad in (2, -1, True, '0', 0.0):
            self.assertIn('nothing sent', self.tools.looper_set_position(bad, looper_reset_confirmed=True))
        self.assertIn('stops and resets the Looper', self.tools.looper_set_position(0))
        self.assertEqual(self.g6.received, [])

    def test_position_once_with_read_back_and_looper_reset(self):
        self.load()
        self.tools.looper_arm_play(True)
        out = self.tools.looper_set_position(0, looper_reset_confirmed=True)
        self.assertIn('POSITION PRE-EFFECTS: confirmed.', out)
        self.assertIn('Read back: volume 60, STOP MODE INSTANT, POSITION PRE-EFFECTS', out)
        self.assertIn('The Looper was reset', out)
        self.assertEqual(self.position_setters(), [[0x64, 0x20, 0x00, 0x64, 0x61, 0, 0, 0, 0, 0]])
        self.assertEqual(self.tools.looper, {'loaded': None, 'armed': False, 'stop_available': False})
        self.assertIn('Not sent: play is not armed', self.tools.looper_play())
        self.assertEqual(self.looper_sent(), [])

    def test_position_same_value_is_not_resent(self):
        out = self.tools.looper_set_position(1, looper_reset_confirmed=True)
        self.assertIn('already POST-EFFECTS: nothing sent', out)
        self.assertEqual(self.sent(), [[0x64, 0x0A]])

    def test_position_unconfirmed_is_not_resent_and_refused_while_playing(self):
        self.g6.silent.add('position')
        self.assertIn('not confirmed by the G6 (not resent)', self.tools.looper_set_position(0, looper_reset_confirmed=True))
        self.assertEqual(len(self.position_setters()), 1)
        self.g6.silent.discard('position')
        self.load()
        self.tools.looper_arm_play(True)
        self.tools.looper_play()
        self.assertIn('Not sent: a play sent by this server', self.tools.looper_set_position(1, looper_reset_confirmed=True))
        self.assertEqual(len(self.position_setters()), 1)

    # ----- device -----

    def test_missing_device(self):
        tools = G6Tools(device=G6Device(backend_factory=lambda: FakeMido(self.g6, present=False)))
        server_tool = __import__('g6_mcp.server', fromlist=['_guard'])._guard(tools, tools.rhythm_start)
        self.assertIn('G6 not found', server_tool())

    def test_every_sent_message_is_allowlisted(self):
        for name in ('patch_list', 'patch_status', 'rhythm_status', 'effects_list', 'storage_wav_count'):
            getattr(self.tools, name)()
        self.tools.effect_set_param(4, 3, 10)
        self.tools.effect_set_enabled(0, False)
        for b in self.sent():
            self.assertTrue(P.is_safe_to_send(b), P.hexs(b))


class Catalog(unittest.TestCase):
    def test_send_return_ids_on_firmware_2_00(self):
        c = EffectCatalog()
        self.assertEqual(c.resolve_id(0x1A000020), 'FX_LOOP.ZD2')
        self.assertEqual(c.resolve_id(0x1A000023), 'SEND.ZD2')
        self.assertEqual(c.resolve_id(0x1A000026), 'RETURN.ZD2')
        self.assertEqual(c.resolve_id(0x030001A8), 'OVRDRV.ZD2')
        self.assertIsNone(c.resolve_id(0x1A000010))   # G11 id, not used by the G6

    def test_effect_ids_of_a_patch(self):
        self.assertEqual(P.ptcf_effect_ids(make_ptcf('X', CHAIN_IDS)), CHAIN_IDS)
        self.assertEqual(P.ptcf_effect_ids(make_ptcf('X')), [])

    def test_modules_added_for_firmware_2_00(self):
        c = EffectCatalog()
        for name, file in (('OverDrive', 'OVRDRV.ZD2'), ('BitCrush', 'BITCRUSH.ZD2'), ('DynaDelay', 'DYNADLY.ZD2'),
                           ('FxLoop', 'FX_LOOP.ZD2'), ('Send', 'SEND.ZD2'), ('Return', 'RETURN.ZD2')):
            self.assertEqual(c.resolve(name), file)

    def test_fallback_needs_an_exact_normalised_name(self):
        c = EffectCatalog(file_table={})
        c.store([{'file': 'FX_LOOP.ZD2', 'params': {}}, {'file': 'OVRDRV.ZD2', 'params': {}}])
        self.assertEqual(c.resolve('FxLoop'), 'FX_LOOP.ZD2')
        self.assertIsNone(c.resolve('OverDrive'))


class PatchFormatGuide(unittest.TestCase):
    def setUp(self):
        self.text = G6Tools(device=G6Device(backend_factory=lambda: None)).patch_format_guide()

    def helpers(self):
        import re
        namespace = {}
        exec(re.search(r'```python\n(.*?)```', self.text, re.S).group(1), namespace)
        return namespace

    def test_effect_table_matches_the_module_table(self):
        import re
        listed = {}
        for row in re.findall(r'^\| (0x[0-9a-f]{8}) \| ([^|]+?) \|(?: (0x[0-9a-f]{8}) \| ([^|]+?) \|)?', self.text, re.M):
            listed[int(row[0], 16)] = row[1]
            if row[2]:
                listed[int(row[2], 16)] = row[3]
        table = json.loads((Path(S.__file__).parent / 'data' / 'effect_files.json').read_text())['effects']
        known = {int(e['id'], 16): e['name'] for e in table}
        self.assertEqual(listed, known)

    def test_helpers_build_a_valid_patch(self):
        h = self.helpers()
        template = make_ptcf('TEMPLATE', [], size=100)
        template = template[:0x24] + b'TXJ1' + bytes(4) + b'TXE1' + bytes(4) + b'EDTB' + bytes(4) + b'PRM2' + \
            (32).to_bytes(4, 'little') + bytes(range(32))
        chain = [(0x01000040, True, [0, 60, 25, 40]),                         # ZNR
                 (0x0400001c, True, [35, 45, 70, 65, 55, 60, 20, 2]),         # MS 1959 (+ hidden 2)
                 (0x00000001, True, []),
                 (0x08000020, False, [449, 20, 12, 1])]                       # AnalogDly 450 ms
        patch = h['build_patch'](template, 'Test', chain, [3, None, 0])
        P.check_ptcf(patch)
        self.assertEqual(len(patch), 100 + 28 * len(chain))
        self.assertEqual(P.ptcf_name(patch), 'Test')
        self.assertEqual(P.ptcf_effect_ids(patch), [0x01000040, 0x0400001c, 1, 0x08000020])
        edtb = patch.find(b'EDTB') + 8
        self.assertEqual(patch[edtb + 48:edtb + 72], bytes([3]) + bytes(23))         # second half of the amp
        delay = int.from_bytes(patch[edtb + 72:edtb + 96], 'little')
        self.assertEqual((delay & 1, (delay >> 1) & 0x1FFFFFFF, (delay >> 30) & 0xFFF), (0, 0x08000020, 449))
        prm2 = patch[patch.find(b'PRM2') + 8:][:32]
        self.assertEqual(prm2[:28], bytes(range(28)))                                # copied from the template
        self.assertEqual(int.from_bytes(prm2[28:], 'little'), 0x000F0F30)
        self.assertEqual(h['build_patch'](template, 'X', [], [2, 6, 7])[-4:], (0x000F7620).to_bytes(4, 'little'))
        with self.assertRaises(ValueError):
            h['edtb_entry'](0x08000020, True, [4096])


class Registration(unittest.TestCase):
    def test_tools_are_registered(self):
        try:
            from g6_mcp.server import build_server
            server = build_server(G6Tools(device=G6Device(backend_factory=lambda: FakeMido(FakeG6()))))
        except ImportError:
            self.skipTest('mcp not installed')
        import asyncio
        names = {t.name for t in asyncio.run(server.list_tools())}
        self.assertEqual(names, set(TOOL_NAMES))
        self.assertEqual(len(TOOL_NAMES), 32)


if __name__ == '__main__':
    unittest.main()
