"""UI applicability is independent of E2E browser visibility."""
import pytest
from api_agent.config import load_shared_schema, resolve_config

@pytest.mark.parametrize("mode,run_ui,required", [
    ("e2e", "False", True), ("e2e", "True", True),
    ("run-local", "False", False), ("run-local", "True", True),
    ("unit", "False", False), ("unit", "True", False),
    ("integration", "False", False), ("integration", "True", False),
])
def test_ui_required_by_operation(mode, run_ui, required, tmp_path):
    result = resolve_config(load_shared_schema(), environ={"ALLURE_S3_BUCKET": "synthetic-results", "TUI_MODE": mode, "RUN_UI": run_ui}, anchors={"owner_root": tmp_path})
    assert ("UI_SOURCE_DIR" in result.missing) is required
    supplied = resolve_config(load_shared_schema(), environ={"ALLURE_S3_BUCKET": "synthetic-results", "TUI_MODE": mode, "RUN_UI": run_ui, "UI_SOURCE_DIR": "frontend"}, anchors={"owner_root": tmp_path})
    supplied.raise_for_errors()
    assert ("UI_SOURCE_DIR" in supplied.values) is required
    if required:
        assert supplied.values["UI_SOURCE_DIR"] == tmp_path / "frontend"
    else:
        ignored = resolve_config(load_shared_schema(), environ={"ALLURE_S3_BUCKET": "synthetic-results", "TUI_MODE": mode, "RUN_UI": run_ui, "UI_SOURCE_DIR": "", "UI_NX_APP": ""}, anchors={"owner_root": tmp_path})
        ignored.raise_for_errors()

def test_e2e_collection_does_not_require_ui():
    result = resolve_config(load_shared_schema(), environ={"TUI_MODE": "e2e", "RUN_UI": "True", "COLLECT_ONLY": "True"})
    result.raise_for_errors()
    assert "UI_SOURCE_DIR" not in result.values
