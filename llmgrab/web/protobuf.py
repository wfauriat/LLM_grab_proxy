"""Schemaless protobuf decoding and Connect-RPC stream framing (claude.ai web API)."""

import struct

from mitmproxy.net import encoding

from llmgrab.records import parse_text


def read_varint(buf, i):
    value = shift = 0
    while True:
        if i >= len(buf) or shift > 63:
            raise ValueError("bad varint")
        b = buf[i]
        value |= (b & 0x7F) << shift
        i += 1
        if not b & 0x80:
            return value, i
        shift += 7


def looks_like_text(raw):
    """UTF-8 text without control characters (besides \\n \\r \\t), else None."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if all(c.isprintable() or c in "\n\r\t" for c in text) else None


def decode_proto(buf, depth=0):
    """Schemaless protobuf -> {"<field>": value or [values]}. Raises ValueError if not a message.

    Length-delimited fields become text when they read as text, else a nested
    message when they parse as one, else hex. Varints stay ints; fixed32/64 too.
    """
    fields, i = {}, 0
    while i < len(buf):
        tag, i = read_varint(buf, i)
        field, wtype = tag >> 3, tag & 7
        if field == 0:
            raise ValueError("field 0")
        if wtype == 0:
            value, i = read_varint(buf, i)
        elif wtype == 1:
            if i + 8 > len(buf):
                raise ValueError("short fixed64")
            value, i = struct.unpack("<Q", buf[i:i + 8])[0], i + 8
        elif wtype == 5:
            if i + 4 > len(buf):
                raise ValueError("short fixed32")
            value, i = struct.unpack("<I", buf[i:i + 4])[0], i + 4
        elif wtype == 2:
            length, i = read_varint(buf, i)
            if i + length > len(buf):
                raise ValueError("short bytes")
            raw, i = buf[i:i + length], i + length
            # Text wins over a nested message: text deltas often start with "\n", which
            # can also parse as field 1 of a message.
            value = looks_like_text(raw)
            if value is None:
                try:
                    value = decode_proto(raw, depth + 1) if raw and depth < 40 else raw.hex()
                except ValueError:
                    value = raw.hex()
        else:
            raise ValueError(f"wire type {wtype}")
        fields.setdefault(str(field), []).append(value)
    return {k: v[0] if len(v) == 1 else v for k, v in fields.items()}


def decode_proto_safe(raw):
    try:
        return decode_proto(raw)
    except ValueError:
        return {"_undecoded_hex": raw.hex()}


def decode_connect_stream(raw, content_encoding):
    """Connect streaming body: frames of flags (1) + length (4) + payload."""
    messages, i = [], 0
    while i + 5 <= len(raw):
        flags, length = raw[i], struct.unpack(">I", raw[i + 1:i + 5])[0]
        payload, i = raw[i + 5:i + 5 + length], i + 5 + length
        if flags & 0x1 and content_encoding:
            payload = encoding.decode(payload, content_encoding)
        if flags & 0x2:  # end of stream: JSON with error / trailers
            messages.append({"end_stream": parse_text(payload.decode("utf-8", errors="replace"))})
        else:
            messages.append(decode_proto_safe(payload))
    return messages
