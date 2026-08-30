"""Rebuild data/gacha_pools.json from the VFG dump.

/gacha_info used to advertise every series with an empty <items>, empty
<pickup_charas> and empty <custom_pickup_items>. That is not a state the client
tolerates:

  * GachaSelectBgMovie.Play() takes movies[0] from
    GachaSeriesServerInfo.GetGachaSelectMovies(). For a Pickup series whose
    PickupKind is not NewGirl that list is seeded with the literal "CutinPlay"
    and then extended once per pickup character - with no pickup characters the
    list is exactly ["CutinPlay"]. Play() therefore calls
    PlayCutIn(GetSpecialPickupItem()), which is empty for the same reason, so
    PlayCutIn falls straight through to BgMovie_Change(), which wraps
    bgMovieIndex back to 0, reads "CutinPlay" again and calls PlayCutIn again.
    Neither hop awaits anything, so the two methods recurse synchronously until
    the stack dies and spice64.exe disappears with no managed exception. That is
    the "pick a gacha type and the game closes" crash.
  * GachaResultInfo.SetItemInfos draws locally through
    GachaSeriesServerInfo.GenerateGachaItemID, which indexes the
    except-pickup pool by the rolled rarity (`dictionary2[rarity]`). An empty -
    or single-character - pool raises KeyNotFoundException on the first roll of
    a rarity it is missing, so every series needs N/R/SR/UR outside its pickup
    set (UR is 2%, so "rare enough to ignore" it is not).

So the server has to send real pools. Everything needed is already in the dump:

  CutinItemMaster.cs          every cutin item (ObjectID, CharaType, Rarity)
  GachaSeriesServerInfo.cs    GachaExtraMasters (PickupKind, LimitedCharaList)
                              and CharaUnlockItemIDDict
  GachaSeriesName.cs          series id <-> series name
  ItemIDExtentions.cs         s_gachaNoDic, the reach-song pool per character
  StreamingAssets/aa/catalog.json
                              Characters/Character<N>/NewGirl/<SeriesName>/...,
                              which is the dump's own record of who each banner
                              features

Character identity comes from the catalog as well: asset paths are
Characters/Character<(int)CharaType + 1>/... and carry the character's name, so
Character1/3D/.../HiyoriAngerEnd.anim pins Hiyori to CharaType.Chara01.

Usage:
    python tools/extract_gacha_pools.py                # rewrite data/gacha_pools.json
    python tools/extract_gacha_pools.py --print        # dump the summary only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSHARP = ROOT / "verification" / "decompiled-csharp"
CATALOG = ROOT.parent / "game" / "MFGClient_Data" / "StreamingAssets" / "aa" / "catalog.json"
OUT = ROOT / "data" / "gacha_pools.json"

# Names as they appear inside asset paths, mapped to the CharaType they sit
# under in the Addressables catalog. Both romanisations KONAMI shipped are kept
# (Tumire/Tsumire, Toitoi/Toytoy, Pain/Pine) because series labels use either.
CHARA_NAMES = {
    "Hiyori": "Chara01",
    "Sen": "Chara02",
    "Tumire": "Chara03",
    "Tsumire": "Chara03",
    "Tenshi": "Chara04",
    "Yao": "Chara05",
    "Mitsuba": "Chara06",
    "Toitoi": "Chara07",
    "Toytoy": "Chara07",
    "Musashi": "Chara08",
    "Pine": "Chara09",
    "Pain": "Chara09",
    "Shiori": "Chara10",
    "Chaos": "Chara11",
    "Clear": "Chara12",
    "Iyo": "Chara13",
    "GrimAroe": "Chara14",
    "Cocoa": "Chara15",
    "Dia": "Chara16",
    "Doubriel": "Chara17",
    "Ippatsu": "Chara18",
    "Shiroe": "Chara19",
}

RARITIES = ("N", "R", "SR", "UR")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_cutin_items() -> list[dict]:
    """(ObjectID, CharaType, Rarity) for every entry of CutinItemMaster._items."""
    src = read(CSHARP / "MFG.MasterGameData" / "CutinItemMaster.cs")
    body = src[src.index("private static List<Item> _items = new List<Item>"):]
    items = []
    for block in re.finditer(r"new Item\s*\{(.*?)\n\t\t\}", body, re.S):
        text = block.group(1)
        oid = re.search(r'ObjectID\s*=\s*"([^"]+)"', text)
        chara = re.search(r"CharaType\s*=\s*CharaType\.(Chara\d+)", text)
        rarity = re.search(r"Rarity\s*=\s*Rarity\.(\w+)", text)
        if not (oid and chara and rarity):
            continue
        items.append({"oid": oid.group(1), "chara": chara.group(1), "rarity": rarity.group(1)})
    if len(items) < 400:
        sys.exit(f"CutinItemMaster parse looks wrong: {len(items)} items")
    return items


def parse_series_names() -> dict[int, str]:
    src = read(CSHARP / "MFG.GameData" / "GachaSeriesName.cs")
    body = src[src.index("{", src.index("enum GachaSeriesName")):]
    names = re.findall(r"^\t(\w+),?$", body, re.M)
    return {i: n for i, n in enumerate(names)}


def parse_extra_masters() -> dict[str, dict]:
    """PickupKind / IsReturens / LimitedCharaList per GachaSeriesName."""
    src = read(CSHARP / "MFG.GameData" / "GachaSeriesServerInfo.cs")
    body = src[src.index("GachaExtraMasters = new List<GachaExtraMasterData>"):]
    body = body[: body.index("public string AddressableAssetPath")]
    out = {}
    for block in re.finditer(r"new GachaExtraMasterData\s*\{(.*?)\n\t\t\}", body, re.S):
        text = block.group(1)
        name = re.search(r"GachaSeriesName\s*=\s*GachaSeriesName\.(\w+)", text)
        if not name:
            continue
        kind = re.search(r"PickupKind\s*=\s*PickupKind\.(\w+)", text)
        out[name.group(1)] = {
            "pickup_kind": kind.group(1) if kind else "None",
            "is_returns": "IsReturens = true" in text,
            "limited_charas": re.findall(r"CharaType\.(Chara\d+)", text),
        }
    return out


def parse_unlock_items() -> dict[str, str]:
    """CharaUnlockItemIDDict, as OIDs (Item12AgariSR01 -> OID_12AgariSR01)."""
    src = read(CSHARP / "MFG.GameData" / "GachaSeriesServerInfo.cs")
    body = src[src.index("CharaUnlockItemIDDict = new Dictionary"):]
    body = body[: body.index("\n\t};")]
    pairs = re.findall(r"CharaType\.(Chara\d+),\s*\n\s*[\w.]*ItemID\.Item(\d+)(\w+)", body)
    return {chara: f"OID_{num}{rest}" for chara, num, rest in pairs}


def parse_music_pools() -> dict[str, list[str]]:
    """s_gachaNoDic: reach-song OIDs per character, best item last."""
    src = read(CSHARP / "MFG.Types.Customize.CustomizeItem" / "ItemIDExtentions.cs")
    body = src[src.index("s_gachaNoDic"):]
    body = body[: body.index("\n\t};")]
    pairs = re.findall(r"ItemID\.(\w+),\s*\n\s*\(CharaType\.(Chara\d+),\s*(\d+)\)", body)
    per_chara: dict[str, list] = defaultdict(list)
    for item, chara, no in pairs:
        per_chara[chara].append((int(no), f"OID_{item}"))
    return {chara: [oid for _, oid in sorted(rows)] for chara, rows in per_chara.items()}


def load_catalog_ids() -> list[str]:
    return json.loads(read(CATALOG))["m_InternalIds"]


def series_charas(name: str, extra: dict, catalog: list[str]) -> list[str]:
    """Who a banner features, in the dump's own words where it says so."""
    # 1. Characters/Character<N>/NewGirl/<SeriesName>/ - the banner's own art.
    charas = sorted(
        {
            f"Chara{int(m.group(1)):02d}"
            for m in (
                re.search(rf"Characters/Character(\d+)/NewGirl/{re.escape(name)}/", path)
                for path in catalog
            )
            if m
        }
    )
    if charas:
        return charas

    # 2. Limited banners carry their roster in GachaExtraMasters.
    if extra.get("limited_charas"):
        return sorted(set(extra["limited_charas"]))

    # 3. Fall back to the character name inside the series name. Longest first
    #    so "Pine" never wins over a name that contains it.
    for label in sorted(CHARA_NAMES, key=len, reverse=True):
        if label in name:
            return [CHARA_NAMES[label]]

    # 4. Guest costumes (Ichiko, Komugi, ...) and "Returns" revivals have no
    #    character of their own in the name; the cutins still live under whoever
    #    wears them, e.g. Character1/CutIn2/Textures/MarchingHiyori.png for
    #    PickupMarchingIchiko. Try the whole costume token, then drop trailing
    #    CamelCase words until something matches.
    if not name.startswith("Pickup"):
        return []
    words = re.findall(r"[A-Z][a-z0-9]*", re.sub(r"\d+$", "", name[len("Pickup"):]))
    while words:
        token = "".join(words)
        charas = sorted(
            {
                f"Chara{int(m.group(1)):02d}"
                for m in (
                    re.search(
                        rf"Characters/Character(\d+)/CutIn2/Textures/{re.escape(token)}\w*\.png",
                        path,
                    )
                    for path in catalog
                )
                if m
            }
        )
        if charas:
            return charas
        words.pop()
    return []


def series_type(name: str, kind: str) -> str:
    if name in ("Normal", "NormalTicket"):
        return "Normal"
    if name.startswith("Unlock"):
        return "Unlock"
    if name.startswith("Music"):
        return "Music"
    if kind == "LimitedReturns":
        return "Limited"
    return "Pickup"


def build() -> dict:
    items = parse_cutin_items()
    names = parse_series_names()
    extras = parse_extra_masters()
    unlock_items = parse_unlock_items()
    music_pools = parse_music_pools()
    catalog = load_catalog_ids()

    unlock_oids = set(unlock_items.values())
    # Rarity.Nothing items are the default cutins every player already owns, and
    # the character-unlock items only ever drop from Unlock/Limited banners.
    standard_pool = [
        i["oid"] for i in items if i["rarity"] in RARITIES and i["oid"] not in unlock_oids
    ]
    # oid -> [CharaType, Rarity], so the server tests can re-check the client's
    # invariants without re-parsing the dump.
    item_index = {i["oid"]: [i["chara"], i["rarity"]] for i in items}

    series = {}
    for sid, name in sorted(names.items()):
        extra = extras.get(name, {"pickup_kind": "None", "is_returns": False, "limited_charas": []})
        stype = series_type(name, extra["pickup_kind"])
        charas = [] if stype == "Normal" else series_charas(name, extra, catalog)

        custom_pickup: list[str] = []
        extra_items: list[str] = []
        if stype == "Unlock":
            # The pickup is the unlock ticket itself, not the whole character:
            # GetPickUpItems() prefers custom_pickup_items, which keeps the rest
            # of the pool in GetExceptPickUpItems() where the lottery needs it.
            custom_pickup = [unlock_items[c] for c in charas if c in unlock_items]
            extra_items = list(custom_pickup)
        elif stype == "Limited":
            # GenerateGachaItemID only ever draws a Limited banner out of the
            # *except*-pickup pool (the pickup branch is gated on Pickup/Unlock),
            # so the returning characters' unlock items have to sit in the plain
            # pool with nothing marked as pickup, or the banner cannot hand out
            # the very characters it advertises.
            extra_items = [unlock_items[c] for c in charas if c in unlock_items]
            charas = []
        elif stype == "Music":
            extra_items = []

        entry = {
            "name": name,
            "type": stype,
            "pickup_kind": extra["pickup_kind"],
            "pickup_charas": charas,
            "custom_pickup_items": custom_pickup,
            "extra_items": extra_items,
        }
        if stype == "Music":
            pool: list[str] = []
            for chara in charas:
                pool += music_pools.get(chara, [])
            entry["music_items"] = pool
        series[str(sid)] = entry

    return {
        "_generated_by": "tools/extract_gacha_pools.py",
        "_source": "VFG:J:A:A:2025122300 (decompiled-csharp + Addressables catalog)",
        "chara_names": {v: k for k, v in sorted(CHARA_NAMES.items(), key=lambda kv: kv[1])},
        "chara_unlock_items": unlock_items,
        "standard_pool": standard_pool,
        "item_index": item_index,
        "series": series,
    }


def validate(data: dict) -> list[str]:
    """Re-check the two invariants the client's own code imposes on a pool."""
    chara_of = {oid: v[0] for oid, v in data["item_index"].items()}
    rarity_of = {oid: v[1] for oid, v in data["item_index"].items()}
    problems = []
    for sid, s in sorted(data["series"].items(), key=lambda kv: int(kv[0])):
        if s["type"] == "Music":
            continue
        pool = list(data["standard_pool"]) + list(s["extra_items"])
        if s["custom_pickup_items"]:
            pickup = set(s["custom_pickup_items"])
        else:
            pickup = {o for o in pool if chara_of.get(o) in set(s["pickup_charas"])}
        except_pool = [o for o in pool if o not in pickup]

        # GenerateGachaItemID indexes the except-pickup pool by rolled rarity.
        for rarity in RARITIES:
            if not any(rarity_of.get(o) == rarity for o in except_pool):
                problems.append(f"{sid} {s['name']}: no {rarity} outside the pickup set")

        # GachaSelectBgMovie recurses forever on a "CutinPlay"-only movie list
        # with nothing to preview, so Pickup banners need one or the other.
        if s["type"] == "Pickup" and s["pickup_kind"] != "NewGirl":
            if not s["pickup_charas"] and not pickup:
                problems.append(f"{sid} {s['name']}: CutinPlay loop (no pickup chara, no pickup item)")
    return problems


def report(data: dict) -> list[str]:
    series = data["series"]
    missing = [
        s["name"]
        for s in series.values()
        if s["type"] not in ("Normal", "Limited") and not s["pickup_charas"]
    ]
    print(f"series          : {len(series)}")
    print(f"standard pool   : {len(data['standard_pool'])} cutin items")
    print(f"unlock items    : {len(data['chara_unlock_items'])}")
    print(f"no pickup chara : {len(missing)}" + (f" -> {missing}" if missing else ""))
    for stype in ("Normal", "Pickup", "Unlock", "Music", "Limited"):
        n = sum(1 for s in series.values() if s["type"] == stype)
        print(f"  {stype:<8}: {n}")
    problems = validate(data)
    print(f"invariants      : {'ok' if not problems else str(len(problems)) + ' PROBLEM(S)'}")
    for p in problems:
        print(f"  ! {p}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--print", action="store_true", help="report only, do not write")
    args = ap.parse_args()

    data = build()
    problems = report(data)
    if problems:
        return 1
    if not args.print:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1024:.0f} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
