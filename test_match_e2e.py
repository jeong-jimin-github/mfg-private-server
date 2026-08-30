"""End-to-end test: drive a whole CPU match through the real HTTP handlers.

Mirrors what MFG.Taikyoku.Command.CommandClient does: /entry_game, then a
/gget + /gpost loop consuming cell_data_N commands, then /end_game.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

import server

APP = "VFG:J:A:A:2025122300"

# RECEIVE_COMMAND_TYPE
TSUMO, SUTEHAI, TSUMOAGARI, RON, RYUKYOKU = 1, 2, 3, 4, 5
PON, CHI, ANKAN, MINKAN, KAKAN = 6, 7, 8, 9, 10
TYOKO, TSUMOCHOICES, SUTECHOICES, KYOKUSTART = 14, 15, 16, 17
KYOKUEND, SCORERANK = 23, 24

F_KAN, F_TSUMOAGARI, F_RON, F_KYUSYU, F_REACH, F_SUTE = 0x8, 0x40, 0x80, 0x100, 0x200, 0x400
S_SUTE, S_TSUMO, S_RON, S_NAKINASHI, S_NEXT = 2, 3, 4, 11, 15


def parse(xml_text):
    return ET.fromstring(xml_text)


def cells(root):
    out = []
    info = root.find("./game/taikyoku/cell_info")
    if info is None or info.get("available") == "0":
        return out
    sno = info.find("cell_sno")
    start = int(sno.get("start"))
    count = int(sno.get("count"))
    for i in range(count):
        e = info.find("cell_data_%d" % (start + i))
        if e is not None:
            out.append((start + i, int(e.get("kind")), e))
    return out


class Client:
    def __init__(self, pcuid="TESTSESSION", gmode=1):
        self.pcuid = pcuid
        self.gmode = gmode
        self.tid = 1
        self.next_sno = 0
        self.hand = []          # our concealed hand, mirrored from the cells
        self.pindex = 0
        self.counts = {}
        self.kyoku_starts = 0
        self.done = False

    def entry(self):
        r = parse(server.handle_entry_game({"pcuid": self.pcuid, "gmode": str(self.gmode)}))
        entry = r.find("entry")
        assert entry is not None, "entry_game returned no <entry>"
        self.tid = int(entry.find("tid").text)
        self.pindex = int(entry.find("pindex").text)
        self.next_sno = int(entry.find("next_sno").text)

    def must(self, *rest):
        return "/".join([APP, self.pcuid, str(self.tid), str(self.pindex), "1"] + [str(x) for x in rest])

    def gget(self, ready=True):
        form = {
            "pcuid": self.pcuid,
            "ready": "1" if ready else "0",
            "must": self.must(self.next_sno),
        }
        return parse(server.handle_gget(form))

    def gpost(self, kind, pai=0, tepai=0, tepai2=0, reach=0, tsumogiri=0):
        must = "/".join([
            APP, self.pcuid, str(self.tid), str(self.pindex), "1",
            str(self.next_sno), str(kind), "0", "0", str(pai),
            str(tepai), str(tepai2), str(reach), str(tsumogiri), "0",
        ])
        return parse(server.handle_gpost({"pcuid": self.pcuid, "must": must}))

    # -- command handling -------------------------------------------------
    def consume(self, root):
        acted = False
        for sno, kind, e in cells(root):
            self.next_sno = sno + 1
            self.counts[kind] = self.counts.get(kind, 0) + 1
            if kind == KYOKUSTART:
                self.kyoku_starts += 1
                p = e.find("player_info%d" % self.pindex)
                self.hand = [int(x) for x in p.find("tepai").text.split()]
            elif kind == TSUMO:
                if int(e.find("pindex").text) == self.pindex:
                    self.hand.append(int(e.find("pai").text))
            elif kind == SUTEHAI:
                if int(e.find("pindex").text) == self.pindex:
                    pai = int(e.find("pai").text)
                    if pai in self.hand:
                        self.hand.remove(pai)
            elif kind in (PON, CHI):
                if int(e.find("pindex").text) == self.pindex:
                    tag = "pon_pai" if kind == PON else "chi_pai"
                    for x in e.find(tag).text.split():
                        if int(x) in self.hand:
                            self.hand.remove(int(x))
            elif kind in (ANKAN, MINKAN, KAKAN):
                if int(e.find("pindex").text) == self.pindex:
                    pai = int(e.find("pai").text)
                    n = 4 if kind == ANKAN else (3 if kind == MINKAN else 1)
                    for _ in range(n):
                        if pai in self.hand:
                            self.hand.remove(pai)
            elif kind == KYOKUEND:
                if e.find("end_stat").text == "1":
                    self.done = True
                else:
                    self.consume(self.gpost(S_NEXT))
                acted = True
            elif kind == TSUMOCHOICES:
                self.on_tsumo_choices(e)
                acted = True
            elif kind == SUTECHOICES:
                sel = int(e.find("select").text)
                if sel & F_RON:
                    self.consume(self.gpost(S_RON))
                else:
                    self.consume(self.gpost(S_NAKINASHI))
                acted = True
        return acted

    def on_tsumo_choices(self, e):
        sel = int(e.find("select").text)
        if sel & F_TSUMOAGARI:
            self.consume(self.gpost(S_TSUMO))
            return
        assert self.hand, "tsumo choices with an empty hand"
        reach = 0
        pai = self.hand[-1]
        if sel & F_REACH:
            ptn = e.find("ptn0")
            if ptn is not None:
                pai = int(ptn.find("sute_pai").text)
                reach = 1
        self.consume(self.gpost(S_SUTE, pai=pai, reach=reach,
                                tsumogiri=1 if reach == 0 else 0))


def run(gmode, label):
    c = Client(pcuid="TEST-%s" % gmode, gmode=gmode)
    c.entry()
    # matching poll, then ready
    root = c.gget(ready=False)
    assert root.find("./game/mwait") is not None, "no mwait"
    guard = 0
    while not c.done and guard < 4000:
        guard += 1
        root = c.gget(ready=True)
        if not c.consume(root) and not cells(root):
            break
    res = parse(server.handle_end_or_kiken({"pcuid": c.pcuid}))
    mg = res.find("mgresult")
    assert mg is not None, "end_game returned no mgresult"
    players = [p for p in mg if p.tag.startswith("player_")]
    names = {1: "TSUMO", 2: "SUTE", 3: "TSUMOAGARI", 4: "RON", 5: "RYUKYOKU",
             6: "PON", 7: "CHI", 8: "ANKAN", 9: "MINKAN", 10: "KAKAN",
             15: "TCHOICE", 16: "SCHOICE", 17: "KYOKUSTART", 23: "KYOKUEND",
             24: "SCORERANK"}
    summary = " ".join("%s=%d" % (names.get(k, k), v)
                       for k, v in sorted(c.counts.items()))
    print("%-8s done=%s kyoku=%d players=%d loops=%d | %s"
          % (label, c.done, c.kyoku_starts, len(players), guard, summary))
    assert c.done, "%s never reached KYOKUEND end_stat=1" % label
    assert c.kyoku_starts >= 1
    return c


def test_stickers():
    server.handle_gchat({"tid": "9", "mid": "1", "pindex": "0", "name": "ME",
                         "contents": "TableSticker001", "param": ""})
    out = server.handle_gchat({"tid": "9", "mid": "1", "pindex": "0", "name": "ME",
                               "contents": "TableSticker002", "param": ""})
    root = parse(out)
    chat = root.find("chat")
    assert chat is not None and len(chat) >= 2, "sticker chat did not echo"
    for d in chat:
        assert d.get("idx") and d.get("mid") is not None and d.get("time")
        assert d.find("name") is not None and d.find("contents") is not None
    print("stickers ok (%d entries)" % len(chat))


def test_dojo():
    f = {"pcuid": "nope"}
    root = parse(server.handle_dojo_get_status(f))
    dojo = root.find("dojo")
    assert dojo is not None and int(dojo.find("slot_nr").text) == 4
    assert len(dojo.findall("slot")) == 4
    root = parse(server.handle_dojo_set_slot({"pcuid": "nope", "slot_id": "0",
                                              "set_character": "OID_CHARACTER_3"}))
    slot = root.find("./dojo/slot")
    assert slot.find("available").text == "1"
    assert slot.find("character_obj").text == "OID_CHARACTER_3"
    root = parse(server.handle_dojo_gain_soul({"pcuid": "nope", "slot_id": "0"}))
    assert root.find("./dojo/get_nr") is not None
    print("spirit gym ok")


def test_events():
    import base64
    import json as _json
    root = parse(server.handle_appli_info({}))
    kinds = {n.get("kind"): n.text for n in root.findall("info_data")}
    assert "events" in kinds and "pro_stats" in kinds
    data = _json.loads(base64.b64decode(kinds["events"]).decode("utf-8"))
    names = {r["name"] for r in data["list"]}
    for need in ("SpiritGymBonusEvent", "ConstancyFireReach", "ConstancyAccelDora",
                 "ConstancyMentanpin", "DecorationSticker"):
        assert need in names, "missing event flag " + need
    for r in data["list"]:
        assert r["active"] is True
        if r["param"]:
            assert "=" in r["param"], "param must be key=value: " + r["param"]
    print("appli_info events ok (%d flags)" % len(names))


def test_event_taku_panel_budget():
    """The Home scene has three mode-panel slots and ModePanel_Create indexes
    them directly, so an event set that builds a fourth banner throws out of
    HomeEventSelectWindow.Open and latches _taikyokuModeSelectStatus - which
    kills the plain 対局 button for the rest of the credit."""
    for name, flags in server.EVENT_TAKU_SETS.items():
        panels = sum(server.EVENT_TAKU_PANELS.get(f, 1) for f in flags)
        if name == "all":
            assert panels > server.EVENT_TAKU_PANEL_SLOTS,                 "the 'all' set is the known-bad one; it should still overflow"
            continue
        assert panels <= server.EVENT_TAKU_PANEL_SLOTS,             "event set %r builds %d panels into %d slots" % (
                name, panels, server.EVENT_TAKU_PANEL_SLOTS)
    live = sum(server.EVENT_TAKU_PANELS.get(n, 1)
               for n, _ in server.ACTIVE_EVENTS
               if n in server.EVENT_TAKU_PANELS)
    assert live <= server.EVENT_TAKU_PANEL_SLOTS,         "advertising %d event mode panels, only %d slots exist" % (
            live, server.EVENT_TAKU_PANEL_SLOTS)
    print("event taku panel budget ok (%d/%d)" % (live, server.EVENT_TAKU_PANEL_SLOTS))


def test_gacha():
    root = parse(server.handle_gacha_info({}))
    sched = root.find("gacha_schedule")
    assert sched is not None and len(sched) >= 1
    for info in sched:
        for tag in ("id", "label", "ticket_nr", "now_active", "series_type",
                    "items", "pickup_charas"):
            assert info.find(tag) is not None, "gacha info missing " + tag
    root = parse(server.handle_req_draw_gacha({"pcuid": "x", "gacha_name": "Normal",
                                               "times": "5"}))
    txn = root.find("./transaction_info/transaction_id")
    assert txn is not None and txn.text
    root = parse(server.handle_get_gacha_result({"pcuid": "x",
                                                 "transaction_id": txn.text,
                                                 "times": "5"}))
    assert len(root.find("lottery_result")) == 5
    print("gacha ok")


def main():
    test_events()
    test_event_taku_panel_budget()
    test_gacha()
    test_dojo()
    test_stickers()
    for gmode, label in ((4, "nima"), (3, "sanma"), (1, "tonpu"), (2, "hanchan"),
                         (6, "firereach"), (8, "acceldora"), (20, "bomb")):
        run(gmode, label)
    print("\nALL OK")


if __name__ == "__main__":
    sys.exit(main())
