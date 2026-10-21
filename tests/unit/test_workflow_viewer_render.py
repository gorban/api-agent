"""Focused controls for versioned workflow graphics and static navigation."""

from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from api_agent.workflows.viewer_render import (
    PlantUMLRenderer, RenderError, ViewBundle, ViewFile, _navigation,
    _companion, _companion_route, _projection_source, _route,
    _svg_route, check_view_bundle, validate_svg,
    verify_bundle_proof, build_view_bundle,
)
from api_agent.workflows import viewer_build
from api_agent.workflows import viewer_render


def _projection():
    document = SimpleNamespace(logical_path="base/flow.md.html", stable_id="graph")
    child = SimpleNamespace(logical_path="base/workflows/components/task.md.html", stable_id="task")
    step = SimpleNamespace(id="task", kind="node", parent=None, owner_path=document.logical_path,
                           declaration_path=child.logical_path)
    edges = (SimpleNamespace(source="START", target="task", condition="always"),
             SimpleNamespace(source="task", target="END", condition="ready"))
    return SimpleNamespace(entry_path=document.logical_path, documents=(document, child),
                           steps=(step,), edges=edges, references=(), resources=()), document


def test_projection_source_preserves_effective_routes_and_admitted_links():
    projection, document = _projection()

    source, links = _projection_source(projection, document)

    assert b'start --> n0 : always' in source
    assert b'n0 --> end : ready' in source
    assert "../../workflows/components/task.md.html" in links
    assert b"!include" not in source


def test_navigation_uses_portable_relative_links():
    projection, document = _projection()

    navigation = _navigation(projection, document)

    assert navigation.startswith(b"![Workflow diagram](.private_resources/diagrams/flow.svg)")
    assert b"[Open standalone diagram](.private_resources/diagrams/flow.svg)" in navigation
    assert b"- [task](workflows/components/task.md.html)" in navigation


def test_companion_markdown_links_resolve_to_declared_targets():
    projection, document = _projection()

    body = _companion(projection, document).decode()
    folder = posixpath.dirname(_companion_route(document.logical_path))
    targets = {posixpath.normpath(posixpath.join(folder, link))
               for link in re.findall(r"\]\(([^)]+)\)", body)}

    assert targets == {_svg_route(document.logical_path), _route(document.logical_path),
                       _companion_route(projection.documents[1].logical_path)}


@pytest.mark.parametrize("malicious", [
    b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10"><script/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10" onload="alert(1)"/>',
    b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10"><a href="https://example.com"/></svg>',
    b'<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10"/>',
])
def test_svg_validator_rejects_active_or_external_content(malicious):
    with pytest.raises(RenderError):
        validate_svg(malicious)


@pytest.mark.parametrize("attribute", [
    b'width="999999999" height="1" viewBox="0 0 10 10"',
    b'width="NaN" height="1" viewBox="0 0 10 10"',
    b'width="10" height="10" viewBox="0 0 999999999 10"',
    b'width="10" height="10" viewBox="0 0 10 10" style="width:999999999px;"',
])
def test_svg_rejects_unbounded_dimensions(attribute):
    data = b'<svg xmlns="http://www.w3.org/2000/svg" ' + attribute + b'/>'
    with pytest.raises(RenderError):
        validate_svg(data)


def test_bundle_check_rejects_graphic_tampering(tmp_path):
    root = tmp_path / "project"
    diagram = root / ".private_resources/diagrams/flow.svg"
    diagram.parent.mkdir(parents=True)
    diagram.write_bytes(b"modified")
    bundle = ViewBundle("r/base/flow.md.html", (ViewFile("r/base/.private_resources/diagrams/flow.svg",
                                                     "image/svg+xml", b"expected"),))

    with pytest.raises(RenderError, match="stale"):
        check_view_bundle(bundle, roots={"base": root})


def test_bundle_check_rejects_metadata_only_repair_of_tampered_graphic(tmp_path):
    root = tmp_path / "project"
    diagram = root / ".private_resources/diagrams/flow.svg"
    diagram.parent.mkdir(parents=True)
    expected = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>'
    diagram.write_bytes(expected.replace(b"svg", b"SVg", 1))
    bundle = ViewBundle("r/base/flow.md.html", (ViewFile("r/base/.private_resources/diagrams/flow.svg",
                                                     "image/svg+xml", expected),))

    with pytest.raises(RenderError, match="stale"):
        check_view_bundle(bundle, roots={"base": root})


def test_special_declaration_path_is_encoded_before_puml_generation():
    assert _route("base/workflows/bad] name.md.html") == "r/base/workflows/bad%5D%20name.md.html"
















def test_source_tool_runs_without_ambient_import_path():
    source = Path(__file__).resolve().parents[2]
    environment = dict(__import__("os").environ, PYTHONPATH="")

    result = subprocess.run([sys.executable, "-I", str(source / "tools/workflow_views.py"), "--help"],
                            cwd=source, env=environment, text=True, capture_output=True, timeout=10)

    assert result.returncode == 0
    assert "prepare,generate,check" in result.stdout




def test_selected_hosted_flow_uses_admitted_replacement_without_original(tmp_path):
    from api_agent.workflows.definition import Replacement, load_definition
    from api_agent.workflows.viewer_projection import project_view

    base, pack = tmp_path / "base", tmp_path / "pack"
    base.mkdir()
    pack.mkdir()
    (base / "flow.md.html").write_text(
        "# Workflow v2\n\n## Graph ID\n\n| ID |\n| --- |\n| ticket |\n\n"
        "## Flow\n\n1.  [node classify](missing-original.md.html)\n")
    (pack / "chosen.md.html").write_text(
        "# Node v2: classify\n\n## Native\n\n| Registry | Kind | Source |\n"
        "| --- | --- | --- |\n| classify | deterministic | [code](worker.py) |\n\n"
        "## Functions\n\n| ID | Registry | Input | Output |\n| --- | --- | --- | --- |\n"
        "| classify | classify | [input](schema.json) | [output](schema.json) |\n")
    (pack / "worker.py").write_text("def classify(inputs, context):\n    return {}\n")
    (pack / "schema.json").write_text('{"type":"object"}')
    selected = project_view(load_definition(base / "flow.md.html", base_root=base, pack_root=pack,
                                            replacements=(Replacement("classify", Path("chosen.md.html"), "pack"),)))

    class StubRenderer:
        def render(self, source, *, allowed_links=frozenset()):
            return b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" viewBox="0 0 1 1"/>'

    bundle = build_view_bundle(selected, roots={"base": base, "pack": pack},
                               renderer=StubRenderer(), verify=False)
    pages = {file.path: file.body for file in bundle.files}

    assert b"missing-original.md.html" not in pages["r/base/flow.md.html"]
    assert b"../pack/chosen.md.html" in pages["r/base/flow.md.html"]
    assert not (base / "missing-original.md.html").exists()


def test_missing_installed_assets_cannot_fall_back_to_sibling_tree(tmp_path, monkeypatch):
    module = tmp_path / "src/api_agent/workflows/viewer_render.py"
    module.parent.mkdir(parents=True)
    module.write_text("# installed copy")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='api-agent'\n")
    decoy = tmp_path / ".private_resources/viewer"
    decoy.mkdir(parents=True)
    (decoy / "viewer.js").write_text("decoy")
    (tmp_path / "src/api_agent-0.1.dist-info").mkdir()
    monkeypatch.setattr(viewer_render, "__file__", str(module))

    with pytest.raises(RenderError, match="assets missing"):
        viewer_render._asset_root()


@pytest.mark.parametrize('installed', [False, True])
def test_bundle_missing_dependencies_provides_source_or_installed_guidance(tmp_path, monkeypatch, installed):
    projection = SimpleNamespace(version=1, documents=(), projection_sha256='0' * 64)
    source = Path(viewer_render.__file__).resolve().parents[3] / '.private_resources/viewer'
    monkeypatch.setattr(viewer_render, '_asset_root', lambda: tmp_path if installed else source)
    monkeypatch.setattr(viewer_render, '_read_view_file', lambda path: None)
    expected = 'reinstall api-agent' if installed else 'run npm ci from the repository root'
    with pytest.raises(RenderError, match=expected):
        build_view_bundle(projection, roots={}, renderer=object(), verify=False)
