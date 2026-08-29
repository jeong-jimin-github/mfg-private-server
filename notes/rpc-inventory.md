# RPC inventory

| Module | Method | First seen | Required? | Implemented? | Notes |
|---|---|---:|---|---|---|
| services | get | dump/log | yes | yes | common e-Amuse |
| pcbtracker | alive | dump/log | yes | yes | common |
| pcbtracker | keepalive | dump/log | yes | yes | common |
| message | get | docs | yes | yes | common |
| facility | get | docs | yes | yes | location id `VFG00001` |
| package | list | config | yes | yes | empty list |
| pcbevent | put | config | yes | yes | stub |
| eventlog | write | analogy | maybe | yes | stub |
| cardmng | inquire/getrefid/authpass/bindmodel | kamunity | for card | yes | local synthetic refid |
| vfgac | service_list | CONFIRMED decompile | yes | yes | returns `http://127.0.0.1:8080/aog` |
| vfgac | update_refer | CONFIRMED | after card | yes | status 0 |
| vfgac | ext_campaign | CONFIRMED | maybe | yes | empty list |
| vfgac | send_paylog | CONFIRMED | billing | yes | status 0 |
| vfglog | put_msg | CONFIRMED | telemetry | yes | status 0 |
| AOG HTTP | /appli_boot | CONFIRMED | yes | yes | XML + serv_st/code=0 |
| AOG HTTP | /appli_info | CONFIRMED | yes | yes | expire_seconds required |
| AOG HTTP | /login | CONFIRMED | yes | yes | guest=1 supported |
| AOG HTTP | /create_player | CONFIRMED | new card | yes | |
| AOG HTTP | /get_menudata | CONFIRMED | yes | yes | mpdata + playmode_list |
| AOG HTTP | /keep_alive | CONFIRMED | yes | yes | |
| AOG HTTP | /client_state_read | CONFIRMED | yes | yes | lossless base64 JSON |
| AOG HTTP | /client_state_write | CONFIRMED | yes | yes | |
| AOG HTTP | /entry_game | CONFIRMED | match | stub | returns table + gserv_url |
| AOG HTTP | /gget /gpost | CONFIRMED | match | stub | matching snapshot only |
