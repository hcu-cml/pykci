"""Reading a GLB back, to check what a builder wrote."""

import json
import struct


def chunks(data: bytes) -> tuple[dict, bytes]:
    """The glTF JSON and the BIN chunk (b"" when it has none) of a GLB, read back; ValueError
    when the container is not one."""
    magic, version, total = struct.unpack_from("<III", data, 0)
    if (magic, version, total) != (0x46546C67, 2, len(data)):
        raise ValueError("not a GLB 2 file of the length its header states")
    n, kind = struct.unpack_from("<II", data, 12)
    if kind != 0x4E4F534A:
        raise ValueError("the first chunk of the GLB is not its JSON")
    doc = json.loads(data[20:20 + n].decode("utf-8"))
    if 20 + n == len(data):
        return doc, b""
    m, kind = struct.unpack_from("<II", data, 20 + n)
    if kind != 0x004E4942 or 28 + n + m != len(data):
        raise ValueError("the second chunk of the GLB is not its binary buffer")
    return doc, data[28 + n:]
