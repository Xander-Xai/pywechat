"""Read-only validation of the Owner-designated 151-row delivery pair."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

CSV_PATH = ROOT / "output/final/wechat_contacts_151.csv"
TXT_PATH = ROOT / "output/final/wechat_contacts_151.txt"
CHECKPOINT_PATH = ROOT / "output/final/fixed_geometry_checkpoint.json"
REPORT_PATH = ROOT / "output/baseline_validation_151/validation.json"
OLD_CSV_SHA256 = "93F9A470893CD6923ACDA09BD43E8D8BBEFBF201778E702955DD627B0D814E6C".lower()
OLD_TXT_SHA256 = "9FFEF6A44136B36F335F8A1A594C61C057C518610AE50CEF0033B68F50DCF2FD".lower()


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _candidate_removal_matches(
    data: bytes, expected: str, *, skip_header: bool = False
) -> list[int]:
    lines = data.splitlines(keepends=True)
    return [
        index + 1
        for index in range(1 if skip_header else 0, len(lines))
        if _hash(b"".join(lines[:index] + lines[index + 1 :])) == expected
    ]


def validate() -> dict:
    from wechat_ocr.masking import contains_mask_placeholder
    from wechat_ocr.validator import validate_wechat_id

    csv_bytes = CSV_PATH.read_bytes()
    txt_bytes = TXT_PATH.read_bytes()
    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["wechat_id"]:
            raise ValueError("CSV_SCHEMA_INVALID")
        csv_ids = [row.get("wechat_id") or "" for row in reader]
    txt_ids = TXT_PATH.read_text(encoding="utf-8-sig").splitlines()
    all_values = csv_ids + txt_ids
    checkpoint = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    checkpoint_id_hashes = set(checkpoint.get("confirmed_frame_hashes", {}))
    current_id_hashes = {_hash(value.encode("utf-8")) for value in csv_ids}
    csv_removals = _candidate_removal_matches(
        csv_bytes, OLD_CSV_SHA256, skip_header=True
    )
    txt_removals = _candidate_removal_matches(txt_bytes, OLD_TXT_SHA256)
    report = {
        "report": "BASELINE_151_BASIC_VALIDATION",
        "csv": {
            "path": "output/final/wechat_contacts_151.csv",
            "bytes": len(csv_bytes),
            "sha256": _hash(csv_bytes),
            "encoding": "UTF-8" if not csv_bytes.startswith(b"\xef\xbb\xbf") else "UTF-8 with BOM",
            "newline": "CRLF" if b"\r\n" in csv_bytes else "LF",
            "rows": len(csv_ids),
            "unique": len(set(csv_ids)),
        },
        "txt": {
            "path": "output/final/wechat_contacts_151.txt",
            "bytes": len(txt_bytes),
            "sha256": _hash(txt_bytes),
            "encoding": "UTF-8" if not txt_bytes.startswith(b"\xef\xbb\xbf") else "UTF-8 with BOM",
            "newline": "CRLF" if b"\r\n" in txt_bytes else "LF",
            "rows": len(txt_ids),
            "unique": len(set(txt_ids)),
        },
        "csv_txt_same_order": csv_ids == txt_ids,
        "blank_values": sum(not value.strip() for value in all_values),
        "masked_values": sum(contains_mask_placeholder(value) for value in all_values),
        "invalid_values": sum(not validate_wechat_id(value).valid for value in all_values),
        "historical_150": {
            "csv_one_record_removal_hash_match_count": len(csv_removals),
            "txt_one_record_removal_hash_match_count": len(txt_removals),
            "candidate_id_hashes_matching_checkpoint": len(current_id_hashes & checkpoint_id_hashes),
            "checkpoint_confirmed_count": checkpoint.get("confirmed_count", len(checkpoint.get("confirmed", []))),
            "provenance": "HISTORICAL_PROVENANCE_UNVERIFIED",
        },
    }
    report["status"] = "PASS" if (
        report["csv"]["rows"] == report["txt"]["rows"] == 151
        and report["csv"]["unique"] == report["txt"]["unique"] == 151
        and report["csv_txt_same_order"]
        and report["blank_values"] == report["masked_values"] == report["invalid_values"] == 0
    ) else "BLOCKED / 151_BASELINE_INVALID"
    return report


def main() -> int:
    report = validate()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "csv_rows": report["csv"]["rows"],
        "txt_rows": report["txt"]["rows"],
        "csv_txt_same_order": report["csv_txt_same_order"],
        "blank_values": report["blank_values"],
        "masked_values": report["masked_values"],
        "invalid_values": report["invalid_values"],
        "historical_provenance": report["historical_150"]["provenance"],
    }))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
