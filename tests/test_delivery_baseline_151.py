from tools.validate_delivery_151 import validate


def test_owner_designated_delivery_151_is_frozen_and_consistent():
    report = validate()
    assert report["status"] == "PASS"
    assert report["csv"]["rows"] == report["txt"]["rows"] == 151
    assert report["csv"]["unique"] == report["txt"]["unique"] == 151
    assert report["csv_txt_same_order"] is True
    assert report["blank_values"] == 0
    assert report["masked_values"] == 0
    assert report["invalid_values"] == 0
    assert report["csv"]["sha256"] == (
        "ef63f3a3a24abcbb17be76eedd4e6cfb02822daf19512266da3425392695a06b"
    )
    assert report["txt"]["sha256"] == (
        "aa48d0c82a9f03ee758f791110430ef82476874802f8818086bc6d8b68caf72f"
    )
    assert report["historical_150"]["provenance"] == "HISTORICAL_PROVENANCE_UNVERIFIED"
