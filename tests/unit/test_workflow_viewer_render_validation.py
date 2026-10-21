"""Admission and geometry checks for bounded workflow views."""

from hashlib import sha256
import os
from types import SimpleNamespace

import pytest

from api_agent.workflows import viewer_render
from api_agent.workflows.viewer_render import (
    PlantUMLRenderer, RenderError, ViewFile, ViewRoute, _projection_source, validate_svg,
    verify_bundle_proof,
)


def _projection(*, label="task", target="base/task.md.html", edges=()):
    entry = SimpleNamespace(logical_path="base/flow.md.html")
    declaration = SimpleNamespace(logical_path="base/task.md.html")
    step = SimpleNamespace(id=label, kind="node", parent=None,
                           declaration_path=target)
    return SimpleNamespace(entry_path=entry.logical_path,
                           documents=(entry, declaration), steps=(step,),
                           edges=edges, references=(), resources=()), entry


def _svg(attributes=b'', body=b''):
    return (b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
            b'viewBox="0 0 10 10" ' + attributes + b'>' + body + b'</svg>')


def test_diagram_rejects_link_to_document_outside_admitted_selection():
    projection, entry = _projection(target="base/unselected.md.html")

    with pytest.raises(RenderError, match="unadmitted diagram link"):
        _projection_source(projection, entry)


@pytest.mark.parametrize("label", ["line\nbreak", "tab\tname", "x" * 257])
def test_diagram_rejects_labels_that_break_bounded_single_line_syntax(label):
    projection, entry = _projection(label=label)

    with pytest.raises(RenderError, match="invalid diagram label"):
        _projection_source(projection, entry)


def test_diagram_rejects_excessive_route_records():
    edge = SimpleNamespace(owner="GRAPH", source="START", target="task", condition="go")
    projection, entry = _projection(edges=(edge,) * 1001)

    with pytest.raises(RenderError, match="record limit"):
        _projection_source(projection, entry)


@pytest.mark.parametrize("attributes", [
    b'width="0" height="10" viewBox="0 0 10 10"',
    b'width="8192" height="8192" viewBox="0 0 10 10"',
    b'width="10" height="10" viewBox="0 0 0 10"',
    b'width="10" height="10" viewBox="0 0 10000 10000"',
    b'width="10" height="10" viewBox="0 0 10 10" style="height:11px;"',
])
def test_svg_rejects_zero_oversized_or_conflicting_geometry(attributes):
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" ' + attributes + b'/>')

    with pytest.raises(RenderError):
        validate_svg(svg)


@pytest.mark.parametrize("body", [
    b'<a href="relative.svg"><text>next</text></a>',
    b'<rect fill="url(https://example.invalid/paint)"/>',
    b'<text style="fill:expression(alert(1))">bad</text>',
    b'<use href="#shape"/>',
    b'<text x="0">&external;</text>',
])
def test_svg_rejects_unadmitted_links_resources_or_markup(body):
    data = _svg(body=body)

    with pytest.raises(RenderError):
        validate_svg(data)


def test_svg_admits_only_declared_relative_link_and_resolved_fragment():
    data = _svg(body=(b'<defs><clipPath id="cut"><rect width="1" height="1"/>'
                      b'</clipPath></defs><a href="../task.md.html"><text>task</text></a>'
                      b'<a href="#cut"><text>local</text></a>'))

    cleaned = validate_svg(data, allowed_links=frozenset({"../task.md.html"}))

    assert b'../task.md.html' in cleaned
    assert b'href="#cut"' in cleaned


def test_svg_rejects_unresolved_fragment():
    with pytest.raises(RenderError, match="broken SVG fragment"):
        validate_svg(_svg(body=b'<a href="#missing"><text>lost</text></a>'))


@pytest.mark.parametrize("style", [
    b"fill:#abc;stroke:#123456;stroke-width:1px",
    b"opacity:0.5;stroke-linecap:round;stroke-linejoin:bevel",
    b"font-size:12px;font-weight:bold;text-anchor:middle",
    b";fill:#abc;",
])
def test_svg_admits_finite_graphic_styles(style):
    graphic = _svg(body=b'<text style="' + style + b'">label</text>')

    assert b"label" in validate_svg(graphic)


@pytest.mark.parametrize("style", [
    b"fill:expression(alert(1))",
    b"fill:url(#gradient)",
    b"opacity:1.1",
    b"stroke-width:0",
    b"stroke-linejoin:inherit",
    b"text-anchor:end;position:absolute",
    b"font-size:99999px",
    b"stroke:#zzzzzz",
])
def test_svg_rejects_unsafe_or_unbounded_style_value(style):
    graphic = _svg(body=b'<text style="' + style + b'">label</text>')

    with pytest.raises(RenderError, match="unsafe SVG style"):
        validate_svg(graphic)


@pytest.mark.parametrize("content", [
    b'<foreignObject><text>outside vocabulary</text></foreignObject>',
    b'<text onclick="alert(1)">label</text>',
    b'<text fill="url(https://example.invalid/paint)">label</text>',
])
def test_svg_rejects_foreign_elements_event_attributes_and_resources(content):
    with pytest.raises(RenderError):
        validate_svg(_svg(body=content))


def test_svg_rejects_entity_declaration_before_xml_processing():
    data = (b'<!ENTITY injected "hidden">' + _svg(body=b'<text>&injected;</text>'))

    with pytest.raises(RenderError, match="unsafe SVG"):
        validate_svg(data)


def test_svg_requires_svg_namespace_root():
    data = (b'<html xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
            b'viewBox="0 0 10 10"/>')

    with pytest.raises(RenderError, match="invalid SVG root"):
        validate_svg(data)


@pytest.mark.parametrize("viewbox", [b"0 0 10", b"0 0 ten 10"])
def test_svg_rejects_malformed_viewbox(viewbox):
    data = (b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
            b'viewBox="' + viewbox + b'"/>')

    with pytest.raises(RenderError, match="invalid SVG viewBox"):
        validate_svg(data)


@pytest.mark.parametrize("style", [
    b"stroke-linecap:inherit",
    b"font-weight:heavy",
    b"text-anchor:baseline",
    b"fill",
])
def test_svg_rejects_invalid_graphic_style_declaration(style):
    graphic = _svg(body=b'<text style="' + style + b'">label</text>')

    with pytest.raises(RenderError, match="unsafe SVG style"):
        validate_svg(graphic)


@pytest.mark.parametrize("artifact", ["missing", "directory", "symlink"])
def test_renderer_rejects_non_regular_jar_before_invoking_java(tmp_path, artifact):
    jar = tmp_path / "renderer.jar"
    if artifact == "directory":
        jar.mkdir()
    elif artifact == "symlink":
        target = tmp_path / "target.jar"
        target.write_bytes(b"fixture")
        jar.symlink_to(target)
    renderer = PlantUMLRenderer(jar, java=str(tmp_path / "nonexistent-java"))

    with pytest.raises(RenderError, match="regular file"):
        renderer.render(b"@startuml\nA\n@enduml\n")


def test_renderer_rejects_jar_truncated_after_file_metadata_check(tmp_path, monkeypatch):
    jar = tmp_path / "renderer.jar"
    jar.write_bytes(b"complete artifact")
    original_fdopen = os.fdopen
    opened = []

    def truncate_before_read(descriptor, mode):
        opened.append(descriptor)
        jar.write_bytes(b"cut")
        return original_fdopen(descriptor, mode)

    monkeypatch.setattr(viewer_render.os, "fdopen", truncate_before_read)

    with pytest.raises(RenderError, match="artifact changed"):
        viewer_render._read_regular_jar(jar)

    assert jar.read_bytes() == b"cut"
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_installed_assets_take_precedence_over_adjacent_source_tree(tmp_path, monkeypatch):
    module = tmp_path / "src/api_agent/workflows/viewer_render.py"
    module.parent.mkdir(parents=True)
    module.write_text("# module")
    installed = module.parents[1] / ".private_resources/viewer"
    installed.mkdir(parents=True)
    (installed / "viewer.js").write_text("installed")
    source = tmp_path / ".private_resources/viewer"
    source.mkdir(parents=True)
    (source / "viewer.js").write_text("source")
    monkeypatch.setattr(viewer_render, "__file__", str(module))

    assert viewer_render._asset_root() == installed


def test_source_checkout_locates_owned_viewer_assets(tmp_path, monkeypatch):
    module = tmp_path / "src/api_agent/workflows/viewer_render.py"
    module.parent.mkdir(parents=True)
    module.write_text("# source copy")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='api-agent'\n")
    assets = tmp_path / ".private_resources/viewer"
    assets.mkdir(parents=True)
    (assets / "viewer.js").write_text("source")
    monkeypatch.setattr(viewer_render, "__file__", str(module))

    assert viewer_render._asset_root() == assets


def test_bundle_proof_rejects_forged_identity_before_route_publication():
    projection = SimpleNamespace(graph_id="graph", definition_id="definition",
                                 projection_sha256="a" * 64,
                                 entry_path="base/flow.md.html", documents=(), resources=())
    file = ViewFile("r/base/flow.md.html", "text/html; charset=utf-8", b"view")
    route = ViewRoute(file.path, file.content_type, sha256(file.body).hexdigest(), len(file.body))
    bundle = viewer_render.ViewBundle(file.path, (file,), "other", "definition",
                                      "a" * 64, (), (route,))

    with pytest.raises(RenderError, match="identity differs"):
        verify_bundle_proof(bundle, projection)
