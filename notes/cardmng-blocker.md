# cardmng / e-Amuse card login blocker

Guest 2P/4P play works. Card tag currently crashes Unity or hangs the XRPC.

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
