# cardmng / e-Amuse card login blocker

> **STATUS 2026-08-30: RESOLVED.** `tools/patch_cardmng_module.py` renames the
> managed XRPC module to `vfgcard`, AVS stops hijacking it, and card login
> completes end to end (see `notes/progress.md` and `log.txt` 14:34).
> Everything below is the history of how the root cause was found.

Guest 2P/4P play works. The historical card-tag crash described below was fixed by the managed-module rename.

Do not probe KONAMI. Localhost only.

## Environment

- Dump: `VFG:J:A:A:2025122300`
- Client: Spice2x + Unity / KAMUNITY XRPC (`text_xml=0`, `plain=0` → kbinxml)
- Server: `http://127.0.0.1:8080`
- Insert Card: Spice overlay `Insert` (vkey 45) on TKL
- Card override (current): `E0047CC78DFBA459` (no `00` byte). Previous `E00401001C4C3683` contains `00` and truncates C strings / desyncs kbinxml attributes.

## What works

- e-Amuse bootstrap: `services.get`, `pcbtracker.alive`, `facility.get`, `message.get`, `package.list`, `pcbevent.put`
- `vfgac.service_list`, `vfglog.put_msg`, AOG guest `/login` `/create_player` `/get_menudata`
- CPU mahjong (`/entry_game` `/gget` `/gpost`) as guest
- `cardmng.inquire` with **`status="112"`** (unregistered) — PIN / new-card UI appears, no crash

## Failure modes (reproduced)

### 1. Unity crash immediately on tag

Spice: `cardmng.inquire` `fin!=0`, `thread: exit`, then process dies. No `getrefid` / `authpass` / AOG login.

Triggers:

- `inquire` `status="0"` `binded="1"` (server treated card as already bound)
- Extra inquire attrs: `userid`, `lock`, `passflag`, `method="inquire"`
- Garbled request kbinxml + `status="0"`

Typical garbled request (kbinxml string decode):

```xml
<cardmng cardid="ﾞ" cardtype="2104083072" method="inquire" model="XbyR" update="1"/>
```

Spice still logs `Inserted card override: E0047CC78DFBA459`. `f=cardmng.inquire` is in the query string so the server can dispatch even when XML is junk.

### 2. Hang on “e-Amuse confirm” after tag

Spice: `cardmng.inquire` result logged **without** `thread: exit` / `dispose` (`fin=0` and stuck). Game waits forever.

Triggers: fat inquire body (`pcode`, `useridflag`, `extidflag`, `lastupdate`, `newflag="1"`).

### 3. Crash or hang after new PIN (`getrefid`)

Flow that reaches PIN:

1. `inquire` → `status="112"`
2. User sets 4-digit PIN
3. `cardmng.getrefid`

Then:

- `getrefid` with `binded`/`newflag` on the response → Unity crash (`thread: exit` then die)
- `getrefid` with `pcode="########"` → XRPC never finishes (no `thread: exit`); PIN screen does not advance
- Last attempted response (not fully retested after tag-crash regression):

  ```xml
  <response><cardmng status="0" refid="..." dataid="..." /></response>
  ```

  After that change the user reported **tag crash again**.

Garbled `getrefid` request example (8-byte ASCII wrongly hexed; since fixed in `protocol.py`):

```xml
<cardmng cardid="ﾞ" cardtype="2104083072" method="6765747265666964" passwd="&lt;car"/>
```

`6765747265666964` is hex of `getrefid`. Do **not** hex printable 8-byte ASCII; only hex 8-byte binary IDms.

## Server behavior at last stop

- `normalize_cardid()`: 16-hex or fallback `E0047CC78DFBA459`
- Unissued inquire: `status="112"` only
- Issued inquire: slim `status="0" binded binded/newflag/expired/exflag/ecflag`
- `getrefid`: `status="0" refid dataid` (no pcode / binded / newflag)
- `authpass` / `bindmodel`: `status="0"`
- Spice `card0` in `%appdata%\spicetools.xml`: `E0047CC78DFBA459`

## Likely root

Spice card override writes 8-byte IDm into kbinxml **string** attributes. Decoder sees `cardid="ﾞ"` and shifted `cardtype`/`model`/`passwd`. Native AVS then keeps a broken card session; Unity NREs when `status=0` gives it a `refid` to load.

`status=112` avoids creating that session, so the new-card UI works.

## Next (not done)

- Capture a **clean** 16-ASCII-hex `cardid=` inquire (seen once: `E00401006117571C`, `cardtype="1"`, `update="0"`) and compare kbinxml to the garbled `update="1"` path
- Make `getrefid` finish (`thread: exit`) then follow `inquire(newflag=1)` → `authpass` → `bindmodel` → `vfgac.update_refer` → AOG `/login`
- Do not return `binded="1"` until `bindmodel` has run **and** the inquire request cardid is 16-hex ASCII
- Do not add `pcode`/`useridflag`/`lastupdate` unless a clean capture shows them

## 2026-08-30 (Claude session) — confirmed root cause

Decoded the raw kbin captures byte by byte
(`captures/transport/0021_cardmng_inquire.bin`, `0024/0051_cardmng_getrefid.bin`).
The data section is well formed, so the decoder is *not* at fault:

| attr | wire bytes | note |
|---|---|---|
| `cardid` | `F0 DC DE 00` | 3 bytes; CP932-decodes to `ﾞ` |
| `cardtype` | `"2038653344"` | different 32-bit garbage every request |
| `method` | `"getrefid"` | correct |
| `model` | `98 25 1D 06 00` | game passes `model=null`, AVS adds garbage |
| `newflag` | `"1"` | correct |
| `passwd` | `"<car"` | first 4 bytes of the request XML buffer |

Managed side is clean: `Bi2xInputVFG.ICCardReader_Update()` builds
`CardIDStrings` as 16 upper-hex chars from `_ePassData[2..9]`, and
`XrpcUtil.CreatetXmlText()` hands AVS a correct XML string.
`W:xrpc: module_add: cardmng: already has same name` shows the game's
`Ea3XrpcAddModule("cardmng", ...)` is rejected because AVS already owns the
module, so AVS regenerates the request from its own (uninitialised) card
session. **The corrupted request cannot be fixed from the server.**

### Crash signature

`Application Error`: `spice64.exe` faulting module `ntdll.dll`,
`0xc0000005` at offset `0x5346d` (heap manager), ~1 s after
`cardmng.getrefid` returns `fin=1`. That is heap corruption inside AVS, not a
managed NRE — it fires whatever the response contains:

- `getrefid` -> `refid`/`dataid` (bemaniutils shape): crash (13:06, 13:55)
- `getrefid` -> `status="110"`: crash (13:14)
- `inquire`  -> `status="112"` only: never crashes

Every cardmng response except a bare `status="112"` has killed the process, so
response tuning alone is unlikely to solve this.

### Server state after this session

- `VFG_CARDMNG_MODE=compat` (default): malformed cardids normalize onto
  `DEFAULT_CARDID` and `getrefid` answers `refid`/`dataid` like bemaniutils.
  `strict` restores the old 112/110 quarantine.
- `VFG_CARDMNG_INQUIRE_MODE=auto` (default) / `new` (always 112).
- The typed PIN never reaches the server (`passwd="<car"`), so `authpass`
  accepts any PIN by design.

### Client-side levers still untried

- `spice64.exe -automap` (enabled 13:58): auto-creates missing AVS property
  nodes and dumps every property to `automap_0.xml`.
- Unity logging: `MFGClient_Data/boot.config` had `nolog=`; removed
  (`boot.config.bak` keeps the original). Unity writes to
  `dev/raw/log/output_log.txt` (`-logFile` is set by spice).
