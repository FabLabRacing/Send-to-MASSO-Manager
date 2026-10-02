import queue
import unittest

from send_to_masso_v1_8_20_rc import (
    MassoClient,
    decode_data_ack_next,
    final_chunk_trailer_len,
)


class ProtocolBoundaryTests(unittest.TestCase):
    def test_final_trailer_lengths(self):
        expected = {
            411: 2,
            906: 3,
            1000: 1,
            1001: 4,
            1002: 3,
            1003: 2,
            1021: 4,
            1327: 2,
        }
        for data_length, trailer in expected.items():
            with self.subTest(data_length=data_length):
                self.assertEqual(final_chunk_trailer_len(data_length), trailer)

    def test_compact_final_payload_after_crc_is_4byte_aligned(self):
        client = MassoClient(queue.Queue())
        for data_length in (1, 411, 906, 1000, 1001, 1002, 1003, 1021, 1327, 1421):
            with self.subTest(data_length=data_length):
                packet = client._build_data_packet(
                    0,
                    b"X" * data_length,
                    final_chunk=True,
                )
                self.assertEqual((len(packet) - 2) % 4, 0)

    def test_full_packet_stays_1438_bytes(self):
        client = MassoClient(queue.Queue())
        packet = client._build_data_packet(0, b"X" * 1422, final_chunk=True)
        self.assertEqual(len(packet), 1438)

    def test_data_ack_is_16bit_little_endian_from_bytes_6_7(self):
        for next_index in (1, 35, 255, 256, 282, 511, 512, 1024, 65535):
            with self.subTest(next_index=next_index):
                ack = (
                    b"\x00\x00\x03\x00\x0b\x00"
                    + next_index.to_bytes(2, "little")
                    + b"\x00\x00"
                )
                self.assertEqual(decode_data_ack_next(ack), next_index)

    def test_invalid_ack_rejected(self):
        with self.assertRaises(ValueError):
            decode_data_ack_next(b"\x00\x00\x03\x00\x0a\x00\x01\x00\x00\x00")


if __name__ == "__main__":
    unittest.main()
