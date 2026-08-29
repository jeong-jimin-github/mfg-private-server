# Mahjong Fight Girl (VFG) local server

Private localhost replacement for KONAMI e-Amusement + the VFG AOG game API.

Supported dump: `VFG:J:A:A:2025122300` (Spice2x + Unity Mono).

This does **not** talk to KONAMI or any third-party host.

## What works in v0.1

- e-Amusement bootstrap (`services.get`, `pcbtracker`, `facility`, `message`, `package`, `pcbevent`, `cardmng`)
- VFG XRPC: `vfgac.service_list`, `vfgac.update_refer`, `vfgac.ext_campaign`, `vfgac.send_paylog`, `vfglog.put_msg`
- Game HTTP API: `/appli_boot`, `/appli_info`, `/login`, `/get_menudata`, guest profile, client-state round-trip
- Every request/response is logged under `captures/`

Guest 2P/4P CPU play works. **e-Amuse card tag currently crashes or hangs** — see `notes/cardmng-blocker.md` and GitHub issues.

Do not probe KONAMI; localhost only.

## Requirements

- Python 3.10+ with `kbinxml` (`pip install kbinxml`)
- Spice2x already sitting in the game folder

## Start the server

```powershell
cd mfg-private-server
.\start.ps1
```

Listens on `http://127.0.0.1:8080`.

## Point the game at it

1. In `spicecfg.exe` → Network:
   - **EA Service URL** = `http://127.0.0.1:8080`
   - **PCBID** = `00010203040506070809` (already in `prop/ea3-config.xml`)
2. Options: **MFG Cabinet Type = HG**
3. Cards: generate Player 1 card (optional; guest play does not need one)
4. This repo also rewrites `prop/ea3-config.xml` and `dev/nvram/ea3-config.xml`:
   - `network/services` → `http://127.0.0.1:8080`
   - `network/ssl` → `0`

Then launch `spice64.exe`.

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

Guest play: press `F1` once, then start / touch the screen.

Card play: `Numpad +` uses `card0.txt` (already present). If the bind does nothing, in `spicecfg.exe` → Options set **Auto Card Insert = P1**.

`dev/nvram/coin.xml` freeplay is now on (`<free>1</free>`). Restart the game once so that takes effect; after that credits should already be available.

## Logs

- Server console + `logs/server.log`
- Raw XRPC/AOG dumps: `captures/requests/`, `captures/responses/`
- Profiles: `data/save.json`

## If the network check still fails

- Confirm the server is running before Spice2x
- Confirm Spice2x EA Service URL is HTTP, not HTTPS
- Confirm Windows firewall allows port 8080 on localhost
