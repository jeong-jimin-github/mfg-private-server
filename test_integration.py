"""End-to-end HTTP and handler coverage for the advertised local-server surface."""

from __future__ import annotations

import tempfile
import threading
import base64
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

import server
from protocol import EamuseDecodeMeta, decode_eamuse_body, encode_eamuse_body

MODEL = "VFG:J:A:A:2025122300"
INFO = "1-01234567-89ab"


def post(url: str, data: bytes, headers: dict[str, str] | None = None) -> tuple[bytes, object]:
    request = Request(url, data=data, headers=headers or {}, method="POST")
    with urlopen(request, timeout=5) as response:
        assert response.status == 200
        return response.read(), response.headers


def parse_xml(text: str) -> ET.Element:
    root = ET.fromstring(text)
    assert root.tag in {"response", "result", "root"}, root.tag
    return root


def check_client_log_channel() -> None:
    """vfglog.put_msg is the only diagnostic the client can send us.

    RequestApiBase.PushRequest reserves a ("network_error", ...) entry naming the
    failing MFG.GameRequest class whenever a response fails to parse, and
    GameUtility.ServerLog flushes those through this module.  If the handler goes
    back to swallowing them we lose the only stack-trace-shaped evidence there is,
    because this build ships with Unity's player log disabled.
    """
    import io
    import logging

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    server.log.addHandler(handler)
    old_level = server.log.level
    server.log.setLevel(logging.INFO)
    try:
        call = ET.fromstring(
            f'<call model="{MODEL}"><vfglog method="put_msg">'
            '<loc_id __type="str">VFG00001</loc_id>'
            '<msg __type="str" label="network_error">'
            '2026-08-30 20:00:00,GetMenuData,630,A1DD6D1B6F9BF4E1,,parse failed</msg>'
            '<msg __type="str" label="storage_info">{"Items":[]}</msg>'
            "</vfglog></call>"
        )
        xml = server.handle_vfglog("put_msg", call, MODEL)
    finally:
        server.log.setLevel(old_level)
        server.log.removeHandler(handler)

    node = parse_xml(xml).find("vfglog")
    assert node is not None and node.attrib.get("status") == "0", xml

    logged = buf.getvalue()
    assert "ERROR [client] network_error" in logged, logged
    assert "GetMenuData" in logged, logged
    assert "[client] storage_info" in logged, logged


def main() -> None:
    old_db, old_host, old_port = server.DB, server.HOST, server.PORT
    with tempfile.TemporaryDirectory() as tmp:
        server.DB = server.ProfileDB(Path(tmp) / "save.json")
        server.HOST = "127.0.0.1"
        httpd = server.ThreadingHTTPServer((server.HOST, 0), server.Handler)
        server.PORT = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://{server.HOST}:{server.PORT}"
        try:
            with urlopen(base + "/health", timeout=5) as response:
                health = response.read().decode("utf-8")
                assert response.status == 200 and "VFG local server ok" in health

            # Full encrypted + outer-LZ77 + UTF-8 KBin HTTP round-trip.
            request_xml = f'<call model="{MODEL}"><services method="get"/></call>'
            request_meta = EamuseDecodeMeta(True, "UTF-8", True)
            wire = encode_eamuse_body(request_xml, INFO, "lz77", request_meta)
            response_wire, response_headers = post(
                base + f"/?model={MODEL}&f=services.get",
                wire,
                {
                    "User-Agent": "EAMUSE",
                    "X-Eamuse-Info": INFO,
                    "X-Compress": "lz77",
                    "Content-Type": "application/octet-stream",
                },
            )
            response_xml, response_meta = decode_eamuse_body(response_wire, INFO, "lz77")
            assert response_meta.used_kbin
            assert response_meta.kbin_encoding == "UTF-8"
            assert response_meta.kbin_compressed is True
            assert response_headers["X-Compress"] == "lz77"
            assert ET.fromstring(response_xml).find("services") is not None

            # Malformed Spice card identity remains quarantined over real HTTP.
            malformed = (
                f'<call model="{MODEL}"><cardmng method="inquire" '
                'cardid="\ue09bﾞ" cardtype="2104083072"/></call>'
            )
            wire = encode_eamuse_body(malformed, INFO, "lz77", request_meta)
            response_wire, _ = post(
                base + f"/?model={MODEL}&f=cardmng.inquire",
                wire,
                {
                    "User-Agent": "EAMUSE",
                    "X-Eamuse-Info": INFO,
                    "X-Compress": "lz77",
                    "Content-Type": "application/octet-stream",
                },
            )
            response_xml, _ = decode_eamuse_body(response_wire, INFO, "lz77")
            card = ET.fromstring(response_xml).find("cardmng")
            assert card is not None and card.attrib == {"status": "112"}, response_xml

            # Exercise every registered AOG route through the actual HTTP stack.
            common = {
                "pcuid": "INTEGRATION-PC",
                "mid": "1",
                "gmode": "4",
                "name": "TEST",
                "kind": "profile",
                "data": base64.b64encode(b"round-trip-state").decode("ascii"),
                "must": ",".join("0" for _ in range(20)),
            }
            for name in server.GAME_HANDLERS:
                body, _ = post(
                    base + "/aog/" + name,
                    urlencode(common).encode("utf-8"),
                    {"Content-Type": "application/x-www-form-urlencoded"},
                )
                parse_xml(body.decode("utf-8"))

            # GetMenuData's managed parser treats battle_item_settings as
            # optional, but if present it unconditionally dereferences both
            # child containers.  Cover the exact card-login regression that
            # previously returned the player to the title screen.
            menu_body, _ = post(
                base + "/aog/get_menudata",
                urlencode(common).encode("utf-8"),
                {"Content-Type": "application/x-www-form-urlencoded"},
            )
            menu = ET.fromstring(menu_body.decode("utf-8"))
            battle = menu.find("menudata/battle_item_settings")
            assert battle is not None
            assert battle.find("basic_settings") is not None
            mode_settings = battle.find("playmode_settings")
            assert mode_settings is not None
            covered_modes = {
                int(node.attrib["gmode"])
                for node in mode_settings.findall("setting")
                if node.attrib.get("taku_class") == "1"
            }
            assert covered_modes == set(server.GAME_MODES)

            # State write/read must preserve the literal payload.
            post(
                base + "/aog/client_state_write",
                urlencode(common).encode("utf-8"),
                {"Content-Type": "application/x-www-form-urlencoded"},
            )
            read_body, _ = post(
                base + "/aog/client_state_read",
                urlencode(common).encode("utf-8"),
                {"Content-Type": "application/x-www-form-urlencoded"},
            )
            state_node = ET.fromstring(read_body.decode("utf-8")).find("state/data")
            assert state_node is not None and state_node.text
            assert base64.b64decode(state_node.text).decode("utf-8") == "round-trip-state"

            # PASELI checkin/consume must answer with a real wallet.
            checkin = (
                f'<call model="{MODEL}"><eacoin method="checkin">'
                '<cardid __type="str">E0047CC78DFBA459</cardid>'
                '<passwd __type="str">1234</passwd></eacoin></call>'
            )
            wire = encode_eamuse_body(checkin, INFO, "lz77", request_meta)
            response_wire, _ = post(
                base + f"/?model={MODEL}&f=eacoin.checkin",
                wire,
                {
                    "User-Agent": "EAMUSE",
                    "X-Eamuse-Info": INFO,
                    "X-Compress": "lz77",
                    "Content-Type": "application/octet-stream",
                },
            )
            response_xml, _ = decode_eamuse_body(response_wire, INFO, "lz77")
            coin = ET.fromstring(response_xml).find("eacoin")
            assert coin is not None and coin.attrib.get("status") == "0", response_xml
            assert int(coin.find("balance").text) > 0
            assert coin.find("sessid") is not None and coin.find("sessid").text
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
            server.DB, server.HOST, server.PORT = old_db, old_host, old_port

    check_client_log_channel()
    print(f"integration coverage OK: {len(server.GAME_HANDLERS)} AOG routes + XRPC/card transport + client log channel")


if __name__ == "__main__":
    main()
