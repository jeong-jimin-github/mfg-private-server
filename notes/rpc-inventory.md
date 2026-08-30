# RPC inventory

## XRPC (e-Amusement)

| Module | Method | Required? | Implemented? | Notes |
|---|---|---|---|---|
| services | get | yes | yes | advertises `cardmng` **and** `vfgcard` |
| pcbtracker | alive / keepalive | yes | yes | |
| message | get | yes | yes | |
| facility | get | yes | yes | location id `VFG00001` |
| package | list | yes | yes | empty list |
| pcbevent | put | yes | yes | stub |
| eventlog | write | maybe | yes | stub |
| cardmng | inquire/getrefid/authpass/bindmodel | for card | yes | local synthetic refid |
| vfgcard | (same five) | for card | yes | shadow module after the kamunity patch |
| eacoin | checkin/opcheckin/consume/getbalance/checkout/getlog | for PASELI | **yes** | local wallet, 57300 balance |
| vfgac | service_list / update_refer / ext_campaign / send_paylog | yes | yes | |
| vfglog | put_msg | telemetry | yes | |

## AOG HTTP

| Path | Required for | Implemented? | Notes |
|---|---|---|---|
| `/appli_boot` | boot | yes | `serv_st/code=0` |
| `/appli_info` | boot | **yes, real payload** | `info_data kind="events"` (GameEventType flags) + `pro_stats` |
| `/login` `/logout` `/create_player` | session | yes | guest + card |
| `/get_menudata` | home | yes | mpdata + all 23 `playmode_list` gmodes + `battle_item_settings` |
| `/keep_alive` | home | yes | |
| `/client_state_read` `/client_state_write` | profile | yes | lossless base64, `one_kind` honoured |
| `/chk_tabooword` | naming | yes | `result=0` means allowed |
| `/entry_game` | match | yes | per-session table id |
| `/gget` `/gpost` | match | **yes, full engine** | see `taikyoku.py` |
| `/end_game` `/kiken_game` | match | yes | real ranks / scores / uma |
| `/end_show` | match | **yes** | `<showresult>` is `MustNeedElement` |
| `/dojo_get_status` | spirit gym | **yes** | 4 slots, time-based spirit growth |
| `/dojo_set_slot` | spirit gym | **yes** | persists chara per slot |
| `/dojo_gain_soul` | spirit gym | **yes** | returns `get_nr` and the refreshed slot |
| `/gacha_info` | gacha | **yes** | series list built from the Addressables catalog |
| `/req_draw_gacha` | gacha | **yes** | transaction id |
| `/get_gacha_result` | gacha | **yes** | `lottery_result` rows |
| `/gacha_log` | gacha | yes | decodes + logs the base64 payload |
| `/music_gacha_play_reserve` | reach-song gacha | **yes** | request id bound to the series |
| `/music_gacha_play` | reach-song gacha | **yes** | grants a real `OID_ReachBgm###` |
| `/gchat` | sticker chat | **yes** | per-table room, CPU replies |
| `/gget_stamp_info` | sticker chat | **yes** | `stamp_info` send + cursor |
| `/get_jongstone_info` | home | **yes** | node is dereferenced without a null check |
| `/get_mg` | home | **yes** | same |
| `/player_record` `/get_record` | records | yes | empty but well-formed |
| `/get_haifu_list` `/get_haifu_data` | replays | yes | empty list |
| `/mission_date` | missions | yes | `info_data kind="missions"` |
| `/present_done` | presents | yes | echoes the ids as successes |
| `/competition_entry` | competition | yes | |
| `/item_gain_log` `/item_consume_log` | telemetry | yes | decoded to the log |
| `/notice_done` `/important_notice_done` `/coop_done` `/eashop_done` `/odekake_done` `/set_favorite_character` | misc | yes | empty success |

## Taikyoku command stream

Receive cells implemented (`MFG.Taikyoku.Command.RECEIVE_COMMAND_TYPE`):
TSUMO(1) SUTEHAI(2) TSUMOAGARI(3) RON(4) RYUKYOKU(5) PON(6) CHI(7) ANKAN(8)
MINKAN(9) KAKAN(10) TYOKO(14) TSUMOCHOICES(15) SUTECHOICES(16) KYOKUSTART(17)
KYOKUEND(23) SCORERANK(24).

Send commands handled (`SEND_COMMAND_TYPE`): ENTRY(1) SUTE_PAI(2) TSUMO_AGARI(3)
RON_AGARI(4) PON(5) CHI(6) ANKAN(7) MINKAN(8) KAKAN(9) KYUSYUKYUHAI(10)
NAKINASHI(11) CYOUKOU(12) KIKEN(13) NEXT_KYOKU_READY(15).
