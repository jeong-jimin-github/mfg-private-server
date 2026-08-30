"""e-Amusement transport: X-Eamuse-Info RC4, LZ77, kbinxml."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional, Tuple, Union

from kbinxml import KBinXML
from kbinxml.kbinxml import KBinXML as _KBinXMLClass


_LATIN_ENCODINGS = {"iso-8859-1", "latin-1", "latin1", "iso8859-1", "cp1252"}


@dataclass(frozen=True)
class EamuseDecodeMeta:
    """Transport details needed to mirror an AVS request in the response."""

    used_kbin: bool
    kbin_encoding: Optional[str] = None
    kbin_compressed: Optional[bool] = None
    decrypted_body: bytes = b""
    decoded_body: bytes = b""

    def __bool__(self) -> bool:
        """Keep legacy callers that treated decode metadata as a bool working."""
        return self.used_kbin



def _looks_binary_bytes(data: bytes) -> bool:
    return any(b < 0x20 or b > 0x7E for b in data)


def _xml_safe(text: str) -> str:
    # lxml refuses NUL/control chars; spice card override injects those into kbin strings.
    return "".join(ch for ch in text if ord(ch) >= 32 or ch in "\t\n\r")


def _data_grab_string_fallback(self):
    data = bytes(self.data_grab_auto()[:-1])
    # 8-byte binary IDms become hex. Printable ASCII (e.g. method="getrefid")
    # must stay text — hexing those made method="6765747265666964" and crashed
    # Unity after PIN.
    if len(data) == 8:
        if all(0x20 <= b <= 0x7E for b in data):
            return data.decode("ascii")
        return data.hex().upper()
    if len(data) == 16:
        try:
            s = data.decode("ascii")
            if re.fullmatch(r"[0-9A-Fa-f]{16}", s):
                return s.upper()
        except UnicodeDecodeError:
            pass
        if _looks_binary_bytes(data):
            return data.hex().upper()
    encs = []
    enc = getattr(self, "encoding", None)
    if enc and enc.lower().replace("_", "-") not in _LATIN_ENCODINGS:
        encs.append(enc)
    for extra in ("utf-8", "cp932"):
        if extra not in encs:
            encs.append(extra)
    text = None
    for codec in encs:
        try:
            text = data.decode(codec)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        if len(data) in (8, 16):
            return data.hex().upper()
        text = data.decode("utf-8", errors="replace")
    return _xml_safe(text)


_KBinXMLClass.data_grab_string = _data_grab_string_fallback

# Documented shared key material used by AVS2 EA3 (26 bytes).
_EAMUSE_KEY = bytes.fromhex("69d74627d985ee2187161570d08d93b12455035b6df0d8205df5")


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray(len(data))
    for n, b in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[n] = b ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


def parse_eamuse_info(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    header = header.strip()
    m = re.match(r"^1-([0-9a-fA-F]{8})-([0-9a-fA-F]{4})$", header)
    if not m:
        return None
    return header


def rc4_key_from_info(info: str) -> bytes:
    # info is 1-{8 hex}-{4 hex}; the 6-byte material is seconds||salt.
    body = info.split("-", 1)[1]
    seconds, salt = body.split("-")
    material = bytes.fromhex(seconds + salt) + _EAMUSE_KEY
    return hashlib.md5(material).digest()


def decrypt(data: bytes, info: Optional[str]) -> bytes:
    if not info:
        return data
    return _rc4(rc4_key_from_info(info), data)


def encrypt(data: bytes, info: Optional[str]) -> bytes:
    return decrypt(data, info)


def lz77_decompress(data: bytes) -> bytes:
    """AVS LZSS used by e-Amusement (12-bit window, 4-bit length)."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        flags = data[i]
        i += 1
        for bit in range(8):
            if i >= n:
                return bytes(out)
            if flags & (1 << bit):
                out.append(data[i])
                i += 1
            else:
                if i + 1 >= n:
                    return bytes(out)
                hi = data[i]
                lo = data[i + 1]
                i += 2
                offset = (hi << 4) | (lo >> 4)
                length = (lo & 0x0F) + 3
                if offset == 0:
                    return bytes(out)
                start = len(out) - offset
                if start < 0:
                    # Match AVS ring buffer initialized to zeros.
                    pad = -start
                    out.extend(b"\x00" * min(pad, length))
                    length -= min(pad, length)
                    start = 0
                for _ in range(length):
                    out.append(out[start])
                    start += 1
    return bytes(out)


def lz77_compress_store(data: bytes) -> bytes:
    """Literal-only LZ77 that AVS accepts (flag 0xFF + 8 literals)."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        chunk = data[i : i + 8]
        flags = 0
        payload = bytearray()
        for k, b in enumerate(chunk):
            flags |= 1 << k
            payload.append(b)
        out.append(flags)
        out.extend(payload)
        i += 8
    # Trailing EOF backref (offset 0).
    out.extend(b"\x00\x00\x00")
    return bytes(out)


def decode_eamuse_body(
    body: bytes, eamuse_info: Optional[str], compress: Optional[str]
) -> Tuple[str, EamuseDecodeMeta]:
    """Return decoded XML and the exact packet properties AVS used.

    Mirroring the request binary-XML encoding matters for newer XRPC bridges.
    kbinxml defaults to CP932 when encoding, even when the peer sent UTF-8.
    """
    decrypted = decrypt(body, eamuse_info)
    blobs: list[bytes] = []
    if (compress or "").lower() == "lz77":
        try:
            blobs.append(lz77_decompress(decrypted))
        except Exception:
            blobs.append(decrypted)
    else:
        blobs.append(decrypted)

    last_err: Optional[Exception] = None
    parsed: list[tuple[str, EamuseDecodeMeta]] = []
    hex_card = re.compile(r'cardid="[0-9A-Fa-f]{16}"')
    for blob in blobs:
        if blob.startswith(b"<?xml") or blob.startswith(b"<"):
            return (
                blob.decode("utf-8", errors="replace"),
                EamuseDecodeMeta(
                    used_kbin=False,
                    decrypted_body=decrypted,
                    decoded_body=blob,
                ),
            )
        try:
            if not KBinXML.is_binary_xml(blob):
                continue
            kbin = KBinXML(blob, convert_illegal_things=True)
            parsed.append(
                (
                    kbin.to_text(),
                    EamuseDecodeMeta(
                        used_kbin=True,
                        kbin_encoding=getattr(kbin, "encoding", None),
                        kbin_compressed=getattr(kbin, "compressed", None),
                        decrypted_body=decrypted,
                        decoded_body=blob,
                    ),
                )
            )
        except Exception as exc:
            last_err = exc
            continue

    # Keep the previous heuristic for old malformed card captures, but preserve
    # the transport metadata belonging to the selected candidate.
    for text, meta in parsed:
        if hex_card.search(text):
            return text, meta
    if parsed:
        return parsed[0]
    if last_err:
        raise last_err
    raise ValueError("e-amuse body is not XML or kbinxml")

def encode_eamuse_body(
    xml_text: str,
    eamuse_info: Optional[str],
    compress: Optional[str],
    meta: Union[EamuseDecodeMeta, bool],
) -> bytes:
    """Encode a response using the same binary-XML flavor as the request."""
    # v0.1 exposed a boolean ``use_kbin`` argument. Accept it during the
    # transition so scripts and downstream integrations do not break while the
    # server itself passes the richer transport metadata.
    if isinstance(meta, bool):
        meta = EamuseDecodeMeta(used_kbin=meta)
    if meta.used_kbin:
        raw = xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text
        kbin = KBinXML(raw)
        # Do not silently fall back to kbinxml's CP932 default. Newer AVS/KAMUNITY
        # requests can use UTF-8 and expect the response packet to mirror it.
        encoding = meta.kbin_encoding or "UTF-8"
        internal_compressed = True if meta.kbin_compressed is None else meta.kbin_compressed
        payload = kbin.to_binary(encoding=encoding, compressed=internal_compressed)
    else:
        payload = xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text
    if (compress or "").lower() == "lz77":
        payload = lz77_compress_store(payload)
    return encrypt(payload, eamuse_info)
