"""Rename the managed cardmng XRPC module so AVS stops hijacking it.

VFG's Unity layer calls Ea3XrpcAddModule("cardmng", "cardmng", [...]), but the
AVS ea3 boot already registered a module with that name, so spice logs

    W:xrpc: module_add: cardmng: already has same name

and the game's registration is dropped. Ea3XrpcApply then serializes the
managed request XML through AVS's own cardmng descriptor, which is bound to an
uninitialised card session. Captured packets show the result: a 3-byte cardid,
a pointer-sized cardtype, a model attribute the game never set, and a passwd
holding the first bytes of the request buffer. Writing refid/dataid back into
that structure corrupts the AVS heap and spice64.exe dies in ntdll about a
second later. vfgac and vfglog register cleanly and are never corrupted.

KAMUNITY.XrpcModuleCardmng interns "cardmng" once in the #US heap and shares it
between AddModule() and all five XrpcUtil.CreateRequestNode() calls, so a single
same-length UTF-16 literal rewrite moves the whole module to a name AVS does not
own. No metadata offsets change.

Usage:
    python tools/patch_cardmng_module.py <path to Managed/kamunity.dll>
    python tools/patch_cardmng_module.py <path> --revert
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

OLD = "cardmng"
NEW = "vfgcard"  # must be the same length: the #US length prefix is not rebuilt
BACKUP_SUFFIX = ".orig-cardmng"


def _u16(text: str) -> bytes:
    return text.encode("utf-16-le")


def _locate(data: bytes, text: str) -> list[int]:
    needle = _u16(text)
    out, start = [], 0
    while True:
        i = data.find(needle, start)
        if i < 0:
            return out
        # #US entries are [compressed length][utf-16 chars][terminal byte].
        if data[i - 1] == len(needle) + 1:
            out.append(i)
        start = i + 1


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    dll = Path(argv[1])
    revert = "--revert" in argv[2:]
    backup = dll.with_suffix(dll.suffix + BACKUP_SUFFIX)

    if revert:
        if not backup.exists():
            print(f"no backup at {backup}")
            return 1
        shutil.copyfile(backup, dll)
        print(f"reverted {dll} from {backup}")
        return 0

    data = dll.read_bytes()
    want, other = (OLD, NEW)
    hits = _locate(data, want)
    if not hits:
        if _locate(data, other):
            print(f"{dll} already patched to {other!r}")
            return 0
        print(f"no #US literal {want!r} in {dll}")
        return 1
    if len(hits) != 1:
        print(f"expected exactly one {want!r} literal, found {len(hits)}: {hits}")
        return 1

    off = hits[0]
    if not backup.exists():
        shutil.copyfile(dll, backup)
        print(f"backup -> {backup}")

    patched = bytearray(data)
    patched[off : off + len(_u16(want))] = _u16(other)
    dll.write_bytes(bytes(patched))
    print(f"patched {dll} @ 0x{off:x}: {want!r} -> {other!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
