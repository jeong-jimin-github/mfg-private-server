#!/usr/bin/env python3
"""Local private server for Mahjong Fight Girl (VFG).

Serves:
  1. e-Amusement / XRPC bootstrap (services, pcbtracker, facility, vfgac, ...)
  2. AOG HTTP game API (form-urlencoded POST, XML body)

Does not talk to KONAMI or any third-party host.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import random
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote_plus, urlparse
from xml.etree import ElementTree as ET

from protocol import decode_eamuse_body, encode_eamuse_body, parse_eamuse_info

ROOT = Path(__file__).resolve().parent
CAPTURE_DIR = ROOT / "captures"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"

HOST = "127.0.0.1"
PORT = 8080
LOCATION_ID = "VFG00001"
FACILITY_NAME = "LOCAL TEST"
COUNTRY = "JP"
REGION = "13"

log = logging.getLogger("vfg")
_seq = 0
_seq_lock = threading.Lock()
_store_lock = threading.Lock()


def next_seq() -> int:
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq


def now_unix() -> int:
    return int(time.time())


def game_base_url() -> str:
    return f"http://{HOST}:{PORT}/aog"


def xml_escape(text: Any) -> str:
    s = "" if text is None else str(text)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def kitem(tag: str, typ: str, value: Any = "", **attrs: Any) -> str:
    extra = "".join(f' {k}="{xml_escape(v)}"' for k, v in attrs.items() if v is not None)
    if value == "" or value is None:
        return f'<{tag} __type="{typ}"{extra} />'
    return f'<{tag} __type="{typ}"{extra}>{xml_escape(value)}</{tag}>'


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class ProfileDB:
    def __init__(self, path: Path):
        self.path = path
        self.data: Dict[str, Any] = load_json(path, {"cards": {}, "profiles": {}, "cabinets": {}})

    def persist(self) -> None:
        save_json(self.path, self.data)

    def cabinet(self, pcbid: str, model: str = "") -> None:
        cabs = self.data.setdefault("cabinets", {})
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        row = cabs.get(pcbid) or {"pcbid": pcbid, "first_seen": now, "enabled": True}
        row["last_seen"] = now
        if model:
            row["model"] = model
        cabs[pcbid] = row
        self.persist()

    def card_profile(self, cardid: str) -> Dict[str, Any]:
        cards = self.data.setdefault("cards", {})
        if cardid not in cards:
            refid = _new_refid()
            cards[cardid] = {"card_id": cardid, "refid": refid, "created_at": now_unix()}
            self.ensure_profile(refid)
            self.persist()
        return cards[cardid]

    def ensure_profile(self, refid: str, name: str = "GUEST") -> Dict[str, Any]:
        profiles = self.data.setdefault("profiles", {})
        if refid not in profiles:
            profiles[refid] = {
                "refid": refid,
                "name": name,
                "player_id": int(refid[:8], 16) & 0x7FFFFFFF or 1,
                "states": {},
                "created_at": now_unix(),
            }
            self.persist()
        return profiles[refid]

    def by_refid(self, refid: str) -> Optional[Dict[str, Any]]:
        return self.data.get("profiles", {}).get(refid)

    def by_player_id(self, mid: int) -> Optional[Dict[str, Any]]:
        for p in self.data.get("profiles", {}).values():
            if int(p.get("player_id") or 0) == int(mid):
                return p
        return None

    def save_state(self, mid: int, kind: str, payload: str) -> None:
        p = self.by_player_id(mid)
        if not p:
            p = self.ensure_profile(f"MID{mid:08d}")
            p["player_id"] = mid
        p.setdefault("states", {})[kind] = payload
        p["updated_at"] = now_unix()
        self.persist()


DB = ProfileDB(DATA_DIR / "save.json")


def save_capture(kind: str, name: str, body: str) -> Path:
    seq = next_seq()
    folder = CAPTURE_DIR / kind
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{seq:04d}_{name}.xml"
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# e-Amusement handlers
# ---------------------------------------------------------------------------


def eamuse_wrap(module: str, inner: str, status: str = "0") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<response><{module} status="{status}">{inner}</{module}></response>'
    )


def handle_services_get(_call: ET.Element, model: str) -> str:
    base = f"http://{HOST}:{PORT}"
    names = [
        "cardmng",
        "eacoin",
        "facility",
        "local",
        "local2",
        "message",
        "netlog",
        "package",
        "pcbevent",
        "pcbtracker",
        "pkglist",
        "posevent",
        "sidmgr",
        "userdata",
        "userid",
        "eventlog",
    ]
    items = "\n".join(f'<item name="{n}" url="{base}"/>' for n in names)
    keepalive = (
        f"{base}/core/keepalive?pa=127.0.0.1&amp;ia=127.0.0.1"
        f"&amp;ga=127.0.0.1&amp;ma=127.0.0.1&amp;t1=2&amp;t2=10"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<response>'
        f'<services expire="10800" method="get" mode="operation" status="0">'
        f'{items}'
        f'<item name="ntp" url="ntp.nict.jp"/>'
        f'<item name="keepalive" url="{keepalive}"/>'
        "</services></response>"
    )


def handle_pcbtracker(_call: ET.Element, _model: str) -> str:
    inner = (
        f' expire="1200" status="0" ecenable="1" eclimit="0" limit="0"'
        f' time="{now_unix()}"'
    )
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<response><pcbtracker{inner} /></response>'


def handle_message_get(_call: ET.Element, _model: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n<response><message expire="300" status="0" /></response>'


def handle_facility_get(_call: ET.Element, _model: str) -> str:
    loc = LOCATION_ID
    inner = (
        "<location>"
        f'{kitem("id", "str", loc)}'
        f'{kitem("country", "str", COUNTRY)}'
        f'{kitem("region", "str", REGION)}'
        f'{kitem("name", "str", FACILITY_NAME)}'
        f'{kitem("type", "u8", 0)}'
        f'{kitem("countryname", "str", "Japan")}'
        f'{kitem("countryjname", "str", "日本")}'
        f'{kitem("regionname", "str", "Tokyo")}'
        f'{kitem("regionjname", "str", "東京都")}'
        f'{kitem("customercode", "str", "VFG")}'
        f'{kitem("companycode", "str", "00")}'
        f'{kitem("latitude", "s32", 0)}'
        f'{kitem("longitude", "s32", 0)}'
        f'{kitem("accuracy", "u8", 0)}'
        "</location>"
        "<line>"
        f'{kitem("id", "str", "0")}'
        f'{kitem("class", "u8", 1)}'
        "</line>"
        "<portfw>"
        f'{kitem("globalip", "ip4", HOST)}'
        f'{kitem("globalport", "u16", PORT)}'
        f'{kitem("privateport", "u16", PORT)}'
        "</portfw>"
        "<public>"
        f'{kitem("flag", "u8", 1)}'
        f'{kitem("name", "str", FACILITY_NAME)}'
        f'{kitem("latitude", "s32", 0)}'
        f'{kitem("longitude", "s32", 0)}'
        "</public>"
        "<share><eacoin>"
        f'{kitem("notchamount", "s32", 0)}'
        f'{kitem("notchcount", "s32", 0)}'
        f'{kitem("supplylimit", "s32", 100000)}'
        "</eacoin><url>"
        f'{kitem("eapass", "str", f"http://{HOST}:{PORT}")}'
        f'{kitem("arcadefan", "str", f"http://{HOST}:{PORT}")}'
        f'{kitem("konaminetdx", "str", f"http://{HOST}:{PORT}")}'
        f'{kitem("konamiid", "str", f"http://{HOST}:{PORT}")}'
        f'{kitem("eagate", "str", f"http://{HOST}:{PORT}")}'
        "</url></share>"
    )
    return eamuse_wrap("facility", inner)


def handle_package_list(_call: ET.Element, _model: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n<response><package expire="600" status="0" /></response>'


def handle_pcbevent_put(_call: ET.Element, _model: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n<response><pcbevent status="0" /></response>'


def handle_eventlog_write(_call: ET.Element, _model: str) -> str:
    inner = kitem("gamesession", "s64", 1) + kitem("logsendflg", "s32", 0) + kitem("logerrlevel", "s32", 0) + kitem("evtidnosendflg", "s32", 0)
    return eamuse_wrap("eventlog", inner)


_CARDID_RE = re.compile(r"^[0-9A-Fa-f]{16}$")
DEFAULT_CARDID = "E0047CC78DFBA459"


def normalize_cardid(raw: str) -> str:
    s = (raw or "").strip().replace(" ", "").upper()
    if _CARDID_RE.match(s):
        return s
    try:
        b = (raw or "").encode("latin-1", errors="ignore")
        if len(b) == 8:
            hx = b.hex().upper()
            if _CARDID_RE.match(hx):
                return hx
    except Exception:
        pass
    return DEFAULT_CARDID


def _cardmng_xml(attrs: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<response><cardmng{attrs} /></response>'


def _new_refid() -> str:
    # Asphyxia/AVS-style refid: letter + 15 hex. Leading digit 0 has crashed
    # some clients when they treat the value as an int.
    return "A" + uuid.uuid4().hex[:15].upper()


def _pcode(refid: str) -> str:
    try:
        return f"{int((refid or 'A')[:8], 16) % 100000000:08d}"
    except ValueError:
        return "00000001"


_PIN_RE = re.compile(r"^\d{4}$")


def _sanitize_pin(raw: str, fallback: str = "0000") -> str:
    s = (raw or "").strip()
    if _PIN_RE.match(s):
        return s
    if _PIN_RE.match(fallback or ""):
        return fallback
    return "0000"


def _ensure_refid_format(rec: Dict[str, Any]) -> str:
    old = (rec.get("refid") or "").upper()
    if len(old) == 16 and old[0] in "0123456789":
        new = "A" + old[1:]
        profiles = DB.data.setdefault("profiles", {})
        if old in profiles and new not in profiles:
            profiles[new] = profiles.pop(old)
            profiles[new]["refid"] = new
        rec["refid"] = new
        return new
    if _CARDID_RE.match(old):
        return old
    new = _new_refid()
    rec["refid"] = new
    return new


def _cardmng_found_xml(refid: str, *, binded: bool, newflag: bool) -> str:
    # Keep this to the attribute set this client has actually finished parsing.
    # Extra fields (pcode/useridflag/lastupdate) left inquire at fin=0, which
    # is the infinite "e-amuse confirm" spinner.
    return _cardmng_xml(
        f' status="0" binded="{1 if binded else 0}" dataid="{refid}" refid="{refid}"'
        f' newflag="{1 if newflag else 0}" expired="0" exflag="0" ecflag="1"'
    )


def handle_cardmng(method: str, call: ET.Element, _model: str) -> str:
    # AVS cardmng: unregistered cards MUST return status=112 (CARD_NEW) so the
    # client follows getrefid -> inquire(newflag=1) -> authpass -> bindmodel.
    # Returning status=0/binded=1 on a never-registered card makes Unity load a
    # missing profile and crash right after inquire fin=1.
    node = call.find(".//cardmng") if call.find(".//cardmng") is not None else call
    raw_cardid = node.attrib.get("cardid") or node.attrib.get("card_id") or ""
    cardid = normalize_cardid(raw_cardid)
    req_refid = (node.attrib.get("refid") or "").strip().upper()
    with _store_lock:
        cards = DB.data.setdefault("cards", {})
        rec = cards.get(cardid)
        if rec is None and req_refid:
            rec = next((c for c in cards.values() if c.get("refid") == req_refid), None)

        log.info(
            "[cardmng] %s cardid=%s raw=%r issued=%s bound=%s refid=%s",
            method,
            cardid,
            raw_cardid,
            bool(rec and rec.get("issued")),
            bool(rec and rec.get("bound")),
            (rec or {}).get("refid"),
        )

        if method == "inquire":
            if rec is None or not rec.get("issued"):
                return _cardmng_xml(' status="112"')
            refid = _ensure_refid_format(rec)
            bound = bool(rec.get("bound"))
            return _cardmng_found_xml(refid, binded=bound, newflag=not bound)

        if method == "getrefid":
            rec = DB.card_profile(cardid)
            rec["issued"] = True
            rec["bound"] = False
            rec["pin"] = _sanitize_pin(node.attrib.get("passwd") or "", rec.get("pin") or "0000")
            refid = _ensure_refid_format(rec)
            DB.persist()
            # pcode on this response left AVS stuck (no xrpc thread exit) so the
            # PIN screen never advanced. bemaniutils only requires refid/dataid.
            return _cardmng_xml(f' status="0" refid="{refid}" dataid="{refid}"')

        if method == "authpass":
            return _cardmng_xml(' status="0"')

        if method in ("bindmodel", "bindcard"):
            if rec is None:
                rec = DB.card_profile(cardid)
            rec["issued"] = True
            rec["bound"] = True
            DB.persist()
            return _cardmng_xml(' status="0"')

        if method == "getdatalist":
            return _cardmng_xml(' status="0"')

    return _cardmng_xml(' status="0"')


def handle_vfgac(method: str, call: ET.Element, _model: str) -> str:
    url = game_base_url()
    if method == "service_list":
        inner = (
            f'{kitem("service_url", "str", url)}'
            "<services>"
            f'<item service="front" mode="operation">{xml_escape(url)}</item>'
            f'<item service="game" mode="operation">{xml_escape(url)}</item>'
            "</services>"
        )
        # Duplicate fields on <response> as well in case the client parses Root.
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<response>"
            f'<vfgac status="0">{inner}</vfgac>'
            f'{kitem("service_url", "str", url)}'
            "<services>"
            f'<item service="front" mode="operation">{xml_escape(url)}</item>'
            f'<item service="game" mode="operation">{xml_escape(url)}</item>'
            "</services>"
            "</response>"
        )
    if method == "update_refer":
        return eamuse_wrap("vfgac", "", "0")
    if method == "ext_campaign":
        return eamuse_wrap("vfgac", "", "0")
    if method == "send_paylog":
        return eamuse_wrap("vfgac", "", "0")
    return eamuse_wrap("vfgac", "", "0")


def handle_vfglog(method: str, _call: ET.Element, _model: str) -> str:
    return eamuse_wrap("vfglog", "", "0")


def dispatch_eamuse(model: str, module: str, method: str, xml_text: str) -> str:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        root = ET.Element("call")

    pcbid = root.attrib.get("srcid") or ""
    if pcbid:
        with _store_lock:
            DB.cabinet(pcbid, model)

    key = f"{module}.{method}"
    log.info("[XRPC] %s model=%s", key, model)

    if module == "services" and method == "get":
        return handle_services_get(root, model)
    if module == "pcbtracker" and method in ("alive", "keepalive"):
        return handle_pcbtracker(root, model)
    if module == "message" and method == "get":
        return handle_message_get(root, model)
    if module == "facility" and method == "get":
        return handle_facility_get(root, model)
    if module == "package" and method == "list":
        return handle_package_list(root, model)
    if module == "pcbevent" and method == "put":
        return handle_pcbevent_put(root, model)
    if module == "eventlog" and method == "write":
        return handle_eventlog_write(root, model)
    if module == "cardmng":
        return handle_cardmng(method, root, model)
    if module == "vfgac":
        return handle_vfgac(method, root, model)
    if module == "vfglog":
        return handle_vfglog(method, root, model)
    if module in ("eacoin", "posevent", "pkglist", "userdata", "userid", "sidmgr", "netlog"):
        return eamuse_wrap(module, "", "0")

    log.warning("[XRPC] unhandled %s — empty success", key)
    return eamuse_wrap(module or "eamuse", "", "0")


# ---------------------------------------------------------------------------
# HTTP game API (AOG)
# ---------------------------------------------------------------------------

GAME_MODES = [
    # gmode, taku_class, payment_mode, table_type, pmax, tenbo, rate
    (1, 1, 0, 0, 4, 25000, 0),  # GLOBAL_TONPU
    (2, 1, 0, 0, 4, 25000, 0),  # GLOBAL_HANCHAN
    (3, 1, 0, 0, 3, 35000, 0),  # GLOBAL_SANMA
    (4, 1, 0, 0, 2, 35000, 0),  # GLOBAL_NIMA
]

# gmode -> seat count. 4 = two-player (nima).
GMODE_SEATS = {1: 4, 2: 4, 3: 3, 4: 2}
MATCHES: Dict[str, Dict[str, Any]] = {}
TABLES: Dict[str, Dict[str, Any]] = {}

# MFG.Types.Pai: man 1-9, sou 11-19, pin 21-29, honors 31-37. Four of each, no reds.
KIND_TSUMO = 1
KIND_SUTEHAI = 2
KIND_TSUMOAGARI = 3
KIND_RON = 4
KIND_TYOKO = 14
KIND_TSUMOCHOICES = 15
KIND_SUTECHOICES = 16
KIND_KYOKUSTART = 17
KIND_KYOKUEND = 23
KIND_SCORERANK = 24
SEND_SUTE_PAI = 2
SEND_TSUMO_AGARI = 3
SEND_RON_AGARI = 4
SEND_NAKINASHI = 11
SEND_CYOUKOU = 12
SEND_KIKEN = 13
SELECT_TSUMOAGARI = 0x40
SELECT_RON = 0x80
SELECT_REACH = 0x200
SELECT_SUTE = 0x400
YAOCHU = (1, 9, 11, 19, 21, 29, 31, 32, 33, 34, 35, 36, 37)
PAI_KINDS = tuple([b + n for b in (1, 11, 21) for n in range(9)] + list(range(31, 38)))


def profile_by_session(pcuid: str) -> Optional[Dict[str, Any]]:
    if not pcuid:
        return None
    for p in DB.data.get("profiles", {}).values():
        if p.get("session_id") == pcuid:
            return p
    return None


def state_b64(profile: Optional[Dict[str, Any]], kind: str) -> str:
    raw = ((profile or {}).get("states") or {}).get(kind)
    if not raw:
        return ""
    if isinstance(raw, str):
        raw_bytes = raw.encode("utf-8")
    else:
        raw_bytes = json.dumps(raw, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw_bytes).decode("ascii")


def matching_player_human(index: int, zaseki: int, profile: Optional[Dict[str, Any]]) -> str:
    states = []
    for kind in ("player_game", "customize_item"):
        b64 = state_b64(profile, kind)
        if b64:
            states.append(
                f'<state kind="{xml_escape(kind)}"><data>{xml_escape(b64)}</data></state>'
            )
    inner = f"<zaseki>{zaseki}</zaseki><cpu_level>0</cpu_level>"
    if states:
        inner += "<client_states>" + "".join(states) + "</client_states>"
    return f'<player_{index} ptype="1">{inner}</player_{index}>'


def matching_player_cpu(index: int, zaseki: int, chara_1based: int, level: int = 1) -> str:
    oid = f"OID_CHARACTER_{chara_1based}"
    return (
        f'<player_{index} ptype="3">'
        f"<cpu_level>{level}</cpu_level>"
        f"<zaseki>{zaseki}</zaseki>"
        f"<cpu_name>{xml_escape(oid)}</cpu_name>"
        f"</player_{index}>"
    )


def ints_xml(tag: str, values) -> str:
    vals = [str(int(v)) for v in values]
    return f'<{tag} __count="{len(vals)}">{" ".join(vals)}</{tag}>'


def parse_must(form: Dict[str, str]) -> list:
    return (form_get(form, "must") or "").split("/")


def must_int(parts, index: int, default: int = 0) -> int:
    try:
        return int(parts[index]) if len(parts) > index and parts[index] != "" else default
    except ValueError:
        return default


def full_wall() -> list:
    tiles = []
    for base in (1, 11, 21):
        for n in range(9):
            tiles.extend([base + n] * 4)
    for j in range(31, 38):
        tiles.extend([j] * 4)
    return tiles


def pai_norm(pai: int) -> int:
    p = int(pai)
    return p - 64 if p >= 64 else p


def pai_counts(tiles) -> list:
    c = [0] * 40
    for t in tiles:
        n = pai_norm(t)
        if 0 <= n < 40:
            c[n] += 1
    return c


def _can_chii_start(i: int) -> bool:
    return (1 <= i <= 7) or (11 <= i <= 17) or (21 <= i <= 27)


def _mentsu_ok(c: list, n: int) -> bool:
    if n == 0:
        return True
    i = next((k for k, v in enumerate(c) if v), None)
    if i is None:
        return n == 0
    if c[i] >= 3:
        c[i] -= 3
        if _mentsu_ok(c, n - 1):
            c[i] += 3
            return True
        c[i] += 3
    if _can_chii_start(i) and c[i] and c[i + 1] and c[i + 2]:
        c[i] -= 1
        c[i + 1] -= 1
        c[i + 2] -= 1
        if _mentsu_ok(c, n - 1):
            c[i] += 1
            c[i + 1] += 1
            c[i + 2] += 1
            return True
        c[i] += 1
        c[i + 1] += 1
        c[i + 2] += 1
    return False


def is_win(tiles) -> bool:
    c = pai_counts(tiles)
    if sum(c) != 14:
        return False
    if all(c[t] >= 1 for t in YAOCHU) and sum(c[t] for t in YAOCHU) == 14 and max(c[t] for t in YAOCHU) == 2:
        return True
    pairs = [i for i, n in enumerate(c) if n == 2]
    if len(pairs) == 7 and all(n in (0, 2) for n in c):
        return True
    for i in range(40):
        if c[i] >= 2:
            c[i] -= 2
            ok = _mentsu_ok(c, 4)
            c[i] += 2
            if ok:
                return True
    return False


def waits_of(tiles13) -> list:
    c = pai_counts(tiles13)
    if sum(c) != 13:
        return []
    waits = []
    for t in PAI_KINDS:
        if c[t] >= 4:
            continue
        c[t] += 1
        tiles = []
        for i, n in enumerate(c):
            tiles.extend([i] * n)
        c[t] -= 1
        if is_win(tiles):
            waits.append(t)
    return waits


def tenpai_discard_patterns(tiles14) -> list:
    c = pai_counts(tiles14)
    if sum(c) != 14:
        return []
    patterns = []
    seen = set()
    for t in list(tiles14):
        n = pai_norm(t)
        if n in seen:
            continue
        seen.add(n)
        left = list(tiles14)
        try:
            left.remove(t)
        except ValueError:
            left.remove(n)
        waits = waits_of(left)
        if waits:
            patterns.append((n, waits))
    return patterns


def make_cell(seq: int, kind: int, pis, inner: str) -> str:
    # ReceiveCommand.ParsePlayerIndexFlag reads pi0..pi3; missing attrs NullRef → error 2600.
    flags = "".join(f' pi{i}="{1 if i in pis else 0}"' for i in range(4))
    return f'<cell_data_{seq} kind="{kind}"{flags}>{inner}</cell_data_{seq}>'


def seat_pis(seats: int):
    return list(range(max(1, seats)))


def kyoku_start_cell(seq: int, table: Dict[str, Any]) -> str:
    seats = int(table["seats"])
    inner = (
        "<chicya>0</chicya>"
        "<oya>0</oya>"
        f"{ints_xml('sai', table['sai'])}"
        "<ba>0</ba>"
        "<kyoku>0</kyoku>"
        "<all_last>0</all_last>"
        "<honba>0</honba>"
        "<rencyan>0</rencyan>"
        "<kyoutaku>0</kyoutaku>"
        f"<nokori>{table['nokori0']}</nokori>"
        "<dora_open>1</dora_open>"
        f"{ints_xml('dora', table['dora'])}"
        f"{ints_xml('ura_dora', table['ura'])}"
        f"<yama_cnt>{len(table['yama0'])}</yama_cnt>"
        f"{ints_xml('yama', table['yama0'])}"
        f"{ints_xml('rinshan', table['rinshan'])}"
    )
    # KyokuStart.ParsePlayer loops TAKU_PLAYER_MAX (4) and stops on missing node.
    # Emit all four so the first missing-node path is never hit.
    dummy = [1] * 13
    for i in range(4):
        if i < seats:
            tepai = table["haipai"][i]
            score = table["score"]
        else:
            tepai = dummy
            score = table["score"]
        inner += (
            f"<player_info{i}>"
            f"<jikaze>{i}</jikaze>"
            f"{ints_xml('tepai', tepai)}"
            f"<score>{score}</score>"
            f"<rank>{i}</rank>"
            f"</player_info{i}>"
        )
    return make_cell(seq, KIND_KYOKUSTART, seat_pis(seats), inner)


def tsumo_cell(seq: int, seats: int, pindex: int, pai: int) -> str:
    return make_cell(
        seq, KIND_TSUMO, seat_pis(seats), f"<pindex>{pindex}</pindex><pai>{int(pai)}</pai>"
    )


def dummy_naki_xml() -> str:
    pais = "".join(
        f"<pai{i}><pai_st>0</pai_st><pai>0</pai></pai{i}>" for i in range(4)
    )
    return f"<type>0</type><kantype>0</kantype>{pais}"


def yaku_xml(tag: str, win_pai: int, hand, han: int, fu: int) -> str:
    nakis = "".join(f"<naki{i}>{dummy_naki_xml()}</naki{i}>" for i in range(4))
    return (
        f"<{tag}>"
        f"<pai>{int(win_pai)}</pai>"
        f"<yaku_han>1</yaku_han>"
        f"<han_num>{han}</han_num>"
        f"<fu_num>{fu}</fu_num>"
        f"<dora_num>0</dora_num>"
        f"<bonus_han>0</bonus_han>"
        f"<yaku1>0</yaku1>"
        f"<yaku2>0</yaku2>"
        f"{ints_xml('tepai', hand)}"
        f"{nakis}"
        f"</{tag}>"
    )


def calc_score_xml(index: int, before: int, delta: int) -> str:
    return (
        f"<calc_score{index}>"
        f"<before_score>{before}</before_score>"
        f"<yaku_score>{delta}</yaku_score>"
        f"<kyotaku_score>0</kyotaku_score>"
        f"<tumifu_score>0</tumifu_score>"
        f"<new_score>{before + delta}</new_score>"
        f"<wherefore>0</wherefore>"
        f"</calc_score{index}>"
    )


def tsumo_choices_cell(seq: int, table: Dict[str, Any], pindex: int = 0) -> str:
    hand = list(table["hands"][pindex])
    riichi = (table.get("riichi") or [False] * 4)
    flags = SELECT_SUTE
    patterns = []
    if is_win(hand):
        flags |= SELECT_TSUMOAGARI
    if not riichi[pindex]:
        patterns = tenpai_discard_patterns(hand)
        if patterns:
            flags |= SELECT_REACH
    inner = f"<select>{flags}</select><ptn_num>{len(patterns)}</ptn_num>"
    for i, (sute, waits) in enumerate(patterns):
        inner += (
            f"<ptn{i}>"
            f"<sute_pai>{sute}</sute_pai>"
            f"<machi_num>{len(waits)}</machi_num>"
            f"{ints_xml('machi_pai', waits)}"
            f"{ints_xml('stat', [0] * len(waits))}"
            f"</ptn{i}>"
        )
    return make_cell(seq, KIND_TSUMOCHOICES, [pindex], inner)


def sute_choices_ron_cell(seq: int, discarder: int, pai: int) -> str:
    inner = (
        f"<select>{SELECT_RON}</select>"
        f"<naki>0</naki>"
        f"<pindex>{discarder}</pindex>"
        f"<sute_pai>{int(pai)}</sute_pai>"
    )
    return make_cell(seq, KIND_SUTECHOICES, [0], inner)


def tyoko_cell(seq: int, seats: int, pindex: int) -> str:
    return make_cell(
        seq, KIND_TYOKO, seat_pis(seats), f"<pindex>{pindex}</pindex>"
    )


def sutehai_cell(seq: int, seats: int, pindex: int, pai: int, stat: int) -> str:
    return make_cell(
        seq,
        KIND_SUTEHAI,
        seat_pis(seats),
        f"<pindex>{pindex}</pindex><pai>{int(pai)}</pai><stat>{int(stat)}</stat>",
    )


def append_cell(table: Dict[str, Any], xml: str) -> None:
    table["cells"].append(xml)


def draw_tile(table: Dict[str, Any]) -> Optional[int]:
    wall = table["wall"]
    if not wall:
        return None
    pai = wall.pop(0)
    table["nokori"] = len(wall)
    return pai


def deal_into_hand(table: Dict[str, Any], pindex: int) -> Optional[int]:
    pai = draw_tile(table)
    if pai is None:
        return None
    table["hands"][pindex].append(pai)
    return pai


def start_kyoku(match: Dict[str, Any]) -> Dict[str, Any]:
    seats = int(match.get("seats") or 2)
    score = 35000 if seats <= 3 else 25000
    wall = full_wall()
    random.shuffle(wall)
    haipai = []
    hands = []
    for _ in range(seats):
        tiles = wall[:13]
        wall = wall[13:]
        haipai.append(list(tiles))
        hands.append(list(tiles))
    live = wall[:-14]
    wan = wall[-14:]
    table: Dict[str, Any] = {
        "seats": seats,
        "score": score,
        "sai": [random.randint(1, 6), random.randint(1, 6)],
        "haipai": haipai,
        "hands": hands,
        "yama0": list(live),
        "wall": list(live),
        "rinshan": wan[:4],
        "dora": wan[4:9],
        "ura": wan[9:14],
        "nokori0": len(live),
        "nokori": len(live),
        "cells": [],
        "waiting_sute": False,
        "pending_choices": False,
        "waiting_naki": False,
        "riichi": [False] * 4,
        "player_scores": [score] * 4,
        "turn": 0,
    }
    append_cell(table, kyoku_start_cell(0, table))
    pai = deal_into_hand(table, 0)
    if pai is not None:
        append_cell(table, tsumo_cell(1, seats, 0, pai))
        append_cell(table, tsumo_choices_cell(2, table, 0))
        table["waiting_sute"] = True
        table["pending_choices"] = False
        table["turn"] = 0
    log.info(
        "kyoku start seats=%s nokori=%s dora=%s hands=%s",
        seats,
        table["nokori0"],
        table["dora"],
        [len(h) for h in table["hands"]],
    )
    return table


def taikyoku_xml(table: Dict[str, Any], start: int) -> str:
    cells = table["cells"]
    if start < 0:
        start = 0
    if start >= len(cells):
        return '<taikyoku><cell_info available="0" /></taikyoku>'
    chunk = cells[start:]
    body = "".join(chunk)
    return (
        "<taikyoku>"
        f'<cell_info available="1">'
        f'<cell_sno start="{start}" count="{len(chunk)}"></cell_sno>'
        f"{body}"
        "</cell_info>"
        "</taikyoku>"
    )


def remove_from_hand(hand, pai: int) -> None:
    try:
        hand.remove(pai)
    except ValueError:
        if hand:
            hand.pop()


def play_cpu_turn(table: Dict[str, Any], cpu_index: int) -> Optional[int]:
    seats = int(table["seats"])
    cpu_pai = deal_into_hand(table, cpu_index)
    if cpu_pai is None:
        return None
    append_cell(table, tsumo_cell(len(table["cells"]), seats, cpu_index, cpu_pai))
    remove_from_hand(table["hands"][cpu_index], cpu_pai)
    append_cell(table, sutehai_cell(len(table["cells"]), seats, cpu_index, cpu_pai, 2))
    return cpu_pai


def offer_human_ron(table: Dict[str, Any], discarder: int, pai: int) -> bool:
    human13 = list(table["hands"][0])
    if not is_win(human13 + [pai]):
        return False
    append_cell(table, sute_choices_ron_cell(len(table["cells"]), discarder, pai))
    table["waiting_naki"] = True
    table["naki_pai"] = pai
    table["naki_from"] = discarder
    table["waiting_sute"] = False
    table["pending_choices"] = False
    return True


def human_draw_next(table: Dict[str, Any]) -> None:
    human_pai = deal_into_hand(table, 0)
    if human_pai is None:
        return
    append_cell(table, tsumo_cell(len(table["cells"]), int(table["seats"]), 0, human_pai))
    table["pending_choices"] = True
    table["waiting_sute"] = True
    table["turn"] = 0


def advance_turns_from(table: Dict[str, Any], start_seat: int) -> None:
    """CPU seats after the human (0) each tsumo-giri, then the human draws."""
    seats = int(table["seats"])
    nxt = start_seat % seats
    while nxt != 0:
        cpu_pai = play_cpu_turn(table, nxt)
        if cpu_pai is None:
            return
        if offer_human_ron(table, nxt, cpu_pai):
            return
        nxt = (nxt + 1) % seats
    human_draw_next(table)


def apply_sute(table: Dict[str, Any], pindex: int, pai: int, reach: int, tsumogiri: int) -> None:
    seats = int(table["seats"])
    pindex = max(0, min(pindex, seats - 1))
    remove_from_hand(table["hands"][pindex], pai)
    if reach:
        riichi = table.setdefault("riichi", [False] * 4)
        riichi[pindex] = True
    stat = (1 if reach else 0) | (2 if tsumogiri else 0)
    append_cell(table, sutehai_cell(len(table["cells"]), seats, pindex, pai, stat))
    table["waiting_sute"] = False
    advance_turns_from(table, (pindex + 1) % seats)


def append_agari_finish(table: Dict[str, Any], winner: int, win_pai: int, is_tsumo: bool, furikomi: int = 1) -> None:
    seats = int(table["seats"])
    scores = list(table.setdefault("player_scores", [table.get("score", 35000)] * 4))
    while len(scores) < 4:
        scores.append(0)
    before = list(scores)
    delta = 2000
    if is_tsumo:
        loser = 1 if winner == 0 else 0
        scores[winner] += delta
        scores[loser] -= delta
    else:
        scores[winner] += delta
        scores[furikomi] -= delta
    table["player_scores"] = scores
    hand = list(table["hands"][winner])
    dora = table.get("dora") or [1, 1, 1, 1, 1]
    ura = table.get("ura") or [1, 1, 1, 1, 1]
    inner = ""
    if is_tsumo:
        inner += f"<pindex>{winner}</pindex>"
    else:
        inner += f"<furikomi_pindex>{furikomi}</furikomi_pindex>"
        inner += ints_xml("ron_flg", [1 if i == winner else 0 for i in range(4)])
    inner += (
        "<dora_open>1</dora_open>"
        f"{ints_xml('dora', dora)}"
        f"{ints_xml('ura_dora', ura)}"
    )
    if is_tsumo:
        inner += yaku_xml("yaku", win_pai, hand, 1, 30)
    else:
        for i in range(4):
            inner += yaku_xml(
                "yaku" + str(i),
                win_pai,
                hand if i == winner else [1] * 13,
                1 if i == winner else 0,
                30,
            )
    for i in range(4):
        inner += calc_score_xml(i, int(before[i]), int(scores[i] - before[i]))
    kind = KIND_TSUMOAGARI if is_tsumo else KIND_RON
    append_cell(table, make_cell(len(table["cells"]), kind, seat_pis(seats), inner))
    rank_inner = "<kyoutaku>0</kyoutaku>"
    ordered = sorted(range(max(seats, 2)), key=lambda i: -scores[i])
    ranks = [0] * 4
    for r, i in enumerate(ordered):
        ranks[i] = r
    for i in range(4):
        rank_inner += (
            f"<riti_after{i}><score>{int(scores[i])}</score><rank>{ranks[i]}</rank></riti_after{i}>"
        )
    append_cell(table, make_cell(len(table["cells"]), KIND_SCORERANK, seat_pis(seats), rank_inner))
    append_cell(
        table,
        make_cell(len(table["cells"]), KIND_KYOKUEND, seat_pis(seats), "<end_stat>1</end_stat>"),
    )
    table["waiting_sute"] = False
    table["pending_choices"] = False
    table["waiting_naki"] = False


def flush_pending_choices(table: Dict[str, Any]) -> None:
    if not table.get("pending_choices"):
        return
    table["pending_choices"] = False
    append_cell(table, tsumo_choices_cell(len(table["cells"]), table, 0))


def mgresult_xml(table: Optional[Dict[str, Any]] = None) -> str:
    seats = int((table or {}).get("seats") or 2)
    score = int((table or {}).get("score") or 35000)
    gmode = 4 if seats <= 2 else (3 if seats == 3 else 1)
    parts = [
        f"<gmode>{gmode}</gmode>",
        "<taku_class>1</taku_class>",
        "<continue_state>0</continue_state>",
        "<continue_fee>0</continue_fee>",
    ]
    for i in range(seats):
        parts.append(
            f"<player_{i}><rank>{i}</rank><score>{score}</score><uma>0</uma></player_{i}>"
        )
    return "<mgresult>" + "".join(parts) + "</mgresult>"


def matching_xml(match: Dict[str, Any], profile: Optional[Dict[str, Any]]) -> str:
    seats = int(match.get("seats") or 2)
    cpu_n = max(0, seats - 1)
    mid = int(match.get("mid") or (profile or {}).get("player_id") or 1)
    name = match.get("name") or (profile or {}).get("name") or "ゲスト"
    pindex = int(match.get("pindex") or 0)
    used = 1
    try:
        pg = json.loads((profile or {}).get("states", {}).get("player_game") or "{}")
        used = int(pg.get("SelectChara") or 0) + 1
    except Exception:
        used = 1
    players = [matching_player_human(0, 0, profile)]
    cpu_chara = 1
    for i in range(1, seats):
        cpu_chara += 1
        if cpu_chara == used:
            cpu_chara += 1
        if cpu_chara > 19:
            cpu_chara = 1 if used != 1 else 2
        players.append(matching_player_cpu(i, i, cpu_chara, level=1))
    mend = "".join(players)
    return (
        "<mwait>"
        "<status>1</status>"
        "<pnum>1</pnum>"
        f"<cpu_num>{cpu_n}</cpu_num>"
        f"<pindex>{pindex}</pindex>"
        f"<epdata_0><name>{xml_escape(name)}</name><mid>{mid}</mid></epdata_0>"
        f"<mend>{mend}</mend>"
        "</mwait>"
    )


def ensure_table(pcuid: str, match: Dict[str, Any]) -> Dict[str, Any]:
    table = TABLES.get(pcuid)
    if table is None:
        table = start_kyoku(match)
        TABLES[pcuid] = table
    return table


def serv_st_ok() -> str:
    return "<serv_st><code>0</code></serv_st>"


def xml_response(*chunks: str) -> str:
    body = "".join(chunks)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<root>{serv_st_ok()}{body}</root>'


def form_get(form: Dict[str, str], *keys: str, default: str = "") -> str:
    for k in keys:
        if k in form and form[k] not in (None, ""):
            return form[k]
    return default


def handle_appli_boot(form: Dict[str, str]) -> str:
    return xml_response(
        "<server_setting>"
        "<mask_ac_link_scene>0</mask_ac_link_scene>"
        "<reviewed_version>false</reviewed_version>"
        "</server_setting>"
        "<boot_mes>"
        "<status>0</status>"
        f"<moserv_url>{xml_escape(game_base_url())}</moserv_url>"
        "<message></message>"
        "</boot_mes>"
    )


def handle_appli_info(form: Dict[str, str]) -> str:
    return xml_response("<expire_seconds>86400</expire_seconds>")


def handle_login(form: Dict[str, str]) -> str:
    guest = form_get(form, "guest") == "1"
    user_id = form_get(form, "user_id", "dataid", default="GUEST")
    session = uuid.uuid4().hex
    with _store_lock:
        profile = DB.ensure_profile(user_id, "GUEST" if guest else "PLAYER")
        profile["session_id"] = session
        profile["is_guest"] = guest
        DB.persist()
    return xml_response(f"<auth><session_id>{session}</session_id></auth>")


def handle_logout(_form: Dict[str, str]) -> str:
    return xml_response()


def handle_create_player(form: Dict[str, str]) -> str:
    name = form_get(form, "name", default="PLAYER")
    user_id = form_get(form, "user_id", default=uuid.uuid4().hex[:16])
    with _store_lock:
        p = DB.ensure_profile(user_id, name)
        p["name"] = name
        DB.persist()
    return xml_response()


def playmode_xml() -> str:
    parts = ["<playmode_list>"]
    for gmode, taku, pay, table, pmax, tenbo, rate in GAME_MODES:
        parts.append(
            "<mode>"
            f"<gmode>{gmode}</gmode>"
            f"<taku_class>{taku}</taku_class>"
            f"<payment_mode>{pay}</payment_mode>"
            f"<table_type>{table}</table_type>"
            f"<pmax>{pmax}</pmax>"
            f"<tenbo>{tenbo}</tenbo>"
            "<state>1</state>"
            f"<rate>{rate}</rate>"
            "<superior_border>0</superior_border>"
            "</mode>"
        )
    parts.append("</playmode_list>")
    return "".join(parts)


def handle_get_menudata(form: Dict[str, str]) -> str:
    name = "GUEST"
    mid = 1
    pcuid = form_get(form, "pcuid")
    with _store_lock:
        for p in DB.data.get("profiles", {}).values():
            if p.get("session_id") == pcuid:
                name = p.get("name") or name
                mid = int(p.get("player_id") or 1)
                break
    return xml_response(
        "<menudata>"
        f"<mpdata><mid>{mid}</mid><name>{xml_escape(name)}</name></mpdata>"
        f"{playmode_xml()}"
        "</menudata>"
    )


def handle_keep_alive(_form: Dict[str, str]) -> str:
    return xml_response()


def handle_client_state_read(form: Dict[str, str]) -> str:
    mid = int(form_get(form, "mid", default="0") or 0)
    one = form_get(form, "one_kind")
    chunks = []
    with _store_lock:
        p = DB.by_player_id(mid) if mid else None
        states = (p or {}).get("states") or {}
        items = states.items() if not one else [(one, states[one])] if one in states else []
        for kind, payload in items:
            raw = payload.encode("utf-8") if isinstance(payload, str) else payload
            b64 = base64.b64encode(raw if isinstance(raw, bytes) else str(raw).encode("utf-8")).decode("ascii")
            chunks.append(f'<state kind="{xml_escape(kind)}"><data>{b64}</data></state>')
    return xml_response(*chunks)


def handle_client_state_write(form: Dict[str, str]) -> str:
    mid = int(form_get(form, "mid", default="0") or 0)
    kind = form_get(form, "kind")
    data = form_get(form, "data")
    if data:
        try:
            decoded = base64.b64decode(unquote_plus(data)).decode("utf-8", errors="replace")
        except Exception:
            decoded = data
        with _store_lock:
            DB.save_state(mid, kind or "unknown", decoded)
    return xml_response()


def handle_entry_game(form: Dict[str, str]) -> str:
    """Return a local table. CPU play still talks GGet/GPost to this host."""
    gmode = int(form_get(form, "gmode", default="1") or 1)
    pcuid = form_get(form, "pcuid")
    seats = GMODE_SEATS.get(gmode, 4)
    with _store_lock:
        profile = profile_by_session(pcuid)
        MATCHES[pcuid] = {
            "gmode": gmode,
            "seats": seats,
            "tid": 1,
            "pindex": 0,
            "mid": int((profile or {}).get("player_id") or 1),
            "name": (profile or {}).get("name") or "ゲスト",
        }
        TABLES.pop(pcuid, None)
    url = game_base_url().rstrip("/") + "/"
    return xml_response(
        "<entry>"
        "<gserv_id>1</gserv_id>"
        "<tid>1</tid>"
        "<pindex>0</pindex>"
        "<next_sno>0</next_sno>"
        "<last_cyoukou_num>3</last_cyoukou_num>"
        "<cyoukou_num>3</cyoukou_num>"
        "<ste_oya1_limit_time>15000</ste_oya1_limit_time>"
        "<ste_limit_time>10000</ste_limit_time>"
        "<ste_reechi1_limit_time>15000</ste_reechi1_limit_time>"
        "<naki_limit_time>8000</naki_limit_time>"
        "<agari_limit_time>10000</agari_limit_time>"
        "<naki_choice_limit_time>8000</naki_choice_limit_time>"
        "<reechi_choice_limit_time>8000</reechi_choice_limit_time>"
        "<last_cyoukou_limit_time>30000</last_cyoukou_limit_time>"
        "<last_time>30000</last_time>"
        f"<gserv_url>{xml_escape(url)}</gserv_url>"
        "<pay_mode>0</pay_mode>"
        f"<gmode>{gmode}</gmode>"
        "</entry>"
    )


def handle_gget(form: Dict[str, str]) -> str:
    """Matching snapshot, then kyoku stream once the client sets ready=1."""
    pcuid = form_get(form, "pcuid")
    ready = form_get(form, "ready") == "1"
    next_sno = must_int(parse_must(form), 5, 0)
    with _store_lock:
        match = MATCHES.get(pcuid) or {
            "gmode": 4,
            "seats": 2,
            "mid": 1,
            "name": "ゲスト",
            "pindex": 0,
        }
        profile = profile_by_session(pcuid)
        mwait = matching_xml(match, profile)
        if ready:
            table = ensure_table(pcuid, match)
            flush_pending_choices(table)
            tai = taikyoku_xml(table, next_sno)
            all_ready = 1
        else:
            tai = ""
            all_ready = 0
    return xml_response(
        "<game>"
        f"<all_ready>{all_ready}</all_ready>"
        f"{mwait}"
        f"{tai}"
        "</game>"
    )


def handle_gpost(form: Dict[str, str]) -> str:
    pcuid = form_get(form, "pcuid")
    parts = parse_must(form)
    kind = must_int(parts, 6, 0)
    pindex = must_int(parts, 3, 0)
    pai = must_int(parts, 9, 0)
    reach = must_int(parts, 12, 0)
    tsumogiri = must_int(parts, 13, 0)
    with _store_lock:
        match = MATCHES.get(pcuid) or {"gmode": 4, "seats": 2, "mid": 1, "name": "ゲスト", "pindex": 0}
        table = ensure_table(pcuid, match)
        start = len(table["cells"])
        if kind == SEND_SUTE_PAI:
            apply_sute(table, pindex, pai, reach, tsumogiri)
            log.info(
                "gpost sute pindex=%s pai=%s reach=%s tsumogiri=%s next=%s",
                pindex,
                pai,
                reach,
                tsumogiri,
                len(table["cells"]),
            )
        elif kind == SEND_TSUMO_AGARI:
            win_pai = table["hands"][0][-1] if table["hands"][0] else pai
            append_agari_finish(table, 0, win_pai, is_tsumo=True)
            log.info("gpost tsumo agari pai=%s", win_pai)
        elif kind == SEND_RON_AGARI:
            win_pai = int(table.get("naki_pai") or pai)
            if win_pai not in table["hands"][0]:
                table["hands"][0].append(win_pai)
            append_agari_finish(table, 0, win_pai, is_tsumo=False, furikomi=1)
            log.info("gpost ron pai=%s", win_pai)
        elif kind == SEND_NAKINASHI:
            log.info("gpost nakinashi from=%s", table.get("naki_from"))
            if table.get("waiting_naki"):
                from_seat = int(table.get("naki_from") or 1)
                table["waiting_naki"] = False
                seats = int(table["seats"])
                advance_turns_from(table, (from_seat + 1) % seats)
        elif kind == SEND_CYOUKOU:
            append_cell(table, tyoko_cell(len(table["cells"]), int(table["seats"]), pindex))
            log.info("gpost cyoukou pindex=%s", pindex)
        elif kind == SEND_KIKEN:
            log.info("gpost kiken pindex=%s", pindex)
        else:
            log.info("gpost kind=%s pindex=%s pai=%s", kind, pindex, pai)
        tai = taikyoku_xml(table, start)
    return xml_response(f"<game>{tai}</game>")


def handle_end_or_kiken(form: Dict[str, str]) -> str:
    pcuid = form_get(form, "pcuid")
    with _store_lock:
        table = TABLES.get(pcuid)
        body = mgresult_xml(table)
    return xml_response(body)


def handle_chk_tabooword(form: Dict[str, str]) -> str:
    return xml_response("<result>0</result>")


GAME_HANDLERS = {
    "appli_boot": handle_appli_boot,
    "appli_info": handle_appli_info,
    "login": handle_login,
    "logout": handle_logout,
    "create_player": handle_create_player,
    "get_menudata": handle_get_menudata,
    "keep_alive": handle_keep_alive,
    "client_state_read": handle_client_state_read,
    "client_state_write": handle_client_state_write,
    "entry_game": handle_entry_game,
    "gget": handle_gget,
    "gpost": handle_gpost,
    "end_game": handle_end_or_kiken,
    "kiken_game": handle_end_or_kiken,
    "reconnect": handle_entry_game,
    "chk_tabooword": handle_chk_tabooword,
    "player_record": lambda f: xml_response(),
    "notice_done": lambda f: xml_response(),
    "present_done": lambda f: xml_response(),
    "important_notice_done": lambda f: xml_response(),
    "mission_date": lambda f: xml_response(),
    "gacha_info": lambda f: xml_response(),
    "gacha_log": lambda f: xml_response(),
    "req_draw_gacha": lambda f: xml_response(),
    "get_gacha_result": lambda f: xml_response(),
    "music_gacha_play": lambda f: xml_response(),
    "music_gacha_play_reserve": lambda f: xml_response(),
    "dojo_get_status": lambda f: xml_response(),
    "dojo_set_slot": lambda f: xml_response(),
    "dojo_gain_soul": lambda f: xml_response(),
    "get_record": lambda f: xml_response(),
    "get_haifu_list": lambda f: xml_response(),
    "get_haifu_data": lambda f: xml_response(),
    "get_jongstone_info": lambda f: xml_response(),
    "gget_stamp_info": lambda f: xml_response(),
    "gchat": lambda f: xml_response(),
    "item_gain_log": lambda f: xml_response(),
    "item_consume_log": lambda f: xml_response(),
    "set_favorite_character": lambda f: xml_response(),
    "competition_entry": lambda f: xml_response(),
    "odekake_done": lambda f: xml_response(),
    "coop_done": lambda f: xml_response(),
    "end_show": lambda f: xml_response(),
    "eashop_done": lambda f: xml_response(),
}


def parse_form(raw: bytes) -> Dict[str, str]:
    text = raw.decode("utf-8", errors="replace")
    qs = parse_qs(text, keep_blank_values=True)
    return {k: (v[-1] if v else "") for k, v in qs.items()}


def game_api_name(path: str) -> str:
    p = path.strip("/")
    if p.startswith("aog/"):
        p = p[4:]
    p = p.split("?")[0].strip("/")
    return p.split("/")[-1] if p else ""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - " + fmt, self.address_string(), *args)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0") or 0)
        return self.rfile.read(length) if length else b""

    def _send(self, status: int, body: bytes, headers: Dict[str, str]) -> None:
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/health", "/status"):
            body = (
                f"VFG local server ok\n"
                f"e-amuse: http://{HOST}:{PORT}\n"
                f"game:    {game_base_url()}\n"
            ).encode("utf-8")
            self._send(200, body, {"Content-Type": "text/plain; charset=utf-8"})
            return
        if parsed.path.startswith("/core/keepalive"):
            self._send(200, b"ok", {"Content-Type": "text/plain"})
            return
        self._send(404, b"not found", {"Content-Type": "text/plain"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        body = self._read_body()
        info = parse_eamuse_info(self.headers.get("X-Eamuse-Info") or self.headers.get("x-eamuse-info"))
        compress = (self.headers.get("X-Compress") or self.headers.get("x-compress") or "none").lower()

        # e-Amusement if encrypted or path looks like XRPC.
        is_xrpc = bool(info) or parsed.path.startswith("//") or "model=" in (parsed.query or "") or parsed.path.startswith("/VFG")
        if is_xrpc or self.headers.get("User-Agent", "").upper().startswith("EAMUSE"):
            self._handle_xrpc(parsed, body, info, compress)
            return
        self._handle_game(parsed, body)

    def _handle_xrpc(self, parsed, body: bytes, info: Optional[str], compress: str) -> None:
        model = ""
        module = ""
        method = ""
        qs = parse_qs(parsed.query)
        if "model" in qs:
            model = qs.get("model", [""])[0]
            module = qs.get("module", [""])[0]
            method = qs.get("method", [""])[0]
        else:
            # //MODEL/module/method  or /MODEL/module/method
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 3:
                model, module, method = parts[0], parts[1], parts[2]
            elif len(parts) == 2:
                module, method = parts[0], parts[1]

        used_kbin = True
        xml_text = ""
        fparam = (qs.get("f") or [""])[0]
        if fparam and "." in fparam and not module:
            module, method = fparam.split(".", 1)
        if not model:
            model = (qs.get("model") or [""])[0]
        try:
            xml_text, used_kbin = decode_eamuse_body(body, info, compress)
        except Exception:
            log.exception("failed to decode e-amuse body info=%s compress=%s bytes=%s", info, compress, len(body))
            dump = CAPTURE_DIR / "requests" / f"raw_{next_seq():04d}_{module}_{method}.bin"
            try:
                dump.write_bytes(body)
                log.warning("raw e-amuse body saved to %s (%s bytes)", dump, len(body))
            except Exception:
                pass
            xml_text = "<call/>"
            used_kbin = True
            if module == "cardmng":
                cardid = DEFAULT_CARDID
                xml_text = (
                    f'<call model="{xml_escape(model)}">'
                    f'<cardmng method="{xml_escape(method or "inquire")}" '
                    f'cardid="{xml_escape(cardid)}" cardtype="1"/>'
                    "</call>"
                )

        if not module:
            try:
                root = ET.fromstring(xml_text)
                child = list(root)[0] if list(root) else root
                module = child.tag
                method = child.attrib.get("method") or method
                model = root.attrib.get("model") or model
            except Exception:
                pass

        req_name = f"{module}.{method}" or "unknown"
        save_capture("requests", req_name.replace(".", "_"), xml_text)

        try:
            resp_xml = dispatch_eamuse(model, module, method, xml_text)
        except Exception:
            log.exception("handler crashed for %s", req_name)
            resp_xml = eamuse_wrap(module or "eamuse", "", "0")

        save_capture("responses", req_name.replace(".", "_"), resp_xml)
        try:
            payload = encode_eamuse_body(resp_xml, info, compress, used_kbin)
        except Exception:
            log.exception("failed to encode kbin, falling back to XML")
            payload = encode_eamuse_body(resp_xml, info, compress, False)

        headers = {
            "Content-Type": "application/octet-stream",
            "X-Compress": compress if compress in ("lz77", "none") else "none",
        }
        if info:
            headers["X-Eamuse-Info"] = info
        self._send(200, payload, headers)

    def _handle_game(self, parsed, body: bytes) -> None:
        name = game_api_name(parsed.path)
        form = parse_form(body)
        log.info("[AOG] /%s keys=%s", name, sorted(form.keys()))
        dump = f"PATH {parsed.path}\n" + "&".join(f"{k}={v}" for k, v in form.items())
        save_capture("requests", f"aog_{name or 'root'}", dump)

        handler = GAME_HANDLERS.get(name)
        if handler is None:
            log.warning("[AOG] unhandled %s — empty success", name or parsed.path)
            xml = xml_response()
        else:
            try:
                xml = handler(form)
            except Exception:
                log.exception("AOG handler crashed for %s", name)
                xml = xml_response()

        save_capture("responses", f"aog_{name or 'root'}", xml)
        data = xml.encode("utf-8")
        self._send(200, data, {"Content-Type": "text/xml; charset=utf-8"})


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = logging.FileHandler(LOG_DIR / "server.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def main() -> None:
    global HOST, PORT
    parser = argparse.ArgumentParser(description="Mahjong Fight Girl local server")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    HOST, PORT = args.host, args.port
    setup_logging()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("VFG local server listening on http://%s:%s", HOST, PORT)
    log.info("Point Spice2x EA Service URL to http://%s:%s", HOST, PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        httpd.server_close()


if __name__ == "__main__":
    main()
