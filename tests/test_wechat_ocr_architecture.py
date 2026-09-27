import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "src" / "wechat_ocr"


def test_shared_core_has_no_automation_package_imports():
    forbidden = {"pywechat", "pyweixin", "contact_exporter"}
    for source in CORE.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not imported.intersection(forbidden), source.name


def test_pywechat_friend_apis_run_shared_ocr_before_uia_and_gate_formal_ids():
    source = (ROOT / "src" / "pywechat" / "WeChatAuto.py").read_text(
        encoding="utf-8-sig"
    )
    tree = ast.parse(source)
    contacts = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "Contacts"
    )
    for name in ("get_friends_info", "get_friends_detail"):
        function = next(
            node
            for node in contacts.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        body = ast.get_source_segment(source, function)
        assert "_inspect_wechat_profile(base_info_pane,main_window)" in body
        assert (
            "profile_ocr_result.value if profile_ocr_result.is_confirmed else '无'"
            in body
        )
        assert body.index(
            "_inspect_wechat_profile(base_info_pane,main_window)"
        ) < body.index("base_info=[element.window_text() for element in base_info]")


def test_pywechat_adapter_uses_shared_result_consensus():
    source = (ROOT / "src" / "pywechat" / "ocr_adapter.py").read_text(encoding="utf-8")
    assert "from wechat_ocr" in source
    assert "reconcile_ocr_result(result, ui_candidate)" in source
    assert 'status="OCR_FAILED"' in source


def test_legacy_pyweixin_module_paths_are_shared_module_aliases():
    import sys

    sys.path.insert(0, str(ROOT / "src"))
    try:
        import pyweixin.ocr.engine as legacy
        import wechat_ocr.engine as shared

        assert legacy is shared
        import pyweixin.ocr.wechat_id as legacy_extractor
        import wechat_ocr.extractor as shared_extractor

        assert legacy_extractor is shared_extractor
    finally:
        sys.path.remove(str(ROOT / "src"))


def test_exporter_uses_shared_validator():
    source = (ROOT / "contact_exporter" / "normalizer.py").read_text(encoding="utf-8")
    assert "from wechat_ocr.validator import validate_wechat_id" in source
    assert "validate_wechat_id(" in source
