import unittest

from chat.protocol import (LineReader, FrameTooLarge, ProtocolError, MAX_FRAME,
                           encode, decode)


class FramingTest(unittest.TestCase):
    def test_roundtrip(self):
        msg = {"type": "msg", "room": "general", "text": "héllo ✓", "id": 7}
        frames = LineReader().feed(encode(msg))
        self.assertEqual([decode(f) for f in frames], [msg])

    def test_split_across_chunks(self):
        data = encode({"type": "ping", "id": 1}) + encode({"type": "ping", "id": 2})
        r = LineReader()
        out = []
        for i in range(len(data)):         # worst case: one byte per recv()
            out.extend(r.feed(data[i:i + 1]))
        self.assertEqual([decode(f)["id"] for f in out], [1, 2])

    def test_many_frames_one_chunk(self):
        data = b"".join(encode({"type": "ping", "id": i}) for i in range(100))
        self.assertEqual(len(LineReader().feed(data)), 100)

    def test_blank_lines_ignored(self):
        self.assertEqual(LineReader().feed(b"\n\r\n" + encode({"type": "ping"})),
                         [b'{"type":"ping"}'])

    def test_oversized_unterminated_frame_rejected(self):
        r = LineReader()
        with self.assertRaises(FrameTooLarge):
            r.feed(b"x" * (MAX_FRAME + 1))

    def test_oversized_terminated_frame_rejected(self):
        with self.assertRaises(FrameTooLarge):
            LineReader().feed(b"x" * (MAX_FRAME + 1) + b"\n")

    def test_encode_rejects_huge(self):
        with self.assertRaises(FrameTooLarge):
            encode({"type": "msg", "text": "x" * MAX_FRAME})

    def test_decode_errors(self):
        for bad in (b"not json", b"[1,2]", b'{"no_type":1}', b'{"type":5}', b"\xff\xfe"):
            with self.assertRaises(ProtocolError):
                decode(bad)


if __name__ == "__main__":
    unittest.main()
