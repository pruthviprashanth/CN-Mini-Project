"""Wire protocol: one UTF-8 JSON object per line ("\\n"-terminated) over TCP.

Every frame is an object with a "type" field. Requests may carry an "id";
the server echoes it in the reply so clients can match replies to requests.
"""
import json

MAX_FRAME = 64 * 1024
ENCODING = "utf-8"

# Reply types answer a request and carry the request's id. "msg"/"pm" frames
# pushed to recipients also carry an id (the sender's), so clients must only
# match ids on these types.
REPLY_TYPES = frozenset(("ok", "error", "ack", "pong", "rooms", "users"))


class ProtocolError(Exception):
    """A frame was not valid JSON or not a typed object."""


class FrameTooLarge(ProtocolError):
    """A frame exceeded MAX_FRAME; the stream cannot be resynchronised safely."""


def encode(msg):  # dict -> JSON line (bytes) ending in a newline; rejects frames over 64 KB
    data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode(ENCODING)
    if len(data) >= MAX_FRAME:
        raise FrameTooLarge("frame of %d bytes exceeds %d" % (len(data), MAX_FRAME))
    return data + b"\n"


def decode(line):  # one JSON line (bytes) -> dict; raises ProtocolError if it is not a valid message
    try:
        obj = json.loads(line.decode(ENCODING))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError("bad frame: %s" % exc)
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        raise ProtocolError("frame must be a JSON object with a string 'type'")
    return obj


class LineReader:
    """Reassembles newline-delimited frames from arbitrary TCP chunks.

    TCP is a byte stream: one recv() may hold half a frame or several frames.
    """

    def __init__(self, max_frame=MAX_FRAME):  # start with an empty buffer
        self.max_frame = max_frame
        self._buf = bytearray()

    def feed(self, data):  # add received bytes, return the complete lines so far, keep any partial line
        """Add received bytes; return the list of complete frames (bytes)."""
        self._buf.extend(data)
        frames = []
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._buf[:nl])
            del self._buf[:nl + 1]
            if len(line) > self.max_frame:
                raise FrameTooLarge("frame exceeds %d bytes" % self.max_frame)
            if line.strip():
                frames.append(line)
        # Checked after splitting so a large recv holding several valid frames is fine.
        if len(self._buf) > self.max_frame:
            raise FrameTooLarge("unterminated frame exceeds %d bytes" % self.max_frame)
        return frames
