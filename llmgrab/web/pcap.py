"""Decrypt a passive browser capture (pcap + NSS key log) into HTTP/2 exchanges.

Pipeline: TCP reassembly -> TLS 1.3 record decryption (SSLKEYLOGFILE secrets)
-> HTTP/2 frames + HPACK -> one JSONL record per stream, in the same shape as
the CLI addon's records (SSE replies are split into events and rebuilt;
protobuf / Connect bodies are decoded without a schema).

Usage:
    llmgrab decode [--pcap P] [--keys K] [-o OUT] [--hosts REGEX] [--max-body N]
"""

import argparse
import bisect
import json
import re
import socket
import struct
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import dpkt
import hpack
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
from mitmproxy.net import encoding

from llmgrab.paths import KEYLOG, PCAP, WEB_RECORDS, open_private
from llmgrab.records import parse_sse, parse_text, reassemble_message, redact_headers
from llmgrab.web.protobuf import decode_connect_stream, decode_proto_safe

DEFAULT_HOSTS = r"(^|\.)(claude\.ai|anthropic\.com)$"
H2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

# TLS 1.3 cipher suites: id -> (AEAD class, key length, hash)
SUITES = {
    0x1301: (AESGCM, 16, hashes.SHA256),
    0x1302: (AESGCM, 32, hashes.SHA384),
    0x1303: (ChaCha20Poly1305, 32, hashes.SHA256),
}
# Bodies of these content types are kept as text/JSON; others only by size.
TEXT_TYPES = ("json", "text/", "javascript", "xml", "x-www-form-urlencoded", "event-stream")
# Protobuf bodies (Connect-RPC) are decoded without a schema.
PROTO_TYPES = ("application/proto", "application/connect+proto", "application/grpc-web+proto")


# --- TCP -----------------------------------------------------------------

class Direction:
    """One side of a TCP connection: segments, then a contiguous byte stream."""

    def __init__(self):
        self.segments = []  # (seq, ts, data)
        self.isn = None
        self.data = b""
        self.ts_index = []  # (end offset, ts) of each appended segment
        self.gap = False

    def add(self, seq, ts, data, syn):
        if syn:
            self.isn = (seq + 1) & 0xFFFFFFFF
        elif data:
            self.segments.append((seq, ts, data))

    def assemble(self):
        if not self.segments:
            return
        base = self.isn if self.isn is not None else min(s for s, _, _ in self.segments)
        parts, cur = [], 0
        for off, ts, data in sorted(((s - base) & 0xFFFFFFFF, ts, d) for s, ts, d in self.segments):
            if off > cur:
                self.gap = True  # missing packets: stop at the first hole
                break
            if off + len(data) <= cur:
                continue  # retransmission
            parts.append(data[cur - off:])
            cur = off + len(data)
            self.ts_index.append((cur, ts))
        self.data = b"".join(parts)
        self.segments = []

    def ts_at(self, offset):
        """Capture time of the segment that carried byte `offset - 1`."""
        i = bisect.bisect_left(self.ts_index, (offset, 0))
        return self.ts_index[min(i, len(self.ts_index) - 1)][1]


def read_tcp(pcap_path):
    """{(client ip, client port, server ip, server port): {"c": Direction, "s": Direction}}."""
    conns = defaultdict(lambda: {"c": Direction(), "s": Direction(), "start": None})
    with open(pcap_path, "rb") as f:
        for ts, buf in dpkt.pcap.Reader(f):
            try:
                ip = dpkt.ethernet.Ethernet(buf).data
                tcp = ip.data
            except (dpkt.UnpackError, AttributeError):
                continue
            if not isinstance(tcp, dpkt.tcp.TCP):
                continue
            family = socket.AF_INET6 if isinstance(ip, dpkt.ip6.IP6) else socket.AF_INET
            src, dst = socket.inet_ntop(family, ip.src), socket.inet_ntop(family, ip.dst)
            if tcp.dport == 443:
                key, side = (src, tcp.sport, dst, 443), "c"
            elif tcp.sport == 443:
                key, side = (dst, tcp.dport, src, 443), "s"
            else:
                continue
            conn = conns[key]
            conn["start"] = conn["start"] or ts
            conn[side].add(tcp.seq, ts, bytes(tcp.data), bool(tcp.flags & dpkt.tcp.TH_SYN))
    for conn in conns.values():
        conn["c"].assemble()
        conn["s"].assemble()
    return conns


# --- TLS 1.3 -------------------------------------------------------------

def load_keylog(path):
    """{client_random hex: {label: secret bytes}}."""
    keys = defaultdict(dict)
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) == 3 and not line.startswith("#"):
                keys[parts[1].lower()][parts[0]] = bytes.fromhex(parts[2])
    return keys


def hkdf_expand_label(secret, label, length, hash_cls):
    full = b"tls13 " + label
    info = struct.pack(">HB", length, len(full)) + full + b"\x00"
    return HKDFExpand(hash_cls(), length, info).derive(secret)


class TrafficKey:
    def __init__(self, secret, suite):
        aead, key_len, hash_cls = SUITES[suite]
        self.secret, self.suite, self.hash_cls = secret, suite, hash_cls
        self.aead = aead(hkdf_expand_label(secret, b"key", key_len, hash_cls))
        self.iv = hkdf_expand_label(secret, b"iv", 12, hash_cls)
        self.seq = 0

    def decrypt(self, header, ciphertext):
        nonce = bytes(a ^ b for a, b in zip(self.iv, self.seq.to_bytes(12, "big")))
        plain = self.aead.decrypt(nonce, ciphertext, header)
        self.seq += 1
        return plain

    def next(self):
        """Key after a KeyUpdate."""
        size = self.hash_cls.digest_size
        return TrafficKey(hkdf_expand_label(self.secret, b"traffic upd", size, self.hash_cls), self.suite)


def tls_records(data):
    """Yield (record type, header, payload, end offset)."""
    i = 0
    while i + 5 <= len(data):
        rtype, length = data[i], struct.unpack(">H", data[i + 3:i + 5])[0]
        if i + 5 + length > len(data):
            break
        yield rtype, data[i:i + 5], data[i + 5:i + 5 + length], i + 5 + length
        i += 5 + length


def parse_hello(payload):
    """(handshake type, random, cipher suite or None, extensions {type: bytes})."""
    htype = payload[0]
    i = 4 + 2
    random = payload[i:i + 32]
    i += 32
    i += 1 + payload[i]  # session id
    suite = None
    if htype == 1:  # ClientHello: cipher suite list, compression list
        i += 2 + struct.unpack(">H", payload[i:i + 2])[0]
        i += 1 + payload[i]
    else:  # ServerHello: chosen suite, compression byte
        suite = struct.unpack(">H", payload[i:i + 2])[0]
        i += 3
    return htype, random, suite, parse_extensions(payload[i:])


def parse_extensions(data):
    """Length-prefixed TLS extension list -> {type: bytes}."""
    exts = {}
    if len(data) < 2:
        return exts
    end = min(len(data), 2 + struct.unpack(">H", data[:2])[0])
    i = 2
    while i + 4 <= end:
        etype, elen = struct.unpack(">HH", data[i:i + 4])
        exts[etype] = data[i + 4:i + 4 + elen]
        i += 4 + elen
    return exts


def handshake_messages(data):
    i = 0
    while i + 4 <= len(data):
        length = int.from_bytes(data[i + 1:i + 4], "big")
        yield data[i], data[i + 4:i + 4 + length]
        i += 4 + length


def decrypt_direction(direction, keys):
    """Decrypt one direction. Returns (app data bytes, [(app end offset, ts)], alpn, notes)."""
    out, ts_index, alpn, notes = [], [], None, Counter()
    size, k = 0, 0
    for rtype, header, payload, end in tls_records(direction.data):
        if rtype != 23:
            continue  # plaintext handshake, change_cipher_spec, alert
        # Try the current key, then the later ones (early -> handshake -> application).
        for j in range(k, len(keys)):
            try:
                plain = keys[j].decrypt(header, payload)
            except Exception:
                continue
            k = j
            break
        else:
            notes["undecryptable records"] += 1
            continue
        plain = plain.rstrip(b"\x00")
        inner, plain = plain[-1], plain[:-1]
        if inner == 23:
            out.append(plain)
            size += len(plain)
            ts_index.append((size, direction.ts_at(end)))
        elif inner == 22:
            for htype, body in handshake_messages(plain):
                if htype == 8 and alpn is None:  # EncryptedExtensions
                    exts = parse_extensions(body)
                    if 16 in exts:  # ALPN: list length (2), name length (1), name
                        alpn = exts[16][3:3 + exts[16][2]].decode()
                elif htype == 24:  # KeyUpdate
                    keys.insert(k + 1, keys[k].next())
                    notes["key updates"] += 1
    return b"".join(out), ts_index, alpn, notes


def decrypt_conn(conn, keylog):
    """Fill conn with sni, tls version, alpn and decrypted streams. Returns status string."""
    client, server = conn["c"], conn["s"]
    hello = server_hello = None
    for rtype, _, payload, _ in tls_records(client.data):
        if rtype == 22 and payload[0] == 1:
            hello = parse_hello(payload)
            break
    for rtype, _, payload, _ in tls_records(server.data):
        if rtype == 22 and payload[0] == 2:
            server_hello = parse_hello(payload)  # the last one wins (HelloRetryRequest)
        elif rtype == 23:
            break
    if not hello:
        return "no ClientHello"
    sni_ext = hello[3].get(0)
    conn["sni"] = sni_ext[5:].decode(errors="replace") if sni_ext else None
    if not server_hello:
        return "no ServerHello"
    version = server_hello[3].get(43)
    if version != b"\x03\x04":
        return "TLS 1.2 (not supported)"
    secrets = keylog.get(hello[1].hex())
    if not secrets:
        return "no keys"
    suite = server_hello[2]
    if suite not in SUITES:
        return f"cipher 0x{suite:04x} not supported"

    def chain(labels):
        return [TrafficKey(secrets[label], suite) for label in labels if label in secrets]

    c_data, c_ts, _, c_notes = decrypt_direction(
        client, chain(["CLIENT_EARLY_TRAFFIC_SECRET", "CLIENT_HANDSHAKE_TRAFFIC_SECRET", "CLIENT_TRAFFIC_SECRET_0"]))
    s_data, s_ts, alpn, s_notes = decrypt_direction(
        server, chain(["SERVER_HANDSHAKE_TRAFFIC_SECRET", "SERVER_TRAFFIC_SECRET_0"]))
    conn.update(alpn=alpn, app={"c": (c_data, c_ts), "s": (s_data, s_ts)}, notes=c_notes + s_notes)
    return "ok"


# --- HTTP/2 --------------------------------------------------------------

def ts_lookup(ts_index, offset):
    i = bisect.bisect_left(ts_index, (offset, 0))
    return ts_index[min(i, len(ts_index) - 1)][1] if ts_index else None


def h2_frames(data, ts_index):
    """Yield (type, flags, stream id, payload, ts)."""
    i = len(H2_PREFACE) if data.startswith(H2_PREFACE) else 0
    while i + 9 <= len(data):
        length = int.from_bytes(data[i:i + 3], "big")
        ftype, flags = data[i + 3], data[i + 4]
        sid = struct.unpack(">I", data[i + 5:i + 9])[0] & 0x7FFFFFFF
        if i + 9 + length > len(data):
            break
        yield ftype, flags, sid, data[i + 9:i + 9 + length], ts_lookup(ts_index, i + 9 + length)
        i += 9 + length


def strip_padding(payload, flags, priority=False):
    pad = 0
    if flags & 0x8:
        pad, payload = payload[0], payload[1:]
    if priority and flags & 0x20:
        payload = payload[5:]
    return payload[:len(payload) - pad] if pad else payload


def h2_streams(conn):
    """{stream id: {"req": {...}, "resp": {...}}} with headers, body bytes and timestamps."""
    streams = defaultdict(lambda: {
        "req": {"headers": [], "body": [], "start": None, "end": None},
        "resp": {"headers": [], "trailers": [], "body": [], "start": None, "first_data": None, "end": None},
        "reset": False,
    })
    for side, part in (("c", "req"), ("s", "resp")):
        decoder = hpack.Decoder()
        decoder.max_allowed_table_size = 1 << 20
        decoder.max_header_list_size = 1 << 24
        data, ts_index = conn["app"][side]
        block = None  # (stream id, header block bytes, end stream) while CONTINUATIONs follow
        for ftype, flags, sid, payload, ts in h2_frames(data, ts_index):
            if ftype in (1, 5, 9):  # HEADERS, PUSH_PROMISE, CONTINUATION
                if ftype == 1:
                    block = [sid, strip_padding(payload, flags, priority=True), flags & 0x1]
                elif ftype == 5:
                    block = [None, strip_padding(payload, flags)[4:], 0]
                else:
                    block[1] += payload
                if not flags & 0x4:  # END_HEADERS not set yet
                    continue
                headers = decoder.decode(block[1], raw=False)  # always decode: keeps HPACK state
                if block[0] is None:
                    block = None
                    continue
                msg = streams[block[0]][part]
                msg["start"] = msg["start"] or ts
                status = dict(headers).get(":status", "")
                if part == "resp" and msg["headers"] and not dict(msg["headers"]).get(":status", "").startswith("1"):
                    msg["trailers"] = headers
                elif not (part == "resp" and status.startswith("1")):
                    msg["headers"] = headers
                if block[2]:
                    msg["end"] = ts
                block = None
            elif ftype == 0:  # DATA
                msg = streams[sid][part]
                body = strip_padding(payload, flags)
                if body:
                    msg["body"].append(body)
                    if part == "resp" and msg["first_data"] is None:
                        msg["first_data"] = ts
                if flags & 0x1:
                    msg["end"] = ts
            elif ftype == 3:  # RST_STREAM
                streams[sid]["reset"] = True
    return streams


# --- records -------------------------------------------------------------

def decode_body(chunks, headers, max_body):
    """(body for the record, raw size, decoded text or None)."""
    raw = b"".join(chunks)
    if not raw:
        return None, 0, None
    h = dict(headers)
    ctype = h.get("content-type", "")
    if h.get("content-encoding"):
        try:
            raw_decoded = encoding.decode(raw, h["content-encoding"])
        except Exception:
            return f"<undecodable {h['content-encoding']} body, {len(raw)} bytes>", len(raw), None
    else:
        raw_decoded = raw
    if ctype.split(";")[0].strip() in PROTO_TYPES:
        if "connect+" in ctype or "grpc-web" in ctype:
            return {"connect_messages": decode_connect_stream(raw_decoded, h.get("connect-content-encoding"))}, len(raw), None
        return decode_proto_safe(raw_decoded), len(raw), None
    if not any(t in ctype for t in TEXT_TYPES) and ctype:
        return f"<{ctype}, {len(raw_decoded)} bytes>", len(raw), None
    text = raw_decoded.decode("utf-8", errors="replace")
    if max_body and len(text) > max_body and "event-stream" not in ctype:
        return f"<{ctype or 'body'}, {len(text)} chars, over --max-body>", len(raw), None
    return parse_text(text), len(raw), text


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


def to_record(conn_id, sni, sid, stream, max_body):
    req, resp = stream["req"], stream["resp"]
    rh, sh = dict(req["headers"]), dict(resp["headers"])
    start = req["start"]
    record = {
        "id": f"{conn_id}/{sid}",
        "timestamp": iso(start),
        "host": rh.get(":authority") or sni,
        "method": rh.get(":method"),
        "path": rh.get(":path"),
        "status": int(sh[":status"]) if sh.get(":status", "").isdigit() else None,
        "duration_s": round(resp["end"] - start, 3) if resp["end"] and start else None,
        "ttfb_s": round(resp["start"] - start, 3) if resp["start"] and start else None,
        "streamed": "event-stream" in sh.get("content-type", ""),
        "reset": stream["reset"],
        "request": {"headers": redact_headers(req["headers"]), "body": None},
        "response": {"headers": redact_headers(resp["headers"]), "body": None},
    }
    record["request"]["body"], _, _ = decode_body(req["body"], req["headers"], max_body)
    body, size, text = decode_body(resp["body"], resp["headers"], max_body)
    record["response"]["size"] = size
    if record["streamed"] and text is not None:
        events = parse_sse(text)
        record["response"]["sse_events"] = events
        record["response"]["body"] = reassemble_message(events)
        if resp["first_data"] and start:
            record["ttfb_s"] = round(resp["first_data"] - start, 3)
    else:
        record["response"]["body"] = body
    if resp["trailers"]:
        record["response"]["trailers"] = redact_headers(resp["trailers"])
    return record


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="llmgrab decode", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pcap", type=Path, default=PCAP)
    ap.add_argument("--keys", type=Path, default=KEYLOG)
    ap.add_argument("-o", "--out", type=Path,
                    help=f"JSONL output (default: {WEB_RECORDS.relative_to(WEB_RECORDS.parents[2])} "
                         "for the default pcap, else <pcap stem>.jsonl next to it)")
    ap.add_argument("--hosts", default=DEFAULT_HOSTS, help=f"SNI regex to decode (default: {DEFAULT_HOSTS})")
    ap.add_argument("--max-body", type=int, default=2_000_000,
                    help="drop non-SSE text bodies longer than this (0 = keep all)")
    args = ap.parse_args(argv)

    keylog = load_keylog(args.keys)
    conns = read_tcp(args.pcap)
    host_re = re.compile(args.hosts)

    records, summary = [], []
    for n, (key, conn) in enumerate(sorted(conns.items(), key=lambda kv: kv[1]["start"])):
        status = decrypt_conn(conn, keylog)
        sni = conn.get("sni")
        if not sni or not host_re.search(sni):
            continue
        conn_id = f"c{n}"
        gap = conn["c"].gap or conn["s"].gap
        if status != "ok":
            summary.append((conn_id, sni, status, "", 0, gap))
            continue
        if conn["alpn"] != "h2":
            summary.append((conn_id, sni, f"ALPN {conn['alpn']} (only h2 decoded)", "", 0, gap))
            continue
        streams = h2_streams(conn)
        for sid in sorted(streams):
            if streams[sid]["req"]["headers"]:
                records.append(to_record(conn_id, sni, sid, streams[sid], args.max_body))
        notes = ", ".join(f"{v} {k}" for k, v in conn["notes"].items())
        summary.append((conn_id, sni, "ok", notes, len(streams), gap))

    out = args.out or (WEB_RECORDS if args.pcap == PCAP else args.pcap.with_suffix(".jsonl"))
    records.sort(key=lambda r: r["timestamp"] or "")
    with open_private(out) as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"{'conn':5} {'host':32} {'streams':>7}  status")
    for conn_id, sni, status, notes, n_streams, gap in summary:
        extra = "; ".join(x for x in (notes, "TCP gap: stream truncated" if gap else "") if x)
        print(f"{conn_id:5} {sni:32} {n_streams:>7}  {status}{'  (' + extra + ')' if extra else ''}")
    print(f"\n{len(records)} exchanges -> {out}")


if __name__ == "__main__":
    main()
