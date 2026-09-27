"""Unit tests for contact_exporter.normalizer."""

from __future__ import annotations

import json
from pathlib import Path

from contact_exporter.normalizer import _clean, normalize, normalize_one
from contact_exporter.models import EXPORT_FIELDS

FIXTURE = Path(__file__).parent / "fixtures" / "sample_contacts.json"
EXPORTED_AT = "2026-09-23 15:30:00"


def _load_fixture() -> list[dict]:
    with FIXTURE.open(encoding="utf-8") as fp:
        return json.load(fp)


def test_basic_key_mapping():
    raw = {
        "昵称": "测试用户甲",
        "微信号": "fixture_wx_alpha",
        "微信号状态": "CONFIRMED",
        "微信号来源": "TEST_SOURCE",
        "地区": "TEST_REGION_A",
        "备注": "TEST_REMARK_A",
        "电话": "10000000001",
        "标签": "TEST_TAG_A",
        "来源": "TEST_SOURCE",
    }
    c = normalize_one(raw, EXPORTED_AT)
    assert c.nickname == "测试用户甲"
    assert c.wechat_id == "fixture_wx_alpha"
    assert c.region == "TEST_REGION_A"
    assert c.remark == "TEST_REMARK_A"
    assert c.phone == "10000000001"
    assert c.tags == "TEST_TAG_A"
    assert c.source == "TEST_SOURCE"
    assert c.exported_at == EXPORTED_AT


def test_none_and_sentinel_fields_become_empty():
    raw = {
        "昵称": "测试用户乙",
        "微信号": "无",
        "备注": None,
        "电话": "",
        "地区": "  无  ",
        "标签": "无",
        "来源": "无",
    }
    c = normalize_one(raw, EXPORTED_AT)
    assert c.nickname == "测试用户乙"
    assert c.wechat_id == ""
    assert c.remark == ""
    assert c.phone == ""
    assert c.region == ""
    assert c.tags == ""
    assert c.source == ""


def test_unconfirmed_wechat_candidate_never_enters_export_field():
    c = normalize_one(
        {
            "昵称": "示例",
            "微信号": "abc_def",
            "微信号状态": "NEED_REVIEW",
            "微信号候选": "abc_def",
            "微信号来源": "TEST_SOURCE",
        },
        EXPORTED_AT,
    )
    assert c.wechat_id == ""
    assert c.wechat_id_candidate == "abc_def"
    assert c.wechat_id_status == "NEED_REVIEW"


def test_legacy_raw_wechat_id_without_status_fails_closed():
    c = normalize_one({"昵称": "示例", "微信号": "abc_def"}, EXPORTED_AT)
    assert c.wechat_id == ""
    assert c.wechat_id_candidate == "abc_def"


def test_invalid_value_does_not_pass_even_with_confirmed_status():
    c = normalize_one(
        {"昵称": "示例", "微信号": "not,valid", "微信号状态": "CONFIRMED"}, EXPORTED_AT
    )
    assert c.wechat_id == ""
    assert c.wechat_id_candidate == "not,valid"


def test_missing_keys_default_empty():
    c = normalize_one({"昵称": "测试用户戊"}, EXPORTED_AT)
    assert c.nickname == "测试用户戊"
    assert c.wechat_id == ""
    assert c.phone == ""
    assert c.source == ""


def test_clean_helper():
    assert _clean(None) == ""
    assert _clean("无") == ""
    assert _clean("") == ""
    assert _clean("  无  ") == ""
    assert _clean("北京") == "北京"
    assert _clean(123) == "123"


def test_chinese_values_preserved():
    raw = {"昵称": "测试用户丙", "地区": "TEST_REGION_C", "标签": "TEST_TAG_C"}
    c = normalize_one(raw, EXPORTED_AT)
    assert c.nickname == "测试用户丙"
    assert c.region == "TEST_REGION_C"
    assert c.tags == "TEST_TAG_C"


def test_special_characters_do_not_break_structure():
    # Outer whitespace is stripped; inner comma/quote/newline/tab preserved.
    raw = {
        "昵称": '逗号,引号"换行\n制表\t中',
        "备注": 'TEST_REMARK,"甲, Inc.',
        "微信号": "weird,id",
    }
    c = normalize_one(raw, EXPORTED_AT)
    assert c.nickname == '逗号,引号"换行\n制表\t中'
    assert c.remark == 'TEST_REMARK,"甲, Inc.'
    assert c.wechat_id == ""
    assert c.wechat_id_candidate == "weird,id"


def test_deduplicate_default_drops_duplicates():
    contacts = normalize(_load_fixture(), EXPORTED_AT, deduplicate=True)
    # fixture has 5 entries; entries 0 and 4 are identical (same wechat_id).
    assert len(contacts) == 4
    ids = [c.wechat_id for c in contacts]
    assert ids.count("fixture_wx_alpha") == 1


def test_no_deduplicate_keeps_all():
    contacts = normalize(_load_fixture(), EXPORTED_AT, deduplicate=False)
    assert len(contacts) == 5


def test_deduplicate_by_nickname_when_no_wechat_id():
    raw = [
        {"昵称": "小明", "备注": "无", "地区": "杭州"},
        {"昵称": "小明", "备注": "无", "地区": "杭州"},
        {"昵称": "小明", "备注": "无", "地区": "成都"},
    ]
    contacts = normalize(raw, EXPORTED_AT, deduplicate=True)
    # first and second identical -> dedup; third differs by region -> kept
    assert len(contacts) == 2


def test_export_fields_order_stable():
    raw = [{"昵称": "x", "来源": "s", "微信号": "w"}]
    c = normalize_one(raw, EXPORTED_AT)
    assert list(c.as_dict().keys()) == EXPORT_FIELDS


def test_non_dict_entries_skipped():
    raw = [{"昵称": "ok"}, "garbage", None, 42, ["list"]]
    contacts = normalize(raw, EXPORTED_AT, deduplicate=True)
    assert len(contacts) == 1
    assert contacts[0].nickname == "ok"


def test_empty_input():
    assert normalize([], EXPORTED_AT) == []


def test_mock_reader_feeds_normalizer():
    from contact_exporter.reader import MockContactReader

    reader = MockContactReader(_load_fixture())
    raw = reader.read_contacts()
    contacts = normalize(raw, EXPORTED_AT, deduplicate=True)
    assert len(contacts) == 4
    assert all(c.exported_at == EXPORTED_AT for c in contacts)
