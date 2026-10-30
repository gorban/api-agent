"""The reviewed built-in LLM cases declare their actual credential owners."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / 'integration'


def test_selected_builtin_llm_cases_have_explicit_owner_manifests():
    expected = {
        'test_llm_request.py': {'coordinator'},
        'test_workflow.py': {'workflow'},
        'test_tool_alternate.py': {'tools', 'workflow'},
        'test_tool_instances.py': {'tools', 'workflow'},
        'test_studio_request.py': {'ui'},
        'test_studio_selection.py': {'ui'},
        'test_reasoning_contrast.py': {'ui'},
    }
    for filename, owners in expected.items():
        module = ast.parse((ROOT / filename).read_text())
        matches = [node for node in ast.walk(module)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr == 'credential_authorities']
        assert len(matches) == 1, filename
        assert all(isinstance(value, ast.Constant) and isinstance(value.value, str) for value in matches[0].args)
        assert {value.value for value in matches[0].args} == owners
        assert not matches[0].keywords
