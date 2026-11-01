"""MIDI transport for the G6: one conversation at a time, allowlisted sends, bounded waits, no retries."""

from __future__ import annotations

import contextlib
import threading
import time

from . import protocol as P


class DeviceError(RuntimeError):
    """The G6 is missing, does not answer, or answered something unexpected."""


def _mido_backend():
    import mido  # imported lazily: tests and `import g6_mcp` never touch MIDI

    return mido


class Conversation:
    """An open input/output pair. Every SysEx goes through the allowlist; nothing is ever resent."""

    def __init__(self, inport, outport, mido):
        self._in, self._out, self._mido = inport, outport, mido

    # ----- sending -----

    def send(self, body) -> None:
        P.assert_safe_to_send(body)
        self._out.send(self._mido.Message('sysex', data=P.HEADER + list(body)))

    def send_channel(self, data) -> None:
        P.assert_safe_channel_message(data)
        self._out.send(self._mido.Message.from_bytes(list(data)))

    # ----- receiving -----

    def drain(self) -> None:
        for _ in self._in.iter_pending():
            pass

    def wait(self, predicate, timeout: float):
        """First incoming mido message accepted by predicate(message), or None after timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in self._in.iter_pending():
                if predicate(message):
                    return message
            time.sleep(0.002)
        return None

    def collect(self, timeout: float, done=None, linger: float = 0.0) -> list:
        """Every incoming message for up to `timeout` seconds. Once `done(message)` is true, keep listening for
        `linger` more seconds (late notifications), then stop. Nothing is sent."""
        received, deadline = [], time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in self._in.iter_pending():
                received.append(message)
                if done is not None and done(message):
                    deadline = min(deadline, time.monotonic() + linger)
            time.sleep(0.005)
        return received

    def wait_frame(self, predicate, timeout: float):
        """First complete G6 SysEx frame (F0 ... F7) accepted by predicate(frame), or None."""
        def match(m):
            if m.type != 'sysex':
                return False
            frame = [0xF0, *m.data, 0xF7]
            return P.body_of(frame) is not None and predicate(frame)

        m = self.wait(match, timeout)
        return [0xF0, *m.data, 0xF7] if m is not None else None

    def wait_body(self, predicate, timeout: float):
        frame = self.wait_frame(lambda f: predicate(P.body_of(f)), timeout)
        return P.body_of(frame) if frame else None

    def request(self, body, predicate, timeout: float = 1.0):
        """Send once, return the first reply body accepted by predicate(body), or None."""
        self.send(body)
        return self.wait_body(predicate, timeout)

    def request_frame(self, body, predicate, timeout: float = 1.0):
        self.send(body)
        return self.wait_frame(predicate, timeout)


class G6Device:
    """Opens the G6 port for the duration of one tool call. A re-entrant lock serialises all tool calls
    of this server process; it does not coordinate other programs (web app, Guitar Lab)."""

    def __init__(self, port_name: str = P.PORT_NAME, backend_factory=_mido_backend):
        self.port_name = port_name
        self._backend_factory = backend_factory
        self.lock = threading.RLock()

    @contextlib.contextmanager
    def conversation(self):
        if not self.lock.acquire(timeout=10):
            raise DeviceError('MIDI port busy: nothing sent.')
        try:
            mido = self._backend_factory()
            with contextlib.ExitStack() as stack:
                try:
                    inport = stack.enter_context(mido.open_input(self.port_name))
                    outport = stack.enter_context(mido.open_output(self.port_name, autoreset=False))
                except (OSError, IOError) as err:
                    raise DeviceError(
                        f'G6 not found ({self.port_name!r}): is it powered on and connected via USB? Nothing sent.'
                    ) from err
                yield Conversation(inport, outport, mido)
        finally:
            self.lock.release()


# ---------- sessions ----------

@contextlib.contextmanager
def pc_session(conv: Conversation):
    """PC Mode (52, then 50), required for stored patch reads/writes and the effect list. Always leaves with 53."""
    if conv.request(P.MSG['enter_pc_mode'], P.is_ok) is None:
        raise DeviceError('The G6 does not respond: is it powered on and connected via USB?')
    try:
        if conv.request(P.MSG['editor_mode'], P.is_ok) is None:
            raise DeviceError('The G6 did not confirm editor mode.')
        yield conv
    finally:
        conv.send(P.MSG['exit_pc_mode'])
        time.sleep(0.1)


def live_session(conv: Conversation) -> bytes | None:
    """50 -> 53 -> 64 13 -> 00 00: editor mode, out of PC Mode, live commands enabled.
    Returns the active patch (PTCF) or None."""
    conv.drain()
    if conv.request(P.MSG['editor_mode'], P.is_ok) is None:
        raise DeviceError('The G6 does not respond: is it powered on and connected via USB?')
    conv.send(P.MSG['exit_pc_mode'])
    time.sleep(0.1)
    ptcf = read_active_patch(conv)
    conv.drain()
    return ptcf


def read_active_patch(conv: Conversation) -> bytes | None:
    dump = conv.request(P.MSG['request_active_patch'], P.is_active_patch_dump, 1.5)
    conv.send(P.MSG['host_ack'])
    time.sleep(0.05)
    if dump is None:
        return None
    try:
        return P.parse_active_patch(dump)
    except P.ProtocolError:
        return None


def read_active_slot(conv: Conversation) -> int | None:
    """33 -> CC0, CC32 = bank, Program Change = position."""
    conv.drain()
    conv.send(P.MSG['request_active_slot'])
    state = {'bank': None}

    def match(m):
        if m.type == 'control_change' and m.control == 32:
            state['bank'] = m.value
        return m.type == 'program_change'

    pc = conv.wait(match, 1.0)
    if pc is None or state['bank'] is None:
        return None
    return P.bank_pos_to_slot(state['bank'], pc.program)


def read_display(conv: Conversation, first: int = P.BLOCK_MIN, last: int = P.BLOCK_MAX) -> dict:
    conv.drain()
    reply = conv.request(P.display_request_msg(first, last), P.is_display, 1.5)
    if reply is None:
        raise DeviceError('No reply from the display request.')
    return P.parse_display(reply)


def read_stored_patch(conv: Conversation, slot: int) -> bytes:
    reply = conv.request(P.read_stored_patch_msg(slot), lambda b: P.is_stored_patch_dump(b, slot), 1.0)
    if reply is None:
        raise DeviceError(f'No reply while reading slot {slot}.')
    return P.parse_stored_patch(reply)


def enumerate_effects(conv: Conversation) -> list[dict]:
    """Effect list (inside a PC session): 64 04 -> OK, then 64 05 until 64 07. One request per record."""
    conv.drain()
    if conv.request(P.MSG['effects_list_start'], P.is_ok) is None:
        raise DeviceError('The G6 did not start the effect list.')
    records = []
    for _ in range(1000):
        frame = conv.request_frame(
            P.MSG['effects_list_next'],
            lambda f: P.is_effect_record_frame(f) or P.is_effects_end(P.body_of(f)),
        )
        if frame is None:
            raise DeviceError('Timeout while reading the effect list.')
        if P.is_effects_end(P.body_of(frame)):
            return records
        records.append(P.parse_effect_record(frame))
    raise DeviceError('Effect list did not end.')
