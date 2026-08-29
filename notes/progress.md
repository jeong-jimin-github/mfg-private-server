# Progress

## 2026-08-29

### Last known working state
- Client previously failed before network check: `unresolve 'eamuse.konami.fun'`.
- Build: `VFG:J:A:A:2025122300`
- Server: local Python e-Amuse + AOG HTTP on `127.0.0.1:8080`

### Newly observed RPCs
- CONFIRMED XRPC modules `vfgac` / `vfglog` via `Ea3XrpcAddModule("vfgac","local",...)`.
- CONFIRMED HTTP game API paths from `MFG.GameRequest.*` (`/appli_boot`, `/login`, `/get_menudata`, ...).
- CONFIRMED XML status node `serv_st/code` (0 = OK). Missing node defaults to 630.

### Implemented
- Transport: RC4 + LZ77 store + kbinxml
- Common e-Amuse bootstrap
- VFG XRPC stubs including `service_list`
- AOG HTTP stubs + profile JSON store
- Spice ea3-config pointed at localhost HTTP

### Current blocker
- Title/menu reached. Attract screen asks for coin or e-Amusement card (expected).
- Full CPU match still needs ReceiveCommand tile stream (`TaikyokuData.cell_info`).

### Next action
- Start server, launch Spice2x, capture the real request sequence, fill any missing fields.
