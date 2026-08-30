"""Regression tests for the VFG cardmng flow and malformed KAMUNITY requests."""

from __future__ import annotations

import copy
import os
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

import server

MODEL = "VFG:J:A:A:2025122300"
CARD = server.DEFAULT_CARDID
GARBLED_CARD = "\ue09bﾞ"


def request(method: str, **attrs: str) -> ET.Element:
    root = ET.Element("call", {"model": MODEL})
    ET.SubElement(root, "cardmng", {"method": method, **attrs})
    return root


def response_attrs(xml: str) -> dict[str, str]:
    node = ET.fromstring(xml).find("cardmng")
    assert node is not None, xml
    return dict(node.attrib)


def main() -> None:
    old_db = server.DB
    with tempfile.TemporaryDirectory() as tmp:
        server.DB = server.ProfileDB(Path(tmp) / "save.json")
        try:
            # New canonical card: CARD_NEW and no state mutation.
            attrs = response_attrs(
                server.handle_cardmng("inquire", request("inquire", cardid=CARD), MODEL)
            )
            assert attrs == {"status": "112"}, attrs
            assert server.DB.data["cards"] == {}, server.DB.data

            # Empty requests model a total XRPC/KBin decode failure. They must
            # never touch the configured fallback card.
            attrs = response_attrs(server.handle_cardmng("getrefid", ET.Element("call"), MODEL))
            assert attrs == {"status": "110"}, attrs
            attrs = response_attrs(server.handle_cardmng("bindmodel", ET.Element("call"), MODEL))
            assert attrs == {"status": "110"}, attrs
            assert server.DB.data["cards"] == {}, server.DB.data

            # Canonical registration uses the minimal AVS success response.
            attrs = response_attrs(
                server.handle_cardmng(
                    "getrefid", request("getrefid", cardid=CARD, passwd="1234"), MODEL
                )
            )
            assert "status" not in attrs and "pcode" not in attrs, attrs
            assert attrs["refid"] == attrs["dataid"], attrs
            refid = attrs["refid"]

            # Issued but unbound canonical card remains a normal success.
            attrs = response_attrs(
                server.handle_cardmng("inquire", request("inquire", cardid=CARD), MODEL)
            )
            assert "status" not in attrs, attrs
            assert attrs["binded"] == "0" and attrs["newflag"] == "1", attrs
            assert "lastupdate" not in attrs, attrs

            # Bind by refid only, matching the managed KAMUNITY call.
            attrs = response_attrs(
                server.handle_cardmng("bindmodel", request("bindmodel", refid=refid), MODEL)
            )
            assert attrs == {"dataid": refid}, attrs

            # KAMUNITY unconditionally parses @lastupdate for binded=1.
            attrs = response_attrs(
                server.handle_cardmng("inquire", request("inquire", cardid=CARD), MODEL)
            )
            assert "status" not in attrs, attrs
            assert attrs["binded"] == "1" and attrs["newflag"] == "0", attrs
            assert attrs["lastupdate"].isdigit(), attrs

            # Exact VFG 2025122300 corruption. VFG_CARDMNG_MODE=strict keeps
            # the old quarantine: never turn a malformed inquire into an
            # existing-card success and never mutate stored state.
            os.environ["VFG_CARDMNG_MODE"] = "strict"
            before = copy.deepcopy(server.DB.data)
            attrs = response_attrs(
                server.handle_cardmng(
                    "inquire", request("inquire", cardid=GARBLED_CARD), MODEL
                )
            )
            assert attrs == {"status": "112"}, attrs
            assert server.DB.data == before, server.DB.data

            before = copy.deepcopy(server.DB.data)
            attrs = response_attrs(
                server.handle_cardmng(
                    "getrefid",
                    request("getrefid", cardid=GARBLED_CARD, passwd="<car"),
                    MODEL,
                )
            )
            assert attrs == {"status": "110"}, attrs
            assert server.DB.data == before, server.DB.data
            os.environ.pop("VFG_CARDMNG_MODE", None)

            # Default compat mode maps the corrupted request onto the single
            # configured local identity and answers with the bemaniutils shape,
            # so the PIN screen can finish instead of failing with 110.
            attrs = response_attrs(
                server.handle_cardmng(
                    "getrefid",
                    request("getrefid", cardid=GARBLED_CARD, passwd="<car"),
                    MODEL,
                )
            )
            assert set(attrs) == {"refid", "dataid"}, attrs
            assert attrs["refid"] == attrs["dataid"] == refid, attrs

            # A malformed inquire then resumes the same identity.
            attrs = response_attrs(
                server.handle_cardmng(
                    "inquire", request("inquire", cardid=GARBLED_CARD), MODEL
                )
            )
            assert "status" not in attrs, attrs
            assert attrs["refid"] == refid, attrs

            # VFG_CARDMNG_INQUIRE_MODE=new forces the only inquire shape that
            # has never crashed this dump.
            os.environ["VFG_CARDMNG_INQUIRE_MODE"] = "new"
            attrs = response_attrs(
                server.handle_cardmng("inquire", request("inquire", cardid=CARD), MODEL)
            )
            assert attrs == {"status": "112"}, attrs
            os.environ.pop("VFG_CARDMNG_INQUIRE_MODE", None)

            # Unknown refids cannot create or bind fallback state.
            before_cards = copy.deepcopy(server.DB.data["cards"])
            attrs = response_attrs(
                server.handle_cardmng(
                    "bindmodel", request("bindmodel", refid="A000000000000000"), MODEL
                )
            )
            assert attrs == {"status": "110"}, attrs
            assert server.DB.data["cards"] == before_cards
        finally:
            server.DB = old_db

    print("cardmng regression tests OK")


if __name__ == "__main__":
    main()
