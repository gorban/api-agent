"""Focused controls for versioned workflow graphics and static navigation."""

import os
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


def _qa_prerequisites():
    jar = Path(os.environ.get("API_AGENT_TEST_PLANTUML_JAR") or
               os.environ.get("API_AGENT_PLANTUML_JAR") or
               Path.home() / ".cache/api-agent/plantuml/plantuml-mit-1.2026.9.jar")
    java = os.environ.get("API_AGENT_TEST_JAVA") or shutil.which("java")
    if not jar.is_file():
        pytest.fail(f"viewer QA requires the pinned PlantUML JAR: {jar}; set API_AGENT_PLANTUML_JAR")
    if not java or not Path(java).is_file():
        pytest.fail("viewer QA requires Java on PATH or API_AGENT_TEST_JAVA")
    return jar, java


def test_real_pinned_smetana_renderer_when_prerequisites_available(monkeypatch):
    jar, java = _qa_prerequisites()
    renderer = PlantUMLRenderer(jar, java)

    first = renderer.render(b'@startuml\nrectangle "Alpha" as n0\n@enduml\n')
    second = renderer.render(b'@startuml\nrectangle "Alpha" as n0\n@enduml\n')

    assert first == second
    assert b"Alpha" in first
    assert b"plantuml-src" not in first


def test_renderer_timeout_kills_process_and_removes_temporary_puml(tmp_path):
    jar, _ = _qa_prerequisites()
    slow_java = tmp_path / "slow-java"
    slow_java.write_text("#!/bin/sh\nexec sleep 10\n")
    slow_java.chmod(0o755)
    before = set(Path(tempfile.gettempdir()).glob("api-agent-diagram-*"))
    started = time.monotonic()

    with pytest.raises(RenderError, match="timed out"):
        PlantUMLRenderer(jar, str(slow_java), timeout=0.2).render(b"@startuml\nA\n@enduml\n")

    assert time.monotonic() - started < 3
    assert set(Path(tempfile.gettempdir()).glob("api-agent-diagram-*")) == before


@pytest.fixture
def copied_source_view(tmp_path):
    source = Path(__file__).resolve().parents[2]
    root = tmp_path / "view"
    root.mkdir()
    for name in ("workflows", "demo", "cfg", ".private_resources"):
        shutil.copytree(source / name, root / name)
    shutil.copy2(source / "flow.md.html", root / "flow.md.html")
    return root


def _qa_renderer():
    jar, java = _qa_prerequisites()
    return PlantUMLRenderer(jar, java)


def test_generation_preserves_edit_after_admission(copied_source_view, monkeypatch):
    renderer = _qa_renderer()
    flow = copied_source_view / "flow.md.html"
    original = flow.read_bytes()
    edited = original + b"\n<!-- concurrent human edit -->\n"
    project = viewer_build._projection

    def edit_after_projection(root):
        result = project(root)
        flow.write_bytes(edited)
        return result

    monkeypatch.setattr(viewer_build, "_projection", edit_after_projection)
    with pytest.raises(RenderError, match="admitted workflow source changed"):
        viewer_build.generate_views(copied_source_view, renderer=renderer)
    assert flow.read_bytes() == edited


def test_generation_refuses_unknown_orphan(copied_source_view):
    renderer = _qa_renderer()
    orphan = copied_source_view / ".private_resources/diagrams/stray.md"
    orphan.write_text("human-authored note")

    with pytest.raises(RenderError, match="orphaned or edited"):
        viewer_build.generate_views(copied_source_view, renderer=renderer, fix_orphans=True)

    assert orphan.read_text() == "human-authored note"


def test_explicit_fix_removes_unchanged_owned_orphan(copied_source_view):
    renderer = _qa_renderer()
    diagrams = copied_source_view / ".private_resources/diagrams"
    orphan = diagrams / "stray.md"
    orphan.write_bytes((diagrams / "flow.md").read_bytes())

    repaired = viewer_build.generate_views(copied_source_view, renderer=renderer,
                                           fix_orphans=True, confirmed_orphans=(orphan,))

    assert orphan in repaired
    assert not orphan.exists()
    viewer_build.check_views(copied_source_view)


def test_bundle_route_proof_detects_mutated_body(copied_source_view):
    renderer = _qa_renderer()
    projection = viewer_build._projection(copied_source_view)
    bundle = build_view_bundle(projection, roots={"base": copied_source_view, "pack": copied_source_view},
                               renderer=renderer)
    changed_file = replace(bundle.files[0], body=bundle.files[0].body + b"changed")
    changed_bundle = replace(bundle, files=(changed_file, *bundle.files[1:]))

    with pytest.raises(RenderError, match="route digest"):
        verify_bundle_proof(changed_bundle, projection)


def test_publication_failure_restores_earlier_owned_output(copied_source_view, monkeypatch):
    renderer = _qa_renderer()
    diagrams = copied_source_view / ".private_resources/diagrams"
    first, second = diagrams / "flow.md", diagrams / "workflows/components/renderer.md"
    first.write_bytes(first.read_bytes() + b"\nlocal edit\n")
    second.write_bytes(second.read_bytes() + b"\nlocal edit\n")
    before = (first.read_bytes(), second.read_bytes())
    actual_replace = viewer_build.os.replace
    calls = 0

    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated second write failure")
        actual_replace(source, target)

    monkeypatch.setattr(viewer_build.os, "replace", fail_second)
    with pytest.raises(OSError, match="second write"):
        viewer_build.generate_views(copied_source_view, renderer=renderer)

    assert (first.read_bytes(), second.read_bytes()) == before
