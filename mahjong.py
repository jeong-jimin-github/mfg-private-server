#!/usr/bin/env python3
"""Mahjong rules engine for the VFG local server.

Tile ids follow MFG.Types.Pai: man 1-9, sou 11-19, pin 21-29, honours 31-37
(J1..J4 = E/S/W/N, J5..J7 = haku/hatsu/chun).  Red fives are the same id + 64.

Internally everything works on a 0..33 index (0-8 man, 9-17 sou, 18-26 pin,
27-33 honours) because that makes the set/shanten maths readable.
"""

from __future__ import annotations

import random
from functools import lru_cache
from typing import Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# tile helpers
# --------------------------------------------------------------------------

MAN, SOU, PIN, HON = 0, 9, 18, 27


def pai_norm(pai: int) -> int:
    """Strip the red-five marker."""
    p = int(pai)
    return p - 64 if p >= 64 else p


def pai_to_idx(pai: int) -> int:
    p = pai_norm(pai)
    if 1 <= p <= 9:
        return MAN + p - 1
    if 11 <= p <= 19:
        return SOU + p - 11
    if 21 <= p <= 29:
        return PIN + p - 21
    if 31 <= p <= 37:
        return HON + p - 31
    return -1


def idx_to_pai(idx: int) -> int:
    if idx < SOU:
        return 1 + idx
    if idx < PIN:
        return 11 + (idx - SOU)
    if idx < HON:
        return 21 + (idx - PIN)
    return 31 + (idx - HON)


def is_honor(idx: int) -> bool:
    return idx >= HON


def is_terminal(idx: int) -> bool:
    return (not is_honor(idx)) and (idx % 9 in (0, 8))


def is_yaochu(idx: int) -> bool:
    return is_honor(idx) or is_terminal(idx)


YAOCHU_IDX = tuple(i for i in range(34) if is_yaochu(i))
# souzu 2,3,4,6,8 + hatsu
GREEN_IDX = (SOU + 1, SOU + 2, SOU + 3, SOU + 5, SOU + 7, HON + 5)

# TakuType (MFG.Types.TakuType)
TONPU, HANCHAN, SANMA, NIMA = 0, 1, 2, 3
SEATS_OF = {TONPU: 4, HANCHAN: 4, SANMA: 3, NIMA: 2}
KYOKU_COUNT = {TONPU: 4, HANCHAN: 8, SANMA: 3, NIMA: 2}
START_SCORE = {TONPU: 25000, HANCHAN: 25000, SANMA: 35000, NIMA: 35000}


def live_kinds(taku: int) -> List[int]:
    """Tile kinds actually present in the wall for this table type."""
    if taku == NIMA:
        # no manzu at all, no west / north
        return [i for i in range(34) if i >= SOU and i not in (HON + 2, HON + 3)]
    if taku == SANMA:
        # manzu 2-8 removed
        return [i for i in range(34) if not (MAN + 1 <= i <= MAN + 7)]
    return list(range(34))


def build_wall(taku: int, rng: random.Random) -> List[int]:
    tiles: List[int] = []
    for idx in live_kinds(taku):
        tiles.extend([idx] * 4)
    rng.shuffle(tiles)
    return tiles


def dora_from_indicator(idx: int, taku: int) -> int:
    """Next tile in the dora cycle, honouring the reduced tile sets."""
    if is_honor(idx):
        n = idx - HON
        if n <= 3:  # winds
            if taku == NIMA:
                # only east / south exist, so south wraps back to east
                return HON + (0 if n == 1 else 1)
            return HON + ((n + 1) % 4)
        return HON + 4 + ((n - 4 + 1) % 3)
    suit, num = idx // 9, idx % 9
    if suit == 0 and taku == SANMA:
        # only M1 / M9 in play
        return MAN + (8 if num == 0 else 0)
    return suit * 9 + ((num + 1) % 9)


def counts_of(tiles: Sequence[int]) -> List[int]:
    c = [0] * 34
    for t in tiles:
        c[t] += 1
    return c


# --------------------------------------------------------------------------
# shanten / agari
# --------------------------------------------------------------------------


def _enum_group(c: List[int], i: int, melds: int, partials: int, pair: bool,
                out: set, runs: bool) -> None:
    """Enumerate (melds, partials, pair) options for one suit / the honours."""
    n = len(c)
    while i < n and c[i] == 0:
        i += 1
    if i >= n or melds + partials >= 5:
        out.add((melds, partials, pair))
        return
    if c[i] >= 3:
        c[i] -= 3
        _enum_group(c, i, melds + 1, partials, pair, out, runs)
        c[i] += 3
    if runs and i + 2 < n and c[i + 1] and c[i + 2]:
        c[i] -= 1
        c[i + 1] -= 1
        c[i + 2] -= 1
        _enum_group(c, i, melds + 1, partials, pair, out, runs)
        c[i] += 1
        c[i + 1] += 1
        c[i + 2] += 1
    if c[i] >= 2:
        if not pair:
            c[i] -= 2
            _enum_group(c, i, melds, partials, True, out, runs)
            c[i] += 2
        c[i] -= 2
        _enum_group(c, i, melds, partials + 1, pair, out, runs)
        c[i] += 2
    if runs and i + 1 < n and c[i + 1]:
        c[i] -= 1
        c[i + 1] -= 1
        _enum_group(c, i, melds, partials + 1, pair, out, runs)
        c[i] += 1
        c[i + 1] += 1
    if runs and i + 2 < n and c[i + 2]:
        c[i] -= 1
        c[i + 2] -= 1
        _enum_group(c, i, melds, partials + 1, pair, out, runs)
        c[i] += 1
        c[i + 2] += 1
    saved = c[i]
    c[i] = 0
    _enum_group(c, i + 1, melds, partials, pair, out, runs)
    c[i] = saved


def _pareto(options) -> Tuple[Tuple[int, int, int], ...]:
    """Drop options that are dominated on every axis."""
    opts = sorted(options, key=lambda o: (-o[0], -o[1], -int(o[2])))
    keep: List[Tuple[int, int, int]] = []
    for o in opts:
        dominated = False
        for k in keep:
            if k[0] >= o[0] and k[1] >= o[1] and int(k[2]) >= int(o[2]):
                dominated = True
                break
        if not dominated:
            keep.append((o[0], o[1], int(o[2])))
    return tuple(keep)


@lru_cache(maxsize=None)
def _group_options(key: Tuple[int, ...], runs: bool) -> Tuple[Tuple[int, int, int], ...]:
    out: set = set()
    _enum_group(list(key), 0, 0, 0, False, out, runs)
    return _pareto(out)


@lru_cache(maxsize=300000)
def _shanten_std_cached(key: Tuple[int, ...], open_melds: int) -> int:
    groups = (
        _group_options(key[0:9], True),
        _group_options(key[9:18], True),
        _group_options(key[18:27], True),
        _group_options(key[27:34], False),
    )
    cur = {(0, 0, 0)}
    for opts in groups:
        nxt = set()
        for m, p, pr in cur:
            for m2, p2, pr2 in opts:
                if pr and pr2:
                    continue
                nm = m + m2
                if nm > 4:
                    nm = 4
                np_ = p + p2
                if np_ > 4:
                    np_ = 4
                nxt.add((nm, np_, pr | pr2))
        cur = set(_pareto(nxt))
    best = 99
    for m, p, pr in cur:
        tm = m + open_melds
        if tm > 4:
            tm = 4
        pp = p
        if tm + pp > 4:
            pp = 4 - tm
        v = (4 - tm) * 2 - pp - (1 if pr else 0)
        if v < best:
            best = v
    return best


def shanten_standard(counts: Sequence[int], open_melds: int = 0) -> int:
    return _shanten_std_cached(tuple(counts), open_melds)


def shanten_chiitoi(counts: Sequence[int]) -> int:
    pairs = sum(1 for n in counts if n >= 2)
    kinds = sum(1 for n in counts if n >= 1)
    return 6 - pairs + max(0, 7 - kinds)


def shanten_kokushi(counts: Sequence[int]) -> int:
    kinds = sum(1 for i in YAOCHU_IDX if counts[i] >= 1)
    has_pair = any(counts[i] >= 2 for i in YAOCHU_IDX)
    return 13 - kinds - (1 if has_pair else 0)


def shanten(counts: Sequence[int], open_melds: int = 0, taku: int = TONPU) -> int:
    best = shanten_standard(counts, open_melds)
    if open_melds == 0:
        best = min(best, shanten_chiitoi(counts))
        if taku != NIMA:
            best = min(best, shanten_kokushi(counts))
    return best


def is_agari(counts: Sequence[int], open_melds: int = 0, taku: int = TONPU) -> bool:
    return shanten(counts, open_melds, taku) < 0


def waits_of(counts: Sequence[int], open_melds: int = 0, taku: int = TONPU) -> List[int]:
    """Tile indices that complete this (3n+1) hand."""
    c = list(counts)
    out = []
    for t in live_kinds(taku):
        if c[t] >= 4:
            continue
        c[t] += 1
        if is_agari(c, open_melds, taku):
            out.append(t)
        c[t] -= 1
    return out


def ukeire(counts: Sequence[int], open_melds: int, taku: int, seen: Sequence[int]) -> int:
    """How many tiles still advance the hand (used by the CPU AI)."""
    cur = shanten(counts, open_melds, taku)
    c = list(counts)
    total = 0
    for t in live_kinds(taku):
        if c[t] >= 4:
            continue
        c[t] += 1
        if shanten(c, open_melds, taku) < cur:
            total += max(0, 4 - seen[t])
        c[t] -= 1
    return total


# --------------------------------------------------------------------------
# hand decomposition (for yaku / fu)
# --------------------------------------------------------------------------

KOTSU, SHUNTSU = 0, 1


def _decompose(c: List[int], i: int, sets: List[Tuple[int, int]], out: List[List[Tuple[int, int]]]) -> None:
    while i < 34 and c[i] == 0:
        i += 1
    if i >= 34:
        out.append(list(sets))
        return
    if c[i] >= 3:
        c[i] -= 3
        sets.append((KOTSU, i))
        _decompose(c, i, sets, out)
        sets.pop()
        c[i] += 3
    if i < HON and (i % 9) <= 6 and c[i + 1] and c[i + 2]:
        c[i] -= 1
        c[i + 1] -= 1
        c[i + 2] -= 1
        sets.append((SHUNTSU, i))
        _decompose(c, i, sets, out)
        sets.pop()
        c[i] += 1
        c[i + 1] += 1
        c[i + 2] += 1


def decompositions(counts: Sequence[int]) -> List[Tuple[int, List[Tuple[int, int]]]]:
    """All (pair, sets) splits of a closed part holding 3n+2 tiles."""
    res: List[Tuple[int, List[Tuple[int, int]]]] = []
    c = list(counts)
    for p in range(34):
        if c[p] < 2:
            continue
        c[p] -= 2
        need = sum(c) // 3
        out: List[List[Tuple[int, int]]] = []
        _decompose(c, 0, [], out)
        for sets in out:
            if len(sets) == need:
                res.append((p, sets))
        c[p] += 2
    return res


# --------------------------------------------------------------------------
# melds
# --------------------------------------------------------------------------

PON, CHI, ANKAN, MINKAN, KAKAN = "pon", "chi", "ankan", "minkan", "kakan"
# MFG FuroData.MentsuType
MENTSU_TYPE = {CHI: 1, PON: 2, ANKAN: 3, MINKAN: 4, KAKAN: 5}


class Meld:
    __slots__ = ("kind", "base", "tiles", "called", "from_seat")

    def __init__(self, kind: str, tiles: List[int], called: int = -1, from_seat: int = -1):
        self.kind = kind
        self.tiles = list(tiles)
        self.called = called
        self.from_seat = from_seat
        self.base = min(tiles)

    @property
    def is_kan(self) -> bool:
        return self.kind in (ANKAN, MINKAN, KAKAN)

    @property
    def is_open(self) -> bool:
        return self.kind != ANKAN

    @property
    def is_concealed_triplet(self) -> bool:
        return self.kind == ANKAN

    def as_kotsu(self) -> Optional[Tuple[int, int]]:
        if self.kind in (PON, ANKAN, MINKAN, KAKAN):
            return (KOTSU, self.tiles[0])
        return None

    def to_dict(self) -> Dict:
        return {
            "kind": self.kind,
            "tiles": list(self.tiles),
            "called": self.called,
            "from_seat": self.from_seat,
        }

    @staticmethod
    def from_dict(d: Dict) -> "Meld":
        return Meld(d["kind"], d["tiles"], d.get("called", -1), d.get("from_seat", -1))


# --------------------------------------------------------------------------
# yaku
# --------------------------------------------------------------------------

# bit index == MFG.Types.Yaku ordinal
Y = {
    "Tenho": 0, "Chiho": 1, "Renho": 2, "Tyuren9": 3, "TyurenTanki": 4,
    "Chinroto": 5, "Tsuiso": 6, "Ryuiso": 7, "Kokushi13": 8, "KokushiTanki": 9,
    "Sukantsu": 10, "Suanko": 11, "SuankoTanki": 12, "Daisushi": 13,
    "Syosushi": 14, "Daisangen": 15, "Daisyarin": 16, "Surenko": 17,
    "Parenchan": 18, "Kazoeyakuman": 19, "Chiniso": 20, "Honroto": 21,
    "Syosangen": 22, "Nagashimangan": 23, "Shisanputo": 24, "Junchan": 25,
    "Ryanpeko": 26, "Honiso": 27, "DoubleRichi": 28, "Sankantsu": 29,
    "Sananko": 30, "Toitoiho": 31, "Chitoitsu": 32, "Sansyokudoko": 33,
    "Sansyokudojun": 34, "Chanta": 35, "Ikkitsukan": 36, "Sanrenko": 37,
    "Haitei": 38, "Hotei": 39, "Chankan": 40, "Rinsyan": 41, "Ipeiko": 42,
    "Tanyao": 43, "Pinfu": 44, "Richi": 45, "Ippatsu": 46, "Menzen": 47,
    "Haku": 48, "Hatsu": 49, "Tyun": 50, "Bakaze": 51, "Jikaze": 52,
    "Dora": 53, "ChinisoNaki": 54, "JunchanNaki": 55, "HonisoNaki": 56,
    "SansyokudojunNaki": 57, "ChantaNaki": 58, "IkkitsukanNaki": 59,
}

# han value per yaku (closed value; the *Naki bits carry the reduced value)
YAKU_HAN = {
    "Tanyao": 1, "Pinfu": 1, "Ipeiko": 1, "Richi": 1, "Ippatsu": 1,
    "Menzen": 1, "Haku": 1, "Hatsu": 1, "Tyun": 1, "Bakaze": 1, "Jikaze": 1,
    "Haitei": 1, "Hotei": 1, "Chankan": 1, "Rinsyan": 1,
    "Chitoitsu": 2, "Toitoiho": 2, "Sananko": 2, "Sankantsu": 2,
    "Sansyokudoko": 2, "DoubleRichi": 2, "Syosangen": 2, "Honroto": 2,
    "Sansyokudojun": 2, "Ikkitsukan": 2, "Chanta": 2,
    "SansyokudojunNaki": 1, "IkkitsukanNaki": 1, "ChantaNaki": 1,
    "Junchan": 3, "JunchanNaki": 2, "Honiso": 3, "HonisoNaki": 2,
    "Ryanpeko": 3, "Chiniso": 6, "ChinisoNaki": 5,
}
YAKUMAN = {
    "Tenho": 1, "Chiho": 1, "Kokushi13": 2, "KokushiTanki": 1, "Suanko": 1,
    "SuankoTanki": 2, "Daisangen": 1, "Syosushi": 1, "Daisushi": 2,
    "Tsuiso": 1, "Chinroto": 1, "Ryuiso": 1, "Tyuren9": 1, "TyurenTanki": 2,
    "Sukantsu": 1,
}


class WinContext:
    """Everything the scorer needs about one winning hand."""

    def __init__(
        self,
        hand: Sequence[int],
        melds: Sequence[Meld],
        win_tile: int,
        is_tsumo: bool,
        seat_wind: int,
        round_wind: int,
        riichi: bool = False,
        double_riichi: bool = False,
        ippatsu: bool = False,
        haitei: bool = False,
        houtei: bool = False,
        rinshan: bool = False,
        chankan: bool = False,
        tenho: bool = False,
        chiho: bool = False,
        dora_indicators: Sequence[int] = (),
        ura_indicators: Sequence[int] = (),
        taku: int = TONPU,
    ):
        # `hand` includes the winning tile
        self.hand = list(hand)
        self.melds = list(melds)
        self.win_tile = win_tile
        self.is_tsumo = is_tsumo
        self.seat_wind = seat_wind
        self.round_wind = round_wind
        self.riichi = riichi
        self.double_riichi = double_riichi
        self.ippatsu = ippatsu
        self.haitei = haitei
        self.houtei = houtei
        self.rinshan = rinshan
        self.chankan = chankan
        self.tenho = tenho
        self.chiho = chiho
        self.dora_indicators = list(dora_indicators)
        self.ura_indicators = list(ura_indicators)
        self.taku = taku

    @property
    def menzen(self) -> bool:
        return all(m.kind == ANKAN for m in self.melds)


def _all_sets(pair: int, sets: List[Tuple[int, int]], melds: Sequence[Meld]) -> List[Tuple[int, int]]:
    full = list(sets)
    for m in melds:
        if m.kind == CHI:
            full.append((SHUNTSU, m.base))
        else:
            full.append((KOTSU, m.tiles[0]))
    return full


def _kokushi(ctx: WinContext) -> Optional[Tuple[int, int, int, int]]:
    c = counts_of(ctx.hand)
    if ctx.melds or sum(c) != 14:
        return None
    if any(c[i] for i in range(34) if not is_yaochu(i)):
        return None
    if not all(c[i] >= 1 for i in YAOCHU_IDX):
        return None
    # 13-wait form: the hand minus the winning tile is exactly one of each
    minus = list(c)
    minus[ctx.win_tile] -= 1
    thirteen = all(minus[i] == 1 for i in YAOCHU_IDX)
    bits = 0
    if thirteen:
        bits |= 1 << Y["Kokushi13"]
        return bits, 2, 25, 2
    bits |= 1 << Y["KokushiTanki"]
    return bits, 1, 25, 1


def _chiitoi_bits(ctx: WinContext) -> Optional[Tuple[int, List[int]]]:
    c = counts_of(ctx.hand)
    if ctx.melds or sum(c) != 14:
        return None
    if sum(1 for n in c if n == 2) != 7:
        return None
    return 0, [i for i, n in enumerate(c) if n == 2]


def _yakuhai_bits(ctx: WinContext, sets: List[Tuple[int, int]]) -> Tuple[int, int]:
    bits, han = 0, 0
    for kind, base in sets:
        if kind != KOTSU or not is_honor(base):
            continue
        n = base - HON
        if n == 4:
            bits |= 1 << Y["Haku"]
            han += 1
        elif n == 5:
            bits |= 1 << Y["Hatsu"]
            han += 1
        elif n == 6:
            bits |= 1 << Y["Tyun"]
            han += 1
        else:
            if n == ctx.round_wind:
                bits |= 1 << Y["Bakaze"]
                han += 1
            if n == ctx.seat_wind:
                bits |= 1 << Y["Jikaze"]
                han += 1
    return bits, han


def _fu_for(ctx: WinContext, pair: int, sets: List[Tuple[int, int]], pinfu: bool, menzen: bool) -> int:
    if pinfu:
        return 20 if ctx.is_tsumo else 30
    fu = 20
    # melds
    for m in ctx.melds:
        if m.kind == CHI:
            continue
        base = m.tiles[0]
        val = 2
        if m.is_kan:
            val = 8
        if m.kind == ANKAN:
            val *= 2
        elif not m.is_kan and m.kind == PON:
            val = 2
        if is_yaochu(base):
            val *= 2
        fu += val
    # concealed part
    closed_counts = counts_of(ctx.hand)
    for kind, base in sets:
        if kind != KOTSU:
            continue
        # a triplet completed by ron counts as open
        concealed = not (not ctx.is_tsumo and base == ctx.win_tile and closed_counts[base] == 3)
        val = 4 if concealed else 2
        if is_yaochu(base):
            val *= 2
        fu += val
    # pair
    if is_honor(pair):
        n = pair - HON
        if n >= 4:
            fu += 2
        else:
            if n == ctx.round_wind:
                fu += 2
            if n == ctx.seat_wind:
                fu += 2
    # wait shape
    fu += _wait_fu(ctx, pair, sets)
    if ctx.is_tsumo:
        fu += 2
    elif menzen:
        fu += 10
    return ((fu + 9) // 10) * 10


def _wait_fu(ctx: WinContext, pair: int, sets: List[Tuple[int, int]]) -> int:
    w = ctx.win_tile
    if pair == w:
        return 2  # tanki
    best = 99
    for kind, base in sets:
        if kind == KOTSU:
            if base == w:
                best = min(best, 0)
            continue
        if base <= w <= base + 2:
            if w == base + 1:
                best = min(best, 2)  # kanchan
            elif (base % 9 == 0 and w == base + 2) or (base % 9 == 6 and w == base):
                best = min(best, 2)  # penchan
            else:
                best = min(best, 0)
    return 0 if best == 99 else best


def _is_pinfu(ctx: WinContext, pair: int, sets: List[Tuple[int, int]]) -> bool:
    if not ctx.menzen or any(m.kind == ANKAN for m in ctx.melds):
        return False
    if any(k == KOTSU for k, _ in sets):
        return False
    if is_honor(pair):
        n = pair - HON
        if n >= 4 or n == ctx.round_wind or n == ctx.seat_wind:
            return False
    # winning tile must complete a two-sided run
    for kind, base in sets:
        if kind != SHUNTSU:
            continue
        if base == ctx.win_tile and not (base % 9 == 6):
            return True
        if base + 2 == ctx.win_tile and not (base % 9 == 0):
            return True
    return False


def evaluate(ctx: WinContext) -> Dict:
    """Return {bits, han, fu, han_rank, dora, yakuman}."""
    kok = _kokushi(ctx)
    if kok:
        bits, ym, fu, _ = kok
        return _finish(ctx, bits, 0, fu, ym, 0)

    best: Optional[Dict] = None

    chi = _chiitoi_bits(ctx)
    if chi is not None:
        bits = 1 << Y["Chitoitsu"]
        han = YAKU_HAN["Chitoitsu"]
        extra, ehan, ym = _common_bits(ctx, None, [], chiitoi=True)
        bits |= extra
        han += ehan
        best = _finish(ctx, bits, han, 25, ym, _dora_count(ctx))

    closed = list(ctx.hand)
    ccounts = counts_of(closed)
    for pair, sets in decompositions(ccounts):
        full = _all_sets(pair, sets, ctx.melds)
        if len(full) != 4:
            continue
        bits = 0
        han = 0
        pinfu = _is_pinfu(ctx, pair, sets)
        if pinfu:
            bits |= 1 << Y["Pinfu"]
            han += 1
        yb, yh = _yakuhai_bits(ctx, full)
        bits |= yb
        han += yh
        extra, ehan, ym = _common_bits(ctx, pair, full)
        bits |= extra
        han += ehan
        # closed-hand shape yaku
        sb, sh = _shape_bits(ctx, pair, sets, full)
        bits |= sb
        han += sh
        fu = _fu_for(ctx, pair, sets, pinfu, ctx.menzen)
        cand = _finish(ctx, bits, han, fu, ym, _dora_count(ctx))
        if best is None or _better(cand, best):
            best = cand
    if best is None:
        # should not happen, but never crash the table
        best = _finish(ctx, 0, 1, 30, 0, _dora_count(ctx))
    return best


def _better(a: Dict, b: Dict) -> bool:
    if a["yakuman"] != b["yakuman"]:
        return a["yakuman"] > b["yakuman"]
    if a["han"] != b["han"]:
        return a["han"] > b["han"]
    return a["fu"] > b["fu"]


def _common_bits(ctx: WinContext, pair, sets, chiitoi: bool = False) -> Tuple[int, int, int]:
    """Yaku that do not depend on the concealed-set split."""
    bits, han, yakuman = 0, 0, 0
    menzen = ctx.menzen
    if ctx.riichi:
        if ctx.double_riichi:
            bits |= 1 << Y["DoubleRichi"]
            han += 2
        else:
            bits |= 1 << Y["Richi"]
            han += 1
        if ctx.ippatsu:
            bits |= 1 << Y["Ippatsu"]
            han += 1
    if menzen and ctx.is_tsumo:
        bits |= 1 << Y["Menzen"]
        han += 1
    if ctx.haitei:
        bits |= 1 << Y["Haitei"]
        han += 1
    if ctx.houtei:
        bits |= 1 << Y["Hotei"]
        han += 1
    if ctx.rinshan:
        bits |= 1 << Y["Rinsyan"]
        han += 1
    if ctx.chankan:
        bits |= 1 << Y["Chankan"]
        han += 1

    all_tiles = list(ctx.hand)
    for m in ctx.melds:
        all_tiles.extend(m.tiles)
    cnt = counts_of(all_tiles)

    if not any(cnt[i] for i in YAOCHU_IDX):
        bits |= 1 << Y["Tanyao"]
        han += 1

    suits = set()
    honors = False
    for i, n in enumerate(cnt):
        if not n:
            continue
        if is_honor(i):
            honors = True
        else:
            suits.add(i // 9)
    if len(suits) == 1 and not honors:
        if menzen:
            bits |= 1 << Y["Chiniso"]
            han += 6
        else:
            bits |= 1 << Y["ChinisoNaki"]
            han += 5
    elif len(suits) <= 1 and honors:
        if menzen:
            bits |= 1 << Y["Honiso"]
            han += 3
        else:
            bits |= 1 << Y["HonisoNaki"]
            han += 2

    kans = sum(1 for m in ctx.melds if m.is_kan)
    if kans == 3:
        bits |= 1 << Y["Sankantsu"]
        han += 2
    elif kans == 4:
        bits |= 1 << Y["Sukantsu"]
        yakuman += 1

    if not honors and all(is_terminal(i) for i, n in enumerate(cnt) if n):
        bits |= 1 << Y["Chinroto"]
        yakuman += 1
    elif all(is_honor(i) for i, n in enumerate(cnt) if n):
        bits |= 1 << Y["Tsuiso"]
        yakuman += 1
    elif all(is_yaochu(i) for i, n in enumerate(cnt) if n):
        bits |= 1 << Y["Honroto"]
        han += 2
    if all(i in GREEN_IDX for i, n in enumerate(cnt) if n):
        bits |= 1 << Y["Ryuiso"]
        yakuman += 1

    dragons = [HON + 4, HON + 5, HON + 6]
    trip = sum(1 for d in dragons if cnt[d] >= 3)
    pairs = sum(1 for d in dragons if cnt[d] == 2)
    if trip == 3:
        bits |= 1 << Y["Daisangen"]
        yakuman += 1
    elif trip == 2 and pairs == 1:
        bits |= 1 << Y["Syosangen"]
        han += 2

    winds = [HON + i for i in range(4)]
    wtrip = sum(1 for d in winds if cnt[d] >= 3)
    wpair = sum(1 for d in winds if cnt[d] == 2)
    if wtrip == 4:
        bits |= 1 << Y["Daisushi"]
        yakuman += 2
    elif wtrip == 3 and wpair == 1:
        bits |= 1 << Y["Syosushi"]
        yakuman += 1

    if menzen and not chiitoi and len(suits) == 1 and not honors:
        base = min(suits) * 9
        pat = [3, 1, 1, 1, 1, 1, 1, 1, 3]
        diff = [cnt[base + k] - pat[k] for k in range(9)]
        if all(d >= 0 for d in diff) and sum(diff) == 1:
            k = diff.index(1)
            if base + k == ctx.win_tile:
                bits |= 1 << Y["TyurenTanki"]
                yakuman += 2
            else:
                bits |= 1 << Y["Tyuren9"]
                yakuman += 1

    if ctx.tenho:
        bits |= 1 << Y["Tenho"]
        yakuman += 1
    elif ctx.chiho:
        bits |= 1 << Y["Chiho"]
        yakuman += 1

    return bits, han, yakuman


def _shape_bits(ctx: WinContext, pair: int, closed_sets, full_sets) -> Tuple[int, int]:
    bits, han = 0, 0
    menzen = ctx.menzen
    runs = [b for k, b in full_sets if k == SHUNTSU]
    trips = [b for k, b in full_sets if k == KOTSU]

    # iipeiko / ryanpeiko (closed only)
    if menzen:
        closed_runs = [b for k, b in closed_sets if k == SHUNTSU]
        dup = 0
        for b in set(closed_runs):
            dup += closed_runs.count(b) // 2
        if dup >= 2:
            bits |= 1 << Y["Ryanpeko"]
            han += 3
        elif dup == 1:
            bits |= 1 << Y["Ipeiko"]
            han += 1

    # sanshoku doujun
    for b in runs:
        if b >= HON:
            continue
        n = b % 9
        if all(any(r == s * 9 + n for r in runs) for s in range(3)):
            if menzen:
                bits |= 1 << Y["Sansyokudojun"]
                han += 2
            else:
                bits |= 1 << Y["SansyokudojunNaki"]
                han += 1
            break

    # ittsu
    for s in range(3):
        if all((s * 9 + k) in runs for k in (0, 3, 6)):
            if menzen:
                bits |= 1 << Y["Ikkitsukan"]
                han += 2
            else:
                bits |= 1 << Y["IkkitsukanNaki"]
                han += 1
            break

    # sanshoku doukou
    for b in trips:
        if b >= HON:
            continue
        n = b % 9
        if all(any(t == s * 9 + n for t in trips) for s in range(3)):
            bits |= 1 << Y["Sansyokudoko"]
            han += 2
            break

    # toitoi / ankou count
    if len(trips) == 4:
        bits |= 1 << Y["Toitoiho"]
        han += 2
    closed_counts = counts_of(ctx.hand)
    ankou = sum(1 for m in ctx.melds if m.kind == ANKAN)
    for k, b in closed_sets:
        if k != KOTSU:
            continue
        if not ctx.is_tsumo and b == ctx.win_tile and closed_counts[b] == 3:
            continue
        ankou += 1
    if ankou >= 4:
        if pair == ctx.win_tile:
            bits |= 1 << Y["SuankoTanki"]
        else:
            bits |= 1 << Y["Suanko"]
    elif ankou == 3:
        bits |= 1 << Y["Sananko"]
        han += 2

    # chanta / junchan (already covers honroutou separately)
    blocks = list(full_sets)
    def touches(kind_base):
        k, b = kind_base
        if k == KOTSU:
            return is_yaochu(b)
        return b % 9 == 0 or b % 9 == 6
    if all(touches(x) for x in blocks) and is_yaochu(pair):
        has_run = any(k == SHUNTSU for k, _ in blocks)
        has_honor = is_honor(pair) or any(is_honor(b) for k, b in blocks if k == KOTSU)
        if has_run:
            if has_honor:
                if menzen:
                    bits |= 1 << Y["Chanta"]
                    han += 2
                else:
                    bits |= 1 << Y["ChantaNaki"]
                    han += 1
            else:
                if menzen:
                    bits |= 1 << Y["Junchan"]
                    han += 3
                else:
                    bits |= 1 << Y["JunchanNaki"]
                    han += 2
    return bits, han


def _dora_count(ctx: WinContext) -> int:
    tiles = list(ctx.hand)
    for m in ctx.melds:
        tiles.extend(m.tiles)
    cnt = counts_of(tiles)
    total = 0
    for ind in ctx.dora_indicators:
        total += cnt[dora_from_indicator(ind, ctx.taku)]
    if ctx.riichi:
        for ind in ctx.ura_indicators:
            total += cnt[dora_from_indicator(ind, ctx.taku)]
    return total


def han_rank(han: int, fu: int, yakuman: int) -> int:
    """MFG.Types.Han value."""
    if yakuman > 0:
        return 9 + yakuman - 1
    if han >= 13:
        return 9
    if han >= 11:
        return 8
    if han >= 8:
        return 7
    if han >= 6:
        return 6
    if han >= 5:
        return 5
    if base_score(han, fu) >= 2000:
        return 5
    return han


def base_score(han: int, fu: int) -> int:
    if han >= 13:
        return 8000
    if han >= 11:
        return 6000
    if han >= 8:
        return 4000
    if han >= 6:
        return 3000
    if han >= 5:
        return 2000
    v = fu << (han + 2)
    return 2000 if v >= 1920 else v


def base_score_rank(rank: int, fu: int) -> int:
    """Same as MahjongUtility.GetBaseScore, keyed on the Han rank."""
    if rank >= 9:
        return 8000 * (rank - 9 + 1)
    if rank == 8:
        return 6000
    if rank == 7:
        return 4000
    if rank == 6:
        return 3000
    if rank == 5:
        return 2000
    v = fu << (rank + 2)
    return 2000 if v >= 1920 else v


def _roundup100(v: int) -> int:
    return v // 100 * 100 + (100 if v % 100 else 0)


def payments(taku: int, rank: int, fu: int, is_oya: bool, is_tsumo: bool) -> Tuple[int, int, int]:
    """(total, ko_payment, oya_payment) - mirrors MahjongUtility.GetScore."""
    b = base_score_rank(rank, fu)
    n4 = _roundup100((6 if is_oya else 4) * b)
    if taku == NIMA:
        return (n4, n4, n4)
    if taku == SANMA:
        ko = _roundup100(n4 // 2)
        oya = ko
        total = n4 if not is_tsumo else (2 * ko if is_oya else oya + ko)
        return (total, ko, oya)
    ko = _roundup100((2 if is_oya else 1) * b)
    oya = _roundup100(2 * b)
    total = n4 if not is_tsumo else (3 * ko if is_oya else oya + 2 * ko)
    return (total, ko, oya)


def _finish(ctx: WinContext, bits: int, han: int, fu: int, yakuman: int, dora: int) -> Dict:
    total_han = han + dora
    if yakuman == 0 and total_han >= 13:
        bits |= 1 << Y["Kazoeyakuman"]
    if dora:
        bits |= 1 << Y["Dora"]
    rank = han_rank(total_han, fu, yakuman)
    return {
        "bits": bits,
        "han": total_han,
        "yaku_han": han,
        "fu": fu,
        "dora": dora,
        "yakuman": yakuman,
        "rank": rank,
    }


# yaku bits that on their own do not make a hand valid
_NO_YAKU_BITS = (1 << Y["Dora"]) | (1 << Y["Kazoeyakuman"])


def has_yaku(result: Dict) -> bool:
    return result["yakuman"] > 0 or (result["bits"] & ~_NO_YAKU_BITS) != 0


def score_hand(ctx: WinContext) -> Optional[Dict]:
    res = evaluate(ctx)
    if not has_yaku(res):
        return None
    return res
