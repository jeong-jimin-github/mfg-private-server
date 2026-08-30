#!/usr/bin/env python3
"""Cross-check our responses against the client's own XML parsers.

Every AOG endpoint has exactly one parser in the decompiled client:

    unity-project/Assets/Scripts/Assembly-CSharp/MFG.GameRequest/<Api>.cs

`OnCreateSendParameter()` names the URL, `OnParse()` reads the response.  The
helpers in MFG.Sytem.Utilities.Xml.Extensions decide what a missing node costs:

    MustNeedElement("x")   -> MustNeedNodeNotFoundException.  The catch blocks in
                              OnParse only swallow NeedNotNodeNotFoundException,
                              so this escapes to RequestApiBase.PushRequest and
                              the request fails.
    .Element("x").Value*   -> NullReferenceException.  Same outcome, and
                              GameDataManager.ExecLoadAtCardEntry maps an NRE to
                              ENTRY_SAVEDATA_VERSION_ERROR, which is why a single
                              missing node shows up as a save-data error rather
                              than a network one.
    .Attribute("x").Value  -> NullReferenceException, same as above.
    NeedNotElement("x")    -> caught and ignored.  Optional.

So: any element in the first three groups that our response does not contain is
a guaranteed client-side exception.  That is the class of bug that produced the
`battle_item_settings` PIN-login failure - GetMenuData dereferenced
basic_settings unconditionally and we did not send it.

Usage:
    python tools/check_parser_contract.py --live           # ask server.py directly
    python tools/check_parser_contract.py                  # newest capture run
    python tools/check_parser_contract.py captures/run-...  # a specific run

`--live` calls every GAME_HANDLERS entry with a synthetic form and checks what
comes back, so it needs no capture and no game.  Prefer it: capture runs go
stale the moment a handler changes, and a directory holding dumps from several
server versions will report nodes that the current code does emit.

Findings are advisory either way.  The extractor is a regex pass over decompiled
C#: it scopes to the OnParse override and blanks `if (IsExistElement(...))` /
null-check blocks, but it still cannot tell that a node read inside `foreach
(Elements("state"))` is only required per-element.  Treat a hit as "go read that
OnParse", not as a bug.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
REQUESTS = (
    ROOT.parent
    / "unity-project"
    / "Assets"
    / "Scripts"
    / "Assembly-CSharp"
    / "MFG.GameRequest"
)
CAPTURES = ROOT / "captures"

# m_sendUrl = base.DefaultFrontServerUrl + "/get_menudata";
URL_RE = re.compile(r'm_sendUrl\s*=[^;]*?"/(\w+)')
# the three fatal accessor shapes, plus the tolerated one
MUST_RE = re.compile(r'MustNeedElement\("([^"]+)"\)')
NEEDNOT_RE = re.compile(r'NeedNotElement\("([^"]+)"\)')
DEREF_RE = re.compile(r'\.Element\("([^"]+)"\)\s*\.\s*(?:Value|AttributeValue)')
ATTR_RE = re.compile(r'\.Attribute\("([^"]+)"\)\s*\.\s*Value')
ATTRHELPER_RE = re.compile(r'AttributeValue(?:Int64|Int|ToDateTime|JavaCurrentTimeMillsToDateTime)\("([^"]+)"\)')


def latest_run() -> Path:
    pointer = CAPTURES / "latest.txt"
    if pointer.is_file():
        candidate = CAPTURES / pointer.read_text(encoding="utf-8").strip()
        if candidate.is_dir():
            return candidate
    runs = sorted(p for p in CAPTURES.glob("run-*") if p.is_dir())
    if runs:
        return runs[-1]
    # pre-run-directory layout
    return CAPTURES


GUARD_RE = re.compile(r"\bif\s*\(")
GUARDED_COND_RE = re.compile(r'IsExistElement\(|!=\s*null|==\s*null')


def _block(text: str, open_at: int, opener: str = "{", closer: str = "}") -> int:
    """Index just past the bracket group that starts at `open_at`."""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


def onparse_body(text: str) -> str:
    """Just the OnParse override, not the whole file.

    EntryGame keeps its optional branch in a separate OnParsePreferenceMatching
    helper that OnParse only calls when the node exists, so scanning to end of
    file reported three required nodes that are not required at all.
    """
    match = re.search(r"UniTask\s+OnParse\s*\(", text)
    if not match:
        return ""
    brace = text.find("{", _block(text, text.index("(", match.start()), "(", ")"))
    if brace < 0:
        return ""
    return text[brace:_block(text, brace)]


def strip_guarded(body: str) -> str:
    """Blank out `if (x.IsExistElement("y")) { ... }` and null-check bodies.

    Whatever accessor sits inside such a block is optional however it is
    spelled, and reporting it as required is the noise that makes a checker get
    ignored.
    """
    out = list(body)
    for match in GUARD_RE.finditer(body):
        paren = body.index("(", match.start())
        end_cond = _block(body, paren, "(", ")")
        if not GUARDED_COND_RE.search(body[paren:end_cond]):
            continue
        brace = body.find("{", end_cond)
        if brace < 0 or body[end_cond:brace].strip():
            continue
        for j in range(match.start(), _block(body, brace)):
            if not out[j].isspace():
                out[j] = " "
    return "".join(out)


def parsers() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in sorted(REQUESTS.glob("*.cs")):
        text = path.read_text(encoding="utf-8", errors="replace")
        url = URL_RE.search(text)
        if not url:
            continue
        body = strip_guarded(onparse_body(text))
        out[url.group(1)] = {
            "file": path.name,
            "must": sorted(set(MUST_RE.findall(body))),
            "deref": sorted(set(DEREF_RE.findall(body))),
            "attrs": sorted(set(ATTR_RE.findall(body)) | set(ATTRHELPER_RE.findall(body))),
            "optional": sorted(set(NEEDNOT_RE.findall(body))),
        }
    return out


def responses(run: Path) -> dict[str, Path]:
    """Newest captured response per endpoint."""
    out: dict[str, Path] = {}
    folder = run / "responses"
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*_aog_*.xml")):
        name = path.stem.split("_aog_", 1)[1]
        out[name] = path  # sorted -> last wins -> newest sequence
    return out


def tags(path: Path) -> set[str]:
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8", errors="replace"))
    except ET.ParseError as exc:
        raise SystemExit(f"{path}: response is not well-formed XML: {exc}")
    return {el.tag for el in root.iter()}


# A form with every key any handler reads, so one dict drives all 45 routes.
LIVE_FORM = {
    "web_id": "1", "area_id": "1", "pcuid": "contract-check", "system_id": "S",
    "sic": "VFG:J:A:A:2025122300", "loc_id": "VFG00001", "mid": "1",
    "kind": "player_game", "one_kind": "player_game", "data": "e30=",
    "slot_id": "0", "set_character": "OID_CHARACTER_1",
    "series_id": "140", "gacha_id": "140", "request_id": "1",
    "tid": "1", "pindex": "0", "voltage": "0", "contribute_percent": "100",
    "bonus": "0", "competition_id": "1", "format": "1", "gmode": "0",
    "taku_class": "1", "match_mode": "0", "entry_id": "1", "word": "a",
    "count": "1", "item_id": "1",
}


def check_live() -> int:
    """Run every handler in server.py and check what it actually returns."""
    sys.path.insert(0, str(ROOT))
    import server  # noqa: PLC0415 - only needed in this mode

    specs = parsers()
    findings = 0
    for endpoint, spec in sorted(specs.items()):
        handler = server.GAME_HANDLERS.get(endpoint)
        if handler is None:
            continue
        try:
            xml = handler(dict(LIVE_FORM))
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            print(f"\n{endpoint}  ({spec['file']})")
            print(f"    handler raised {type(exc).__name__}: {exc}")
            findings += 1
            continue
        try:
            present = {el.tag for el in ET.fromstring(xml).iter()}
        except ET.ParseError as exc:
            print(f"\n{endpoint}  ({spec['file']})")
            print(f"    response is not well-formed XML: {exc}")
            findings += 1
            continue
        missing_must = [t for t in spec["must"] if t not in present]
        missing_deref = [t for t in spec["deref"] if t not in present]
        if not missing_must and not missing_deref:
            continue
        findings += 1
        print(f"\n{endpoint}  ({spec['file']})")
        for t in missing_must:
            print(f'    MustNeedElement("{t}") -> MustNeedNodeNotFoundException')
        for t in missing_deref:
            print(f'    .Element("{t}").Value  -> NullReferenceException')

    routed = sum(1 for e in specs if e in server.GAME_HANDLERS)
    print(f"\n{len(specs)} parsers, {routed} routed to a handler, {findings} with missing required nodes")
    return 1 if findings else 0


def main() -> int:
    if "--live" in sys.argv[1:]:
        return check_live()
    run = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_run()
    if not run.is_dir():
        raise SystemExit(f"no such capture run: {run}")
    print(f"capture run: {run}")

    specs = parsers()
    caught = responses(run)
    if not caught:
        raise SystemExit(
            f"{run}/responses has no aog_* captures - play a session first"
        )

    findings = 0
    unseen = []
    for endpoint, spec in sorted(specs.items()):
        path = caught.get(endpoint)
        if path is None:
            unseen.append(endpoint)
            continue
        present = tags(path)
        missing_must = [t for t in spec["must"] if t not in present]
        missing_deref = [t for t in spec["deref"] if t not in present]
        if not missing_must and not missing_deref:
            continue
        findings += 1
        print(f"\n{endpoint}  ({spec['file']}, {path.name})")
        for t in missing_must:
            print(f"    MustNeedElement(\"{t}\") -> MustNeedNodeNotFoundException")
        for t in missing_deref:
            print(f"    .Element(\"{t}\").Value  -> NullReferenceException")

    print(f"\n{len(specs)} parsers, {len(caught)} endpoints captured, {findings} with missing required nodes")
    if unseen:
        print(f"not exercised this run ({len(unseen)}): {', '.join(sorted(unseen))}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
