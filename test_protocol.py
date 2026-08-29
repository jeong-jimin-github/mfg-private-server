"""Smoke-test kbin + RC4 + LZ77 round-trip for a services.get response."""

from protocol import decode_eamuse_body, encode_eamuse_body, parse_eamuse_info
from server import handle_services_get, handle_facility_get, xml_response, handle_appli_boot
from xml.etree import ElementTree as ET

INFO = "1-01234567-89ab"


def roundtrip(xml: str, compress: str = "lz77") -> str:
    blob = encode_eamuse_body(xml, INFO, compress, True)
    text, used_kbin = decode_eamuse_body(blob, INFO, compress)
    assert used_kbin, "expected kbin"
    ET.fromstring(text)
    return text


def main() -> None:
    assert parse_eamuse_info(INFO) == INFO
    s = handle_services_get(ET.Element("call"), "VFG:J:A:A:2025122300")
    print("services.get", len(roundtrip(s)), "bytes xml after decode")
    f = handle_facility_get(ET.Element("call"), "VFG:J:A:A:2025122300")
    print("facility.get", len(roundtrip(f)), "bytes xml after decode")
    a = handle_appli_boot({})
    print("appli_boot xml ok", "serv_st" in a)
    print("OK")


if __name__ == "__main__":
    main()
