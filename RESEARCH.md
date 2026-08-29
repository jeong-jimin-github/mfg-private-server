# Research notes

## Transport (CONFIRMED)

- `ea3-config` format = `binary` (kbinxml) and SSL originally on.
- Packets use `X-Eamuse-Info` RC4 and optional `X-Compress: lz77`.
- Game-specific XRPC is registered onto service `local`:
  - `vfgac`: `service_list`, `update_refer`, `ext_campaign`, `send_paylog`
  - `vfglog`: `put_msg`

## Game API (CONFIRMED)

Not XRPC. JubeatPlus-style HTTP POST `application/x-www-form-urlencoded` returning XML.

Base URL while kamunity-booted is `SystemConfigGameData.FrontServerUrl`, intended to come from `vfgac.service_list` / `service_url`.

Hardcoded KONAMI hosts exist (`https://rproxy0400.ea.konami.net/aog`) but are only used when kamunity boot is off.

XML success is `serv_st/code = 0`. Missing node is treated as HTTP status 630.

Player blobs are Unity JSON, base64 inside `<state kind="..."><data>...</data></state>`. Store losslessly.

## Match (HYPOTHESIS)

`/entry_game` returns `gserv_url` + table ids. In-match traffic is `/gget` `/gpost` with `TaikyokuData.cell_info` command cells. A real CPU game needs that command stream; v0.1 only stubs matching.

## Do not

- Probe KONAMI or third-party private servers
- Commit game binaries
