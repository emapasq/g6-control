"""Shared protocol test vectors against the Python codec: `python -m unittest discover -s tests` (no dependencies)."""

import json
import unittest
from pathlib import Path

from g6_mcp import protocol as P

VECTORS = json.loads((Path(__file__).resolve().parents[2] / 'protocol' / 'test-vectors.json').read_text())


def frame(hex_text):
    return [int(x, 16) for x in hex_text.split()]


class Vectors(unittest.TestCase):
    def test_requests_are_built_and_allowed(self):
        by_id = {v['id']: frame(v['frame']) for v in VECTORS['requests']}
        self.assertEqual(P.to_sysex(P.MSG['request_rhythm_state']), by_id['rhythm-state-request'])
        self.assertEqual(P.to_sysex(P.MSG['rhythm_start']), by_id['rhythm-start'])
        self.assertEqual(P.to_sysex(P.MSG['rhythm_stop']), by_id['rhythm-stop'])
        self.assertEqual(P.to_sysex(P.MSG['request_sd_space']), by_id['sd-space-request'])
        self.assertEqual(P.to_sysex(P.MSG['request_wav_count']), by_id['wav-count-request'])
        for f in by_id.values():
            P.assert_safe_to_send(P.body_of(f))

    def test_setters(self):
        for v in VECTORS['setters']:
            with self.subTest(v['id']):
                body = P.set_rhythm_param_msg(v['param'], v['value'])
                self.assertEqual(P.to_sysex(body), frame(v['frame']))
                P.assert_safe_to_send(body)

    def test_confirmations(self):
        for v in VECTORS['confirmations']:
            with self.subTest(v['id']):
                body = P.body_of(frame(v['frame']))
                self.assertEqual(P.parse_rhythm_param(body), v['expect'])
                self.assertTrue(P.is_rhythm_confirmation(body, v['expect']['name']))

    def test_rhythm_states(self):
        for v in VECTORS['rhythm_states']:
            with self.subTest(v['id']):
                self.assertEqual(P.parse_rhythm_state(frame(v['frame'])), v['expect'])

    def test_notifications(self):
        for v in VECTORS['notifications']:
            with self.subTest(v['id']):
                self.assertEqual(P.parse_patch_changed(P.body_of(frame(v['frame']))), v['expect']['slot'])

    def test_blocked(self):
        for v in VECTORS['must_be_blocked']:
            if 'mcp' not in v.get('blocked_in', ['webapp', 'mcp']):
                continue
            with self.subTest(v['id']):
                with self.assertRaises(P.BlockedMessage):
                    P.assert_safe_to_send(P.body_of(frame(v['frame'])))

    def test_write_start_is_allowed_here(self):
        P.assert_safe_to_send(P.MSG['write_begin'])

    def test_live_messages(self):
        for v in VECTORS['live']:
            with self.subTest(v['id']):
                b = dict(v['build'])
                name = b.pop('msg')
                body = getattr(P, f'{name}_msg')(**b) if b else P.MSG[name]
                self.assertEqual(P.to_sysex(body), frame(v['frame']))
                P.assert_safe_to_send(body)

    def test_storage(self):
        s = VECTORS['storage']
        self.assertEqual(P.parse_sd_space(frame(s['sd_space']['frame'])), s['sd_space']['expect'])
        self.assertEqual(P.parse_wav_count(frame(s['wav_count']['frame'])), s['wav_count']['expect']['count'])
        r = s['wav_list_request']
        body = P.wav_list_msg(r['start'], r['end'], r['known_count'])
        self.assertEqual(P.to_sysex(body), frame(r['frame']))
        P.assert_safe_to_send(body)
        v = s['wav_list_reply']
        self.assertEqual(P.parse_wav_list(frame(v['frame']), v['start'], v['end']), v['expect']['names'])
        with self.assertRaises(P.ProtocolError):
            P.parse_wav_list(frame(v['frame']), 0, 2)

    def test_effect_list(self):
        e = VECTORS['effects']
        self.assertEqual(P.to_sysex(P.MSG['effects_list_start']), frame(e['list_start']['frame']))
        self.assertTrue(P.is_ok(P.body_of(frame(e['list_start']['reply']))))
        self.assertEqual(P.to_sysex(P.MSG['effects_list_next']), frame(e['list_next']['frame']))
        record = P.parse_effect_record(frame(e['record']['frame']))
        expect = e['record']['expect']
        self.assertEqual(record['file'], expect['file'])
        self.assertEqual(record['params'], {int(k): v for k, v in expect['params'].items()})
        self.assertTrue(P.is_effects_end(P.body_of(frame(e['end']['frame']))))


class WavLoad(unittest.TestCase):
    def test_vectors(self):
        w = VECTORS['wav_load']
        r = w['request']
        body = P.wav_load_msg(r['index'], r['known_count'])
        self.assertEqual(P.to_sysex(body), frame(r['frame']))
        P.assert_safe_to_send(body)
        self.assertEqual(P.ack_result(P.body_of(frame(w['reply_ok']['frame']))), w['reply_ok']['expect']['result'])
        self.assertEqual(P.ack_result(P.body_of(frame(w['reply_error']['frame']))), w['reply_error']['expect']['result'])
        self.assertEqual(len(frame(w['file_event']['frame'])), 71)
        self.assertEqual(P.parse_looper_file_event(P.body_of(frame(w['file_event']['frame']))), w['file_event']['expect']['name'])

    def test_index_validation(self):
        self.assertEqual(P.wav_load_msg(200, 256), [0x64, 0x39, 200 & 0x7F, 1])
        for index, count in ((45, 45), (-1, 45), (0, 0), (0, 257), (True, 45), (1.0, 45)):
            with self.assertRaises(P.ProtocolError):
                P.wav_load_msg(index, count)

    def test_name_resolution(self):
        names = ['hey joe.wav', 'LOOPER_001.WAV', 'LOOPER_002.WAV']
        self.assertEqual(P.find_wav_index(names, 'looper_002.wav'), 2)
        self.assertEqual(P.find_wav_index(names, ' hey joe.wav '), 0)
        with self.assertRaises(P.ProtocolError):
            P.find_wav_index(names, 'LOOPER_003.WAV')
        with self.assertRaises(P.ProtocolError):
            P.find_wav_index(['A.WAV', 'a.wav'], 'A.WAV')
        for bad in ('', '  ', 'a\0b', 'x' * 64, None):
            with self.assertRaises(P.ProtocolError):
                P.find_wav_index(names, bad)

    def test_unrelated_replies_are_not_load_results(self):
        self.assertIsNone(P.ack_result([0x64, 0x36, 1, 0]))
        self.assertIsNone(P.parse_looper_file_event([0x64, 0x3B, 0x41]))


class Looper(unittest.TestCase):
    def test_vectors(self):
        lp = VECTORS['looper']
        self.assertEqual(P.to_sysex(P.MSG['looper_play']), frame(lp['play']['frame']))
        self.assertEqual(P.to_sysex(P.MSG['looper_stop']), frame(lp['stop']['frame']))
        P.assert_safe_to_send(P.MSG['looper_play'])
        P.assert_safe_to_send(P.MSG['looper_stop'])


class LooperSettings(unittest.TestCase):
    def test_vectors(self):
        ls = VECTORS['looper_settings']
        for v in ls['states']:
            with self.subTest(v['id']):
                self.assertEqual(P.parse_looper_settings(frame(v['frame'])), v['expect'])
        for v in ls['setters']:
            with self.subTest(v['id']):
                body = P.set_stop_mode_msg(v['value'])
                self.assertEqual(P.to_sysex(body), frame(v['frame']))
                P.assert_safe_to_send(body)
        for v in ls['confirmations']:
            with self.subTest(v['id']):
                self.assertEqual(P.parse_stop_mode_change(P.body_of(frame(v['frame']))), v['expect'])

    def test_looper_volume_vectors(self):
        ls = VECTORS['looper_settings']
        for v in ls['volume_setters']:
            with self.subTest(v['id']):
                body = P.set_looper_volume_msg(v['value'])
                self.assertEqual(P.to_sysex(body), frame(v['frame']))
                P.assert_safe_to_send(body)
        for v in ls['volume_confirmations']:
            with self.subTest(v['id']):
                self.assertEqual(P.parse_looper_volume_change(P.body_of(frame(v['frame']))), v['expect'])
        for bad in (101, -1, True, 50.0, '50'):
            with self.assertRaises(P.ProtocolError):
                P.set_looper_volume_msg(bad)
        self.assertEqual(P.set_looper_volume_msg(100)[5:7], [100, 0])

    def test_older_capture_and_unverified_values(self):
        old = frame(VECTORS['rhythm_states'][0]['frame'])   # 31 October: STOP MODE 0, POSITION 1, LOOP MODE 0
        s = P.parse_looper_settings(old)
        self.assertEqual((s['stopModeName'], s['positionName'], s['loopModeName']), ('INSTANT', 'POST-EFFECTS', 'MONO'))
        old[48], old[49] = 0, 1
        s = P.parse_looper_settings(old)
        self.assertEqual(s['positionName'], 'PRE-EFFECTS')
        self.assertEqual(s['loopModeName'], 'STEREO (not verified)')
        old[48] = 2
        self.assertEqual(P.parse_looper_settings(old)['positionName'], 'unknown (2)')
        old[45], old[49] = 3, 2
        s = P.parse_looper_settings(old)
        self.assertEqual(s['stopModeName'], 'unknown (3)')
        self.assertEqual(s['loopModeName'], 'not qualified (2)')

    def test_position_vectors(self):
        ls = VECTORS['looper_settings']
        for v in ls['position_setters']:
            with self.subTest(v['id']):
                body = P.set_position_msg(v['value'])
                self.assertEqual(P.to_sysex(body), frame(v['frame']))
                P.assert_safe_to_send(body)
        for v in ls['position_confirmations']:
            with self.subTest(v['id']):
                self.assertEqual(P.parse_position_change(P.body_of(frame(v['frame']))), v['expect'])
        for bad in (2, -1, True, 1.0, '1'):
            with self.assertRaises(P.ProtocolError):
                P.set_position_msg(bad)
        self.assertIsNone(P.parse_position_change([0x64, 0x20, 0x01, 0x64, 0x5E, 1, 0, 0, 0, 0]))

    def test_only_three_stop_modes(self):
        for bad in (3, -1, True, 1.0, '1'):
            with self.assertRaises(P.ProtocolError):
                P.set_stop_mode_msg(bad)
        self.assertIsNone(P.parse_stop_mode_change([0x64, 0x20, 0x01, 0x64, 0x5B, 1, 0, 0, 0, 0]))


class Codec(unittest.TestCase):
    def test_val5(self):
        self.assertEqual(P.val5(90), [0x5A, 0, 0, 0, 0])
        self.assertEqual(P.val5(153), [0x19, 0x01, 0, 0, 0])
        self.assertEqual(P.decode7(P.val5(123456)), 123456)
        for bad in (-1, True, 1.5, 1 << 35):
            with self.assertRaises(P.ProtocolError):
                P.val5(bad)

    def test_pack_roundtrip_and_write_frame(self):
        ptcf = b'PTCF' + (100).to_bytes(4, 'little') + bytes(range(18)) + b'TESTPATCH ' + bytes(range(128, 192))
        self.assertEqual(len(ptcf), 100)
        buf = ptcf + bytes(P.PATCH_SIZE - len(ptcf))
        self.assertEqual(P.unpack7(P.pack7(buf))[:P.PATCH_SIZE], buf)
        msg = P.write_stored_patch_msg(114, ptcf)
        self.assertEqual(msg[:9], [0x45, 0, 0, 28, 0, 1, 0, 0x28, 0x06])
        self.assertEqual(len(msg), 9 + P.PACKED_PATCH_SIZE + 5)
        P.assert_safe_to_send(msg)
        corrupted = msg[:-1] + [msg[-1] ^ 1]
        self.assertFalse(P.is_safe_to_send(corrupted))  # wrong CRC is never sent
        self.assertEqual(P.ptcf_name(P.parse_stored_patch(msg + [0] * 0)), 'TESTPATCH')

    def test_ptcf_checks(self):
        with self.assertRaises(P.ProtocolError):
            P.check_ptcf(b'XXXX' + (8).to_bytes(4, 'little'))
        with self.assertRaises(P.ProtocolError):
            P.check_ptcf(b'PTCF' + (9).to_bytes(4, 'little'))

    def test_allowlist_limits(self):
        self.assertFalse(P.is_safe_to_send([0x64, 0x20, 0x00, 0x03, 0x00, 1, 0, 0, 0, 0]))  # on/off as parameter 0
        self.assertFalse(P.is_safe_to_send([0x64, 0x20, 0x00, 0x03, 0x01, 1, 0, 0, 0, 0]))  # effect type
        self.assertFalse(P.is_safe_to_send([0x64, 0x20, 0x00, 0x09, 0x02, 1, 0, 0, 0, 0]))  # block 9
        self.assertFalse(P.is_safe_to_send([0x64, 0x02, 0x00, 0x09, 0x00]))                 # display beyond 8
        self.assertFalse(P.is_safe_to_send([0x64, 0x33, 0x00]))                             # not documented
        self.assertFalse(P.is_safe_to_send([0x60, 0x00]))                                   # file system family
        with self.assertRaises(P.ProtocolError):
            P.effect_param_msg(3, 0, 1)
        with self.assertRaises(P.BlockedMessage):
            P.assert_safe_channel_message([0xC0, 4])

    def test_slots(self):
        self.assertEqual(P.slot_to_bank_pos(114), (28, 1))
        self.assertEqual(P.bank_pos_to_slot(28, 1), 114)
        self.assertEqual(P.patch_select_messages(1), [[0xB0, 0, 0], [0xB0, 32, 0], [0xC0, 0]])
        with self.assertRaises(P.ProtocolError):
            P.read_stored_patch_msg(241)

    def test_pattern_names(self):
        self.assertEqual(len(P.RHYTHM_PATTERNS), 68)
        self.assertEqual(P.RHYTHM_PATTERNS[32], 'IndiePop')


if __name__ == '__main__':
    unittest.main()
