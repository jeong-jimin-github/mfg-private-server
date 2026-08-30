"""/gacha_info must not hand the client a banner it will crash on.

Two rules, both taken from the client's own code (see the comment block above
_gacha_pool in server.py and the header of tools/extract_gacha_pools.py):

  1. A Pickup banner whose PickupKind is not NewGirl needs a pickup character or
     a custom pickup item. Without either, GachaSelectBgMovie.Play() and
     BgMovie_Change() recurse into each other forever over a movie list that is
     just ["CutinPlay"], and spice64.exe dies with no managed exception - the
     "enter gacha, pick a type, game closes" crash.
  2. Every non-Music banner needs N/R/SR/UR *outside* its pickup set, because
     GenerateGachaItemID indexes the except-pickup pool by the rolled rarity and
     KeyNotFoundException is one 2% UR roll away otherwise.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

import server

RARITIES = ("N", "R", "SR", "UR")


def series_infos() -> list[ET.Element]:
    root = ET.fromstring("<root>" + server._gacha_info_xml() + "</root>")
    return list(root.iter("info"))


def main() -> None:
    index = server.GACHA_POOLS["item_index"]
    pools = server.GACHA_SERIES_POOLS
    infos = series_infos()
    assert infos, "gacha_info advertised no series at all"

    checked_pickup = 0
    for info in infos:
        sid = info.findtext("id")
        label = info.findtext("label")
        stype = info.findtext("series_type")
        items = [node.text for node in info.find("items")]
        charas = [node.text for node in info.find("pickup_charas")]
        custom = [node.text for node in info.find("custom_pickup_items")]
        where = f"series {sid} ({label})"

        assert items, f"{where}: empty item pool"

        if stype == "Music":
            # Music banners carry reach-song OIDs, which the client parses into
            # MusicItems rather than through CutinItemMaster.
            assert not custom, f"{where}: music banners have no custom pickup"
            continue

        for oid in items + custom:
            assert oid in index, f"{where}: {oid} is not a cutin item the client knows"

        # GetPickUpItems() prefers custom_pickup_items and falls back to
        # "every pooled item belonging to a pickup character".
        pickup = set(custom) if custom else {o for o in items if index[o][0] in set(charas)}
        kind = pools.get(str(sid), {}).get("pickup_kind", "None")
        if stype == "Pickup" and kind != "NewGirl":
            assert charas or pickup, f"{where}: CutinPlay recursion - no pickup chara, no pickup item"
            checked_pickup += 1

        rarities = {index[o][1] for o in items if o not in pickup}
        for rarity in RARITIES:
            assert rarity in rarities, f"{where}: no {rarity} left outside the pickup set"

    # The reach-song pools follow ItemIDExtentions.s_gachaNoDic, where MusicYao
    # is Chara05 and MusicTenshi is Chara04.
    assert server.MUSIC_GACHA_POOL, "no reach-song pools loaded"
    served = {int(i.findtext("id")): [n.text for n in i.find("items")] for i in infos}
    for sid, pool in server.MUSIC_GACHA_POOL.items():
        assert len(pool) == 4, f"music series {sid}: expected 4 songs, got {len(pool)}"
        assert set(served[sid]) == set(pool), f"music series {sid}: banner and draw pools differ"
    assert set(server.MUSIC_GACHA_POOL[107]) == {
        "OID_ReachBgm160",
        "OID_ReachBgm161",
        "OID_ReachBgm162",
        "OID_ReachBgm163",
    }, "MusicYao (107) must serve Chara05's songs"

    print(f"gacha pools OK: {len(infos)} series, {checked_pickup} cutin-preview banners")


if __name__ == "__main__":
    main()
