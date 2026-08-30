# Mahjong Fight Girl (MFG) local server

Private localhost replacement for KONAMI e-Amusement + the MFG AOG game API.

Supported dump: `VFG:J:A:A:2025122300` (Spice2x + Unity Mono).

This does **not** talk to KONAMI or any third-party host.

## What works

### e-Amusement / XRPC
- `services.get`, `pcbtracker`, `facility`, `message`, `package`, `pcbevent`, `eventlog`
- `cardmng` (and the `vfgcard` shadow module used after the kamunity patch below)
- `eacoin` — local PASELI wallet (`checkin` / `opcheckin` / `consume` / `getbalance` / `checkout`)
- `vfgac.service_list`, `vfgac.update_refer`, `vfgac.ext_campaign`, `vfgac.send_paylog`, `vfglog.put_msg`

### AOG game API
- boot + session: `/appli_boot`, `/appli_info`, `/login`, `/logout`, `/create_player`,
  `/get_menudata`, `/keep_alive`, `/chk_tabooword`
- profile blobs: `/client_state_read`, `/client_state_write` (lossless base64 JSON)
- **feature flags**: `/appli_info` now returns the `events` blob that drives
  `GameEventType`, so event tables, sticker decoration, the spirit-gym bonus and the
  unlockable characters are switched on
- **match**: `/entry_game`, `/gget`, `/gpost`, `/end_game`, `/kiken_game`, `/end_show`
  — a full mahjong engine (see below)
- **spirit gym (スピリットジム)**: `/dojo_get_status`, `/dojo_set_slot`, `/dojo_gain_soul`
  with four slots that accumulate spirit over real time and persist in `data/save.json`
- **gacha**: `/gacha_info` (series list *and* item pools generated from the dump's
  own Addressables catalog + master data — see "Gacha pools" below),
  `/req_draw_gacha`, `/get_gacha_result`, `/gacha_log`
- **reach-song gacha**: `/music_gacha_play_reserve`, `/music_gacha_play`
- **sticker chat (スティッカーチャット)**: `/gchat`, `/gget_stamp_info`, plus the `<chat>`
  block inside `/gget`; CPU seats sticker back
- misc: `/player_record`, `/get_haifu_list`, `/get_jongstone_info`, `/get_mg`,
  `/mission_date`, `/present_done`, `/competition_entry`, `/item_gain_log`,
  `/item_consume_log`, `/notice_done`, `/set_favorite_character`, `/odekake_done`, …

Every request/response is logged under `captures/`.

## The CPU match

`mahjong.py` + `taikyoku.py` implement a real engine, not a tile-echo stub:

- correct wall per table type — nima drops all manzu plus west/north, sanma drops
  M2–M8, and the dora cycle follows those reduced sets
- shanten / ukeire / wait calculation (standard + chiitoitsu + kokushi)
- yaku evaluation with the client's own `MFG.Types.Yaku` bit layout, fu counting,
  han ranks and the `MahjongUtility.GetScore` payment table
- **the CPU actually plays**: it declares riichi, tsumo and ron, calls pon / chi /
  ankan / minkan / kakan, folds against riichi using genbutsu safety, and refuses to
  call when it would leave the hand yakuless
- ippatsu, double riichi, furiten (permanent + temporary), haitei / houtei /
  rinshan / chankan, ura dora on riichi
- full games: multiple kyoku, honba, riichi sticks, renchan, tenpai payments at
  ryuukyoku, kyuushu kyuuhai, bust detection, final ranks and uma to `/end_game`

Table types: `gmode` 1 tonpu (4p), 2 hanchan (4p), 3 sanma (3p), 4 nima (2p) plus
every event `GAME_MODE` (5–23).

## Requirements

- Python 3.10+ with `kbinxml` (`pip install kbinxml`)
- Spice2x already sitting in the game folder

## Start the server

```powershell
cd mfg-private-server
.\start.ps1
```

Listens on `http://127.0.0.1:8080` (`--port` to change it).

## Point the game at it

1. In `spicecfg.exe` → Network:
   - **EA Service URL** = `http://127.0.0.1:8080`
   - **PCBID** = `00010203040506070809` (already in `prop/ea3-config.xml`)
2. Options: **MFG Cabinet Type = HG**
3. This repo also rewrites `prop/ea3-config.xml` and `dev/nvram/ea3-config.xml`:
   - `network/services` → `http://127.0.0.1:8080`
   - `network/ssl` → `0`

Then launch `spice64.exe`.

## e-Amuse card login (required for most menu features)

The stock client cannot log a card in: AVS already owns the `cardmng` XRPC module,
so the game's own `Ea3XrpcAddModule("cardmng", …)` is dropped and AVS regenerates
the request from an uninitialised card session (3-byte `cardid`, garbage
`cardtype`), then corrupts its own heap when a response comes back.

Rename the managed module once so AVS stops hijacking it:

```bash
python tools/patch_cardmng_module.py game/MFGClient_Data/Managed/kamunity.dll
```

(`--revert` restores the backup.) After that spice logs `vfgcard.inquire` /
`vfgcard.authpass` and card login completes; the server answers both module names.

**This matters because the client itself disables the gacha button, the spirit gym,
missions, notices, presents, character select and the sticker-chat button whenever
`GameDataManager.Account.IsGuestUser` is true.** No amount of server data changes
that — only a real card login does.

Cards: `Numpad +` / `Insert` uses `card0.txt`. If the bind does nothing, in
`spicecfg.exe` → Options set **Auto Card Insert = P1**.

## Two client-side gates worth knowing

| Feature | Client rule | What to do |
|---|---|---|
| Gacha button | `HomeFlow` requires `!GameUtility.IsFreePlay()` | set `<free>0</free>` in `dev/nvram/coin.xml` and pay with coins (`F1`) or PASELI |
| Event tables (이벤트대국) | `BaseEventGameData.IsAvailable()` returns false for guests | log a card in; guests still get the *constancy* tables (炎のリーチ道場 / 暗黒ドラ卓 / メンタンピン教室), which the server enables |

`dev/nvram/coin.xml` freeplay is currently on (`<free>1</free>`), which is convenient
for match testing but hides the gacha button.

## Attract / “insert coin or card” screen

That screen means the network check already passed. It is the normal arcade credit wait.

Default Spice2x keys (from this dump’s `log.txt`):

| Action | Key |
|---|---|
| Insert coin | `F1` |
| Insert Player 1 card | `Numpad +` |
| Card manager | `F7` |
| Virtual keypad | `F5` |
| Overlay menu | `Esc` |

## Event tables and the dead 対局 button

`HomeEventSelectWindow.ModePanel_Create` instantiates one banner per available
event table into `_modePanelInstanceParents[index]` - a list of parent slots
serialised in the Home scene, not a growing container. The normal 対局 window
always builds exactly three panels (yonma / sanma / nima), so the scene has
three slots. Advertising every `EventTakuMaster` `CurrentFlagType` builds
twelve, and the fourth one indexes past the end of the list.

The throw lands inside `await m_modeSelectWindow.Open(...)`, called from
`OnEventTaikyokuButton()` - an `async void` that has already set
`_taikyokuModeSelectStatus = SelectingEvent`. Only the window's own close
button resets that field, and the window never opens, so `OnTaikyokuButton()`
returns at its first `if` from then on: **both 対局 and イベント対局 go dead for
the rest of the credit**, with no sound, no window and no request reaching the
server.

`VFG_EVENT_TAKU` keeps the count inside the budget. `min` (the default) runs
炎のリーチ道場 / 革命のカタルシス / 霧雨魔法店, one banner each. Note that a table
whose master has `EnableTakuType[1]` set contributes *two* banners (tonpu plus
sanma) - `EVENT_TAKU_PANELS` in `server.py` records the per-flag cost and
`test_match_e2e.py` asserts the total.

## Gacha pools

`/gacha_info` has to send real `<items>` / `<pickup_charas>`, because the client does
the drawing and the banner presentation locally and neither survives an empty pool:

- `GachaSelectBgMovie.Play()` reads `movies[0]` from `GetGachaSelectMovies()`. For a
  Pickup banner whose `PickupKind` is not `NewGirl` that list starts with the literal
  `"CutinPlay"` and then gets one entry per pickup character — with no pickup
  characters it is exactly `["CutinPlay"]`. `Play()` then calls
  `PlayCutIn(GetSpecialPickupItem())`, also empty, which falls straight through to
  `BgMovie_Change()`, which wraps the index back to 0, reads `"CutinPlay"` again and
  calls `PlayCutIn` again. Nothing on that path awaits, so the two recurse until the
  stack dies: **the game closes the moment you pick a gacha type**, with no managed
  exception logged anywhere.
- `GachaResultInfo.SetItemInfos` draws through `GenerateGachaItemID`, which indexes
  the except-pickup pool by the rolled rarity. Every banner needs N/R/SR/UR *outside*
  its own pickup set or the first unlucky pull throws `KeyNotFoundException` (UR
  alone is 2%).

`data/gacha_pools.json` holds the pools, the per-banner pickup characters and the
reach-song lists. Regenerate it from the dump with:

```bash
python tools/extract_gacha_pools.py
```

It reads `CutinItemMaster` / `GachaSeriesServerInfo` / `GachaSeriesName` /
`ItemIDExtentions` out of `verification/decompiled-csharp` plus the Addressables
catalog (`Characters/Character<N>/NewGirl/<SeriesName>/…` is the dump's own record of
who each banner features), re-checks both invariants above and refuses to write if
one fails. Character identity comes from the catalog too — asset paths are
`Characters/Character<(int)CharaType + 1>/…` and carry the name, so
`Character1/3D/…/HiyoriAngerEnd.anim` pins Hiyori to `CharaType.Chara01`. That also
fixed the reach-song table, which had MusicYao and MusicTenshi swapped.

Banner shapes: Pickup lists the featured characters and lets the client derive the
pickup set; Unlock puts the character-unlock item in `custom_pickup_items` so the
rest of the pool stays drawable; Limited marks nothing as pickup, because the
client's lottery never draws a Limited pickup set.

## Environment switches

| Variable | Default | Effect |
|---|---|---|
| `VFG_EVENT_TAKU` | `min` | which event tables to advertise: `min` (3 banners), `off`, `all` (12 banners - the known-bad set, see below) |
| `VFG_GACHA_ALL` | off | advertise all 134 catalog gacha series instead of the curated 27 |
| `VFG_CARDMNG_MODE` | `compat` | `strict` restores the old 112/110 card quarantine |
| `VFG_CARDMNG_INQUIRE_MODE` | `auto` | `new` always answers `status="112"` |

## Tests

```bash
python test_protocol.py       # kbin/RC4/LZ77 transport round-trip
python test_cardmng.py        # cardmng regression matrix
python test_gacha_pools.py    # every advertised banner is one the client can render
python test_integration.py    # every AOG route + XRPC over real HTTP
python test_match_e2e.py      # drives whole CPU matches through the handlers
python ../test_engine.py      # 160-game engine soak test
```

## Logs

- Server console + `logs/server.log`
- Raw XRPC/AOG dumps: `captures/requests/`, `captures/responses/`
- Profiles: `data/save.json`

## If the network check still fails

- Confirm the server is running before Spice2x
- Confirm Spice2x EA Service URL is HTTP, not HTTPS
- Confirm Windows firewall allows the port on localhost
