#!/usr/bin/env python3
"""Server-side mahjong table for the VFG local server.

Owns one CPU match: wall, hands, melds, turn order, the CPU AI and the
`cell_data_N` command stream the Unity client consumes through /gget and
/gpost.

Cell kinds and field names come from MFG.Taikyoku.Command.Receive.*.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

import mahjong as M
from mahjong import Meld

log = logging.getLogger("vfg.taikyoku")

# RECEIVE_COMMAND_TYPE
K_TSUMO = 1
K_SUTEHAI = 2
K_TSUMOAGARI = 3
K_RON = 4
K_RYUKYOKU = 5
K_PON = 6
K_CHI = 7
K_ANKAN = 8
K_MINKAN = 9
K_KAKAN = 10
K_TYOKO = 14
K_TSUMOCHOICES = 15
K_SUTECHOICES = 16
K_KYOKUSTART = 17
K_KYOKUEND = 23
K_SCORERANK = 24

# SEND_COMMAND_TYPE
S_ENTRY = 1
S_SUTE_PAI = 2
S_TSUMO_AGARI = 3
S_RON_AGARI = 4
S_PON = 5
S_CHI = 6
S_ANKAN = 7
S_MINKAN = 8
S_KAKAN = 9
S_KYUSYUKYUHAI = 10
S_NAKINASHI = 11
S_CYOUKOU = 12
S_KIKEN = 13
S_RECONNECT = 14
S_NEXT_KYOKU_READY = 15

# SELECTABLE_TYPE_FLAG
F_NONE = 0x1
F_PON = 0x2
F_CHI = 0x4
F_KAN = 0x8
F_TSUMOAGARI = 0x40
F_RON = 0x80
F_KYUSYU = 0x100
F_REACH = 0x200
F_SUTE = 0x400

TAKU_PLAYER_MAX = 4


def _ints(tag: str, values: Sequence[int]) -> str:
    vals = [str(int(v)) for v in values] or ["0"]
    return '<%s __count="%d">%s</%s>' % (tag, len(vals), " ".join(vals), tag)


def _pais(values: Sequence[int]) -> List[int]:
    return [M.idx_to_pai(v) for v in values]


class Table:
    """One CPU match (matchmaking itself is handled by the caller)."""

    def __init__(self, taku: int, human_seat: int = 0, seed: Optional[int] = None):
        self.taku = taku
        self.seats = M.SEATS_OF[taku]
        self.human = human_seat % self.seats
        self.rng = random.Random(seed)
        self.total_kyoku = M.KYOKU_COUNT[taku]
        self.scores = [M.START_SCORE[taku]] * 4
        for i in range(self.seats, 4):
            self.scores[i] = 0
        self.kyoku_index = 0          # 0-based across the whole game
        self.honba = 0
        self.kyotaku = 0
        self.cells: List[str] = []
        self.state = "init"           # init|discard|call|kyoku_end|game_end
        self.pending_tsumo_choices: Optional[Dict[str, Any]] = None
        self.call_ctx: Optional[Dict[str, Any]] = None
        self.finished = False
        self.advance_kyoku = True
        self.nokori_start = 0
        self._new_kyoku_state()

    # ------------------------------------------------------------------
    # bookkeeping
    # ------------------------------------------------------------------

    @property
    def oya(self) -> int:
        return self.kyoku_index % self.seats

    @property
    def ba(self) -> int:
        return 0 if self.kyoku_index < self.seats else 1

    @property
    def kyoku(self) -> int:
        return self.kyoku_index % self.seats

    @property
    def is_all_last(self) -> bool:
        return self.kyoku_index >= self.total_kyoku - 1

    def seat_wind(self, seat: int) -> int:
        return (seat - self.oya) % self.seats

    def _new_kyoku_state(self) -> None:
        self.hands: List[List[int]] = [[] for _ in range(4)]
        self.melds: List[List[Meld]] = [[] for _ in range(4)]
        self.discards: List[List[int]] = [[] for _ in range(4)]
        self.discard_log: List[Tuple[int, int]] = []
        self.riichi = [False] * 4
        self.double_riichi = [False] * 4
        self.ippatsu = [False] * 4
        self.riichi_at = [-1] * 4
        self.furiten = [False] * 4
        self.temp_furiten = [False] * 4
        self.wall: List[int] = []
        self.rinshan: List[int] = []
        self.dora_ind: List[int] = []
        self.ura_ind: List[int] = []
        self.dora_open = 1
        self.kan_count = 0
        self.turn = 0
        self.drawn: List[Optional[int]] = [None] * 4
        self.last_draw_rinshan = False
        self.first_go_around = True
        self.discard_count = 0
        self.any_call = False

    # ------------------------------------------------------------------
    # cells
    # ------------------------------------------------------------------

    def _cell(self, kind: int, inner: str, pis: Optional[Sequence[int]] = None) -> None:
        seq = len(self.cells)
        targets = list(range(self.seats)) if pis is None else list(pis)
        flags = "".join(' pi%d="%d"' % (i, 1 if i in targets else 0) for i in range(4))
        self.cells.append(
            '<cell_data_%d kind="%d"%s>%s</cell_data_%d>' % (seq, kind, flags, inner, seq)
        )

    def cells_from(self, start: int) -> str:
        if start < 0:
            start = 0
        if start >= len(self.cells):
            return '<taikyoku><cell_info available="0" /></taikyoku>'
        chunk = self.cells[start:]
        return (
            "<taikyoku>"
            '<cell_info available="1">'
            '<cell_sno start="%d" count="%d"></cell_sno>' % (start, len(chunk))
            + "".join(chunk)
            + "</cell_info></taikyoku>"
        )

    # ------------------------------------------------------------------
    # kyoku life cycle
    # ------------------------------------------------------------------

    def start_kyoku(self) -> None:
        self._new_kyoku_state()
        tiles = M.build_wall(self.taku, self.rng)
        dead = tiles[-14:]
        live = tiles[:-14]
        self.rinshan = dead[0:4]
        self.dora_ind = dead[4:9]
        self.ura_ind = dead[9:14]
        for s in range(self.seats):
            self.hands[s] = sorted(live[:13])
            live = live[13:]
        self.wall = live
        self.nokori_start = len(live)
        self.state = "discard"

        inner = (
            "<chicya>0</chicya>"
            "<oya>%d</oya>" % self.oya
            + _ints("sai", [self.rng.randint(1, 6), self.rng.randint(1, 6)])
            + "<ba>%d</ba>" % self.ba
            + "<kyoku>%d</kyoku>" % self.kyoku
            + "<all_last>%d</all_last>" % (1 if self.is_all_last else 0)
            + "<honba>%d</honba>" % self.honba
            + "<rencyan>0</rencyan>"
            + "<kyoutaku>%d</kyoutaku>" % self.kyotaku
            + "<nokori>%d</nokori>" % self.nokori_start
            + "<dora_open>1</dora_open>"
            + _ints("dora", _pais(self.dora_ind))
            + _ints("ura_dora", _pais(self.ura_ind))
            + "<yama_cnt>%d</yama_cnt>" % len(self.wall)
            + _ints("yama", _pais(self.wall))
            + _ints("rinshan", _pais(self.rinshan))
        )
        ranks = self._ranks()
        for i in range(TAKU_PLAYER_MAX):
            if i < self.seats:
                tepai = _pais(self.hands[i])
                score = self.scores[i]
                rank = ranks[i]
                jikaze = self.seat_wind(i)
            else:
                tepai = [1] * 13
                score = 0
                rank = i
                jikaze = i
            inner += (
                "<player_info%d>" % i
                + "<jikaze>%d</jikaze>" % jikaze
                + _ints("tepai", tepai)
                + "<score>%d</score>" % score
                + "<rank>%d</rank>" % rank
                + "</player_info%d>" % i
            )
        self._cell(K_KYOKUSTART, inner)
        log.info(
            "kyoku %d/%d oya=%d honba=%d kyotaku=%d nokori=%d",
            self.kyoku_index + 1, self.total_kyoku, self.oya, self.honba,
            self.kyotaku, self.nokori_start,
        )
        self._begin_turn(self.oya)

    def _ranks(self) -> List[int]:
        order = sorted(range(self.seats), key=lambda i: (-self.scores[i], self.seat_wind(i)))
        ranks = [0] * 4
        for r, i in enumerate(order):
            ranks[i] = r
        for i in range(self.seats, 4):
            ranks[i] = i
        return ranks

    # ------------------------------------------------------------------
    # drawing / turns
    # ------------------------------------------------------------------

    def _draw(self, seat: int, from_rinshan: bool = False) -> Optional[int]:
        if from_rinshan:
            if not self.wall or self.kan_count > len(self.rinshan):
                return None
            tile = self.rinshan[self.kan_count - 1]
            self.wall.pop()          # live wall feeds the dead wall back
        else:
            if not self.wall:
                return None
            tile = self.wall.pop(0)
        self.hands[seat].append(tile)
        self.hands[seat].sort()
        self.drawn[seat] = tile
        self.last_draw_rinshan = from_rinshan
        return tile

    def _begin_turn(self, seat: int, from_rinshan: bool = False) -> None:
        if self.state in ("kyoku_end", "game_end"):
            return
        tile = self._draw(seat, from_rinshan)
        if tile is None:
            self._ryuukyoku()
            return
        self.turn = seat
        self._cell(K_TSUMO, "<pindex>%d</pindex><pai>%d</pai>" % (seat, M.idx_to_pai(tile)))
        self.temp_furiten[seat] = False
        if seat == self.human:
            self._offer_tsumo_choices(seat)
        else:
            self._cpu_turn(seat)

    def _next_seat(self, seat: int) -> int:
        return (seat + 1) % self.seats

    # ------------------------------------------------------------------
    # human choice cells
    # ------------------------------------------------------------------

    def _tenpai_patterns(self, seat: int) -> List[Tuple[int, List[int]]]:
        """(discard, waits) options that leave the hand tenpai."""
        hand = self.hands[seat]
        opened = len(self.melds[seat])
        out = []
        seen = set()
        for t in hand:
            if t in seen:
                continue
            seen.add(t)
            rest = list(hand)
            rest.remove(t)
            c = M.counts_of(rest)
            if M.shanten(c, opened, self.taku) != 0:
                continue
            w = M.waits_of(c, opened, self.taku)
            if w:
                out.append((t, w))
        return out

    def _visible_counts(self, seat: int) -> List[int]:
        c = [0] * 34
        for t in self.hands[seat]:
            c[t] += 1
        for s in range(self.seats):
            for t in self.discards[s]:
                c[t] += 1
            for m in self.melds[s]:
                for t in m.tiles:
                    c[t] += 1
        for t in self.dora_ind[: self.dora_open]:
            c[t] += 1
        return [min(4, n) for n in c]

    def _ankan_options(self, seat: int) -> List[Tuple[int, int]]:
        """[(tile_idx, kan_type)] with kan_type 1 = ankan, 3 = kakan."""
        out: List[Tuple[int, int]] = []
        if not self.wall or self.kan_count >= 4:
            return out
        c = M.counts_of(self.hands[seat])
        opened = len(self.melds[seat])
        for t in range(34):
            if c[t] != 4:
                continue
            if self.riichi[seat]:
                if self.drawn[seat] != t:
                    continue
                before_hand = list(self.hands[seat])
                before_hand.remove(t)
                before = sorted(M.waits_of(M.counts_of(before_hand), opened, self.taku))
                after_hand = [x for x in self.hands[seat] if x != t]
                after = sorted(M.waits_of(M.counts_of(after_hand), opened + 1, self.taku))
                if before != after or not after:
                    continue
            out.append((t, 1))
        if not self.riichi[seat]:
            for m in self.melds[seat]:
                if m.kind == M.PON and c[m.tiles[0]] >= 1:
                    out.append((m.tiles[0], 3))
        return out

    def _kyuushu_ok(self, seat: int) -> bool:
        if not self.first_go_around or self.any_call:
            return False
        if self.discards[seat]:
            return False
        kinds = {t for t in self.hands[seat] if M.is_yaochu(t)}
        return len(kinds) >= 9

    def _offer_tsumo_choices(self, seat: int) -> None:
        """Build the TSUMOCHOICES payload; flushed on the next /gget."""
        opened = len(self.melds[seat])
        flags = F_SUTE
        patterns: List[Tuple[int, List[int]]] = []
        if self._win_result(seat, self.drawn[seat], True) is not None:
            flags |= F_TSUMOAGARI
        if (not self.riichi[seat] and opened == 0 and self.scores[seat] >= 1000
                and len(self.wall) >= 4):
            patterns = self._tenpai_patterns(seat)
            if patterns:
                flags |= F_REACH
        kans = self._ankan_options(seat)
        if kans:
            flags |= F_KAN
        if self._kyuushu_ok(seat):
            flags |= F_KYUSYU
        self.pending_tsumo_choices = {
            "seat": seat,
            "flags": flags,
            "patterns": patterns,
            "kans": kans,
        }
        self.state = "discard"

    def flush_pending(self) -> None:
        p = self.pending_tsumo_choices
        if not p:
            return
        self.pending_tsumo_choices = None
        seat = p["seat"]
        vis = self._visible_counts(seat)
        inner = "<select>%d</select>" % p["flags"]
        inner += "<ptn_num>%d</ptn_num>" % len(p["patterns"])
        for i, (sute, waits) in enumerate(p["patterns"]):
            stat = [2 if vis[w] >= 4 else 0 for w in waits]
            inner += (
                "<ptn%d>" % i
                + "<sute_pai>%d</sute_pai>" % M.idx_to_pai(sute)
                + "<machi_num>%d</machi_num>" % len(waits)
                + _ints("machi_pai", _pais(waits))
                + _ints("stat", stat)
                + "</ptn%d>" % i
            )
        if p["kans"]:
            inner += _ints("kan_pai", [M.idx_to_pai(t) for t, _ in p["kans"]])
            inner += _ints("kan_type", [k for _, k in p["kans"]])
        self._cell(K_TSUMOCHOICES, inner, pis=[seat])

    # ------------------------------------------------------------------
    # win evaluation
    # ------------------------------------------------------------------

    def _win_result(self, seat: int, win_tile: Optional[int], is_tsumo: bool,
                    chankan: bool = False) -> Optional[Dict[str, Any]]:
        if win_tile is None:
            return None
        hand = list(self.hands[seat])
        if not is_tsumo:
            hand = hand + [win_tile]
        if len(hand) % 3 != 2:
            return None
        if not M.is_agari(M.counts_of(hand), len(self.melds[seat]), self.taku):
            return None
        if not is_tsumo and (self.furiten[seat] or self.temp_furiten[seat]):
            return None
        last_tile = len(self.wall) == 0
        ctx = M.WinContext(
            hand=hand,
            melds=self.melds[seat],
            win_tile=win_tile,
            is_tsumo=is_tsumo,
            seat_wind=self.seat_wind(seat),
            round_wind=self.ba,
            riichi=self.riichi[seat],
            double_riichi=self.double_riichi[seat],
            ippatsu=self.ippatsu[seat],
            haitei=is_tsumo and last_tile and not self.last_draw_rinshan,
            houtei=(not is_tsumo) and last_tile and not chankan,
            rinshan=is_tsumo and self.last_draw_rinshan,
            chankan=chankan,
            tenho=(is_tsumo and self.first_go_around and not self.any_call
                   and seat == self.oya and self.discard_count == 0),
            chiho=(is_tsumo and self.first_go_around and not self.any_call
                   and seat != self.oya and not self.discards[seat]),
            dora_indicators=self.dora_ind[: self.dora_open],
            ura_indicators=self.ura_ind[: self.dora_open],
            taku=self.taku,
        )
        return M.score_hand(ctx)

    def _update_furiten(self, seat: int) -> None:
        opened = len(self.melds[seat])
        c = M.counts_of(self.hands[seat])
        if sum(c) % 3 != 1:
            return
        w = set(M.waits_of(c, opened, self.taku))
        self.furiten[seat] = bool(w) and any(t in w for t in self.discards[seat])

    # ------------------------------------------------------------------
    # discard
    # ------------------------------------------------------------------

    def _do_discard(self, seat: int, tile: int, riichi: bool, tsumogiri: bool) -> None:
        if tile not in self.hands[seat]:
            if self.drawn[seat] is not None and self.drawn[seat] in self.hands[seat]:
                tile = self.drawn[seat]
            elif self.hands[seat]:
                tile = self.hands[seat][-1]
            else:
                return
        self.hands[seat].remove(tile)
        self.hands[seat].sort()
        self.discards[seat].append(tile)
        self.discard_log.append((seat, tile))
        self.drawn[seat] = None
        self.discard_count += 1
        if riichi:
            self.riichi[seat] = True
            self.ippatsu[seat] = True
            self.riichi_at[seat] = len(self.discard_log)
            if self.first_go_around and not self.any_call:
                self.double_riichi[seat] = True
            self.scores[seat] -= 1000
            self.kyotaku += 1
        else:
            # a post-riichi discard by the same player consumes their ippatsu
            self.ippatsu[seat] = False
        stat = (1 if riichi else 0) | (2 if tsumogiri else 0)
        self._cell(
            K_SUTEHAI,
            "<pindex>%d</pindex><pai>%d</pai><stat>%d</stat>"
            % (seat, M.idx_to_pai(tile), stat),
        )
        if riichi:
            self._score_rank_cell()
        self._update_furiten(seat)
        if self.discard_count >= self.seats:
            self.first_go_around = False
        self._after_discard(seat, tile)

    def _score_rank_cell(self) -> None:
        ranks = self._ranks()
        inner = "<kyoutaku>%d</kyoutaku>" % self.kyotaku
        for i in range(TAKU_PLAYER_MAX):
            score = self.scores[i] if i < self.seats else 0
            inner += (
                "<riti_after%d><score>%d</score><rank>%d</rank></riti_after%d>"
                % (i, score, ranks[i], i)
            )
        self._cell(K_SCORERANK, inner)

    # ------------------------------------------------------------------
    # calls after a discard
    # ------------------------------------------------------------------

    def _pon_options(self, seat: int, tile: int) -> List[List[int]]:
        if M.counts_of(self.hands[seat])[tile] < 2:
            return []
        return [[tile, tile]]

    def _chi_options(self, seat: int, tile: int) -> List[List[int]]:
        if M.is_honor(tile):
            return []
        c = M.counts_of(self.hands[seat])
        n = tile % 9
        out = []
        if n >= 2 and c[tile - 2] and c[tile - 1]:
            out.append([tile - 2, tile - 1])
        if 1 <= n <= 7 and c[tile - 1] and c[tile + 1]:
            out.append([tile - 1, tile + 1])
        if n <= 6 and c[tile + 1] and c[tile + 2]:
            out.append([tile + 1, tile + 2])
        return out

    def _minkan_ok(self, seat: int, tile: int) -> bool:
        return (M.counts_of(self.hands[seat])[tile] >= 3
                and self.kan_count < 4 and bool(self.wall))

    def _after_discard(self, discarder: int, tile: int) -> None:
        human = self.human
        if human != discarder:
            ron = self._win_result(human, tile, False) is not None
            pon = (not self.riichi[human]) and bool(self._pon_options(human, tile))
            chi = ((not self.riichi[human]) and human == self._next_seat(discarder)
                   and bool(self._chi_options(human, tile)))
            kan = (not self.riichi[human]) and self._minkan_ok(human, tile)
            if ron or pon or chi or kan:
                self._offer_sute_choices(discarder, tile, ron, pon, chi, kan)
                return
        self._cpu_calls(discarder, tile)

    def _offer_sute_choices(self, discarder: int, tile: int, ron: bool,
                            pon: bool, chi: bool, kan: bool,
                            chankan: bool = False) -> None:
        flags = 0
        naki = 0
        if ron:
            flags |= F_RON
        if pon:
            flags |= F_PON
            naki |= F_PON
        if chi:
            flags |= F_CHI
            naki |= F_CHI
        if kan:
            flags |= F_KAN
            naki |= F_KAN
        inner = (
            "<select>%d</select>" % flags
            + "<naki>%d</naki>" % naki
            + "<pindex>%d</pindex>" % discarder
            + "<sute_pai>%d</sute_pai>" % M.idx_to_pai(tile)
        )
        if chi:
            flat: List[int] = []
            for o in self._chi_options(self.human, tile)[:6]:
                flat.extend(M.idx_to_pai(t) for t in o)
            inner += _ints("chi_pai", flat)
        if pon:
            inner += _ints("pon_pai", [M.idx_to_pai(tile), M.idx_to_pai(tile)])
        if kan:
            inner += _ints("kan_pai", [M.idx_to_pai(tile)])
            inner += _ints("kan_type", [2])
        self._cell(K_SUTECHOICES, inner, pis=[self.human])
        self.call_ctx = {
            "discarder": discarder,
            "tile": tile,
            "ron": ron,
            "chankan": chankan,
        }
        self.state = "call"

    def _cpu_calls(self, discarder: int, tile: int) -> None:
        """CPU ron / pon / chi / kan on the tile just discarded."""
        order = [(discarder + i) % self.seats for i in range(1, self.seats)]
        for s in order:
            if s == self.human:
                continue
            res = self._win_result(s, tile, False)
            if res is not None:
                self._apply_ron([s], discarder, tile, {s: res})
                return
        for s in order:
            if s == self.human or self.riichi[s]:
                continue
            if self._minkan_ok(s, tile) and self._cpu_wants_pon(s, tile):
                self._apply_minkan(s, discarder, tile)
                return
            if self._pon_options(s, tile) and self._cpu_wants_pon(s, tile):
                self._apply_pon(s, discarder, tile, [tile, tile])
                return
        nxt = self._next_seat(discarder)
        if nxt != self.human and not self.riichi[nxt]:
            pick = self._cpu_pick_chi(nxt, tile, self._chi_options(nxt, tile))
            if pick is not None:
                self._apply_chi(nxt, discarder, tile, pick)
                return
        self._begin_turn(self._next_seat(discarder))

    def _resume_after_chankan(self, kan_seat: int) -> None:
        self._begin_turn(kan_seat, from_rinshan=True)

    # ------------------------------------------------------------------
    # meld application
    # ------------------------------------------------------------------

    def _break_ippatsu(self) -> None:
        for s in range(self.seats):
            self.ippatsu[s] = False
        self.any_call = True
        self.first_go_around = False

    def _apply_pon(self, seat: int, from_seat: int, tile: int, own: Sequence[int]) -> None:
        for t in own:
            if t in self.hands[seat]:
                self.hands[seat].remove(t)
        self.melds[seat].append(Meld(M.PON, [tile, tile, tile], tile, from_seat))
        self._break_ippatsu()
        self._cell(
            K_PON,
            "<pindex>%d</pindex><sute_pindex>%d</sute_pindex><pai>%d</pai>%s"
            % (seat, from_seat, M.idx_to_pai(tile),
               _ints("pon_pai", [M.idx_to_pai(t) for t in own])),
        )
        self.turn = seat
        self.drawn[seat] = None
        if seat == self.human:
            self._offer_tsumo_choices(seat)
        else:
            self._cpu_discard_after_call(seat)

    def _apply_chi(self, seat: int, from_seat: int, tile: int, own: Sequence[int]) -> None:
        for t in own:
            if t in self.hands[seat]:
                self.hands[seat].remove(t)
        self.melds[seat].append(Meld(M.CHI, sorted([tile] + list(own)), tile, from_seat))
        self._break_ippatsu()
        self._cell(
            K_CHI,
            "<pindex>%d</pindex><sute_pindex>%d</sute_pindex><pai>%d</pai>%s"
            % (seat, from_seat, M.idx_to_pai(tile),
               _ints("chi_pai", [M.idx_to_pai(t) for t in own])),
        )
        self.turn = seat
        self.drawn[seat] = None
        if seat == self.human:
            self._offer_tsumo_choices(seat)
        else:
            self._cpu_discard_after_call(seat)

    def _apply_minkan(self, seat: int, from_seat: int, tile: int) -> None:
        for _ in range(3):
            if tile in self.hands[seat]:
                self.hands[seat].remove(tile)
        self.melds[seat].append(Meld(M.MINKAN, [tile] * 4, tile, from_seat))
        self._break_ippatsu()
        self.kan_count += 1
        self.dora_open = min(5, self.dora_open + 1)
        self._cell(
            K_MINKAN,
            "<pindex>%d</pindex><sute_pindex>%d</sute_pindex><pai>%d</pai>"
            % (seat, from_seat, M.idx_to_pai(tile)),
        )
        self._begin_turn(seat, from_rinshan=True)

    def _apply_ankan(self, seat: int, tile: int) -> None:
        for _ in range(4):
            if tile in self.hands[seat]:
                self.hands[seat].remove(tile)
        self.melds[seat].append(Meld(M.ANKAN, [tile] * 4, tile, seat))
        self.kan_count += 1
        self.dora_open = min(5, self.dora_open + 1)
        self.any_call = True
        self._cell(K_ANKAN, "<pindex>%d</pindex><pai>%d</pai>" % (seat, M.idx_to_pai(tile)))
        self._begin_turn(seat, from_rinshan=True)

    def _apply_kakan(self, seat: int, tile: int) -> None:
        if tile in self.hands[seat]:
            self.hands[seat].remove(tile)
        for m in self.melds[seat]:
            if m.kind == M.PON and m.tiles[0] == tile:
                m.kind = M.KAKAN
                m.tiles = [tile] * 4
                break
        self.kan_count += 1
        self.dora_open = min(5, self.dora_open + 1)
        self.any_call = True
        self._cell(K_KAKAN, "<pindex>%d</pindex><pai>%d</pai>" % (seat, M.idx_to_pai(tile)))
        for i in range(1, self.seats):
            s = (seat + i) % self.seats
            res = self._win_result(s, tile, False, chankan=True)
            if res is None:
                continue
            if s == self.human:
                self._offer_sute_choices(seat, tile, True, False, False, False,
                                         chankan=True)
                return
            self._apply_ron([s], seat, tile, {s: res})
            return
        self._begin_turn(seat, from_rinshan=True)

    # ------------------------------------------------------------------
    # CPU AI
    # ------------------------------------------------------------------

    def _danger(self, seat: int, tile: int) -> int:
        """0 = safe, higher = riskier against riichi opponents."""
        risk = 0
        for s in range(self.seats):
            if s == seat or not self.riichi[s]:
                continue
            if tile in self.discards[s]:
                continue
            start = max(0, self.riichi_at[s])
            if any(t == tile for _, t in self.discard_log[start:]):
                continue
            risk += 4 if M.is_yaochu(tile) else 10
            if not M.is_honor(tile) and 2 <= tile % 9 <= 6:
                risk += 4
        return risk

    def _cpu_choose_discard(self, seat: int) -> Tuple[int, bool]:
        """Return (tile, declare_riichi)."""
        drawn = self.drawn[seat]
        if self.riichi[seat]:
            return (drawn if drawn is not None else self.hands[seat][-1]), False

        opened = len(self.melds[seat])
        seen = self._visible_counts(seat)
        threat = any(self.riichi[s] for s in range(self.seats) if s != seat)
        dora = {M.dora_from_indicator(d, self.taku) for d in self.dora_ind[: self.dora_open]}
        yakuhai = set(self._cpu_yakuhai_kinds(seat))

        best_tile = None
        best_score = None
        best_sh = 99
        for t in sorted(set(self.hands[seat])):
            rest = list(self.hands[seat])
            rest.remove(t)
            c = M.counts_of(rest)
            sh = M.shanten(c, opened, self.taku)
            uk = M.ukeire(c, opened, self.taku, seen) if sh <= 3 else 0
            danger = self._danger(seat, t)
            keep = 0
            if t in yakuhai and M.counts_of(self.hands[seat])[t] >= 2:
                keep += 3
            if t in dora:
                keep += 3
            score = sh * 120.0 - uk * 1.5 + keep * 6.0
            if threat:
                weight = 3.0 if sh >= 2 else 1.2
                score += danger * weight
            score += self.rng.random() * 0.5
            if best_score is None or score < best_score:
                best_score, best_tile, best_sh = score, t, sh
        tile = best_tile if best_tile is not None else self.hands[seat][-1]

        declare = False
        if (opened == 0 and best_sh == 0 and self.scores[seat] >= 1000
                and len(self.wall) >= 4):
            rest = list(self.hands[seat])
            rest.remove(tile)
            if M.waits_of(M.counts_of(rest), 0, self.taku):
                declare = True
        return tile, declare

    def _cpu_yakuhai_kinds(self, seat: int) -> List[int]:
        out = [M.HON + 4, M.HON + 5, M.HON + 6, M.HON + self.ba,
               M.HON + self.seat_wind(seat)]
        return [t for t in out if M.HON <= t < 34]

    def _cpu_wants_pon(self, seat: int, tile: int) -> bool:
        if self.riichi[seat]:
            return False
        opened = len(self.melds[seat])
        rest = list(self.hands[seat])
        for _ in range(2):
            if tile in rest:
                rest.remove(tile)
        before = M.shanten(M.counts_of(self.hands[seat]), opened, self.taku)
        after = M.shanten(M.counts_of(rest), opened + 1, self.taku)
        if after > before:
            return False
        if tile in self._cpu_yakuhai_kinds(seat):
            return True
        if after >= before:
            return False
        allt = list(rest) + [tile] * 3
        for m in self.melds[seat]:
            allt.extend(m.tiles)
        if not any(M.is_yaochu(t) for t in allt):
            return True                  # tanyao route
        if opened and all(m.kind != M.CHI for m in self.melds[seat]):
            return True                  # toitoi route
        return False

    def _cpu_pick_chi(self, seat: int, tile: int, opts: List[List[int]]) -> Optional[List[int]]:
        if not opts or self.riichi[seat]:
            return None
        opened = len(self.melds[seat])
        before = M.shanten(M.counts_of(self.hands[seat]), opened, self.taku)
        best = None
        for o in opts:
            rest = list(self.hands[seat])
            ok = True
            for t in o:
                if t in rest:
                    rest.remove(t)
                else:
                    ok = False
            if not ok:
                continue
            after = M.shanten(M.counts_of(rest), opened + 1, self.taku)
            if after >= before:
                continue
            allt = list(rest) + list(o) + [tile]
            for m in self.melds[seat]:
                allt.extend(m.tiles)
            if any(M.is_yaochu(t) for t in allt):
                continue                 # would leave the hand without tanyao
            if best is None or after < best[0]:
                best = (after, o)
        return best[1] if best else None

    def _cpu_wants_kan(self, seat: int, tile: int, ktype: int) -> bool:
        if self.riichi[seat]:
            return ktype == 1
        opened = len(self.melds[seat])
        before = M.shanten(M.counts_of(self.hands[seat]), opened, self.taku)
        rest = list(self.hands[seat])
        drop = 4 if ktype == 1 else 1
        for _ in range(drop):
            if tile in rest:
                rest.remove(tile)
        after = M.shanten(M.counts_of(rest), opened + 1, self.taku)
        return after <= before

    def _cpu_turn(self, seat: int) -> None:
        drawn = self.drawn[seat]
        res = self._win_result(seat, drawn, True)
        if res is not None:
            self._apply_tsumo(seat, drawn, res)
            return
        for tile, ktype in self._ankan_options(seat):
            if not self._cpu_wants_kan(seat, tile, ktype):
                continue
            if ktype == 1:
                self._apply_ankan(seat, tile)
            else:
                self._apply_kakan(seat, tile)
            return
        tile, declare = self._cpu_choose_discard(seat)
        self._do_discard(seat, tile, declare, tile == drawn)

    def _cpu_discard_after_call(self, seat: int) -> None:
        tile, _ = self._cpu_choose_discard(seat)
        self._do_discard(seat, tile, False, False)

    # ------------------------------------------------------------------
    # agari / ryuukyoku
    # ------------------------------------------------------------------

    _DUMMY_NAKI = "".join(
        "<naki%d><type>0</type><kantype>0</kantype>%s</naki%d>"
        % (i, "".join("<pai%d><pai_st>0</pai_st><pai>0</pai></pai%d>" % (j, j)
                      for j in range(4)), i)
        for i in range(4)
    )

    def _yaku_xml(self, tag: str, res: Optional[Dict[str, Any]],
                  win_pai_idx: int, hand_idx: Sequence[int]) -> str:
        if res is None:
            bits = han = fu = dora = rank = 0
        else:
            bits = res["bits"]
            han = res["han"]
            fu = res["fu"]
            dora = res["dora"]
            rank = res["rank"]
        return (
            "<%s>" % tag
            + "<pai>%d</pai>" % M.idx_to_pai(win_pai_idx)
            + "<yaku_han>%d</yaku_han>" % rank
            + "<han_num>%d</han_num>" % han
            + "<fu_num>%d</fu_num>" % fu
            + "<dora_num>%d</dora_num>" % dora
            + "<bonus_han>0</bonus_han>"
            + "<yaku1>%d</yaku1>" % (bits & 0xFFFFFFFF)
            + "<yaku2>%d</yaku2>" % ((bits >> 32) & 0xFFFFFFFF)
            + _ints("tepai", _pais(hand_idx))
            + self._DUMMY_NAKI
            + "</%s>" % tag
        )

    def _calc_score_xml(self, before: Sequence[int], yaku: Sequence[int],
                        kyotaku: Sequence[int], tsumifu: Sequence[int]) -> str:
        out = ""
        for i in range(TAKU_PLAYER_MAX):
            b, y = before[i], yaku[i]
            k, t = kyotaku[i], tsumifu[i]
            out += (
                "<calc_score%d>" % i
                + "<before_score>%d</before_score>" % b
                + "<yaku_score>%d</yaku_score>" % y
                + "<kyotaku_score>%d</kyotaku_score>" % k
                + "<tumifu_score>%d</tumifu_score>" % t
                + "<new_score>%d</new_score>" % (b + y + k + t)
                + "<wherefore>0</wherefore>"
                + "</calc_score%d>" % i
            )
        return out

    def _apply_tsumo(self, seat: int, win_tile: int, res: Dict[str, Any]) -> None:
        before = list(self.scores)
        yaku = [0] * 4
        kyo = [0] * 4
        fu = [0] * 4
        is_oya = seat == self.oya
        _total, ko, oya = M.payments(self.taku, res["rank"], res["fu"], is_oya, True)
        gain = 0
        for s in range(self.seats):
            if s == seat:
                continue
            pay = oya if (s == self.oya and not is_oya) else ko
            yaku[s] = -pay
            fu[s] = -100 * self.honba
            gain += pay
        yaku[seat] = gain
        fu[seat] = 100 * self.honba * (self.seats - 1)
        kyo[seat] = 1000 * self.kyotaku
        for i in range(4):
            self.scores[i] = before[i] + yaku[i] + kyo[i] + fu[i]
        self.kyotaku = 0

        inner = (
            "<pindex>%d</pindex>" % seat
            + "<dora_open>%d</dora_open>" % self.dora_open
            + _ints("dora", _pais(self.dora_ind))
            + _ints("ura_dora", _pais(self.ura_ind))
            + self._yaku_xml("yaku", res, win_tile, self.hands[seat])
            + self._calc_score_xml(before, yaku, kyo, fu)
        )
        self._cell(K_TSUMOAGARI, inner)
        log.info("tsumo seat=%d han=%d fu=%d rank=%d", seat, res["han"], res["fu"], res["rank"])
        self._end_kyoku(winners=[seat])

    def _apply_ron(self, winners: List[int], discarder: int, win_tile: int,
                   results: Dict[int, Dict[str, Any]]) -> None:
        before = list(self.scores)
        yaku = [0] * 4
        kyo = [0] * 4
        fu = [0] * 4
        first = True
        for s in winners:
            res = results[s]
            total, _ko, _oya = M.payments(self.taku, res["rank"], res["fu"],
                                          s == self.oya, False)
            yaku[s] += total
            yaku[discarder] -= total
            fu[s] += 300 * self.honba
            fu[discarder] -= 300 * self.honba
            if first:
                kyo[s] += 1000 * self.kyotaku
                first = False
        self.kyotaku = 0
        for i in range(4):
            self.scores[i] = before[i] + yaku[i] + kyo[i] + fu[i]

        inner = (
            "<furikomi_pindex>%d</furikomi_pindex>" % discarder
            + _ints("ron_flg", [1 if i in winners else 0 for i in range(4)])
            + "<dora_open>%d</dora_open>" % self.dora_open
            + _ints("dora", _pais(self.dora_ind))
            + _ints("ura_dora", _pais(self.ura_ind))
        )
        for i in range(TAKU_PLAYER_MAX):
            if i in winners:
                inner += self._yaku_xml("yaku%d" % i, results[i], win_tile,
                                        sorted(self.hands[i] + [win_tile]))
            else:
                inner += self._yaku_xml("yaku%d" % i, None, win_tile, [0] * 13)
        inner += self._calc_score_xml(before, yaku, kyo, fu)
        self._cell(K_RON, inner)
        log.info("ron winners=%s from=%d", winners, discarder)
        self._end_kyoku(winners=winners)

    def _ryuukyoku(self, abortive: bool = False) -> None:
        before = list(self.scores)
        yaku = [0] * 4
        tenpai: List[int] = []
        machi: Dict[int, List[int]] = {}
        if not abortive:
            for s in range(self.seats):
                c = M.counts_of(self.hands[s])
                if sum(c) % 3 != 1:
                    continue
                if M.shanten(c, len(self.melds[s]), self.taku) != 0:
                    continue
                w = M.waits_of(c, len(self.melds[s]), self.taku)
                if w:
                    tenpai.append(s)
                    machi[s] = w
            n = len(tenpai)
            if 0 < n < self.seats:
                gain = 3000 // n
                loss = 3000 // (self.seats - n)
                for s in range(self.seats):
                    yaku[s] = gain if s in tenpai else -loss
        for i in range(4):
            self.scores[i] = before[i] + yaku[i]

        inner = ""
        for i in range(TAKU_PLAYER_MAX):
            is_tenpai = i in tenpai
            pais = _pais(machi.get(i, [])) if is_tenpai else [0]
            inner += (
                "<ryukyoku_status%d>" % i
                + "<end_stat>%d</end_stat>" % (1 if is_tenpai else 0)
                + _ints("machi_pai", pais)
                + "</ryukyoku_status%d>" % i
            )
        inner += self._calc_score_xml(before, yaku, [0] * 4, [0] * 4)
        self._cell(K_RYUKYOKU, inner)
        log.info("ryuukyoku tenpai=%s abortive=%s", tenpai, abortive)
        renchan = abortive or (self.oya in tenpai)
        self._end_kyoku(winners=[], renchan=renchan, draw=True)

    def _end_kyoku(self, winners: List[int], renchan: Optional[bool] = None,
                   draw: bool = False) -> None:
        self._score_rank_cell()
        if renchan is None:
            renchan = self.oya in winners
        if renchan or draw:
            self.honba += 1
        else:
            self.honba = 0
        self.advance_kyoku = not renchan
        last = self.kyoku_index >= self.total_kyoku - 1
        busted = any(self.scores[i] < 0 for i in range(self.seats))
        game_over = busted or (last and not renchan)
        self._cell(K_KYOKUEND, "<end_stat>%d</end_stat>" % (1 if game_over else 0))
        self.pending_tsumo_choices = None
        self.call_ctx = None
        if game_over:
            self.state = "game_end"
            self.finished = True
        else:
            self.state = "kyoku_end"

    def next_kyoku(self) -> None:
        """Client sent NEXT_KYOKU_READY."""
        if self.state != "kyoku_end":
            return
        if self.advance_kyoku:
            self.kyoku_index += 1
        self.start_kyoku()

    # ------------------------------------------------------------------
    # client commands
    # ------------------------------------------------------------------

    def on_command(self, kind: int, pindex: int, pai: int, tepai_id: int,
                   tepai_id2: int, reach: int, tsumogiri: int) -> None:
        seat = self.human
        try:
            self._dispatch(kind, seat, pindex, pai, tepai_id, tepai_id2, reach, tsumogiri)
        except Exception:
            log.exception("table command failed kind=%s pai=%s", kind, pai)

    def _dispatch(self, kind: int, seat: int, pindex: int, pai: int, tepai_id: int,
                  tepai_id2: int, reach: int, tsumogiri: int) -> None:
        if kind == S_SUTE_PAI:
            # Be lenient about `state`: refusing a discard would hang the client
            # forever, so anything that leaves a legal 3n+2 hand is accepted.
            if self.state in ("kyoku_end", "game_end"):
                return
            if len(self.hands[seat]) % 3 != 2:
                return
            self.call_ctx = None
            self.pending_tsumo_choices = None
            tile = M.pai_to_idx(pai)
            if tile < 0:
                return
            self._do_discard(seat, tile, bool(reach), bool(tsumogiri))
        elif kind == S_TSUMO_AGARI:
            self.pending_tsumo_choices = None
            drawn = self.drawn[seat]
            res = self._win_result(seat, drawn, True)
            if res is None:
                self._offer_tsumo_choices(seat)
                return
            self._apply_tsumo(seat, drawn, res)
        elif kind == S_RON_AGARI:
            ctx = self.call_ctx
            if not ctx:
                return
            self.call_ctx = None
            tile = ctx["tile"]
            res = self._win_result(seat, tile, False, chankan=ctx.get("chankan", False))
            if res is None:
                self._decline_call(ctx)
                return
            self._apply_ron([seat], ctx["discarder"], tile, {seat: res})
        elif kind == S_PON:
            ctx = self.call_ctx
            if not ctx:
                return
            self.call_ctx = None
            own = [M.pai_to_idx(tepai_id), M.pai_to_idx(tepai_id2)]
            if any(o < 0 for o in own):
                own = [ctx["tile"], ctx["tile"]]
            self._apply_pon(seat, ctx["discarder"], ctx["tile"], own)
        elif kind == S_CHI:
            ctx = self.call_ctx
            if not ctx:
                return
            self.call_ctx = None
            own = [M.pai_to_idx(tepai_id), M.pai_to_idx(tepai_id2)]
            opts = self._chi_options(seat, ctx["tile"])
            if sorted(own) not in [sorted(o) for o in opts]:
                own = opts[0] if opts else []
            if not own:
                self._decline_call(ctx)
                return
            self._apply_chi(seat, ctx["discarder"], ctx["tile"], own)
        elif kind == S_MINKAN:
            ctx = self.call_ctx
            if not ctx:
                return
            self.call_ctx = None
            if self._minkan_ok(seat, ctx["tile"]):
                self._apply_minkan(seat, ctx["discarder"], ctx["tile"])
            else:
                self._decline_call(ctx)
        elif kind == S_ANKAN:
            self.pending_tsumo_choices = None
            tile = M.pai_to_idx(pai)
            if tile >= 0 and M.counts_of(self.hands[seat])[tile] == 4:
                self._apply_ankan(seat, tile)
            else:
                self._offer_tsumo_choices(seat)
        elif kind == S_KAKAN:
            self.pending_tsumo_choices = None
            tile = M.pai_to_idx(pai)
            if any(m.kind == M.PON and m.tiles[0] == tile for m in self.melds[seat]):
                self._apply_kakan(seat, tile)
            else:
                self._offer_tsumo_choices(seat)
        elif kind == S_KYUSYUKYUHAI:
            self.pending_tsumo_choices = None
            self._ryuukyoku(abortive=True)
        elif kind == S_NAKINASHI:
            ctx = self.call_ctx
            if not ctx:
                return
            self.call_ctx = None
            self._decline_call(ctx)
        elif kind == S_CYOUKOU:
            self._cell(K_TYOKO, "<pindex>%d</pindex>" % pindex)
        elif kind == S_NEXT_KYOKU_READY:
            self.next_kyoku()
        elif kind == S_KIKEN:
            self.state = "game_end"
            self.finished = True

    def _decline_call(self, ctx: Dict[str, Any]) -> None:
        seat = self.human
        if ctx.get("ron"):
            # missed ron: temporary furiten (permanent while riichi)
            self.temp_furiten[seat] = True
            c = M.counts_of(self.hands[seat])
            if sum(c) % 3 == 1 and ctx["tile"] in M.waits_of(
                    c, len(self.melds[seat]), self.taku):
                if self.riichi[seat]:
                    self.furiten[seat] = True
        self.state = "discard"
        if ctx.get("chankan"):
            self._resume_after_chankan(ctx["discarder"])
        else:
            self._cpu_calls(ctx["discarder"], ctx["tile"])

    # ------------------------------------------------------------------
    # result for /end_game
    # ------------------------------------------------------------------

    def result_rows(self) -> List[Tuple[int, int, int]]:
        """[(rank, score, uma)] per seat."""
        ranks = self._ranks()
        uma_table = {
            4: [20000, 10000, -10000, -20000],
            3: [20000, 0, -20000],
            2: [10000, -10000],
        }
        uma = uma_table.get(self.seats, [0, 0, 0, 0])
        rows = []
        for s in range(self.seats):
            r = ranks[s]
            rows.append((r, self.scores[s], uma[r] if r < len(uma) else 0))
        return rows
