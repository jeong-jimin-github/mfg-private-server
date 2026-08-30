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
import hashlib
import json
import logging
import os
import random
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote_plus, urlparse
from xml.etree import ElementTree as ET

import mahjong
import taikyoku
from protocol import EamuseDecodeMeta, decode_eamuse_body, encode_eamuse_body, parse_eamuse_info

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
_store_lock = threading.RLock()
_stamp_lock = threading.Lock()


def _player_id_from_refid(refid: str) -> int:
    """Return a stable positive player id for both hex and synthetic identities."""
    prefix = (refid or "")[:8]
    try:
        value = int(prefix, 16)
    except ValueError:
        value = int.from_bytes(hashlib.sha256((refid or "GUEST").encode("utf-8")).digest()[:4], "big")
    return value & 0x7FFFFFFF or 1


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
                "player_id": _player_id_from_refid(refid),
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


# Captures used to land in captures/<kind>/<seq>_<name>.xml with `seq` reset to
# zero on every start, so each restart silently overwrote the previous session's
# dumps from 0001 up.  The one run that actually matters - the one that just
# crashed - was routinely destroyed by the restart that came after it.  Give
# every run its own directory instead, and keep captures/<kind> as a symlink-ish
# "latest" pointer via captures/latest.txt.
RUN_ID = time.strftime("%Y%m%d-%H%M%S")
RUN_DIR = CAPTURE_DIR / f"run-{RUN_ID}"


def save_capture(kind: str, name: str, body: str) -> Path:
    seq = next_seq()
    folder = RUN_DIR / kind
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
        CARDMNG_SHADOW_MODULE,
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


# ---------------------------------------------------------------------------
# eacoin (PASELI)
# ---------------------------------------------------------------------------

PASELI_BALANCE = 57300      # what bemaniutils calls "infinite PASELI"
PASELI_SESSIONS: Dict[str, int] = {}


def _call_child(call: ET.Element, name: str) -> str:
    node = call.find(name)
    return (node.text or "").strip() if node is not None else ""


def handle_eacoin(method: str, call: ET.Element, _model: str) -> str:
    """Local PASELI wallet.  Everything is offline, so the balance is a
    constant and every consume simply succeeds."""
    if method in ("checkin", "opcheckin"):
        sess = uuid.uuid4().hex[:16]
        with _store_lock:
            PASELI_SESSIONS[sess] = PASELI_BALANCE
        if method == "opcheckin":
            return eamuse_wrap("eacoin", kitem("sessid", "str", sess))
        inner = (
            kitem("sequence", "s16", 0)
            + kitem("acstatus", "u8", 0)
            + kitem("acid", "str", "LOCAL")
            + kitem("acname", "str", FACILITY_NAME)
            + kitem("balance", "s32", PASELI_BALANCE)
            + kitem("sessid", "str", sess)
        )
        return eamuse_wrap("eacoin", inner)
    if method == "consume":
        sess = _call_child(call, "sessid")
        try:
            payment = int(_call_child(call, "payment") or 0)
        except ValueError:
            payment = 0
        with _store_lock:
            balance = PASELI_SESSIONS.get(sess, PASELI_BALANCE) - payment
            if balance < 0:
                balance = 0
            PASELI_SESSIONS[sess] = balance
        log.info("[eacoin] consume %s -> balance %s", payment, balance)
        inner = (
            kitem("acstatus", "u8", 0)
            + kitem("autocharge", "u8", 0)
            + kitem("balance", "s32", balance)
        )
        return eamuse_wrap("eacoin", inner)
    if method == "getbalance":
        sess = _call_child(call, "sessid")
        with _store_lock:
            balance = PASELI_SESSIONS.get(sess, PASELI_BALANCE)
        return eamuse_wrap(
            "eacoin", kitem("acstatus", "u8", 0) + kitem("balance", "s32", balance)
        )
    if method == "checkout":
        sess = _call_child(call, "sessid")
        with _store_lock:
            PASELI_SESSIONS.pop(sess, None)
        return eamuse_wrap("eacoin", "")
    if method in ("getlog", "getoplog", "getcampaign"):
        return eamuse_wrap("eacoin", '<topic><sumdate __type="str">0</sumdate></topic>')
    return eamuse_wrap("eacoin", "")


def handle_package_list(_call: ET.Element, _model: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n<response><package expire="600" status="0" /></response>'


def handle_pcbevent_put(_call: ET.Element, _model: str) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n<response><pcbevent status="0" /></response>'


def handle_eventlog_write(_call: ET.Element, _model: str) -> str:
    inner = kitem("gamesession", "s64", 1) + kitem("logsendflg", "s32", 0) + kitem("logerrlevel", "s32", 0) + kitem("evtidnosendflg", "s32", 0)
    return eamuse_wrap("eventlog", inner)


_CARDID_RE = re.compile(r"^[0-9A-Fa-f]{16}$")
DEFAULT_CARDID = "E0047CC78DFBA459"

# AVS registers its own "cardmng" XRPC module during ea3 boot, so the game's
# Ea3XrpcAddModule("cardmng", ...) is refused ("already has same name") and the
# managed XML is serialized through the built-in descriptor instead. That is
# what puts a 3-byte cardid and a pointer-sized cardtype on the wire and then
# corrupts the AVS heap. vfgac/vfglog register cleanly and are never corrupted.
#
# The patched client (tools/patch_cardmng_module.py) registers the same five
# methods under this name, which AVS does not own, so the request is built from
# the game's own XML. Requests and responses use this element name instead of
# "cardmng".
CARDMNG_SHADOW_MODULE = "vfgcard"


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


# Set per request so the response element mirrors the module the client used.
_cardmng_wire_node = threading.local()


def _cardmng_node_name() -> str:
    return getattr(_cardmng_wire_node, "name", None) or "cardmng"


def _cardmng_xml(attrs: str) -> str:
    node = _cardmng_node_name()
    header = '<?xml version="1.0" encoding="UTF-8"?>' + chr(10)
    return header + f"<response><{node}{attrs} /></response>"


def _refid_len() -> int:
    """Wire length of refid/dataid, 1..16 (default 16).

    Every VFG cardmng response that carried refid/dataid has killed spice64.exe
    in the ntdll heap manager about a second later, which is what a fixed-size
    AVS field overflowed by a 16-char write then freed by Ea3XrpcDestroy looks
    like. VFG_REFID_LEN shortens the value so that theory can be bisected
    without editing code between test runs.
    """
    try:
        n = int(os.environ.get("VFG_REFID_LEN") or 16)
    except ValueError:
        return 16
    return max(1, min(16, n))


def _new_refid() -> str:
    # Asphyxia/AVS-style refid: letter + 15 hex. Leading digit 0 has crashed
    # some clients when they treat the value as an int.
    return ("A" + uuid.uuid4().hex[:15].upper())[: _refid_len()]


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
    want = _refid_len()
    if old and len(old) != want:
        # VFG_REFID_LEN changed between runs: re-key the stored identity so
        # authpass/bindmodel still resolve the refid the client was handed.
        new = old[:want] if len(old) > want else _new_refid()
        profiles = DB.data.setdefault("profiles", {})
        if old in profiles and new not in profiles:
            profiles[new] = profiles.pop(old)
            profiles[new]["refid"] = new
        rec["refid"] = new
        return new
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


def _cardmng_found_xml(
    refid: str, *, binded: bool, newflag: bool, lastupdate: Optional[int] = None
) -> str:
    # Successful cardmng responses intentionally omit status. This matches AVS
    # cardmng implementations such as bemaniutils; status is used for errors
    # and authpass results. VFG can crash when a garbled card session receives
    # a refid at all, so this helper is only used for canonical 16-hex cards.
    attrs = (
        f' binded="{1 if binded else 0}" dataid="{refid}" refid="{refid}"'
        f' newflag="{1 if newflag else 0}" expired="0" exflag="0" ecflag="1"'
    )
    # Managed KAMUNITY unconditionally reads @lastupdate when binded=1. Keep
    # the attribute off unbound responses, where older live captures showed
    # that extra fields can leave the native XRPC request unfinished.
    if binded:
        attrs += f' lastupdate="{int(lastupdate or now_unix())}"'
    return _cardmng_xml(attrs)


def _cardid_is_canonical(raw: str) -> bool:
    return bool(_CARDID_RE.fullmatch((raw or "").strip().replace(" ", "")))


def _cardmng_mode() -> str:
    """compat (default) or strict; see handle_cardmng()."""
    return (os.environ.get("VFG_CARDMNG_MODE") or "compat").strip().lower()


def _cardmng_inquire_mode() -> str:
    """auto (default) reports the stored card state; new always answers 112.

    ``new`` reproduces the only inquire shape that has never crashed this dump,
    and is the fallback if resuming a registered card kills spice64.exe again.
    """
    return (os.environ.get("VFG_CARDMNG_INQUIRE_MODE") or "auto").strip().lower()


def handle_cardmng(method: str, call: ET.Element, _model: str) -> str:
    # Unregistered cards return status=112. Spice can produce a garbled string
    # attribute for an 8-byte IDm; keep the local fallback identity but never
    # resume that untrusted request as an already-bound card.
    node = None
    for _name in (CARDMNG_SHADOW_MODULE, "cardmng"):
        found = call.find(f".//{_name}")
        if found is not None:
            node = found
            break
    if node is None:
        node = call
    raw_cardid = node.attrib.get("cardid") or node.attrib.get("card_id") or ""
    req_refid = (node.attrib.get("refid") or "").strip().upper()

    # A decode failure is dispatched with an empty <call/> because the method
    # is still known from the query string. Never let that empty request fall
    # through normalize_cardid() and mutate the configured fallback card.
    if not raw_cardid and method in ("inquire", "getrefid"):
        log.warning("[cardmng] %s rejected: missing cardid", method)
        return _cardmng_xml(' status="112"' if method == "inquire" else ' status="110"')
    if method in ("bindmodel", "bindcard") and not req_refid:
        log.warning("[cardmng] %s rejected: missing refid", method)
        return _cardmng_xml(' status="110"')

    canonical_cardid = _cardid_is_canonical(raw_cardid)

    # VFG 2025122300 never puts the real IDm on the wire. Managed code reads a
    # clean 16-hex number from Bi2xInputVFG.GetCardID(), but the native AVS
    # cardmng module rebuilds the request from its own state, so every captured
    # packet carries a pointer-sized cardtype plus a 3-4 byte cardid/model and
    # a passwd that is really the first bytes of the request buffer ("<car").
    # Rejecting that request only guarantees ENTRY_NETWORK_ERROR on the PIN
    # screen, so map it onto the single configured local identity instead and
    # answer with the exact bemaniutils response shape.
    #
    # VFG_CARDMNG_MODE=strict restores the old quarantine (112 / 110).
    if not canonical_cardid and method in ("inquire", "getrefid"):
        if _cardmng_mode() == "strict":
            status = "112" if method == "inquire" else "110"
            log.warning(
                "[cardmng] %s quarantined (strict): malformed cardid=%r", method, raw_cardid
            )
            return _cardmng_xml(f' status="{status}"')
        log.warning(
            "[cardmng] %s recovering malformed cardid=%r -> %s",
            method, raw_cardid, DEFAULT_CARDID,
        )

    cardid = normalize_cardid(raw_cardid)
    with _store_lock:
        cards = DB.data.setdefault("cards", {})
        rec_by_card = cards.get(cardid)
        rec_by_refid = (
            next((c for c in cards.values() if (c.get("refid") or "").upper() == req_refid), None)
            if req_refid
            else None
        )
        rec = rec_by_refid if req_refid else rec_by_card

        log.info(
            "[cardmng] %s cardid=%s raw=%r canonical=%s issued=%s bound=%s refid=%s",
            method, cardid, raw_cardid, canonical_cardid,
            bool(rec and rec.get("issued")), bool(rec and rec.get("bound")),
            (rec or {}).get("refid"),
        )

        if method == "inquire":
            if _cardmng_inquire_mode() == "new":
                log.info("[cardmng] inquire forced to CARD_NEW (VFG_CARDMNG_INQUIRE_MODE=new)")
                return _cardmng_xml(' status="112"')
            if rec_by_card is None or not rec_by_card.get("issued"):
                return _cardmng_xml(' status="112"')
            refid = _ensure_refid_format(rec_by_card)
            bound = bool(rec_by_card.get("bound"))
            lastupdate = int(
                rec_by_card.get("updated_at")
                or rec_by_card.get("created_at")
                or now_unix()
            )
            return _cardmng_found_xml(
                refid, binded=bound, newflag=not bound, lastupdate=lastupdate
            )

        if method == "getrefid":
            rec = DB.card_profile(cardid)
            rec["issued"] = True
            rec["bound"] = False
            rec["pin"] = _sanitize_pin(node.attrib.get("passwd") or "", rec.get("pin") or "0000")
            refid = _ensure_refid_format(rec)
            DB.persist()
            # Minimal AVS success shape. pcode and explicit status=0 both caused
            # the captured VFG PIN/XRPC path to stall.
            return _cardmng_xml(f' refid="{refid}" dataid="{refid}"')

        if method == "authpass":
            # authpass explicitly uses status to report success/invalid PIN.
            # Keep it permissive because the Spice IDm bug can shift passwd.
            return _cardmng_xml(' status="0"')

        if method in ("bindmodel", "bindcard"):
            if rec is None:
                return _cardmng_xml(' status="110"')
            rec["issued"] = True
            rec["bound"] = True
            refid = _ensure_refid_format(rec)
            DB.persist()
            return _cardmng_xml(f' dataid="{refid}"')

        if method == "getdatalist":
            return _cardmng_xml("")

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


def handle_vfglog(method: str, call: ET.Element, _model: str) -> str:
    # vfglog.put_msg is the client's own log channel, and the only one we get:
    # this build has Unity's player log disabled, and KAMUNITY.Logger's
    # AvsLogMisc("UNITY", ...) never reaches spice's log.txt either.
    #
    # RequestApiBase.PushRequest reserves ("network_error", "<JST>,<RequestType>,
    # <StatusCode>,<DataID>,<serverLog>,<errorMessage>") for every request that
    # throws, and GameUtility.ServerLog.SendReservedLog flushes them here.  So a
    # `network_error` line names the exact MFG.GameRequest class whose OnParse
    # our response broke - which beats guessing from where log.txt stops.
    if method == "put_msg":
        for msg in call.iter("msg"):
            label = msg.attrib.get("label") or "?"
            value = (msg.text or "").strip()
            if label == "network_error":
                log.error("[client] network_error: %s", value)
            else:
                log.info("[client] %s: %s", label, value[:500])
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
    if module in ("cardmng", CARDMNG_SHADOW_MODULE):
        _cardmng_wire_node.name = module
        try:
            return handle_cardmng(method, root, model)
        finally:
            _cardmng_wire_node.name = None
    if module == "vfgac":
        return handle_vfgac(method, root, model)
    if module == "vfglog":
        return handle_vfglog(method, root, model)
    if module == "eacoin":
        return handle_eacoin(method, root, model)
    if module in ("posevent", "pkglist", "userdata", "userid", "sidmgr", "netlog"):
        return eamuse_wrap(module, "", "0")

    log.warning("[XRPC] unhandled %s — empty success", key)
    return eamuse_wrap(module or "eamuse", "", "0")


# ---------------------------------------------------------------------------
# HTTP game API (AOG)
# ---------------------------------------------------------------------------

# gmode -> TakuType, from MFG.Taikyoku.Command.GAME_MODE crossed with
# MFG.Types.MahjongTypesUtility._gameModes.  taku: 0 tonpu, 1 hanchan,
# 2 sanma, 3 nima.  5..23 are the event tables.
GMODE_TAKU = {
    1: 0, 2: 1, 3: 2, 4: 3,
    5: 0, 6: 0, 7: 2, 8: 0, 9: 0, 10: 2, 11: 0, 12: 2, 13: 0, 14: 2,
    15: 0, 16: 0, 17: 2, 18: 2, 19: 2, 20: 0, 21: 2, 22: 0, 23: 2,
}
GMODE_SEATS = {g: mahjong.SEATS_OF[t] for g, t in GMODE_TAKU.items()}
GMODE_TENBO = {g: mahjong.START_SCORE[t] for g, t in GMODE_TAKU.items()}

# Every mode the client knows about is offered; the event ones only light up
# when their GameEventType flag is active in /appli_info.
GAME_MODES = sorted(GMODE_TAKU.keys())

MATCHES: Dict[str, Dict[str, Any]] = {}
TABLES: Dict[str, taikyoku.Table] = {}
STAMPS: Dict[int, List[Dict[str, Any]]] = {}
_stamp_seq = 0


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


def parse_must(form: Dict[str, str]) -> list:
    return (form_get(form, "must") or "").split("/")


def must_int(parts, index: int, default: int = 0) -> int:
    try:
        return int(parts[index]) if len(parts) > index and parts[index] != "" else default
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# in-match sticker chat
# ---------------------------------------------------------------------------


def stamp_post(tid: int, mid: int, pindex: int, name: str, contents: str, param: str) -> None:
    global _stamp_seq
    if not contents:
        return
    with _stamp_lock:
        _stamp_seq += 1
        room = STAMPS.setdefault(int(tid), [])
        room.append(
            {
                "idx": _stamp_seq,
                "mid": int(mid or 0),
                "pindex": int(pindex or 0),
                "time": int(time.time() * 1000),
                "name": name or "",
                "contents": contents,
                "param": param or "",
            }
        )
        del room[:-40]


def stamp_xml(tag: str, tid: int, since: int = 0) -> str:
    with _stamp_lock:
        room = list(STAMPS.get(int(tid), []))
    rows = [e for e in room if e["idx"] > since]
    if not rows:
        return f"<{tag}></{tag}>"
    body = "".join(
        '<d idx="{idx}" mid="{mid}" pindex="{pindex}" time="{time}">'
        "<name>{name}</name><contents>{contents}</contents><param>{param}</param>"
        "</d>".format(
            idx=e["idx"],
            mid=e["mid"],
            pindex=e["pindex"],
            time=e["time"],
            name=xml_escape(e["name"]),
            contents=xml_escape(e["contents"]),
            param=xml_escape(e["param"]),
        )
        for e in rows
    )
    return f"<{tag}>{body}</{tag}>"


# ---------------------------------------------------------------------------
# match / table plumbing
# ---------------------------------------------------------------------------


def mgresult_xml(table: Optional[taikyoku.Table], match: Optional[Dict[str, Any]]) -> str:
    gmode = int((match or {}).get("gmode") or 1)
    parts = [
        f"<gmode>{gmode}</gmode>",
        "<taku_class>1</taku_class>",
        "<continue_state>0</continue_state>",
        "<continue_fee>0</continue_fee>",
    ]
    if table is not None:
        rows = table.result_rows()
    else:
        seats = GMODE_SEATS.get(gmode, 4)
        score = GMODE_TENBO.get(gmode, 25000)
        rows = [(i, score, 0) for i in range(seats)]
    for i, (rank, score, uma) in enumerate(rows):
        parts.append(
            f"<player_{i}><rank>{rank}</rank><score>{score}</score><uma>{uma}</uma></player_{i}>"
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


def ensure_table(pcuid: str, match: Dict[str, Any]) -> taikyoku.Table:
    table = TABLES.get(pcuid)
    if table is None:
        taku = GMODE_TAKU.get(int(match.get("gmode") or 1), 0)
        table = taikyoku.Table(taku, human_seat=0)
        table.start_kyoku()
        TABLES[pcuid] = table
        log.info("table created gmode=%s taku=%s seats=%s",
                 match.get("gmode"), taku, table.seats)
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
        "<message>0</message>"
        "</boot_mes>"
    )


# ---------------------------------------------------------------------------
# /appli_info - GameEventType feature flags
# ---------------------------------------------------------------------------

# Names must match MFG's GameEventType enum exactly; unknown names are ignored
# by the client (GameEventData.OnAfterDeserialize logs and drops them).

# --- how many event tables may be advertised at once ---------------------
#
# HomeEventSelectWindow.ModePanel_Create writes each banner it builds into
# `_modePanelInstanceParents[index]` - a list of parent slots serialised in the
# Home scene, not a growing container.  The normal 対局 window always builds
# exactly three panels (yonma / sanma / nima), so the scene has three slots.
# The event window builds one panel per available event table *per enabled
# taku type*, so advertising every EventTakuMaster CurrentFlagType produced
# twelve panels and the fourth one walked off the end of the list.
#
# That throw happens inside `await m_modeSelectWindow.Open(...)`, called from
# `OnEventTaikyokuButton()` - an `async void` that has already set
# `_taikyokuModeSelectStatus = SelectingEvent`.  Nothing resets that field
# except the window's own close button, which never appears, so after one
# press of イベント対局 the plain 対局 button returns at its first `if` too:
# both buttons go dead for the rest of the credit, with no sound, no window
# and no request reaching us.  Keep the panel count at three or fewer.
EVENT_TAKU_PANEL_SLOTS = 3

# flag -> how many mode panels the client builds from it.  A count of 2 means
# the master entry has EnableTakuType[0] and [1] set, so it contributes both a
# tonpu and a sanma banner.  Only CurrentFlagType values appear here; the
# Constancy* flags below add panels only when no CurrentFlagType is active.
EVENT_TAKU_PANELS = {
    "BlowAwaySanma": 1,        # トイトイのぶっとびギャラクシー
    "FireReach2": 1,           # 気炎万丈 炎のリーチ道場
    "AotenjoEvent2": 1,        # 究極青天井
    "ComebackTakuEvent": 1,    # 六魂清浄 革命のカタルシス
    "KirisameTakuEvent": 1,    # 霧雨魔法店
    "ReversalTakuEvent": 1,    # 恋もツモれば！らぶらぶ突撃麻雀 (sanma only)
    "MeldBonusTakuEvent2": 2,  # オペレーションPCK
    "BombTakuEvent": 2,        # 天才！爆砕！ボンバー卓
    "AllGreenTaku": 2,         # GET READYですヨ！全緑卓
    # Competition6/7/8 are the CurrentFlagType of AccelDora / Mentanpin /
    # FireGalaxy, but BaseEventGameData.IsAvailable() returns false when
    # CurrentFlagType == CompetitionFlagType, and CompetitionTakuAvailables()
    # needs an in-progress competition we never send.  They contribute no
    # panels - they only hide those three tables.
    "Competition6": 0,
    "Competition7": 0,
    "Competition8": 0,
}

# Which event tables to advertise.  VFG_EVENT_TAKU picks a set:
#   min  (default) three single-banner tables - fits the window exactly
#   off            no event tables; イベント対局 stays greyed out
#   all            every flag, twelve panels - reproduces the dead-button bug
EVENT_TAKU_SETS = {
    "off": (),
    "min": ("FireReach2", "ComebackTakuEvent", "KirisameTakuEvent"),
    "all": (
        "BlowAwaySanma", "FireReach2", "Competition7", "Competition8",
        "AotenjoEvent2", "ComebackTakuEvent", "KirisameTakuEvent",
        "MeldBonusTakuEvent2", "Competition6", "ReversalTakuEvent",
        "BombTakuEvent", "AllGreenTaku",
    ),
}


def _event_taku_set_name() -> str:
    name = (os.environ.get("VFG_EVENT_TAKU") or "min").strip().lower()
    return name if name in EVENT_TAKU_SETS else "min"


def _event_taku_flags() -> tuple:
    return EVENT_TAKU_SETS[_event_taku_set_name()]


def log_event_taku_budget() -> None:
    """Called from main(), once logging is configured."""
    asked = (os.environ.get("VFG_EVENT_TAKU") or "min").strip().lower()
    name = _event_taku_set_name()
    if asked and asked != name:
        log.warning("VFG_EVENT_TAKU=%r unknown, using %r", asked, name)
    flags = EVENT_TAKU_SETS[name]
    panels = sum(EVENT_TAKU_PANELS.get(f, 1) for f in flags)
    if panels > EVENT_TAKU_PANEL_SLOTS:
        log.warning(
            "VFG_EVENT_TAKU=%s advertises %d event mode panels but the Home "
            "scene only has %d slots - イベント対局 will throw out of "
            "ModePanel_Create and kill the 対局 button with it",
            name, panels, EVENT_TAKU_PANEL_SLOTS)
    else:
        log.info("event tables (VFG_EVENT_TAKU=%s): %s - %d/%d mode panels",
                 name, ", ".join(flags) or "none", panels,
                 EVENT_TAKU_PANEL_SLOTS)


# Everything that is not an event table.  These drive features, not banners,
# so none of them feed the mode-select window.
BASE_EVENTS: tuple = (
    # spirit gym ("スピリットジム") bonus - GameDataManager.GetSpiritGymBonusRatio
    # reads ParamValue<string>("OID"), so param must stay in key=value form.
    ("SpiritGymBonusEvent", "OID=OID_DOJO_BONUS_3X"),
    # constancy (常設) variants.  They only produce banners when no
    # CurrentFlagType is active, which is why they are safe to leave on.
    ("ConstancyFireReach", ""),
    ("ConstancyFireReachAppearance", ""),
    ("ConstancyAccelDora", ""),
    ("ConstancyAccelDoraSchedule", ""),
    ("ConstancyMentanpin", ""),
    ("ConstancyMentanpinSchedule", ""),
    # sticker chat / decoration
    ("StickerEditNotice", ""),
    ("DecorationSticker", ""),
    # characters + their gacha unlocks so the gacha list is not empty
    ("ChaosUsable", ""),
    ("ClearAppearance", ""),
    ("IyoAppearance", ""),
    ("GrimAroeAppearance", ""),
    ("CocoaAppearance", ""),
    ("DiaAppearance", ""),
    ("DoubrielAppearance", ""),
    ("IppatsuAppearance", ""),
    ("ShiroeAppearance", ""),
    ("ShioriAppearance", ""),
    ("PineAppearance", ""),
    ("ZoudaiAppearance", ""),
    ("CinderellaMitsubaAppearance", ""),
    # QoL / misc
    ("PremiumStartEnable", ""),
    ("RevengeContinueEnable", ""),
    ("EnableOdekake", ""),
    ("ItemGainLogEnable", ""),
    ("PrivateMatchingDisplay", ""),
    ("PrivateMatchingEnable", ""),
    ("FavoBonusEvent", ""),
    ("FanBonusEvent", ""),
    ("ReachSongVoiceGacha", ""),
)

ACTIVE_EVENTS: tuple = BASE_EVENTS + tuple((f, "") for f in _event_taku_flags())

EVENT_BEGIN = "2020/01/01 00:00:00"
EVENT_END = "2099/12/31 23:59:59"


def _events_json() -> str:
    rows = [
        {
            "name": name,
            "active": True,
            "begin": EVENT_BEGIN,
            "end": EVENT_END,
            "param": param,
        }
        for name, param in ACTIVE_EVENTS
    ]
    return json.dumps({"list": rows}, ensure_ascii=False)


def _pro_stats_json() -> str:
    return json.dumps({"now_pro_stats": False, "ProStayDatas": []})


def _info_data(kind: str, payload: str) -> str:
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    return f'<info_data kind="{kind}">{b64}</info_data>'


def handle_appli_info(form: Dict[str, str]) -> str:
    return xml_response(
        "<expire_seconds>3600</expire_seconds>",
        _info_data("events", _events_json()),
        _info_data("pro_stats", _pro_stats_json()),
    )


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
    for gmode in GAME_MODES:
        taku = GMODE_TAKU[gmode]
        parts.append(
            "<mode>"
            f"<gmode>{gmode}</gmode>"
            "<taku_class>1</taku_class>"
            "<payment_mode>0</payment_mode>"
            "<table_type>0</table_type>"
            f"<pmax>{mahjong.SEATS_OF[taku]}</pmax>"
            f"<tenbo>{mahjong.START_SCORE[taku]}</tenbo>"
            "<state>1</state>"
            "<rate>0</rate>"
            "<superior_border>0</superior_border>"
            "</mode>"
        )
    parts.append("</playmode_list>")
    return "".join(parts)


def battle_item_xml() -> str:
    # The client parser requires these two containers whenever the optional
    # battle_item_settings node is present.  The old max_use_num/max_have_num
    # shape dereferenced a missing basic_settings element, so card login
    # completed at the transport layer but ExecLoadAtCardEntry failed while
    # parsing GetMenuData and immediately logged out to the title screen.
    parts = ["<battle_item_settings><basic_settings/><playmode_settings>"]
    for gmode in GAME_MODES:
        parts.append(f'<setting gmode="{gmode}" taku_class="1"/>')
    parts.append("</playmode_settings></battle_item_settings>")
    return "".join(parts)


def handle_get_menudata(form: Dict[str, str]) -> str:
    name = "GUEST"
    mid = 1
    pcuid = form_get(form, "pcuid")
    with _store_lock:
        p = profile_by_session(pcuid)
        if p:
            name = p.get("name") or name
            mid = int(p.get("player_id") or 1)
    return xml_response(
        "<menudata>"
        f"<mpdata><mid>{mid}</mid><name>{xml_escape(name)}</name></mpdata>"
        f"{playmode_xml()}"
        f"{battle_item_xml()}"
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
        if p is None:
            p = profile_by_session(form_get(form, "pcuid"))
        states = (p or {}).get("states") or {}
        items = states.items() if not one else ([(one, states[one])] if one in states else [])
        for kind, payload in items:
            raw = payload.encode("utf-8") if isinstance(payload, str) else payload
            b64 = base64.b64encode(raw if isinstance(raw, bytes) else str(raw).encode("utf-8")).decode("ascii")
            chunks.append(f'<state kind="{xml_escape(kind)}"><data>{b64}</data></state>')
    return xml_response(*chunks)


def handle_client_state_write(form: Dict[str, str]) -> str:
    mid = int(form_get(form, "mid", default="0") or 0)
    kind = form_get(form, "kind")
    data = form_get(form, "data")
    if not mid:
        p = profile_by_session(form_get(form, "pcuid"))
        mid = int((p or {}).get("player_id") or 0)
    if data and mid:
        try:
            decoded = base64.b64decode(unquote_plus(data)).decode("utf-8", errors="replace")
        except Exception:
            decoded = data
        with _store_lock:
            DB.save_state(mid, kind or "unknown", decoded)
    return xml_response()


# ---------------------------------------------------------------------------
# match
# ---------------------------------------------------------------------------


def handle_entry_game(form: Dict[str, str]) -> str:
    """Seat the player at a local CPU table."""
    gmode = int(form_get(form, "gmode", default="1") or 1)
    if gmode not in GMODE_TAKU:
        gmode = 1
    pcuid = form_get(form, "pcuid")
    seats = GMODE_SEATS[gmode]
    with _store_lock:
        profile = profile_by_session(pcuid)
        tid = (MATCHES.get(pcuid, {}).get("tid") or 0) + 1
        MATCHES[pcuid] = {
            "gmode": gmode,
            "seats": seats,
            "tid": tid,
            "pindex": 0,
            "mid": int((profile or {}).get("player_id") or 1),
            "name": (profile or {}).get("name") or "ゲスト",
        }
        TABLES.pop(pcuid, None)
        STAMPS.pop(tid, None)
    url = game_base_url().rstrip("/") + "/"
    return xml_response(
        "<entry>"
        "<gserv_id>1</gserv_id>"
        f"<tid>{tid}</tid>"
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


def _default_match(pcuid: str) -> Dict[str, Any]:
    return MATCHES.setdefault(
        pcuid,
        {"gmode": 1, "seats": 4, "tid": 1, "mid": 1, "name": "ゲスト", "pindex": 0},
    )


def handle_gget(form: Dict[str, str]) -> str:
    """Matching snapshot, then the kyoku stream once the client is ready."""
    pcuid = form_get(form, "pcuid")
    ready = form_get(form, "ready") == "1"
    parts = parse_must(form)
    tid = must_int(parts, 2, 1)
    next_sno = must_int(parts, 5, 0)
    with _store_lock:
        match = _default_match(pcuid)
        profile = profile_by_session(pcuid)
        mwait = matching_xml(match, profile)
        if ready:
            table = ensure_table(pcuid, match)
            table.flush_pending()
            tai = table.cells_from(next_sno)
            all_ready = 1
        else:
            tai = ""
            all_ready = 0
    chat = stamp_xml("chat", tid, must_int(parts, 6, 0))
    return xml_response(
        "<game>"
        f"<all_ready>{all_ready}</all_ready>"
        f"{mwait}"
        f"{tai}"
        "</game>"
        f"{chat}"
    )


def handle_gpost(form: Dict[str, str]) -> str:
    pcuid = form_get(form, "pcuid")
    parts = parse_must(form)
    tid = must_int(parts, 2, 1)
    kind = must_int(parts, 6, 0)
    pindex = must_int(parts, 3, 0)
    pai = must_int(parts, 9, 0)
    tepai_id = must_int(parts, 10, 0)
    tepai_id2 = must_int(parts, 11, 0)
    reach = must_int(parts, 12, 0)
    tsumogiri = must_int(parts, 13, 0)
    with _store_lock:
        match = _default_match(pcuid)
        table = ensure_table(pcuid, match)
        start = len(table.cells)
        table.on_command(kind, pindex, pai, tepai_id, tepai_id2, reach, tsumogiri)
        if kind in (taikyoku.S_NAKINASHI, taikyoku.S_PON, taikyoku.S_CHI,
                    taikyoku.S_MINKAN, taikyoku.S_ANKAN, taikyoku.S_KAKAN,
                    taikyoku.S_NEXT_KYOKU_READY):
            table.flush_pending()
        tai = table.cells_from(start)
    log.info("gpost kind=%s pai=%s reach=%s -> %s cells", kind, pai, reach,
             len(table.cells) - start)
    return xml_response(f"<game>{tai}</game>", stamp_xml("chat", tid, 10 ** 9))


def handle_end_or_kiken(form: Dict[str, str]) -> str:
    pcuid = form_get(form, "pcuid")
    with _store_lock:
        table = TABLES.get(pcuid)
        match = MATCHES.get(pcuid)
        if table is not None:
            table.state = "game_end"
            table.finished = True
        body = mgresult_xml(table, match)
    # The table is kept until the next /entry_game: the client can still poll
    # /gget for a moment after the result screen opens, and dropping it here
    # would deal a brand new hand underneath it.
    return xml_response(body)


def handle_end_show(form: Dict[str, str]) -> str:
    voltage = int(form_get(form, "voltage", default="0") or 0)
    contribute = int(form_get(form, "contribute_percent", default="100") or 100)
    bonus = int(form_get(form, "bonus", default="0") or 0)
    return xml_response(
        "<showresult>"
        f"<voltage>{voltage}</voltage>"
        f"<contribute_percent>{contribute}</contribute_percent>"
        "<card_effect_percent>0</card_effect_percent>"
        "<item_effect_percent>0</item_effect_percent>"
        f"<bonus>{bonus}</bonus>"
        f"<get_point>{max(0, voltage // 10)}</get_point>"
        "</showresult>"
    )


# ---------------------------------------------------------------------------
# sticker chat (matching room + in-match)
# ---------------------------------------------------------------------------


def handle_gchat(form: Dict[str, str]) -> str:
    tid = int(form_get(form, "tid", default="1") or 1)
    mid = int(form_get(form, "mid", default="0") or 0)
    pindex = int(form_get(form, "pindex", default="0") or 0)
    name = form_get(form, "name")
    contents = form_get(form, "contents")
    param = form_get(form, "param")
    if contents:
        stamp_post(tid, mid, pindex, name, contents, param)
        _maybe_cpu_stamp(tid, pindex)
    return xml_response(stamp_xml("chat", tid, 0))


CPU_STAMP_REPLIES = (
    "TableSticker001",
    "TableSticker002",
    "TableSticker003",
    "TableSticker004",
)


def _seats_for_tid(tid: int) -> int:
    for m in MATCHES.values():
        if int(m.get("tid") or 0) == int(tid):
            return int(m.get("seats") or 4)
    return 4


def _maybe_cpu_stamp(tid: int, human_pindex: int) -> None:
    """Give the CPU seats a chance to sticker back so the chat feels alive."""
    if random.random() > 0.55:
        return
    seats = _seats_for_tid(tid)
    if seats < 2:
        return
    seat = (human_pindex + random.randint(1, seats - 1)) % seats
    stamp_post(tid, 0, seat, "CPU", random.choice(CPU_STAMP_REPLIES), "")


def handle_gget_stamp_info(form: Dict[str, str]) -> str:
    parts = parse_must(form)
    tid = must_int(parts, 2, 1)
    pindex = must_int(parts, 3, 0)
    mid = must_int(parts, 4, 0)
    info = (form_get(form, "stamp_info") or "").split(",")
    since = 0
    try:
        since = int(info[1]) if len(info) > 1 and info[1] != "" else 0
    except ValueError:
        since = 0
    if len(info) >= 3 and info[2]:
        stamp_post(tid, mid, pindex, info[3] if len(info) > 3 else "", info[2], "")
        _maybe_cpu_stamp(tid, pindex)
    return xml_response(stamp_xml("stamp_info", tid, since))


# ---------------------------------------------------------------------------
# spirit gym (dojo)
# ---------------------------------------------------------------------------

DOJO_SLOTS = 4
DOJO_STOCK_MAX = 3
DOJO_LESSON_SECONDS = 300


def _dojo_state(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    slots = profile.setdefault("dojo", [])
    while len(slots) < DOJO_SLOTS:
        slots.append({"available": False, "chara": "", "start": 0, "next": 0, "stock": 0})
    return slots


def _dojo_refresh(slot: Dict[str, Any]) -> None:
    """Grow the spirit stock with elapsed real time."""
    if not slot.get("available"):
        return
    now = now_unix()
    nxt = int(slot.get("next") or 0)
    while slot["stock"] < DOJO_STOCK_MAX and nxt and now >= nxt:
        slot["stock"] += 1
        nxt += DOJO_LESSON_SECONDS
    slot["next"] = nxt if slot["stock"] < DOJO_STOCK_MAX else nxt


def _dojo_slot_xml(idx: int, slot: Dict[str, Any]) -> str:
    if not slot.get("available"):
        return f'<slot idx="{idx}"><available>0</available></slot>'
    has_next = 1 if slot["stock"] < DOJO_STOCK_MAX else 0
    return (
        f'<slot idx="{idx}">'
        "<available>1</available>"
        f"<character_obj>{xml_escape(slot.get('chara') or 'OID_CHARACTER_1')}</character_obj>"
        f"<start_time>{int(slot.get('start') or now_unix())}</start_time>"
        f"<next_time>{int(slot.get('next') or now_unix())}</next_time>"
        f"<has_next>{has_next}</has_next>"
        f"<reserve_souls>{int(slot.get('stock') or 0)}</reserve_souls>"
        f"<all_souls>{DOJO_STOCK_MAX}</all_souls>"
        "</slot>"
    )


def _dojo_profile(form: Dict[str, str]) -> Dict[str, Any]:
    p = profile_by_session(form_get(form, "pcuid"))
    if p is None:
        p = DB.ensure_profile("GUEST", "GUEST")
    return p


def handle_dojo_get_status(form: Dict[str, str]) -> str:
    with _store_lock:
        p = _dojo_profile(form)
        slots = _dojo_state(p)
        for s in slots:
            _dojo_refresh(s)
        DB.persist()
        body = "".join(_dojo_slot_xml(i, s) for i, s in enumerate(slots))
    return xml_response(f"<dojo><slot_nr>{DOJO_SLOTS}</slot_nr>{body}</dojo>")


def handle_dojo_set_slot(form: Dict[str, str]) -> str:
    slot_id = int(form_get(form, "slot_id", default="0") or 0)
    chara = form_get(form, "set_character", default="OID_CHARACTER_1")
    with _store_lock:
        p = _dojo_profile(form)
        slots = _dojo_state(p)
        slot_id = max(0, min(slot_id, DOJO_SLOTS - 1))
        slot = slots[slot_id]
        slot.update(
            {
                "available": True,
                "chara": chara,
                "start": now_unix(),
                "next": now_unix() + DOJO_LESSON_SECONDS,
                "stock": 0,
            }
        )
        DB.persist()
        body = _dojo_slot_xml(slot_id, slot)
    return xml_response(
        f"<dojo><slot_id>{slot_id}</slot_id><updated>1</updated>{body}</dojo>"
    )


def handle_dojo_gain_soul(form: Dict[str, str]) -> str:
    slot_id = int(form_get(form, "slot_id", default="0") or 0)
    with _store_lock:
        p = _dojo_profile(form)
        slots = _dojo_state(p)
        slot_id = max(0, min(slot_id, DOJO_SLOTS - 1))
        slot = slots[slot_id]
        _dojo_refresh(slot)
        got = int(slot.get("stock") or 0)
        slot["stock"] = 0
        slot["start"] = now_unix()
        slot["next"] = now_unix() + DOJO_LESSON_SECONDS
        DB.persist()
        body = _dojo_slot_xml(slot_id, slot)
    return xml_response(
        f"<dojo><slot_id>{slot_id}</slot_id><get_nr>{got}</get_nr>{body}</dojo>"
    )


# ---------------------------------------------------------------------------
# gacha
# ---------------------------------------------------------------------------

# SeriesID == MFG.GameData.GachaSeriesName ordinal, and the client drops any
# series whose banner assets are missing (GachaInfoGameData.Validate), so this
# list was generated from the dump's own Addressables catalog:
#   Banner/BannerGacha<NNN><Name>.prefab
#   GachaPlayBG/Textures/GachaTitle<NNN><Name>.png
# Set VFG_GACHA_ALL=1 to advertise every series in the catalog instead of the
# curated set (that costs ~400 extra addressable lookups during boot).
GACHA_SERIES_CURATED = (
    (0, "Normal", 0, "Normal"),
    (1, "NormalTicket", 1, "Normal"),
    (25, "UnlockClear", 0, "Unlock"),
    (44, "UnlockIyo", 0, "Unlock"),
    (56, "UnlockGrimAroe", 0, "Unlock"),
    (63, "UnlockCocoa", 0, "Unlock"),
    (74, "UnlockDia", 0, "Unlock"),
    (101, "UnlockDoubriel", 0, "Unlock"),
    (125, "UnlockIppatsu", 0, "Unlock"),
    (135, "UnlockShiroe", 0, "Unlock"),
    (91, "MusicHiyori", 0, "Music"),
    (92, "MusicSen", 0, "Music"),
    (107, "MusicYao", 0, "Music"),
    (114, "MusicTenshi", 0, "Music"),
    (132, "MusicMusashi", 0, "Music"),
    (133, "LimitedCharaReturns", 0, "Limited"),
    (124, "PickupIppatsu", 0, "Pickup"),
    (128, "PickupMizugiReturns4", 0, "Pickup"),
    (129, "PickupUniformDia", 0, "Pickup"),
    (130, "PickupYukataToytoy2", 0, "Pickup"),
    (131, "PickupUniformDoubriel", 0, "Pickup"),
    (134, "PickupShiroe", 0, "Pickup"),
    (136, "PickupXmasGrimAroe", 0, "Pickup"),
    (137, "PickupMarchingIchiko", 0, "Pickup"),
    (138, "PickupKimonoClear", 0, "Pickup"),
    (139, "PickupBomberPine2", 0, "Pickup"),
    (140, "PickupLillyIppatsu", 0, "Pickup"),
)

GACHA_SERIES_ALL = (
    (0, "Normal", 0, "Normal"),
    (1, "NormalTicket", 1, "Normal"),
    (2, "PickupPine", 0, "Pickup"),
    (3, "PickupMizugiSen", 0, "Pickup"),
    (4, "PickupMizugiMitsuba", 0, "Pickup"),
    (5, "PickupMizugiMusashi", 0, "Pickup"),
    (6, "PickupMizugiHiyori", 0, "Pickup"),
    (7, "PickupMizugiYao", 0, "Pickup"),
    (8, "PickupMizugiPain", 0, "Pickup"),
    (9, "PickupMizugiTenshi", 0, "Pickup"),
    (10, "PickupShiori", 0, "Pickup"),
    (11, "PickupMizugiTumire", 0, "Pickup"),
    (12, "PickupMizugiToitoi", 0, "Pickup"),
    (13, "PickupHalfAnniversary", 0, "Pickup"),
    (14, "PickupMizugiIchiko", 0, "Pickup"),
    (15, "PickupMizugiTeiyaku", 0, "Pickup"),
    (16, "PickupHalloweenMusashi", 0, "Pickup"),
    (17, "PickupMizugiNijo", 0, "Pickup"),
    (18, "PickupChaos", 0, "Pickup"),
    (19, "PickupCinderellaMitsuba", 0, "Pickup"),
    (20, "PickupXmasTenshi", 0, "Pickup"),
    (21, "PickupNewYearTsumire", 0, "Pickup"),
    (22, "PickupNewYearYao", 0, "Pickup"),
    (23, "PickupValentineToytoy", 0, "Pickup"),
    (24, "PickupClear", 0, "Pickup"),
    (25, "UnlockClear", 0, "Unlock"),
    (26, "PickupSuccubusTenshi", 0, "Pickup"),
    (27, "PickupParodiusTsumire", 0, "Pickup"),
    (28, "PickupRaceQueenHiyori", 0, "Pickup"),
    (29, "PickupShiori2", 0, "Pickup"),
    (30, "PickupMizugiChaos", 0, "Pickup"),
    (31, "PickupHalfAnniversary2", 0, "Pickup"),
    (32, "PickupMizugiClear", 0, "Pickup"),
    (33, "PickupAliceMaidPine", 0, "Pickup"),
    (34, "PickupMizugiReturns1", 0, "Pickup"),
    (35, "PickupJerseyMitsuba", 0, "Pickup"),
    (36, "PickupUniformMusashi", 0, "Pickup"),
    (37, "PickupUniformSen", 0, "Pickup"),
    (38, "PickupChaos2", 0, "Pickup"),
    (39, "PickupUniformYao", 0, "Pickup"),
    (40, "PickupYukataToytoy", 0, "Pickup"),
    (41, "PickupClear2", 0, "Pickup"),
    (42, "PickupShiori3", 0, "Pickup"),
    (43, "PickupIyo", 0, "Pickup"),
    (44, "UnlockIyo", 0, "Unlock"),
    (45, "PickupTohoHiyori", 0, "Pickup"),
    (46, "PickupTohoTenshi", 0, "Pickup"),
    (47, "PickupTohoClear", 0, "Pickup"),
    (48, "PickupMizugiKomugi", 0, "Pickup"),
    (49, "PickupMizugiReturns2", 0, "Pickup"),
    (50, "PickupDateMizugiSen", 0, "Pickup"),
    (51, "PickupSisterTsumire", 0, "Pickup"),
    (52, "PickupUniformSen2", 0, "Pickup"),
    (53, "PickupHalloweenMusashi2", 0, "Pickup"),
    (54, "PickupBomberPine", 0, "Pickup"),
    (55, "PickupGrimAroe", 0, "Pickup"),
    (56, "UnlockGrimAroe", 0, "Unlock"),
    (57, "PickupChaos3", 0, "Pickup"),
    (58, "PickupClear3", 0, "Pickup"),
    (59, "PickupIyo2", 0, "Pickup"),
    (60, "PickupDarknessRobeMitsuba", 0, "Pickup"),
    (61, "PickupNurseChaos", 0, "Pickup"),
    (62, "PickupCocoa", 0, "Pickup"),
    (63, "UnlockCocoa", 0, "Unlock"),
    (64, "PickupClear4", 0, "Pickup"),
    (65, "PickupGrimAroe2", 0, "Pickup"),
    (66, "PickupXmasTenshi2", 0, "Pickup"),
    (67, "PickupJiangshiIyo", 0, "Pickup"),
    (68, "PickupNewYearReturns1", 0, "Pickup"),
    (69, "PickupFundoshiMusashi", 0, "Pickup"),
    (70, "PickupValentineToytoy2", 0, "Pickup"),
    (71, "PickupBellyDanceYao", 0, "Pickup"),
    (72, "PickupAngelTenshi", 0, "Pickup"),
    (73, "PickupDia", 0, "Pickup"),
    (74, "UnlockDia", 0, "Unlock"),
    (75, "PickupIyo3", 0, "Pickup"),
    (76, "PickupGrimAroe3", 0, "Pickup"),
    (77, "PickupCocoa2", 0, "Pickup"),
    (78, "PickupLolitaToytoy", 0, "Pickup"),
    (79, "PickupGalHiyori", 0, "Pickup"),
    (80, "PickupGalGrimAroe", 0, "Pickup"),
    (81, "PickupGalClear", 0, "Pickup"),
    (82, "PickupParodiusTsumire2", 0, "Pickup"),
    (83, "PickupAnimalMizugiCocoa", 0, "Pickup"),
    (84, "PickupMizugiReturns3", 0, "Pickup"),
    (85, "PickupParkaMusashi", 0, "Pickup"),
    (86, "PickupAnimalMizugiDia", 0, "Pickup"),
    (87, "PickupSuccubusTenshi2", 0, "Pickup"),
    (88, "PickupDressChaos", 0, "Pickup"),
    (89, "PickupRaceQueenHiyori2", 0, "Pickup"),
    (90, "PickupMaidTsumire", 0, "Pickup"),
    (91, "MusicHiyori", 0, "Music"),
    (92, "MusicSen", 0, "Music"),
    (93, "PickupShiori4", 0, "Pickup"),
    (94, "PickupChaos4", 0, "Pickup"),
    (95, "PickupClear5", 0, "Pickup"),
    (96, "PickupIyo4", 0, "Pickup"),
    (97, "PickupGrimAroe4", 0, "Pickup"),
    (98, "PickupCocoa3", 0, "Pickup"),
    (99, "PickupDia2", 0, "Pickup"),
    (100, "PickupDoubriel", 0, "Pickup"),
    (101, "UnlockDoubriel", 0, "Unlock"),
    (102, "PickupPartTimerMitsuba", 0, "Pickup"),
    (103, "PickupUniformYao2", 0, "Pickup"),
    (104, "PickupCinderellaMitsuba2", 0, "Pickup"),
    (105, "PickupAliceMaidPine2", 0, "Pickup"),
    (106, "PickupSchoolMizugiSen", 0, "Pickup"),
    (107, "MusicYao", 0, "Music"),
    (108, "PickupUniformMusashi2", 0, "Pickup"),
    (109, "PickupSchoolMizugiIyo", 0, "Pickup"),
    (110, "PickupSchoolMizugiToytoy", 0, "Pickup"),
    (111, "PickupJerseyMitsuba2", 0, "Pickup"),
    (112, "PickupUniformSen3", 0, "Pickup"),
    (113, "PickupBellyDancePine", 0, "Pickup"),
    (114, "MusicTenshi", 0, "Music"),
    (115, "PickupYukataYao", 0, "Pickup"),
    (123, "PickupDoubriel2", 0, "Pickup"),
    (124, "PickupIppatsu", 0, "Pickup"),
    (125, "UnlockIppatsu", 0, "Unlock"),
    (126, "PickupUniformTenshi", 0, "Pickup"),
    (127, "PickupMaidToytoyIyo", 0, "Pickup"),
    (128, "PickupMizugiReturns4", 0, "Pickup"),
    (129, "PickupUniformDia", 0, "Pickup"),
    (130, "PickupYukataToytoy2", 0, "Pickup"),
    (131, "PickupUniformDoubriel", 0, "Pickup"),
    (132, "MusicMusashi", 0, "Music"),
    (133, "LimitedCharaReturns", 0, "Limited"),
    (134, "PickupShiroe", 0, "Pickup"),
    (135, "UnlockShiroe", 0, "Unlock"),
    (136, "PickupXmasGrimAroe", 0, "Pickup"),
    (137, "PickupMarchingIchiko", 0, "Pickup"),
    (138, "PickupKimonoClear", 0, "Pickup"),
    (139, "PickupBomberPine2", 0, "Pickup"),
    (140, "PickupLillyIppatsu", 0, "Pickup"),
)


def gacha_series():
    if os.environ.get("VFG_GACHA_ALL", "").strip().lower() in ("1", "true", "yes", "on"):
        return GACHA_SERIES_ALL
    return GACHA_SERIES_CURATED


# ---------------------------------------------------------------------------
# gacha pools
#
# A banner may not go out with an empty <items>/<pickup_charas>: the client does
# the drawing and the banner presentation itself, and both blow up on an empty
# pool.
#
#   * GachaSelectBgMovie.Play() reads movies[0] from GetGachaSelectMovies(). For
#     a Pickup series whose PickupKind is not NewGirl that list starts with the
#     literal "CutinPlay" and then gets one entry per pickup character, so with
#     no pickup characters it is exactly ["CutinPlay"]. Play() then calls
#     PlayCutIn(GetSpecialPickupItem()) - also empty - which falls straight
#     through to BgMovie_Change(), which wraps the index back to 0, sees
#     "CutinPlay" again and calls PlayCutIn again. Nothing on that path awaits,
#     so the two recurse until the stack dies: the game vanishes the moment you
#     pick a gacha type, with no managed exception anywhere.
#   * GachaResultInfo.SetItemInfos draws through GenerateGachaItemID, which
#     indexes the except-pickup pool by the rolled rarity. Every banner needs
#     N/R/SR/UR outside its own pickup set or the first unlucky roll throws
#     KeyNotFoundException (UR alone is 2%).
#
# data/gacha_pools.json carries the pools, the per-banner pickup characters and
# the reach-song lists, all lifted from the dump itself - see
# tools/extract_gacha_pools.py.
GACHA_POOLS_PATH = DATA_DIR / "gacha_pools.json"


def _load_gacha_pools() -> Dict[str, Any]:
    try:
        with GACHA_POOLS_PATH.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        log.error("[gacha] cannot read %s (%s); banners will be empty", GACHA_POOLS_PATH, exc)
        return {"standard_pool": [], "series": {}}


GACHA_POOLS = _load_gacha_pools()
GACHA_STANDARD_POOL: List[str] = GACHA_POOLS.get("standard_pool", [])
GACHA_SERIES_POOLS: Dict[str, Dict[str, Any]] = GACHA_POOLS.get("series", {})
# Every character has at least one N/R/SR item, so this is a pickup character
# that can never leave GetSpecialPickupItem() empty.
GACHA_FALLBACK_CHARA = "Chara01"


def _gacha_pool(sid: int, stype: str) -> Dict[str, List[str]]:
    """items / pickup_charas / custom_pickup_items for one series."""
    entry = GACHA_SERIES_POOLS.get(str(sid), {})
    charas = list(entry.get("pickup_charas", []))
    custom = list(entry.get("custom_pickup_items", []))

    if stype == "Music":
        return {"items": list(entry.get("music_items", [])), "charas": charas, "custom": []}

    # Backstop for a series the pool file does not cover (a hand-edited series
    # list, or VFG_GACHA_ALL running ahead of a regenerated pool file): without
    # a pickup character or a pickup item, a Pickup banner is the CutinPlay
    # recursion above. Limited banners are deliberately left alone - the client
    # previews them from its own hard-coded LimitedCharaReturnsMovieItemIDs, and
    # its lottery never draws a Limited pickup set, so marking one only makes
    # those items undrawable.
    if stype == "Pickup" and not charas and not custom:
        charas = [GACHA_FALLBACK_CHARA]

    # The standard pool already holds every character's cutins; extra_items adds
    # the character-unlock tickets, which only Unlock and Limited banners drop.
    items = list(GACHA_STANDARD_POOL) + [
        oid for oid in entry.get("extra_items", []) if oid not in GACHA_STANDARD_POOL
    ]
    return {"items": items, "charas": charas, "custom": custom}


def _build_gacha_info_xml() -> str:
    rows = []
    for sid, label, ticket, stype in gacha_series():
        pool = _gacha_pool(sid, stype)
        items = "".join(f"<item>{xml_escape(oid)}</item>" for oid in pool["items"])
        charas = "".join(f"<chara>{xml_escape(c)}</chara>" for c in pool["charas"])
        custom = "".join(f"<item>{xml_escape(oid)}</item>" for oid in pool["custom"])
        rows.append(
            "<info>"
            f"<id>{sid}</id>"
            f"<label>{xml_escape(label)}</label>"
            f"<ticket_nr>{ticket}</ticket_nr>"
            "<now_active>1</now_active>"
            f"<series_type>{stype}</series_type>"
            f"<items>{items}</items>"
            f"<pickup_charas>{charas}</pickup_charas>"
            f"<custom_pickup_items>{custom}</custom_pickup_items>"
            "<exchange_items></exchange_items>"
            f"<start_date>{EVENT_BEGIN}</start_date>"
            f"<end_date>{EVENT_END}</end_date>"
            "</info>"
        )
    return "<gacha_schedule>" + "".join(rows) + "</gacha_schedule>"


# ~200 KiB of item lists that never change while the server is up, and the
# client asks for them on every boot and every trip into the gacha scene.
_gacha_info_cache: Dict[bool, str] = {}


def _gacha_info_xml() -> str:
    key = gacha_series() is GACHA_SERIES_ALL
    xml = _gacha_info_cache.get(key)
    if xml is None:
        xml = _build_gacha_info_xml()
        _gacha_info_cache[key] = xml
    return xml


def handle_gacha_info(_form: Dict[str, str]) -> str:
    return xml_response(_gacha_info_xml())


def handle_req_draw_gacha(form: Dict[str, str]) -> str:
    txn = uuid.uuid4().hex[:16]
    with _store_lock:
        p = profile_by_session(form_get(form, "pcuid"))
        if p is not None:
            p["gacha_txn"] = {
                "id": txn,
                "gacha": form_get(form, "gacha_name"),
                "times": int(form_get(form, "times", default="1") or 1),
            }
            DB.persist()
    return xml_response(
        f"<transaction_info><transaction_id>{txn}</transaction_id></transaction_info>"
    )


def handle_get_gacha_result(form: Dict[str, str]) -> str:
    times = int(form_get(form, "times", default="1") or 1)
    rows = "".join(
        "<data><character_id>0</character_id>"
        f"<unique_id>{uuid.uuid4().hex[:12]}</unique_id></data>"
        for _ in range(max(1, times))
    )
    return xml_response(
        f"<lottery_result>{rows}</lottery_result>"
        "<gift><acquired>0</acquired><prev>0</prev><after>0</after></gift>"
    )


def handle_gacha_log(form: Dict[str, str]) -> str:
    raw = form_get(form, "log")
    if raw:
        try:
            log.info("[gacha] %s", base64.b64decode(unquote_plus(raw)).decode("utf-8"))
        except Exception:
            pass
    return xml_response()


# Reach-song ("リーチソング＆ボイスガチャ") pools: only five characters have gacha
# songs. These come from data/gacha_pools.json, which walks the series name back
# to a CharaType and then reads that character's songs out of
# ItemIDExtentions.s_gachaNoDic - MusicYao is Chara05 and MusicTenshi is Chara04,
# which the hand-written table this replaced had the wrong way round.
MUSIC_GACHA_POOL: Dict[int, tuple] = {
    int(sid): tuple(entry["music_items"])
    for sid, entry in GACHA_SERIES_POOLS.items()
    if entry.get("type") == "Music" and entry.get("music_items")
}
MUSIC_GACHA_RESERVES: Dict[int, int] = {}
_music_req_seq = 1000


def handle_music_gacha_play_reserve(form: Dict[str, str]) -> str:
    """Hand out a request id bound to the reach-song series being played."""
    global _music_req_seq
    series = int(form_get(form, "gacha_id", default="0") or 0)
    with _store_lock:
        _music_req_seq += 1
        req = _music_req_seq
        MUSIC_GACHA_RESERVES[req] = series
    return xml_response(
        "<gacha_reserve>"
        "<is_success>1</is_success>"
        f"<request_id>{req}</request_id>"
        "</gacha_reserve>"
    )


def handle_music_gacha_play(form: Dict[str, str]) -> str:
    try:
        req = int(form_get(form, "request_id", default="0") or 0)
    except ValueError:
        req = 0
    with _store_lock:
        series = MUSIC_GACHA_RESERVES.pop(req, 0)
    pool = MUSIC_GACHA_POOL.get(series)
    if not pool:
        # unknown series: fall back to any pool so the client never sees an
        # empty gain_items list (MusicGachaPlay reads GainItems[0]).
        pool = MUSIC_GACHA_POOL.get(91) or ("OID_ReachBgm148",)
    oid = random.choice(pool)
    log.info("[music-gacha] series=%s request=%s -> %s", series, req, oid)
    return xml_response(
        "<gacha_result>"
        "<is_success>1</is_success>"
        f"<gain_items><item>{xml_escape(oid)}</item></gain_items>"
        "<gift>2</gift>"
        "<fight_spirits></fight_spirits>"
        "</gacha_result>"
    )


def handle_get_jongstone_info(_form: Dict[str, str]) -> str:
    return xml_response(
        "<jongstone_info><free_point>0</free_point><record_point>0</record_point></jongstone_info>"
    )


def handle_get_mg(_form: Dict[str, str]) -> str:
    return xml_response("<mg_info><mg>0</mg><additional_mg>0</additional_mg></mg_info>")


def handle_mission_date(_form: Dict[str, str]) -> str:
    payload = json.dumps({"list": []}, ensure_ascii=False)
    return xml_response(_info_data("missions", payload))


def handle_player_record(_form: Dict[str, str]) -> str:
    return xml_response("<player_record></player_record>")


def handle_get_haifu_list(_form: Dict[str, str]) -> str:
    return xml_response("<haifu_list></haifu_list>")


def handle_present_done(form: Dict[str, str]) -> str:
    ids = [i for i in (form_get(form, "done_ids") or "").split(",") if i.strip()]
    rows = "".join(
        f"<data><id>{xml_escape(i)}</id><success>1</success>"
        "<content></content><amount>0</amount></data>"
        for i in ids
    )
    return xml_response(f"<present>{rows}</present>")


def handle_competition_entry(_form: Dict[str, str]) -> str:
    return xml_response("<competition><entry_result>1</entry_result></competition>")


def handle_log_only(form: Dict[str, str]) -> str:
    raw = form_get(form, "log")
    if raw:
        try:
            log.info("[itemlog] %s", base64.b64decode(unquote_plus(raw)).decode("utf-8"))
        except Exception:
            pass
    return xml_response()


TABOO_WORDS: tuple = ()


def _is_taboo_word(word: str) -> bool:
    """Local-only NG filter. Empty by default; add entries to TABOO_WORDS."""
    s = (word or "").strip().casefold()
    return any(bad in s for bad in TABOO_WORDS)


def handle_chk_tabooword(form: Dict[str, str]) -> str:
    # MFG.GameRequest.ChkTabooword.OnParse reads <taboo_chk><result>, and leaves
    # ResultData.IsAvailable at its default false when that node is missing, so a
    # bare <result> made every nickname report as a banned word. result=0 means
    # the string is allowed (Extensions.ValueBool -> int.Parse(value) != 0).
    word = form_get(form, "str")
    banned = 1 if _is_taboo_word(word) else 0
    if banned:
        log.info("[AOG] chk_tabooword rejected %r", word)
    return xml_response(f"<taboo_chk><result>{banned}</result></taboo_chk>")


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
    "end_show": handle_end_show,
    "reconnect": handle_entry_game,
    "chk_tabooword": handle_chk_tabooword,
    # spirit gym
    "dojo_get_status": handle_dojo_get_status,
    "dojo_set_slot": handle_dojo_set_slot,
    "dojo_gain_soul": handle_dojo_gain_soul,
    # gacha
    "gacha_info": handle_gacha_info,
    "gacha_log": handle_gacha_log,
    "req_draw_gacha": handle_req_draw_gacha,
    "get_gacha_result": handle_get_gacha_result,
    "music_gacha_play": handle_music_gacha_play,
    "music_gacha_play_reserve": handle_music_gacha_play_reserve,
    # sticker chat
    "gchat": handle_gchat,
    "gget_stamp_info": handle_gget_stamp_info,
    # profile / misc
    "player_record": handle_player_record,
    "get_record": handle_player_record,
    "get_haifu_list": handle_get_haifu_list,
    "get_haifu_data": lambda f: xml_response(),
    "get_jongstone_info": handle_get_jongstone_info,
    "get_mg": handle_get_mg,
    "mission_date": handle_mission_date,
    "present_done": handle_present_done,
    "competition_entry": handle_competition_entry,
    "item_gain_log": handle_log_only,
    "item_consume_log": handle_log_only,
    "notice_done": lambda f: xml_response(),
    "important_notice_done": lambda f: xml_response(),
    "set_favorite_character": lambda f: xml_response(),
    "odekake_done": lambda f: xml_response(),
    "coop_done": lambda f: xml_response(),
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

        xml_text = ""
        fparam = (qs.get("f") or [""])[0]
        if fparam and "." in fparam and not module:
            module, method = fparam.split(".", 1)
        if not model:
            model = (qs.get("model") or [""])[0]

        decode_meta = EamuseDecodeMeta(used_kbin=True)
        try:
            xml_text, decode_meta = decode_eamuse_body(body, info, compress)
            if module == "cardmng":
                # Local diagnostics only (captures/ is gitignored). Preserve the
                # exact decrypted KBin and transport metadata for independent
                # protocol validation without needing another packet brute-force.
                diag_seq = next_seq()
                diag_dir = RUN_DIR / "transport"
                diag_dir.mkdir(parents=True, exist_ok=True)
                (diag_dir / f"{diag_seq:04d}_{module}_{method}.bin").write_bytes(
                    decode_meta.decoded_body
                )
                (diag_dir / f"{diag_seq:04d}_{module}_{method}.json").write_text(
                    json.dumps(
                        {
                            "x_eamuse_info": info,
                            "x_compress": compress,
                            "used_kbin": decode_meta.used_kbin,
                            "kbin_encoding": decode_meta.kbin_encoding,
                            "kbin_compressed": decode_meta.kbin_compressed,
                            "wire_bytes": len(body),
                            "decoded_bytes": len(decode_meta.decoded_body),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                log.info(
                    "[XRPC] card transport encoding=%s internal_compressed=%s x_compress=%s",
                    decode_meta.kbin_encoding,
                    decode_meta.kbin_compressed,
                    compress,
                )
        except Exception:
            log.exception("failed to decode e-amuse body info=%s compress=%s bytes=%s", info, compress, len(body))
            dump = RUN_DIR / "requests" / f"raw_{next_seq():04d}_{module}_{method}.bin"
            try:
                dump.write_bytes(body)
                log.warning("raw e-amuse body saved to %s (%s bytes)", dump, len(body))
            except Exception:
                pass
            # Never synthesize a real card on a transport decode failure.
            # Doing so can issue/bind a real profile from an undecodable packet.
            xml_text = "<call/>"
            decode_meta = EamuseDecodeMeta(used_kbin=True)

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
            payload = encode_eamuse_body(resp_xml, info, compress, decode_meta)
        except Exception:
            log.exception("failed to encode kbin, falling back to XML")
            payload = encode_eamuse_body(
                resp_xml, info, compress, EamuseDecodeMeta(used_kbin=False)
            )

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
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (CAPTURE_DIR / "latest.txt").write_text(RUN_DIR.name + "\n", encoding="utf-8")


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
    log.info("captures for this run: %s", RUN_DIR)
    log_event_taku_budget()
    log.info("Point Spice2x EA Service URL to http://%s:%s", HOST, PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        httpd.server_close()


if __name__ == "__main__":
    main()
