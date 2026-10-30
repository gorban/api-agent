"""Portable controls for viewer routes, ownership, and renderer preparation."""

from dataclasses import replace
from hashlib import sha256
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from multiprocessing import active_children
from pathlib import Path
import shutil
from threading import Event, Thread
import time
from types import SimpleNamespace
import runpy
import subprocess
import sys

import pytest

from api_agent.workflows import viewer_build
from api_agent.workflows import viewer_render
from api_agent.workflows.viewer_render import (
    PlantUMLRenderer, RenderError, ViewBundle, ViewFile, ViewRoute, build_view_bundle,
    _annotate_hosted_resources, _build_view_bundle, _physical_file, _projection_source,
    _route, _svg_route, check_view_bundle,
    validate_svg, verify_bundle_proof,
)


class TinyRenderer:
    def render(self, source, *, allowed_links=frozenset()):
        return (b'<?xml version="1.0"?>\n'
                b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" viewBox="0 0 1 1"/>')


@pytest.mark.parametrize("attribute", [
    b'style="width:10px;WIDTH:999999999px"',
    b'viewBox="-999999999999 0 10 10"',
])
def test_svg_rejects_css_override_and_large_origin(attribute):
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
           b'viewBox="0 0 10 10" ' + attribute + b'/>' if b'style' in attribute else
           b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" ' + attribute + b'/>')

    with pytest.raises(RenderError):
        validate_svg(svg)


@pytest.mark.parametrize("shape", [
    b'<path d="M1e1000 1e1000"/>',
    b'<rect x="999999999999999999999" width="1" height="1"/>',
    b'<g transform="scale(1e1000)"/>',
    b'<g transform="scale(101)"/>',
    b'<g transform="evil(1)"/>',
    b'<polygon points="0,0 1e1000,1"/>',
    b'<text textLength="1e1000">label</text>',
])
def test_svg_rejects_unbounded_child_geometry(shape):
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
           b'viewBox="0 0 10 10">' + shape + b'</svg>')

    with pytest.raises(RenderError, match="SVG geometry|SVG transform"):
        validate_svg(svg)


def test_svg_accepts_bounded_alternative_geometry():
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="12" height="10" '
           b'viewBox="0 0 12 10"><g transform="translate(1, 2) scale(1.1)">'
           b'<path d="M1,2 L3,4"/><text x="3" textLength="5">label</text></g></svg>')

    assert b"label" in validate_svg(svg)


def test_svg_accepts_plantuml_pixel_dimensions():
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="12px" height="10px" '
           b'viewBox="0 0 12 10"><rect x="1" y="2" width="5px" height="4px"/></svg>')

    assert b'width="12px"' in validate_svg(svg)


@pytest.mark.parametrize("route", [
    "r/base/%2e%2e/escape.svg",
    "r/base/a%2Fb.svg",
    "r/base/a%5Cb.svg",
    "r/other/diagram.svg",
    "base/diagram.svg",
])
def test_physical_route_rejects_noncanonical_or_unadmitted_owner(tmp_path, route):
    with pytest.raises(RenderError):
        _physical_file(route, {"base": tmp_path})


def test_physical_route_rejects_symlinked_output(tmp_path):
    (tmp_path / "diagram.svg").symlink_to(tmp_path / "outside.svg")

    with pytest.raises(RenderError, match="symlink"):
        _physical_file("r/base/diagram.svg", {"base": tmp_path})


@pytest.mark.parametrize("logical", ["else/flow.md.html", "base/a?b.md.html",
                                           "base/a\\b.md.html", "base/a#b.md.html"])
def test_projection_route_rejects_unsafe_path(logical):
    with pytest.raises(RenderError, match="invalid projection path"):
        _route(logical)


def test_svg_route_requires_declaration_suffix():
    with pytest.raises(RenderError, match="invalid declaration path"):
        _svg_route("base/diagram.svg")


def test_physical_route_rejects_parent_swapped_to_outside_after_symlink_scan(tmp_path, monkeypatch):
    root = tmp_path / "root"
    folder = root / "assets"
    folder.mkdir(parents=True)
    (folder / "file.txt").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.txt").write_bytes(b"outside")
    target = folder / "file.txt"
    original_resolve = Path.resolve
    swapped = False

    def swap_before_final_resolve(path, *args, **kwargs):
        nonlocal swapped
        if path == target and not swapped:
            swapped = True
            folder.rename(root / "saved-assets")
            folder.symlink_to(outside, target_is_directory=True)
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", swap_before_final_resolve)
    with pytest.raises(RenderError, match="escapes owner"):
        _physical_file("r/base/assets/file.txt", {"base": root})
    assert swapped
    assert (root / "saved-assets/file.txt").read_bytes() == b"inside"
    assert (outside / "file.txt").read_bytes() == b"outside"


def test_hosted_view_labels_only_actual_captured_nonserved_links():
    raw = (b"[config](../cfg/settings.json#field) and [code](../native/worker.py)\n"
           b"[selected](../other.md.html)\n"
           b"`[inline](../cfg/settings.json)`\n"
           b"``multiline code\n[inline next line](../cfg/settings.json)\nend``\n"
           b"```text\n[example](../cfg/settings.json)\n```\n")
    document = SimpleNamespace(logical_path="base/workflows/task.md.html", executable_bytes=raw)
    selected = SimpleNamespace(logical_path="base/other.md.html")
    config = SimpleNamespace(logical_path="base/cfg/settings.json", kind="schema", sha256="a" * 64)
    code = SimpleNamespace(logical_path="base/native/worker.py", kind="native_source",
                           sha256=__import__("hashlib").sha256(b"source").hexdigest(), content_bytes=b"source")
    projection = SimpleNamespace(documents=(document, selected), resources=(config, code))

    hosted = _annotate_hosted_resources(projection, document, raw.splitlines(keepends=True))

    assert b"[config](../cfg/settings.json#field)" not in hosted
    assert b"captured schema: base/cfg/settings.json; SHA-256 " + b"a" * 64 in hosted
    assert b"[code](../native/worker.py)" in hosted
    assert b"[selected](../other.md.html)" in hosted
    assert b"`[inline](../cfg/settings.json)`" in hosted
    assert b"[inline next line](../cfg/settings.json)" in hosted
    assert b"[example](../cfg/settings.json)" in hosted
    assert document.executable_bytes == raw


@pytest.mark.parametrize("source,annotated", [
    (b"[config](../cfg/settings.json)\n", True),
    (b"![config](../cfg/settings.json)\n", False),
    (b"\\[config](../cfg/settings.json)\n", False),
    (b"[config](https://example.test/cfg/settings.json)\n", False),
    (b"[config](../cfg/settings.json?query=1)\n", False),
    (b"[config](../../cfg/settings.json)\n", False),
    (b"[config](../cfg/missing.json)\n", False),
    (b"[config](../cfg/settings.json%5Cbad)\n", False),
    (b"[config](../cfg/settings.json%01)\n", False),
    (b"`unmatched [config](../cfg/settings.json)\n", True),
    (b"``[config](../cfg/settings.json)``\n", False),
    (b"    [config](../cfg/settings.json)\n", False),
    (b"<!-- [config](../cfg/settings.json) -->\n", False),
    (b"~~~md\n[config](../cfg/settings.json)\n~~~\n", False),
])
def test_hosted_reference_annotation_obeys_markdown_and_admitted_path(source, annotated):
    document = SimpleNamespace(logical_path="base/workflows/task.md.html")
    resource = SimpleNamespace(logical_path="base/cfg/settings.json", kind="schema", sha256="a" * 64)
    projection = SimpleNamespace(documents=(document,), resources=(resource,))
    result = _annotate_hosted_resources(projection, document, source.splitlines(keepends=True))
    assert (b"captured schema:" in result) is annotated
    if not annotated:
        assert result == source


def test_hosted_reference_annotation_ignores_invalid_utf8_link_target():
    document = SimpleNamespace(logical_path="base/workflows/task.md.html")
    resource = SimpleNamespace(logical_path="base/cfg/settings.json", kind="schema", sha256="a" * 64)
    projection = SimpleNamespace(documents=(document,), resources=(resource,))
    source = b"[config](../cfg/settings.json\xff)\n"
    assert _annotate_hosted_resources(projection, document, [source]) == source


def test_hosted_annotation_skips_code_closer_inside_fenced_example():
    document = SimpleNamespace(logical_path="base/workflows/task.md.html")
    resource = SimpleNamespace(logical_path="base/cfg/settings.json", kind="schema", sha256="a" * 64)
    projection = SimpleNamespace(documents=(document,), resources=(resource,))
    source = (b"`opening\n~~~md\n` inside fence\n~~~\n"
              b"[config](../cfg/settings.json) closing`\n")
    result = _annotate_hosted_resources(projection, document, source.splitlines(keepends=True))
    assert result == source


def test_hosted_annotation_requires_matching_fence_length():
    document = SimpleNamespace(logical_path="base/workflows/task.md.html")
    resource = SimpleNamespace(logical_path="base/cfg/settings.json", kind="schema", sha256="a" * 64)
    projection = SimpleNamespace(documents=(document,), resources=(resource,))
    source = (b"````markdown\n```\n[config](../cfg/settings.json)\n````\n")
    assert _annotate_hosted_resources(projection, document, source.splitlines(keepends=True)) == source


def test_hosted_bundle_annotates_captured_resources_without_source_write():
    root = Path(__file__).resolve().parents[2]
    flow = root / "flow.md.html"
    nested = root / "workflows/components/agent.md.html"
    before = (flow.read_bytes(), nested.read_bytes())
    projection = viewer_build._projection(root)

    bundle = build_view_bundle(projection, roots={"base": root, "pack": root},
                               renderer=TinyRenderer(), verify=False)

    pages = {file.path: file.body for file in bundle.files}
    hosted = pages["r/base/workflows/components/agent.md.html"]
    assert b"captured native_source:" not in hosted
    assert b"[Python]" in hosted
    assert b"[input](../contracts/llm_call_input.json)" in hosted
    assert b"[output](../contracts/llm_call_output.json)" in hosted
    assert pages["r/base/workflows/contracts/llm_call_input.json"] == (
        root / "workflows/contracts/llm_call_input.json").read_bytes()
    assert b"../../flow.md.html" in hosted
    assert (flow.read_bytes(), nested.read_bytes()) == before


def test_projection_source_renders_component_children_and_schema_fields():
    root = SimpleNamespace(logical_path="base/flow.md.html")
    component = SimpleNamespace(logical_path="base/component.md.html")
    child = SimpleNamespace(logical_path="base/child.md.html")
    group = SimpleNamespace(id="group", kind="component", parent=None,
                            declaration_path=component.logical_path)
    task = SimpleNamespace(id="task", kind="node", parent="group",
                           declaration_path=child.logical_path)
    edges = (SimpleNamespace(owner="GRAPH", source="START", target="group", condition="always"),
             SimpleNamespace(owner="GRAPH", source="group", target="END", condition="always"),
             SimpleNamespace(owner="group", source="START", target="task", condition="always"),
             SimpleNamespace(owner="group", source="task", target="END", condition="ready"))
    content = b'{"properties":{"name":{},"age":{}}}'
    digest = sha256(content).hexdigest()
    reference = SimpleNamespace(owner_path=child.logical_path, owner_id="task", kind="functions", target_path="base/schema.json",
                                target_sha256=digest,
                                attributes=(("field", "Input"),))
    resource = SimpleNamespace(logical_path="base/schema.json",
                               sha256=digest, content_bytes=content)
    projection = SimpleNamespace(entry_path=root.logical_path, documents=(root, component, child),
                                 steps=(group, task), edges=edges, references=(reference,),
                                 resources=(resource,))

    source, links = _projection_source(projection, root)

    local_source, local_links = _projection_source(projection, component)

    assert b'rectangle "group" as n0' in source
    assert b"package " not in source
    assert b"task" not in source
    assert b'rectangle "task" as n0' in local_source
    assert b'Input: schema&#46;json' in local_source
    assert b'n0 --> end : ready' in local_source
    assert len(links) == 1
    assert len(local_links) == 2


@pytest.mark.parametrize("content", [b"not-json", b"x" * 70000])
def test_projection_source_rejects_unusable_schema_details(content):
    root = SimpleNamespace(logical_path="base/flow.md.html")
    task = SimpleNamespace(id="task", kind="node", parent=None,
                           declaration_path=root.logical_path)
    digest = sha256(content).hexdigest()
    reference = SimpleNamespace(owner_id="task", kind="functions", target_path="base/schema.json",
                                target_sha256=digest,
                                attributes=(("field", "Input"),))
    resource = SimpleNamespace(logical_path="base/schema.json", sha256=digest,
                               content_bytes=content)
    projection = SimpleNamespace(entry_path=root.logical_path, documents=(root,), steps=(task,),
                                 edges=(), references=(reference,), resources=(resource,))

    with pytest.raises(RenderError, match='reference|display'):
        _projection_source(projection, root)


def test_projection_source_drops_unadmitted_edge_and_caps_encoded_diagram():
    document = SimpleNamespace(logical_path="base/flow.md.html")
    task = SimpleNamespace(id="task", kind="node", parent=None,
                           declaration_path=document.logical_path)
    ghost_edge = SimpleNamespace(owner="GRAPH", source="task", target="missing", condition="unadmitted")
    projection = SimpleNamespace(entry_path=document.logical_path, documents=(document,),
                                 steps=(task,), edges=(ghost_edge,), references=(), resources=())
    source, _ = _projection_source(projection, document)
    assert b"unadmitted" not in source
    large_edge = SimpleNamespace(owner="GRAPH", source="task", target="task", condition="A" * 240)
    projection.edges = (large_edge,) * 999
    with pytest.raises(RenderError, match="diagram exceeds input limit"):
        _projection_source(projection, document)


def test_bundle_rejects_extra_route_with_matching_digest():
    root = Path(__file__).resolve().parents[2]
    projection = viewer_build._projection(root)
    bundle = build_view_bundle(projection, roots={"base": root, "pack": root},
                               renderer=TinyRenderer(), verify=False)
    extra = ViewFile("r/base/unknown.txt", "text/plain; charset=utf-8", b"unknown")
    forged = replace(bundle, files=(*bundle.files, extra),
                     routes=(*bundle.routes, ViewRoute(extra.path, extra.content_type,
                                                       sha256(extra.body).hexdigest(), len(extra.body))))

    with pytest.raises(RenderError, match="route inventory"):
        verify_bundle_proof(forged, projection)


def test_bundle_rejects_mime_only_tampering_even_with_matching_route_metadata():
    root = Path(__file__).resolve().parents[2]
    projection = viewer_build._projection(root)
    bundle = build_view_bundle(projection, roots={"base": root, "pack": root},
                               renderer=TinyRenderer(), verify=False)
    original = bundle.files[0]
    changed = replace(original, content_type="text/plain; charset=utf-8")
    files = (changed, *bundle.files[1:])
    routes = tuple(ViewRoute(file.path, file.content_type, sha256(file.body).hexdigest(), len(file.body))
                   for file in files)
    forged = replace(bundle, files=files, routes=routes)
    with pytest.raises(RenderError, match="route inventory"):
        verify_bundle_proof(forged, projection)


def test_bundle_rejects_changed_body_with_stale_route_digest():
    root = Path(__file__).resolve().parents[2]
    projection = viewer_build._projection(root)
    bundle = build_view_bundle(projection, roots={"base": root, "pack": root},
                               renderer=TinyRenderer(), verify=False)
    changed = replace(bundle.files[0], body=bundle.files[0].body + b"tampered")
    with pytest.raises(RenderError, match="route digest"):
        verify_bundle_proof(replace(bundle, files=(changed, *bundle.files[1:])), projection)


@pytest.mark.parametrize("change,diagnostic", [
    (lambda bundle: replace(bundle, entry_path="r/base/other.md.html"), "entry differs"),
    (lambda bundle: replace(bundle, files=(bundle.files[0], *bundle.files)), "duplicate or absent"),
    (lambda bundle: replace(bundle, sources=()), "source inventory"),
])
def test_bundle_proof_rejects_identity_route_or_source_tampering(change, diagnostic):
    root = Path(__file__).resolve().parents[2]
    projection = viewer_build._projection(root)
    bundle = build_view_bundle(projection, roots={"base": root, "pack": root},
                               renderer=TinyRenderer(), verify=False)
    with pytest.raises(RenderError, match=diagnostic):
        verify_bundle_proof(change(bundle), projection)


def test_default_renderer_accepts_explicit_pinned_location_and_reports_missing(tmp_path, monkeypatch):
    jar = tmp_path / "plantuml-mit-1.2026.9.jar"
    jar.write_bytes(b"fixture location")
    monkeypatch.setenv("API_AGENT_PLANTUML_JAR", str(jar))
    assert viewer_render.default_renderer().jar == jar
    jar.unlink()
    with pytest.raises(RenderError, match="renderer missing"):
        viewer_render.default_renderer()
    monkeypatch.delenv("API_AGENT_PLANTUML_JAR")
    monkeypatch.setattr(viewer_render.Path, "home", lambda: tmp_path)
    with pytest.raises(RenderError, match="renderer missing"):
        viewer_render.default_renderer()


@pytest.mark.parametrize("field,value,diagnostic", [
    ("flow_line", 999999, "Flow line absent"),
    ("id", "forged-step-id", "Flow link differs"),
])
def test_hosted_declaration_requires_admitted_flow_line_and_stable_id(field, value, diagnostic):
    root = Path(__file__).resolve().parents[2]
    projection = viewer_build._projection(root)
    step = next(item for item in projection.steps if item.owner_path == projection.entry_path)
    document = next(item for item in projection.documents if item.logical_path == projection.entry_path)
    altered = replace(step, **{field: value})
    tampered = replace(projection, steps=(altered,))
    with pytest.raises(RenderError, match=diagnostic):
        viewer_render._hosted_declaration(tampered, document)


def test_bundle_rejects_unsupported_projection_version():
    root = Path(__file__).resolve().parents[2]
    projection = replace(viewer_build._projection(root), version=2)

    with pytest.raises(RenderError, match="unsupported projection"):
        build_view_bundle(projection, roots={"base": root, "pack": root},
                          renderer=TinyRenderer(), verify=False)


def test_bundle_requires_entry_document_even_when_projection_has_no_documents():
    root = Path(__file__).resolve().parents[2]
    projection = replace(viewer_build._projection(root), documents=(), steps=(),
                         edges=(), resources=(), references=())
    with pytest.raises(RenderError, match="duplicate or absent workflow view route"):
        build_view_bundle(projection, roots={"base": root, "pack": root},
                          renderer=TinyRenderer(), verify=False)


def test_companion_without_peer_documents_has_no_navigation():
    document = SimpleNamespace(logical_path="base/flow.md.html", stable_id="root")
    projection = SimpleNamespace(documents=(document,))
    body = viewer_render._companion(projection, document)
    assert b"## Navigation" not in body
    assert b"[Source declaration]" in body


def test_hosted_selection_cannot_claim_static_source_verification():
    root = Path(__file__).resolve().parents[2]
    projection = viewer_build._projection(root)

    with pytest.raises(RenderError, match="hosted selected"):
        build_view_bundle(projection, roots={"base": root, "pack": root},
                          renderer=TinyRenderer(), verify=True, hosted_selected=True)


def test_bundle_rejects_renderer_without_xml_declaration():
    root = Path(__file__).resolve().parents[2]
    projection = viewer_build._projection(root)

    class BadRenderer:
        def render(self, source, *, allowed_links=frozenset()):
            return b'<svg xmlns="http://www.w3.org/2000/svg"/>'

    with pytest.raises(RenderError, match="no XML declaration"):
        build_view_bundle(projection, roots={"base": root, "pack": root},
                          renderer=BadRenderer(), verify=False)


def test_bundle_rejects_expired_shared_budget_before_rendering():
    root = Path(__file__).resolve().parents[2]
    projection = viewer_build._projection(root)

    with pytest.raises(RenderError, match="generation timed out"):
        _build_view_bundle(projection, roots={"base": root, "pack": root},
                           renderer=TinyRenderer(), verify=False, hosted_selected=True,
                           budget=SimpleNamespace(remaining=0))


def test_static_check_rejects_orphaned_diagram(tmp_path):
    diagram_dir = tmp_path / ".private_resources/diagrams"
    diagram_dir.mkdir(parents=True)
    (diagram_dir / "expected.svg").write_bytes(b"expected")
    (diagram_dir / "orphan.svg").write_bytes(b"orphan")
    bundle = ViewBundle("r/base/flow.md.html", (
        ViewFile("r/base/.private_resources/diagrams/expected.svg", "image/svg+xml", b"expected"),))

    with pytest.raises(RenderError, match="orphaned workflow diagram"):
        check_view_bundle(bundle, roots={"base": tmp_path})


def test_static_check_accepts_two_owners_without_diagram_directories(tmp_path):
    base = tmp_path / "base"
    pack = tmp_path / "pack"
    base.mkdir()
    pack.mkdir()
    (base / "flow.md.html").write_bytes(b"base")
    (pack / "flow.md.html").write_bytes(b"pack")
    bundle = ViewBundle("r/base/flow.md.html", (
        ViewFile("r/base/flow.md.html", "text/html; charset=utf-8", b"base"),
        ViewFile("r/pack/flow.md.html", "text/html; charset=utf-8", b"pack"),))
    check_view_bundle(bundle, roots={"base": base, "pack": pack})


def test_static_check_accepts_exact_svg_and_companion_inventory(tmp_path):
    diagram = tmp_path / ".private_resources/diagrams"
    diagram.mkdir(parents=True)
    (diagram / "flow.svg").write_bytes(b"svg")
    (diagram / "flow.md").write_bytes(b"companion")
    bundle = ViewBundle("r/base/flow.md.html", (
        ViewFile("r/base/.private_resources/diagrams/flow.svg", "image/svg+xml", b"svg"),
        ViewFile("r/base/.private_resources/diagrams/flow.md", "text/markdown; charset=utf-8", b"companion"),))
    check_view_bundle(bundle, roots={"base": tmp_path})


def test_bundle_verify_true_invokes_static_byte_check_without_renderer(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    projection = viewer_build._projection(root)
    seen = []
    monkeypatch.setattr(viewer_render, "check_view_bundle",
                        lambda bundle, *, roots, check_orphans: seen.append((bundle, roots, check_orphans)))
    monkeypatch.setattr(viewer_render, "default_renderer",
                        lambda: pytest.fail("static check must not load Java renderer"))
    monkeypatch.setattr(viewer_render, "_saved_graphic",
                        lambda path, *, metadata, allowed_links, script=b"": path.read_bytes())
    bundle = build_view_bundle(projection, roots={"base": root, "pack": root}, verify=True)
    assert seen == [(bundle, {"base": root, "pack": root}, False)]


def test_saved_diagram_checks_markup_provenance_and_actual_graphic(tmp_path):
    source = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10"><text>ready</text></svg>'
    graphic = validate_svg(source)
    expected = {"puml": sha256(b"current markup").hexdigest(), "jar_sha256": "pinned"}
    metadata = {**expected, "graphic_sha256": sha256(graphic).hexdigest()}
    marker = b"<!-- api-agent-view-v1 " + json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode() + b" -->\n"
    declaration, body = graphic.split(b"\n", 1)
    path = tmp_path / "diagram.svg"
    path.write_bytes(declaration + b"\n" + marker + body)

    assert viewer_render._saved_graphic(path, metadata=expected, allowed_links=frozenset()) == path.read_bytes()
    alternate = validate_svg(source.replace(b'width="10"', b'width="11"').replace(
        b'viewBox="0 0 10 10"', b'viewBox="0 0 11 10"').replace(b'<text>', b'<text x="2" textLength="5">'))
    alternate_metadata = {**expected, "graphic_sha256": sha256(alternate).hexdigest()}
    alternate_marker = b"<!-- api-agent-view-v1 " + json.dumps(alternate_metadata, sort_keys=True, separators=(",", ":")).encode() + b" -->\n"
    alternate_declaration, alternate_body = alternate.split(b"\n", 1)
    path.write_bytes(alternate_declaration + b"\n" + alternate_marker + alternate_body)
    assert viewer_render._saved_graphic(path, metadata=expected, allowed_links=frozenset()) == path.read_bytes()
    with pytest.raises(RenderError, match="stale workflow diagram provenance"):
        viewer_render._saved_graphic(path, metadata={**expected, "puml": "stale"}, allowed_links=frozenset())
    path.write_bytes(path.read_bytes().replace(b"ready", b"wrong"))
    with pytest.raises(RenderError, match="stale workflow diagram provenance"):
        viewer_render._saved_graphic(path, metadata=expected, allowed_links=frozenset())


def test_saved_diagram_rejects_unsafe_graphic_even_with_rehashed_metadata(tmp_path):
    graphic = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10"><script/></svg>'
    expected = {"puml": "current"}
    metadata = {**expected, "graphic_sha256": sha256(graphic).hexdigest()}
    marker = b"<!-- api-agent-view-v1 " + json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode() + b" -->\n"
    declaration, body = graphic.split(b"\n", 1)
    path = tmp_path / "diagram.svg"
    path.write_bytes(declaration + b"\n" + marker + body)

    with pytest.raises(RenderError, match="unsafe SVG element"):
        viewer_render._saved_graphic(path, metadata=expected, allowed_links=frozenset())


def test_saved_diagram_rejects_missing_malformed_and_noncanonical_graphics(tmp_path):
    path = tmp_path / "diagram.svg"
    expected = {"puml": "current"}
    with pytest.raises(RenderError, match="missing or stale"):
        viewer_render._saved_graphic(path, metadata=expected, allowed_links=frozenset())
    path.write_bytes(b"<svg/>")
    with pytest.raises(RenderError, match="invalid workflow diagram provenance"):
        viewer_render._saved_graphic(path, metadata=expected, allowed_links=frozenset())
    path.write_bytes(b'<?xml version="1.0"?>\n<!-- api-agent-view-v1 {bad} -->\n<svg/>')
    with pytest.raises(RenderError, match="invalid workflow diagram provenance"):
        viewer_render._saved_graphic(path, metadata=expected, allowed_links=frozenset())
    graphic = (b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
               b'width="10" height="10" viewBox="0 0 10 10" ></svg>')
    metadata = {**expected, "graphic_sha256": sha256(graphic).hexdigest()}
    marker = b"<!-- api-agent-view-v1 " + json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode() + b" -->\n"
    declaration, body = graphic.split(b"\n", 1)
    path.write_bytes(declaration + b"\n" + marker + body)
    with pytest.raises(RenderError, match="noncanonical workflow diagram graphic"):
        viewer_render._saved_graphic(path, metadata=expected, allowed_links=frozenset())


def test_saved_diagram_reader_rejects_nonregular_and_oversized_outputs(tmp_path):
    missing = tmp_path / "missing.svg"
    assert viewer_render._read_view_file(missing) is None
    symlink = tmp_path / "link.svg"
    symlink.symlink_to(missing)
    with pytest.raises(RenderError, match="not accessible"):
        viewer_render._read_view_file(symlink)
    with pytest.raises(RenderError, match="bounded regular file"):
        viewer_render._read_view_file(tmp_path)
    oversized = tmp_path / "large.svg"
    with oversized.open("wb") as output:
        output.truncate(8 * 1024 * 1024 + 1)
    with pytest.raises(RenderError, match="bounded regular file"):
        viewer_render._read_view_file(oversized)


def test_saved_diagram_reader_rejects_file_changed_during_read(tmp_path, monkeypatch):
    path = tmp_path / "diagram.svg"
    path.write_bytes(b"original")

    def truncated_read(descriptor, mode):
        viewer_render.os.close(descriptor)
        return BytesIO(b"short")

    monkeypatch.setattr(viewer_render.os, "fdopen", truncated_read)
    with pytest.raises(RenderError, match="changed during read"):
        viewer_render._read_view_file(path)


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_orphan_requires_explicit_path_even_with_matching_self_hash(tmp_path, monkeypatch):
    diagrams = tmp_path / ".private_resources/diagrams"
    diagrams.mkdir(parents=True)
    orphan = diagrams / "unknown.md"
    body = b"# independent notes\n"
    orphan.write_bytes(b"<!-- api-agent-companion-v1 sha256:" + sha256(body).hexdigest().encode()
                       + b" -->\n" + body)
    monkeypatch.setattr(viewer_build, "discover_view_entries", lambda root: (root / "flow.md.html",))
    monkeypatch.setattr(viewer_build, "_projection", lambda root: SimpleNamespace(documents=(), resources=()))
    monkeypatch.setattr(viewer_build, "build_view_bundle", lambda *args, **kwargs: ViewBundle("", ()))

    with pytest.raises(RenderError, match="orphaned or edited"):
        viewer_build.generate_views(tmp_path, renderer=TinyRenderer(), fix_orphans=True)
    assert orphan.exists()

    repaired = viewer_build.generate_views(tmp_path, renderer=TinyRenderer(), fix_orphans=True,
                                           confirmed_orphans=(orphan,))
    assert repaired == (orphan,)
    assert not orphan.exists()


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_orphan_swap_to_symlink_does_not_copy_external_bytes(tmp_path, monkeypatch):
    diagrams = tmp_path / ".private_resources/diagrams"
    diagrams.mkdir(parents=True)
    external = tmp_path / "outside.txt"
    external.write_bytes(b"outside content")
    body = b"# generated companion\n"
    marker = b"<!-- api-agent-companion-v1 sha256:" + sha256(body).hexdigest().encode() + b" -->\n"
    orphan = diagrams / "old.md"
    orphan.write_bytes(marker + body)
    monkeypatch.setattr(viewer_build, "discover_view_entries", lambda root: (root / "flow.md.html",))
    monkeypatch.setattr(viewer_build, "_projection", lambda root: SimpleNamespace(documents=(), resources=()))
    monkeypatch.setattr(viewer_build, "build_view_bundle", lambda *args, **kwargs: ViewBundle("", ()))
    replace = viewer_build.os.rename

    def swap_before_move(source, target, **kwargs):
        if source == orphan.name:
            orphan.unlink()
            orphan.symlink_to(external)
        return replace(source, target, **kwargs)

    monkeypatch.setattr(viewer_build.os, "rename", swap_before_move)

    with pytest.raises(RenderError, match="orphaned diagram changed"):
        viewer_build.generate_views(tmp_path, renderer=TinyRenderer(), fix_orphans=True,
                                    confirmed_orphans=(orphan,))
    assert orphan.is_symlink()
    assert orphan.resolve() == external
    assert external.read_bytes() == b"outside content"
    assert not list(tmp_path.glob(".workflow-view-recovery-*"))


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_orphan_swap_to_directory_keeps_unknown_content_for_recovery(tmp_path, monkeypatch):
    diagrams = tmp_path / ".private_resources/diagrams"
    diagrams.mkdir(parents=True)
    body = b"# generated companion\n"
    orphan = diagrams / "old.md"
    orphan.write_bytes(b"<!-- api-agent-companion-v1 sha256:" + sha256(body).hexdigest().encode()
                       + b" -->\n" + body)
    monkeypatch.setattr(viewer_build, "discover_view_entries", lambda root: (root / "flow.md.html",))
    monkeypatch.setattr(viewer_build, "_projection", lambda root: SimpleNamespace(documents=(), resources=()))
    monkeypatch.setattr(viewer_build, "build_view_bundle", lambda *args, **kwargs: ViewBundle("", ()))
    replace = viewer_build.os.rename
    recovery_before = set(tmp_path.parent.glob(".workflow-view-recovery-*"))

    def swap_before_move(source, target, **kwargs):
        if source == orphan.name:
            orphan.unlink()
            orphan.mkdir()
            (orphan / "human.txt").write_bytes(b"human directory content")
        return replace(source, target, **kwargs)

    monkeypatch.setattr(viewer_build.os, "rename", swap_before_move)
    with pytest.raises(RenderError, match="recover"):
        viewer_build.generate_views(tmp_path, renderer=TinyRenderer(), fix_orphans=True,
                                    confirmed_orphans=(orphan,))
    recovery = set(tmp_path.parent.glob(".workflow-view-recovery-*")) - recovery_before
    assert len(recovery) == 1
    saved = next(iter(recovery))
    assert next(saved.rglob("human.txt")).read_bytes() == b"human directory content"
    assert not list(tmp_path.glob(".workflow-view-recovery-*"))
    shutil.rmtree(saved)


def test_moved_output_match_requires_opened_regular_inode_and_exact_bytes(tmp_path):
    original = tmp_path / "original.md"
    original.write_bytes(b"original")
    info = original.stat()
    identity = (info.st_dev, info.st_ino)
    assert viewer_build._moved_output_matches(original, b"original", identity)
    assert not viewer_build._moved_output_matches(original, b"changed", identity)
    assert not viewer_build._moved_output_matches(original, b"original", (info.st_dev, info.st_ino + 1))
    assert not viewer_build._moved_output_matches(tmp_path, b"original", identity)
    assert not viewer_build._moved_output_matches(tmp_path / "absent", b"original", identity)


def test_moved_output_match_closes_descriptor_if_fstat_fails(tmp_path, monkeypatch):
    original = tmp_path / "original.md"
    original.write_bytes(b"original")
    info = original.stat()
    actual_fstat = viewer_build.os.fstat
    seen = []

    def fail_fstat(descriptor):
        seen.append(descriptor)
        raise OSError("injected descriptor failure")

    monkeypatch.setattr(viewer_build.os, "fstat", fail_fstat)
    with pytest.raises(OSError, match="descriptor failure"):
        viewer_build._moved_output_matches(original, b"original", (info.st_dev, info.st_ino))
    assert len(seen) == 1
    with pytest.raises(OSError):
        actual_fstat(seen[0])  # The failing descriptor was closed before propagation.


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_orphan_confirmation_must_name_exact_owned_path(tmp_path, monkeypatch):
    diagrams = tmp_path / ".private_resources/diagrams"
    diagrams.mkdir(parents=True)
    body = b"# generated companion\n"
    orphan = diagrams / "old.md"
    orphan.write_bytes(b"<!-- api-agent-companion-v1 sha256:" + sha256(body).hexdigest().encode()
                       + b" -->\n" + body)
    monkeypatch.setattr(viewer_build, "discover_view_entries", lambda root: (root / "flow.md.html",))
    monkeypatch.setattr(viewer_build, "_projection", lambda root: SimpleNamespace(documents=(), resources=()))
    monkeypatch.setattr(viewer_build, "build_view_bundle", lambda *args, **kwargs: ViewBundle("", ()))
    with pytest.raises(RenderError, match="orphaned or edited"):
        viewer_build.generate_views(tmp_path, renderer=TinyRenderer(), fix_orphans=True,
                                    confirmed_orphans=(diagrams / "other.md",))
    assert orphan.exists()


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_generation_rechecks_orphan_ownership_at_move(tmp_path, monkeypatch):
    diagrams = tmp_path / ".private_resources/diagrams"
    diagrams.mkdir(parents=True)
    body = b"# generated companion\n"
    orphan = diagrams / "old.md"
    orphan.write_bytes(b"<!-- api-agent-companion-v1 sha256:" + sha256(body).hexdigest().encode()
                       + b" -->\n" + body)
    monkeypatch.setattr(viewer_build, "discover_view_entries", lambda root: (root / "flow.md.html",))
    monkeypatch.setattr(viewer_build, "_projection", lambda root: SimpleNamespace(documents=(), resources=()))
    monkeypatch.setattr(viewer_build, "build_view_bundle", lambda *args, **kwargs: ViewBundle("", ()))
    original_owned = viewer_build._owned_orphan
    calls = 0

    def edit_on_final_ownership_check(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b"concurrent human note")
            return False
        return original_owned(path)

    monkeypatch.setattr(viewer_build, "_owned_orphan", edit_on_final_ownership_check)
    with pytest.raises(RenderError, match="orphaned diagram changed"):
        viewer_build.generate_views(tmp_path, renderer=TinyRenderer(), fix_orphans=True,
                                    confirmed_orphans=(orphan,))
    assert orphan.read_bytes() == b"concurrent human note"


def test_renderer_download_deadline_stops_blocked_transfer(tmp_path, monkeypatch):
    entered = Event()
    finished = Event()
    entry_time = []

    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.flush()
            entry_time.append(time.monotonic())
            entered.set()
            time.sleep(1)
            finished.set()

        def log_message(self, *args):
            pass

    class Budget:
        startup_deadline = time.monotonic() + 5

        @property
        def remaining(self):
            deadline = entry_time[0] + 0.2 if entered.is_set() else self.startup_deadline
            return max(0, deadline - time.monotonic())

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    children_before = {child.pid for child in active_children()}
    monkeypatch.setattr(viewer_build, "PLANTUML_URL", f"http://127.0.0.1:{server.server_port}/artifact")
    try:
        with pytest.raises(RenderError, match="timed out"):
            viewer_build._prepare_renderer(cache=tmp_path, budget=Budget())
        elapsed_after_entry = time.monotonic() - entry_time[0] if entered.is_set() else None
        transfer_pending = not finished.is_set()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert entered.is_set(), "download must reach a blocked HTTP body before the deadline"
    assert elapsed_after_entry is not None and elapsed_after_entry < 0.6
    assert transfer_pending
    assert not list(tmp_path.glob(".plantuml-*"))
    assert {child.pid for child in active_children()} == children_before


def test_renderer_preparation_downloads_verified_bytes_and_reuses_cache(tmp_path, monkeypatch):
    body = b"local renderer fixture"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class Budget:
        @property
        def remaining(self):
            return 3.0

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(viewer_build, "PLANTUML_URL", f"http://127.0.0.1:{server.server_port}/artifact")
    monkeypatch.setattr(viewer_build, "PLANTUML_SHA256", sha256(body).hexdigest())
    try:
        first = viewer_build._prepare_renderer(cache=tmp_path, budget=Budget())
        second = viewer_build._prepare_renderer(cache=tmp_path, budget=Budget())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert first == second
    assert first.read_bytes() == body
    assert not list(tmp_path.glob(".plantuml-*"))


@pytest.mark.parametrize("limit,digest,error", [
    (3, sha256(b"artifact").hexdigest(), "exceeds size"),
    (100, "0" * 64, "digest mismatch"),
])
def test_renderer_preparation_rejects_oversize_or_wrong_digest(tmp_path, monkeypatch,
                                                                limit, digest, error):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"artifact")

        def log_message(self, *args):
            pass

    class Budget:
        @property
        def remaining(self):
            return 3.0

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(viewer_build, "PLANTUML_URL", f"http://127.0.0.1:{server.server_port}/artifact")
    monkeypatch.setattr(viewer_build, "PLANTUML_SHA256", digest)
    monkeypatch.setattr(viewer_build, "_MAX_JAR", limit)
    try:
        with pytest.raises(RenderError, match=error):
            viewer_build._prepare_renderer(cache=tmp_path, budget=Budget())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert not (tmp_path / "plantuml-mit-1.2026.9.jar").exists()
    assert not list(tmp_path.glob(".plantuml-*"))


@pytest.fixture
def fake_renderer(tmp_path, monkeypatch):
    jar = tmp_path / "renderer.jar"
    jar.write_bytes(b"fixture jar")
    monkeypatch.setattr(viewer_render, "PLANTUML_SHA256", sha256(jar.read_bytes()).hexdigest())

    def make(script: str, *, timeout: float = 1.0):
        java = tmp_path / "java"
        java.write_text("#!/bin/sh\n" + script + "\n")
        java.chmod(0o755)
        return PlantUMLRenderer(jar, str(java), timeout=timeout)

    return make


def test_renderer_rejects_failed_process(fake_renderer):
    renderer = fake_renderer('printf "failure" >&2; exit 2')

    with pytest.raises(RenderError, match="renderer failed"):
        renderer.render(b"@startuml\nA\n@enduml\n")


def test_renderer_rejects_missing_output(fake_renderer):
    renderer = fake_renderer('exit 0')

    with pytest.raises(RenderError, match="output missing"):
        renderer.render(b"@startuml\nA\n@enduml\n")


def test_renderer_rejects_excess_diagnostics(fake_renderer):
    renderer = fake_renderer('head -c 70000 /dev/zero >&2')

    with pytest.raises(RenderError, match="diagnostics exceeded"):
        renderer.render(b"@startuml\nA\n@enduml\n")


def test_controlled_renderer_reads_and_validates_successful_svg(fake_renderer):
    renderer = fake_renderer("printf '%s\\n' '<?xml version=\"1.0\"?>' "
                             "'<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"1\" "
                             "height=\"1\" viewBox=\"0 0 1 1\"/>' > diagram.svg")
    result = renderer.render(b"@startuml\nA\n@enduml\n")
    assert b"<svg" in result and b"viewBox=\"0 0 1 1\"" in result


def test_generated_svg_uses_installed_font_fallbacks(fake_renderer):
    renderer = fake_renderer("printf '%s\\n' '<?xml version=\"1.0\"?>' "
                             "'<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"10\" "
                             "height=\"10\" viewBox=\"0 0 10 10\"><g font-family=\"sans-serif\"/></svg>' > diagram.svg")

    graphic = renderer.render(b"@startuml\nA\n@enduml\n")

    assert b'font-family="DejaVu Sans, Liberation Sans, Arial, sans-serif"' in graphic


def test_renderer_timeout_kills_owned_process_group(fake_renderer):
    renderer = fake_renderer("exec sleep 10", timeout=0.2)
    with pytest.raises(RenderError, match="renderer timed out"):
        renderer.render(b"@startuml\nA\n@enduml\n")


def _assert_child_stopped(pid):
    deadline = time.monotonic() + 1.0
    while True:
        stat_path = Path(f"/proc/{pid}/stat")
        try:
            state = stat_path.read_text().split()[2]
        except (FileNotFoundError, ProcessLookupError):
            state = "gone"
        if state in {"Z", "X", "gone"}:
            return
        if time.monotonic() >= deadline:
            pytest.fail(f"renderer descendant {pid} remained active ({state})")
        time.sleep(0.01)


def test_renderer_timeout_kills_descendant_after_leader_exits(fake_renderer, tmp_path):
    child_pid = tmp_path / "child.pid"
    renderer = fake_renderer(f"sleep 10 & echo $! > {child_pid}; exit 0", timeout=0.3)

    with pytest.raises(RenderError, match="renderer timed out"):
        renderer.render(b"@startuml\nA\n@enduml\n")

    pid = int(child_pid.read_text())
    _assert_child_stopped(pid)


@pytest.mark.parametrize("error", [FileNotFoundError, ProcessLookupError])
def test_child_stopped_helper_accepts_disappearance_during_proc_read(monkeypatch, error):
    def vanished(path):
        raise error("controlled process disappeared")
    monkeypatch.setattr(Path, "read_text", vanished)

    _assert_child_stopped(123)


def test_child_stopped_helper_still_rejects_live_descendant(monkeypatch):
    moments = iter((0.0, 2.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(Path, "read_text", lambda path: "123 child S")

    with pytest.raises(pytest.fail.Exception, match=r"remained active \(S\)"):
        _assert_child_stopped(123)


def test_child_stopped_helper_propagates_unrelated_proc_error(monkeypatch):
    def denied(path):
        raise PermissionError("controlled unrelated error")
    monkeypatch.setattr(Path, "read_text", denied)

    with pytest.raises(PermissionError, match="controlled unrelated error"):
        _assert_child_stopped(123)


def test_renderer_successful_leader_exit_still_kills_owned_descendant(fake_renderer, tmp_path):
    child_pid = tmp_path / "child.pid"
    renderer = fake_renderer(f"sleep 10 >/dev/null 2>&1 & echo $! > {child_pid}; exit 0", timeout=2)

    with pytest.raises(RenderError, match="output missing"):
        renderer.render(b"@startuml\nA\n@enduml\n")

    pid = int(child_pid.read_text())
    _assert_child_stopped(pid)


def test_drain_enforces_deadline_after_pipes_close(monkeypatch):
    read_out, write_out = __import__("os").pipe()
    read_err, write_err = __import__("os").pipe()
    __import__("os").close(write_out)
    __import__("os").close(write_err)
    events = []

    class Process:
        args = ["controlled-renderer"]
        pid = 1234
        returncode = None
        stdout = __import__("os").fdopen(read_out, "rb")
        stderr = __import__("os").fdopen(read_err, "rb")

        def poll(self):
            return None

        def wait(self, timeout=None):
            events.append(("wait", timeout))

    ticks = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(viewer_render.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(viewer_render.os, "waitid", lambda *args: events.append("waitid"))
    monkeypatch.setattr(viewer_render.os, "killpg", lambda *args: events.append("killpg"))
    with pytest.raises(subprocess.TimeoutExpired):
        viewer_render._drain(Process(), timeout=1.0)
    assert events == ["waitid", "killpg", ("wait", None)]


@pytest.mark.parametrize("disappeared_at", ["leader", "group"])
def test_drain_reaps_when_process_group_disappears_during_cleanup(monkeypatch, disappeared_at):
    os = __import__("os")
    read_out, write_out = os.pipe()
    read_err, write_err = os.pipe()
    os.close(write_out)
    os.close(write_err)
    reaped = []

    class Process:
        args = ["controlled-renderer"]
        pid = 1234
        returncode = None
        stdout = os.fdopen(read_out, "rb")
        stderr = os.fdopen(read_err, "rb")

        def wait(self, timeout=None):
            reaped.append(timeout)

    def vanished_leader(*args):
        raise ChildProcessError

    def vanished_group(*args):
        raise ProcessLookupError

    monkeypatch.setattr(viewer_render.os, "waitid",
                        vanished_leader if disappeared_at == "leader" else lambda *args: None)
    monkeypatch.setattr(viewer_render.os, "killpg", vanished_group)
    ticks = iter((0.0, 1.0))
    monkeypatch.setattr(viewer_render.time, "monotonic", lambda: next(ticks))

    with pytest.raises(subprocess.TimeoutExpired):
        viewer_render._drain(Process(), timeout=0.001)

    assert reaped == [None]


def test_drain_reaps_already_exited_process_after_interrupted_wait():
    read_out, write_out = __import__("os").pipe()
    read_err, write_err = __import__("os").pipe()
    __import__("os").close(write_out)
    __import__("os").close(write_err)
    events = []

    class Process:
        args = ["controlled-renderer"]
        pid = 1234
        returncode = 0
        stdout = __import__("os").fdopen(read_out, "rb")
        stderr = __import__("os").fdopen(read_err, "rb")

        def poll(self):
            return 0

        def kill(self):
            events.append("unexpected kill")

        def wait(self, timeout=None):
            events.append(("wait", timeout))
            if timeout is not None:
                raise OSError("interrupted process wait")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(viewer_render.os, "waitid", lambda *args: object())
        patch.setattr(viewer_render.os, "killpg", lambda *args: None)
        with pytest.raises(OSError, match="interrupted process wait"):
            viewer_render._drain(Process(), timeout=1.0)
    assert len(events) == 2 and events[0][0] == "wait" and events[0][1] is not None
    assert events[1] == ("wait", None)


def test_drain_waits_for_leader_then_tolerates_empty_process_group(monkeypatch):
    os = __import__("os")
    read_out, write_out = os.pipe()
    read_err, write_err = os.pipe()
    os.close(write_out)
    os.close(write_err)
    waited = []

    class Process:
        args = ["controlled-renderer"]
        pid = 1234
        returncode = 0
        stdout = os.fdopen(read_out, "rb")
        stderr = os.fdopen(read_err, "rb")

        def wait(self, timeout=None):
            waited.append(timeout)

    results = iter((None, object()))
    monkeypatch.setattr(viewer_render.os, "waitid", lambda *args: next(results))
    monkeypatch.setattr(viewer_render.os, "killpg", lambda *args: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(viewer_render.time, "sleep", lambda delay: None)

    viewer_render._drain(Process(), timeout=1.0)

    assert len(waited) == 1 and waited[0] > 0


@pytest.mark.parametrize("timeout,source", [
    (0, b"@startuml\nA\n@enduml\n"),
    (121, b"@startuml\nA\n@enduml\n"),
    (1, b"x" * 250001),
])
def test_renderer_rejects_invalid_limits_before_process(fake_renderer, timeout, source):
    renderer = fake_renderer("exit 0", timeout=timeout)

    with pytest.raises(RenderError, match="invalid renderer limits"):
        renderer.render(source)


def test_renderer_rejects_changed_jar_before_process(fake_renderer):
    renderer = fake_renderer("exit 0")
    renderer.jar.write_bytes(b"modified jar")

    with pytest.raises(RenderError, match="artifact digest mismatch"):
        renderer.render(b"@startuml\nA\n@enduml\n")


def test_renderer_reports_missing_java(fake_renderer):
    renderer = fake_renderer("exit 0")
    renderer = replace(renderer, java=str(renderer.jar.parent / "missing-java"))

    with pytest.raises(RenderError, match="Java 17 or newer"):
        renderer.render(b"@startuml\nA\n@enduml\n")


def test_renderer_preparation_rejects_changed_cached_artifact(tmp_path):
    cached = tmp_path / "plantuml-mit-1.2026.9.jar"
    cached.write_bytes(b"changed")

    with pytest.raises(RenderError, match="cached PlantUML digest mismatch"):
        viewer_build._prepare_renderer(cache=tmp_path, budget=SimpleNamespace(remaining=1))


@pytest.mark.parametrize("limit,error", [(20, None), (3, "exceeds size")])
def test_download_worker_caps_bytes_and_reports_result(tmp_path, monkeypatch, limit, error):
    class Response:
        def __init__(self):
            self.reads = iter((b"artifact", b""))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, count):
            return next(self.reads)

    class Channel:
        result = None
        closed = False

        def send(self, value):
            self.result = value

        def close(self):
            self.closed = True

    monkeypatch.setattr(viewer_build, "urlopen", lambda request, timeout: Response())
    channel = Channel()
    target = tmp_path / "download.jar"

    viewer_build._download_renderer(target, 1.0, "https://example.invalid/artifact", limit, channel)

    assert channel.closed
    assert (error is None and channel.result is None) or (error in channel.result)
    assert target.read_bytes() == (b"artifact" if error is None else b"")


def _exit_download_process(*args):
    __import__("os")._exit(17)


def test_unexpected_download_process_exit_reports_bounded_error(tmp_path, monkeypatch):
    children_before = {child.pid for child in active_children()}
    monkeypatch.setattr(viewer_build, "_download_renderer", _exit_download_process)

    with pytest.raises(RenderError, match="worker exited without a result"):
        viewer_build._prepare_renderer(cache=tmp_path, budget=SimpleNamespace(remaining=2))

    assert {child.pid for child in active_children()} == children_before
    assert not list(tmp_path.glob(".plantuml-*"))


def test_download_process_start_failure_closes_temporary_file(tmp_path, monkeypatch):
    context = viewer_build.multiprocessing.get_context("spawn")

    class FailingProcess:
        def start(self):
            raise OSError("process startup failed")

    class FailingContext:
        Pipe = context.Pipe

        def Process(self, *, target, args):
            return FailingProcess()

    monkeypatch.setattr(viewer_build.multiprocessing, "get_context", lambda method: FailingContext())

    with pytest.raises(OSError, match="startup failed"):
        viewer_build._prepare_renderer(cache=tmp_path, budget=SimpleNamespace(remaining=1))

    assert not list(tmp_path.glob(".plantuml-*"))


def test_download_worker_that_ignores_termination_is_killed_and_reaped(tmp_path, monkeypatch):
    context = viewer_build.multiprocessing.get_context("spawn")
    events = []

    class StuckProcess:
        exitcode = None

        def __init__(self, channel):
            self.channel = channel
            self.alive = True

        def start(self):
            self.channel.send(None)
            self.channel.close()

        def join(self, timeout=None):
            events.append(("join", timeout))

        def is_alive(self):
            return self.alive

        def terminate(self):
            events.append("terminate")

        def kill(self):
            events.append("kill")
            self.alive = False

    class StuckContext:
        Pipe = context.Pipe

        def Process(self, *, target, args):
            return StuckProcess(args[-1])

    monkeypatch.setattr(viewer_build.multiprocessing, "get_context", lambda method: StuckContext())
    with pytest.raises(RenderError, match="preparation timed out"):
        viewer_build._prepare_renderer(cache=tmp_path, budget=SimpleNamespace(remaining=0.01))
    assert events[1:4] == ["terminate", ("join", 0.05), "kill"]
    assert not list(tmp_path.glob(".plantuml-*"))


def test_prepare_renderer_wraps_shared_timeout(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(viewer_build, "_prepare_renderer",
                        lambda *, cache, budget: calls.append((cache, budget)) or tmp_path / "renderer.jar")

    result = viewer_build.prepare_renderer(cache=tmp_path)

    assert result == tmp_path / "renderer.jar"
    assert len(calls) == 1 and calls[0][0] == tmp_path
    assert calls[0][1].remaining is not None


def test_renderer_cache_refuses_symlink(tmp_path):
    cache = tmp_path / "cache"
    cache.symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(RenderError, match="cache symlink"):
        viewer_build._prepare_renderer(cache=cache, budget=SimpleNamespace(remaining=1))


@pytest.mark.parametrize("operation", ["cache", "render"])
def test_fifo_renderer_artifact_is_rejected_without_blocking(tmp_path, operation):
    cache = tmp_path / "cache"
    cache.mkdir()
    artifact = cache / "plantuml-mit-1.2026.9.jar"
    artifact_path = artifact if operation == "cache" else tmp_path / "renderer.jar"
    __import__("os").mkfifo(artifact_path)
    script = ("from pathlib import Path\n"
              "from types import SimpleNamespace\n"
              "from api_agent.workflows import viewer_build\n"
              "from api_agent.workflows.viewer_render import PlantUMLRenderer\n"
              f"path = Path({str(artifact_path)!r})\n"
              "viewer_build._prepare_renderer(cache=path.parent, budget=SimpleNamespace(remaining=0.2))\n"
              if operation == "cache" else
              "from pathlib import Path\n"
              "from api_agent.workflows.viewer_render import PlantUMLRenderer\n"
              f"PlantUMLRenderer(Path({str(artifact_path)!r}), timeout=0.2).render(b'@startuml\\nA\\n@enduml\\n')\n")

    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=2)

    assert result.returncode != 0
    assert "bounded regular file" in result.stderr


def test_fifo_diagram_output_is_rejected_without_blocking(tmp_path):
    diagram = tmp_path / "old.md"
    __import__("os").mkfifo(diagram)
    script = ("from pathlib import Path\n"
              "from api_agent.workflows.viewer_build import _owned_orphan\n"
              f"_owned_orphan(Path({str(diagram)!r}))\n")

    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=2)

    assert result.returncode != 0
    assert "bounded regular file" in result.stderr


@pytest.mark.parametrize("metadata,expected", [
    (b'{"version":1,"graphic_sha256":"DIGEST"}', True),
    (b'{"version":1,"graphic_sha256":"wrong"}', False),
    (b'{"version":2,"graphic_sha256":"DIGEST"}', False),
    (b'{broken}', False),
])
def test_svg_orphan_marker_requires_valid_graphic_binding(tmp_path, metadata, expected):
    graphic = b'<?xml version="1.0"?>\n<svg/>'
    marker = metadata.replace(b"DIGEST", sha256(graphic).hexdigest().encode())
    artifact = tmp_path / "old.svg"
    artifact.write_bytes(b"<!-- api-agent-view-v1 " + marker + b" -->\n" + graphic)

    assert viewer_build._owned_orphan(artifact) is expected


@pytest.mark.parametrize('valid', [False, True])
def test_contract_orphan_marker_requires_matching_page_bytes(tmp_path, valid):
    directory = tmp_path / 'contracts'
    directory.mkdir()
    body = b'<!doctype html><title>Captured field</title>'
    digest = sha256(body).hexdigest().encode() if valid else b'0' * 64
    artifact = directory / ('0' * 24 + '.md.html')
    artifact.write_bytes(b'<!-- api-agent-reference-v1 sha256:' + digest + b' -->\n' + body)

    assert viewer_build._owned_orphan(artifact) is valid


def test_output_snapshot_refuses_symlink(tmp_path):
    diagrams = tmp_path / ".private_resources/diagrams"
    diagrams.mkdir(parents=True)
    (diagrams / "linked.svg").symlink_to(tmp_path / "elsewhere.svg")

    with pytest.raises(RenderError, match="output symlink"):
        viewer_build._output_snapshot(tmp_path)


@pytest.mark.parametrize("command", ["prepare", "generate", "check"])
def test_source_cli_dispatches_explicit_view_operation(tmp_path, monkeypatch, capsys, command):
    called = []
    monkeypatch.setattr(viewer_build, "prepare_renderer",
                        lambda: called.append("prepare") or tmp_path / "renderer.jar")
    monkeypatch.setattr(viewer_build, "generate_views",
                        lambda root, *, fix_orphans, confirmed_orphans, repository_url=None:
                        called.append(("generate", root, fix_orphans, confirmed_orphans)) or (tmp_path / "flow.svg",))
    monkeypatch.setattr(viewer_build, "check_views", lambda root: called.append(("check", root)))

    args = [command, "--root", str(tmp_path)]
    if command == "generate":
        args.extend(["--fix-orphans", "--confirm-orphan", str(tmp_path / "old.svg")])
    result = viewer_build.main(args)

    assert result == 0
    assert called == ({"prepare": ["prepare"],
                       "generate": [("generate", tmp_path, True, (tmp_path / "old.svg",))],
                       "check": [("check", tmp_path)]}[command])
    assert (capsys.readouterr().out != "") == (command != "check")


def test_source_cli_reports_renderer_error(tmp_path, monkeypatch, capsys):
    def fail(root):
        raise RenderError("stale diagram")
    monkeypatch.setattr(viewer_build, "check_views", fail)

    with pytest.raises(SystemExit) as exit_info:
        viewer_build.main(["check", "--root", str(tmp_path)])

    assert exit_info.value.code == 2
    assert "stale diagram" in capsys.readouterr().err


def test_source_module_entry_reports_missing_source_as_cli_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["viewer_build", "check", "--root", str(tmp_path)])
    with pytest.warns(RuntimeWarning, match="found in sys.modules"):
        with pytest.raises(SystemExit) as exit_info:
            runpy.run_module("api_agent.workflows.viewer_build", run_name="__main__")
    assert exit_info.value.code == 2
    assert "workflow discovery found no graph entries" in capsys.readouterr().err


@pytest.fixture
def controlled_generation(tmp_path, monkeypatch):
    monkeypatch.setattr(viewer_build, "discover_view_entries", lambda root: (root / "flow.md.html",))
    flow = tmp_path / "flow.md.html"
    flow.write_bytes(b"admitted declaration")
    diagram_dir = tmp_path / ".private_resources/diagrams"
    diagram_dir.mkdir(parents=True)
    first, second = diagram_dir / "first.svg", diagram_dir / "second.svg"
    first.write_bytes(b"old first")
    second.write_bytes(b"old second")
    document = SimpleNamespace(logical_path="base/flow.md.html", raw_bytes=flow.read_bytes())
    projection = SimpleNamespace(documents=(document,), resources=())
    bundle = ViewBundle("r/base/flow.md.html", (
        ViewFile("r/base/.private_resources/diagrams/first.svg", "image/svg+xml", b"new first"),
        ViewFile("r/base/.private_resources/diagrams/second.svg", "image/svg+xml", b"new second"),
    ))
    monkeypatch.setattr(viewer_build, "_projection", lambda root: projection)
    monkeypatch.setattr(viewer_build, "build_view_bundle", lambda *args, **kwargs: bundle)
    return tmp_path, flow, first, second


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_generation_refuses_changed_admitted_declaration(controlled_generation, monkeypatch):
    root, flow, first, second = controlled_generation
    original_projection = viewer_build._projection

    def changed_after_admission(root):
        projection = original_projection(root)
        flow.write_bytes(b"human edit")
        return projection

    monkeypatch.setattr(viewer_build, "_projection", changed_after_admission)

    with pytest.raises(RenderError, match="admitted workflow source changed"):
        viewer_build.generate_views(root, renderer=TinyRenderer())
    assert flow.read_bytes() == b"human edit"
    assert first.read_bytes() == b"old first"
    assert second.read_bytes() == b"old second"


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_generation_rolls_back_first_output_when_second_replace_fails(controlled_generation,
                                                                       monkeypatch):
    root, flow, first, second = controlled_generation
    replace = viewer_build._publish_link
    calls = 0

    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second publication failed")
        return replace(source, target)

    monkeypatch.setattr(viewer_build, "_publish_link", fail_second)

    with pytest.raises(OSError, match="second publication failed"):
        viewer_build.generate_views(root, renderer=TinyRenderer())
    assert flow.read_bytes() == b"admitted declaration"
    assert first.read_bytes() == b"old first"
    assert second.read_bytes() == b"old second"


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_generation_refuses_output_edit_after_projection(controlled_generation, monkeypatch):
    root, flow, first, second = controlled_generation
    original_build = viewer_build.build_view_bundle

    def edit_after_projection(*args, **kwargs):
        result = original_build(*args, **kwargs)
        first.write_bytes(b"human output edit")
        return result

    monkeypatch.setattr(viewer_build, "build_view_bundle", edit_after_projection)

    with pytest.raises(RenderError, match="output changed during generation"):
        viewer_build.generate_views(root, renderer=TinyRenderer())
    assert first.read_bytes() == b"human output edit"
    assert second.read_bytes() == b"old second"


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_generation_preserves_concurrent_edit_during_rollback(controlled_generation,
                                                               monkeypatch):
    root, flow, first, second = controlled_generation
    replace = viewer_build._publish_link
    calls = 0

    def edit_first_then_fail(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            first.write_bytes(b"concurrent output edit")
            raise OSError("second publication failed")
        return replace(source, target)

    monkeypatch.setattr(viewer_build, "_publish_link", edit_first_then_fail)

    with pytest.raises(RenderError, match="rollback conflict; recover original bytes"):
        viewer_build.generate_views(root, renderer=TinyRenderer())
    assert first.read_bytes() == b"concurrent output edit"
    assert second.read_bytes() == b"old second"


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_generation_detects_edit_between_output_replacements(controlled_generation,
                                                              monkeypatch):
    root, flow, first, second = controlled_generation
    replace = viewer_build._publish_link
    calls = 0

    def edit_second_after_first(source, target):
        nonlocal calls
        calls += 1
        result = replace(source, target)
        if calls == 1:
            second.write_bytes(b"concurrent second edit")
        return result

    monkeypatch.setattr(viewer_build, "_publish_link", edit_second_after_first)

    with pytest.raises(RenderError, match="output changed during generation"):
        viewer_build.generate_views(root, renderer=TinyRenderer())
    assert flow.read_bytes() == b"admitted declaration"
    assert first.read_bytes() == b"old first"
    assert second.read_bytes() == b"concurrent second edit"


def test_view_check_is_read_only_and_requires_fresh_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(viewer_build, "discover_view_entries", lambda root: (root / "flow.md.html",))
    projection = SimpleNamespace(graph_id="captured")
    called = []
    monkeypatch.setattr(viewer_build, "_projection", lambda root: projection)

    def verify(admitted, *, roots, verify, check_orphans, repositories, repository_sources):
        assert check_orphans is False
        called.append((admitted, roots, verify))
        raise RenderError("stale graphic")

    monkeypatch.setattr(viewer_build, "build_view_bundle", verify)

    with pytest.raises(RenderError, match="stale graphic"):
        viewer_build.check_views(tmp_path)

    assert called == [(projection, {"base": tmp_path, "pack": tmp_path}, True)]
    assert not list(tmp_path.iterdir())


def test_check_does_not_request_renderer(tmp_path, monkeypatch):
    monkeypatch.setattr(viewer_build, "discover_view_entries", lambda root: (root / "flow.md.html",))
    projection = SimpleNamespace(graph_id="captured")
    received = []
    monkeypatch.setattr(viewer_build, "_projection", lambda root: projection)
    def checked_bundle(admitted, *, roots, verify, check_orphans, repositories, repository_sources):
        assert check_orphans is False
        received.append((admitted, roots, verify))
        return ViewBundle("", ())

    monkeypatch.setattr(viewer_build, "build_view_bundle", checked_bundle)

    viewer_build.check_views(tmp_path)

    assert received == [(projection, {"base": tmp_path, "pack": tmp_path}, True)]


def test_regular_view_reader_rejects_missing_special_oversized_and_changed(tmp_path, monkeypatch):
    missing = tmp_path / "missing.md"
    with pytest.raises(RenderError, match="accessible regular file"):
        viewer_build._read_regular_view(missing)
    with pytest.raises(RenderError, match="bounded regular file"):
        viewer_build._read_regular_view(Path("/dev/null"))
    with pytest.raises(RenderError, match="bounded regular file"):
        viewer_build._read_regular_view(tmp_path)
    large = tmp_path / "large.md"
    with large.open("wb") as output:
        output.truncate(viewer_build._MAX_VIEW_FILE + 1)
    with pytest.raises(RenderError, match="bounded regular file"):
        viewer_build._read_regular_view(large)
    file = tmp_path / "changed.md"
    file.write_bytes(b"original")
    original_fdopen = viewer_build.os.fdopen

    def truncate_before_read(descriptor, mode):
        stream = original_fdopen(descriptor, mode)
        file.write_bytes(b"x")
        return stream

    monkeypatch.setattr(viewer_build.os, "fdopen", truncate_before_read)
    with pytest.raises(RenderError, match="changed during read"):
        viewer_build._read_regular_view(file)


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_generation_rejects_source_removed_after_admission(controlled_generation, monkeypatch):
    root, flow, first, second = controlled_generation
    original_build = viewer_build.build_view_bundle

    def remove_after_projection(*args, **kwargs):
        result = original_build(*args, **kwargs)
        flow.unlink()
        return result

    monkeypatch.setattr(viewer_build, "build_view_bundle", remove_after_projection)
    with pytest.raises(RenderError, match="admitted workflow source changed"):
        viewer_build.generate_views(root, renderer=TinyRenderer())
    assert not flow.exists()
    assert first.read_bytes() == b"old first"


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_generation_detects_output_edit_after_last_snapshot(controlled_generation, monkeypatch):
    root, flow, first, second = controlled_generation
    original_read = viewer_build._read_regular_view
    calls = 0

    def race_on_prior_read(path, *, parent=None):
        nonlocal calls
        value = original_read(path, parent=parent)
        if parent is None and path == first:
            calls += 1
            if calls == 2:
                first.write_bytes(b"late human edit")
        return value

    monkeypatch.setattr(viewer_build, "_read_regular_view", race_on_prior_read)
    with pytest.raises(RenderError, match="workflow view changed during generation|output changed during generation"):
        viewer_build.generate_views(root, renderer=TinyRenderer())
    assert first.read_bytes() == b"late human edit"


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_generation_rollback_preserves_symlink_swapped_output(controlled_generation, monkeypatch):
    root, flow, first, second = controlled_generation
    external = root.parent / "external-human-file"
    external.write_bytes(b"human bytes")
    replace = viewer_build._publish_link
    calls = 0

    def swap_first_then_fail(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            first.unlink()
            first.symlink_to(external)
            raise OSError("second publication failed")
        return replace(source, target)

    monkeypatch.setattr(viewer_build, "_publish_link", swap_first_then_fail)
    with pytest.raises(RenderError, match="rollback conflict"):
        viewer_build.generate_views(root, renderer=TinyRenderer())
    assert first.is_symlink()
    assert external.read_bytes() == b"human bytes"
    assert second.read_bytes() == b"old second"


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_generation_removes_new_output_on_rollback(controlled_generation, monkeypatch):
    root, flow, first, second = controlled_generation
    first.unlink()
    replace = viewer_build._publish_link
    calls = 0

    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second publication failed")
        return replace(source, target)

    monkeypatch.setattr(viewer_build, "_publish_link", fail_second)
    with pytest.raises(OSError, match="second publication failed"):
        viewer_build.generate_views(root, renderer=TinyRenderer())
    assert not first.exists()
    assert second.read_bytes() == b"old second"


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_generation_rejects_mismatched_admitted_output_before_staging(controlled_generation, monkeypatch):
    root, flow, first, second = controlled_generation
    projection = viewer_build._projection(root)
    resource = SimpleNamespace(logical_path="base/.private_resources/diagrams/first.svg",
                               content_bytes=b"declared earlier")
    monkeypatch.setattr(viewer_build, "_projection",
                        lambda path: SimpleNamespace(documents=projection.documents,
                                                     resources=(resource,)))
    with pytest.raises(RenderError, match="admitted workflow source changed"):
        viewer_build.generate_views(root, renderer=TinyRenderer())
    assert first.read_bytes() == b"old first"


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_generation_detects_admitted_edit_between_snapshot_and_planning(controlled_generation,
                                                                         monkeypatch):
    root, flow, first, second = controlled_generation
    bundle = viewer_build.build_view_bundle(None)
    monkeypatch.setattr(viewer_build, "build_view_bundle", lambda *args, **kwargs:
                        replace(bundle, files=(ViewFile("r/base/flow.md.html", "text/html; charset=utf-8",
                                                       b"new presentation"), *bundle.files)))
    original_read = viewer_build._read_regular_view
    calls = 0

    def edit_on_planned_read(path, *, parent=None):
        nonlocal calls
        if parent is None and path == flow:
            calls += 1
            if calls == 2:
                flow.write_bytes(b"late human source edit")
        return original_read(path, parent=parent)

    monkeypatch.setattr(viewer_build, "_read_regular_view", edit_on_planned_read)
    with pytest.raises(RenderError, match="admitted workflow source changed"):
        viewer_build.generate_views(root, renderer=TinyRenderer())
    assert flow.read_bytes() == b"late human source edit"


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_generation_updates_admitted_snapshot_when_publishing_selected_source(tmp_path, monkeypatch):
    monkeypatch.setattr(viewer_build, "discover_view_entries", lambda root: (root / "flow.md.html",))
    flow = tmp_path / "flow.md.html"
    flow.write_bytes(b"admitted source")
    document = SimpleNamespace(logical_path="base/flow.md.html", raw_bytes=b"admitted source")
    monkeypatch.setattr(viewer_build, "_projection",
                        lambda root: SimpleNamespace(documents=(document,), resources=()))
    bundle = ViewBundle("r/base/flow.md.html", (
        ViewFile("r/base/flow.md.html", "text/html; charset=utf-8", b"published presentation"),))
    monkeypatch.setattr(viewer_build, "build_view_bundle", lambda *args, **kwargs: bundle)
    assert viewer_build.generate_views(tmp_path, renderer=TinyRenderer()) == (flow,)
    assert flow.read_bytes() == b"published presentation"


def test_admitted_snapshot_ignores_resource_outside_selected_owners(tmp_path):
    resource = SimpleNamespace(logical_path="external/reference.txt", content_bytes=b"metadata")
    projection = SimpleNamespace(documents=(), resources=(resource,))
    assert viewer_build._admitted_snapshot(projection, {"base": tmp_path, "pack": tmp_path}) == {}


def test_admitted_snapshot_keeps_selected_document_bytes_over_duplicate_resource(tmp_path):
    document = SimpleNamespace(logical_path="base/flow.md.html", raw_bytes=b"selected declaration")
    resource = SimpleNamespace(logical_path="base/flow.md.html", content_bytes=b"duplicate resource")
    projection = SimpleNamespace(documents=(document,), resources=(resource,))
    assert viewer_build._admitted_snapshot(projection, {"base": tmp_path, "pack": tmp_path}) == {
        tmp_path / "flow.md.html": b"selected declaration"}


def test_output_snapshot_ignores_nested_directories(tmp_path):
    nested = tmp_path / ".private_resources/diagrams/nested"
    nested.mkdir(parents=True)
    artifact = nested / "flow.svg"
    artifact.write_bytes(b"svg")
    assert viewer_build._output_snapshot(tmp_path) == {artifact: b"svg"}


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_generation_does_not_replace_already_current_outputs(controlled_generation, monkeypatch):
    root, flow, first, second = controlled_generation
    bundle = ViewBundle("r/base/flow.md.html", (
        ViewFile("r/base/.private_resources/diagrams/first.svg", "image/svg+xml", b"old first"),
        ViewFile("r/base/.private_resources/diagrams/second.svg", "image/svg+xml", b"old second"),))
    monkeypatch.setattr(viewer_build, "build_view_bundle", lambda *args, **kwargs: bundle)
    assert viewer_build.generate_views(root, renderer=TinyRenderer()) == ()
    assert first.read_bytes() == b"old first" and second.read_bytes() == b"old second"


@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_generation_detects_edit_after_final_output_snapshot(controlled_generation, monkeypatch):
    root, flow, first, second = controlled_generation
    original_assert = viewer_build._assert_output_unchanged
    calls = 0

    def edit_after_check(snapshot, root, *, exclude=()):
        nonlocal calls
        original_assert(snapshot, root, exclude=exclude)
        calls += 1
        if calls == 3:
            first.write_bytes(b"edit after final snapshot")

    monkeypatch.setattr(viewer_build, "_assert_output_unchanged", edit_after_check)
    with pytest.raises(RenderError, match="workflow view changed during generation"):
        viewer_build.generate_views(root, renderer=TinyRenderer())
    assert first.read_bytes() == b"edit after final snapshot"


@pytest.mark.parametrize("suffix, data", [
    (".svg", b"<svg/>") ,
    (".svg", b'<!-- api-agent-view-v1 {bad json} -->\n<svg/>'),
    (".txt", b"unowned"),
])
@pytest.mark.usefixtures("recorded_repository_for_generation_controls")
def test_orphan_ownership_requires_valid_format(tmp_path, suffix, data):
    artifact = tmp_path / ("stray" + suffix)
    artifact.write_bytes(data)
    assert not viewer_build._owned_orphan(artifact)


def test_renderer_owns_and_kills_dedicated_process_group(tmp_path):
    jar = tmp_path / "fixture.jar"
    jar.write_bytes(b"controlled renderer bytes")
    pgid = tmp_path / "java-pgid.txt"
    java = tmp_path / "java"
    java.write_text(f"#!/bin/sh\nps -o pgid= -p $$ > {pgid}\nexec sleep 10\n")
    java.chmod(0o755)
    script = ("import os\n"
              "from hashlib import sha256\n"
              "from pathlib import Path\n"
              "from api_agent.workflows import viewer_render\n"
              f"jar = Path({str(jar)!r})\n"
              f"pgid = Path({str(pgid)!r})\n"
              "viewer_render.PLANTUML_SHA256 = sha256(jar.read_bytes()).hexdigest()\n"
              "renderer = viewer_render.PlantUMLRenderer(jar, " + repr(str(java)) + ", timeout=0.4)\n"
              "try:\n"
              "    renderer.render(b'@startuml\\nA\\n@enduml\\n')\n"
              "except viewer_render.RenderError as exc:\n"
              "    assert 'timed out' in str(exc)\n"
              "else:\n"
              "    raise AssertionError('expected renderer timeout')\n"
              "assert int(pgid.read_text()) != os.getpgrp()\n")

    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=3)

    assert result.returncode == 0, result.stderr


@pytest.fixture
def recorded_repository_for_generation_controls(tmp_path):
    """Keep race/publication tests focused on existing configured owners."""
    directory = tmp_path / "cfg"
    directory.mkdir(exist_ok=True)
    (directory / "workflow-view.toml").write_text('version = 1\nrepository_url = "//github.com/example/agent"\n')


def test_native_only_projection_preserves_its_source_link():
    from hashlib import sha256
    source = b'[Python](code.py)\n'
    document = SimpleNamespace(logical_path='base/flow.md.html')
    resource = SimpleNamespace(logical_path='base/code.py', kind='native_source',
                               content_bytes=b'print("source")', sha256=sha256(b'print("source")').hexdigest())
    projection = SimpleNamespace(documents=(document,), resources=(resource,), references=(), tool_bindings=())
    assert _annotate_hosted_resources(projection, document, [source]) == source
