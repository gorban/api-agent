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
from html import unescape
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
from api_agent.workflows.viewer_projection import ViewerReference
from api_agent.workflows.viewer_projection import ViewerController
from api_agent.workflows.viewer_repository import RepositoryIdentity


def _projection():
    document = SimpleNamespace(logical_path="base/flow.md.html", stable_id="graph")
    child = SimpleNamespace(logical_path="base/workflows/components/task.md.html", stable_id="task")
    step = SimpleNamespace(id="task", kind="node", parent=None, owner_path=document.logical_path,
                           declaration_path=child.logical_path)
    edges = (SimpleNamespace(owner="GRAPH", source="START", target="task", condition="always"),
             SimpleNamespace(owner="GRAPH", source="task", target="END", condition="ready"))
    return SimpleNamespace(entry_path=document.logical_path, documents=(document, child),
                           steps=(step,), edges=edges, references=(), resources=()), document


def test_projection_source_preserves_effective_routes_and_admitted_links():
    projection, document = _projection()

    source, links = _projection_source(projection, document)

    assert b'start --> n0 : always' in source
    assert b'n0 --> end : ready' in source
    assert "../../workflows/components/task.md.html" in links
    assert b"!include" not in source


def test_projection_source_preserves_long_admitted_predicate():
    projection, document = _projection()
    predicate = 'task["route"] == "' + ('x' * 237) + '"'
    assert len(predicate) == 256
    projection.edges = (projection.edges[0],
                        SimpleNamespace(owner="GRAPH", source="task", target="END",
                                        condition=predicate))

    source, _ = _projection_source(projection, document)

    assert viewer_render._label(predicate).encode() in source


@pytest.mark.parametrize('literal', [
    'route == "[[https://example.org]]"',
    'route == "a__b"',
    'route == "a\\\\b"',
    'route == "<tag>&"',
])
def test_predicate_labels_encode_plantuml_markup_as_exact_text(literal):
    projection, document = _projection()
    projection.edges = (projection.edges[0],
                        SimpleNamespace(owner='GRAPH', source='task', target='END',
                                        condition=literal))

    source, links = _projection_source(projection, document)

    assert viewer_render._label(literal).encode() in source
    assert b'https://' not in source
    assert links == frozenset({'../../workflows/components/task.md.html'})


def test_schema_references_have_separate_lines_and_captured_pages():
    projection, document = _projection()
    schema = b'{"type":"object","properties":{"answer":{"type":"string"}}}'
    digest = __import__('hashlib').sha256(schema).hexdigest()
    reference = ViewerReference('functions', document.logical_path, 'task', 1,
                                'base/contracts/answer.json', digest, (('field', 'Input'),))
    resource = SimpleNamespace(logical_path=reference.target_path, sha256=digest,
                               content_bytes=schema)
    projection.references = (reference,)
    projection.resources = (resource,)

    source, links = _projection_source(projection, document)
    pages = viewer_render._reference_pages(projection)

    assert b'rectangle "task" as n0' in source
    assert b'Input: answer&#46;json' in source
    assert len(pages) == 1
    assert b'&quot;answer&quot;' in next(iter(pages.values()))
    assert any('contracts/' in link for link in links)

    _, repository_links = _projection_source(
        projection, document,
        repositories={'base': RepositoryIdentity('//github.com/example/viewer')})
    assert any(link.startswith('//github.com/example/viewer/blob/_____switch_branch_____/')
               and '/contracts/' in link and link.endswith('.md.html')
               for link in repository_links)


def test_cross_owner_schema_page_uses_referencing_declaration_owner():
    projection, document = _projection()
    document.logical_path = 'pack/task.md.html'
    projection.entry_path = document.logical_path
    projection.documents = (document,)
    projection.steps = (SimpleNamespace(id='task', kind='node', parent=None,
                                        owner_path=document.logical_path,
                                        declaration_path=document.logical_path),)
    schema = b'{"type":"object"}'
    digest = __import__('hashlib').sha256(schema).hexdigest()
    reference = ViewerReference('functions', document.logical_path, 'task', 1,
                                'base/contracts/input.json', digest, (('field', 'Input'),))
    projection.references = (reference,)
    projection.resources = (SimpleNamespace(logical_path=reference.target_path,
                                            sha256=digest, content_bytes=schema),)

    pages = viewer_render._reference_pages(projection)
    assert len(pages) == 1
    route = next(iter(pages))
    assert route.startswith('r/pack/.private_resources/diagrams/contracts/')
    assert b'&quot;type&quot;' in next(iter(pages.values()))
    _, links = _projection_source(
        projection, document,
        repositories={'pack': RepositoryIdentity('//github.com/example/viewer', 'demo')})
    assert any('/demo/.private_resources/diagrams/contracts/' in link for link in links)


@pytest.mark.parametrize('kind, field, path, data', [
    ('functions', 'Input', 'base/cfg/input.toml', b'{"type":"object"}'),
    ('timeouts', 'LIMIT', 'base/contracts/limit.json', b'[fields.LIMIT]\ndefault = 1\n'),
])
def test_reference_pages_skip_non_companion_target_extensions(kind, field, path, data):
    projection, document = _projection()
    digest = __import__('hashlib').sha256(data).hexdigest()
    attributes = (('field', field),) if kind == 'functions' else (('Key', field),)
    projection.references = (ViewerReference(kind, document.logical_path, 'task', 1,
                                             path, digest, attributes),)
    projection.resources = (SimpleNamespace(logical_path=path, sha256=digest,
                                            content_bytes=data),)

    assert viewer_render._reference_pages(projection) == {}


def test_timeout_companion_rejects_absent_selected_field():
    projection, document = _projection()
    data = b'[fields.OTHER]\ndefault = 1\n'
    digest = __import__('hashlib').sha256(data).hexdigest()
    path = 'base/cfg/limits.toml'
    projection.references = (ViewerReference('timeouts', document.logical_path, 'task', 1,
                                             path, digest, (('Key', 'LIMIT'),)),)
    projection.resources = (SimpleNamespace(logical_path=path, sha256=digest,
                                            content_bytes=data),)

    with pytest.raises(RenderError, match='invalid captured reference'):
        viewer_render._reference_pages(projection)


def test_pretty_reference_cannot_expand_past_display_budget():
    projection, document = _projection()
    data = b'[' + b','.join([b'{}'] * 18_000) + b']'
    assert len(data) < viewer_render._MAX_REFERENCE
    digest = __import__('hashlib').sha256(data).hexdigest()
    path = 'base/contracts/input.json'
    projection.references = (ViewerReference('functions', document.logical_path, 'task', 1,
                                             path, digest, (('field', 'Input'),)),)
    projection.resources = (SimpleNamespace(logical_path=path, sha256=digest,
                                            content_bytes=data),)

    with pytest.raises(RenderError, match='reference display exceeds limit'):
        viewer_render._reference_pages(projection)


def test_markdown_navigation_rejects_non_id_markup():
    assert viewer_render._markdown_id('llm_call') == 'llm_call'
    with pytest.raises(RenderError, match='navigation ID'):
        viewer_render._markdown_id('llm[call]')


def test_forged_reference_owner_cannot_borrow_selected_repository_identity():
    projection, document = _projection()
    data = b'{"type":"object"}'
    digest = __import__('hashlib').sha256(data).hexdigest()
    path = 'base/contracts/input.json'
    projection.references = (ViewerReference('functions', 'pack/foreign.md.html', 'task', 1,
                                             path, digest, (('field', 'Input'),)),)
    projection.resources = (SimpleNamespace(logical_path=path, sha256=digest,
                                            content_bytes=data),)

    with pytest.raises(RenderError, match='missing workflow repository identity'):
        _projection_source(projection, document,
                           repositories={'base': RepositoryIdentity('//github.com/example/viewer')})


def test_timeout_page_contains_only_selected_field():
    projection, document = _projection()
    config = (b'[fields.GRAPH_LIMIT]\ntype = "float"\ndefault = 600\n'
              b'[fields.SECRET_OTHER]\ndefault = "hidden"\n')
    digest = __import__('hashlib').sha256(config).hexdigest()
    reference = ViewerReference('timeouts', document.logical_path, 'task', 1,
                                'base/cfg/limits.toml', digest, (('Key', 'GRAPH_LIMIT'),))
    projection.references = (reference,)
    projection.resources = (SimpleNamespace(logical_path=reference.target_path,
                                            sha256=digest, content_bytes=config),)

    page = next(iter(viewer_render._reference_pages(projection).values()))

    assert b'600' in page
    assert b'SECRET_OTHER' not in page
    assert b'hidden' not in page


def test_reference_pages_escape_selected_bytes_and_exclude_native_source():
    projection, document = _projection()
    schema = b'{"description":"<script>alert(1)</script>","type":"object"}'
    config = (b'[fields.LIMIT]\ndefault = "<img src=x onerror=alert(1)>"\n'
              b'[fields.OTHER]\ndefault = "private unrelated declaration"\n')
    references = []
    resources = []
    for kind, field, path, body in (
            ('functions', 'Input', 'base/contracts/input.json', schema),
            ('timeouts', 'LIMIT', 'base/cfg/settings.toml', config),
            ('native_source', 'Input', 'base/code/task.py', b'private native body')):
        digest = __import__('hashlib').sha256(body).hexdigest()
        attributes = (('field', field),) if kind != 'timeouts' else (('Key', field),)
        references.append(ViewerReference(kind, document.logical_path, 'task', 1,
                                          path, digest, attributes))
        resources.append(SimpleNamespace(logical_path=path, sha256=digest,
                                         content_bytes=body))
    projection.references = tuple(references)
    projection.resources = tuple(resources)

    pages = viewer_render._reference_pages(projection)
    combined = b''.join(pages.values())

    assert len(pages) == 2
    assert combined.count(b'Content-Security-Policy') == 2
    assert b'&lt;script&gt;' in combined and b'<script>' not in combined
    assert b'&lt;img' in combined and b'<img' not in combined
    assert b'private unrelated declaration' not in combined
    assert b'private native body' not in combined


@pytest.mark.parametrize('captured', [False, True])
def test_reference_page_requires_matching_captured_resource(captured):
    projection, document = _projection()
    body = b'{"type":"object"}'
    digest = __import__('hashlib').sha256(body).hexdigest()
    reference = ViewerReference('functions', document.logical_path, 'task', 1,
                                'base/contracts/input.json', digest, (('field', 'Input'),))
    projection.references = (reference,)
    projection.resources = ((SimpleNamespace(logical_path=reference.target_path,
                                              sha256='0' * 64, content_bytes=body),)
                            if captured else ())

    with pytest.raises(RenderError, match='reference differs from captured resource'):
        viewer_render._reference_pages(projection)


def test_component_guard_routes_show_accepted_retry_and_exhaustion():
    root = SimpleNamespace(logical_path='base/flow.md.html', stable_id='graph')
    group = SimpleNamespace(logical_path='base/group.md.html', stable_id='group')
    first = SimpleNamespace(id='draft', kind='node', parent='group',
                            declaration_path=group.logical_path, declaration_fragment='node-draft')
    guard = SimpleNamespace(id='verify', kind='node', parent='group',
                            declaration_path=group.logical_path, declaration_fragment='node-verify')
    component = SimpleNamespace(id='group', kind='component', parent=None,
                                declaration_path=group.logical_path)
    edges = (SimpleNamespace(owner='group', source='START', target='draft', condition='always'),
             SimpleNamespace(owner='group', source='draft', target='verify', condition='always'),
             SimpleNamespace(owner='group', source='verify', target='END', condition='always'))
    controller = ViewerController('group', 'verify', 3, True, True, True)
    projection = SimpleNamespace(entry_path=root.logical_path, documents=(root, group),
                                 steps=(component, first, guard), edges=edges,
                                 references=(), resources=(), controllers=(controller,))

    source, links = _projection_source(projection, group)

    display = unescape(source.decode())
    assert 'n1 --> end : verify["accepted"] == True' in display
    assert 'n1 ..> n0 : verify["accepted"] == False and attempt < 3' in display
    assert 'n1 ..> exhausted : verify["accepted"] == False and attempt == 3' in display
    assert source.count(b'Attempt context: attempt 1..3; max_passes 3') == 2
    assert b'Max passes: 3' in source
    assert any(link.endswith('group.md.html#components') for link in links)
    assert any(link.endswith('group.md.html#node-draft') for link in links)


def _guard_projection(*, guard='verify', retry=True, exhaustion=True):
    root = SimpleNamespace(logical_path='base/flow.md.html', stable_id='graph')
    group = SimpleNamespace(logical_path='base/group.md.html', stable_id='group')
    component = SimpleNamespace(id='group', kind='component', parent=None,
                                declaration_path=group.logical_path)
    draft = SimpleNamespace(id='draft', kind='node', parent='group',
                            declaration_path=group.logical_path,
                            declaration_fragment='node-draft')
    verify = SimpleNamespace(id='verify', kind='node', parent='group',
                             declaration_path=group.logical_path,
                             declaration_fragment='node-verify')
    controller = ViewerController('group', guard, 3, True, retry, exhaustion)
    projection = SimpleNamespace(entry_path=root.logical_path, documents=(root, group),
                                 steps=(component, draft, verify), edges=(),
                                 references=(), resources=(), controllers=(controller,))
    return projection, group


def test_component_controller_rejects_missing_guard_step():
    projection, group = _guard_projection(guard='ghost')

    with pytest.raises(RenderError, match='controller differs from selected steps'):
        _projection_source(projection, group)


@pytest.mark.parametrize('accepted,attempt,message', [
    (True, 1, 'acceptance metadata'),
    (False, 1, 'retry metadata'),
    (False, 3, 'exhaustion metadata'),
])
def test_component_controller_rejects_runtime_decision_drift(
        monkeypatch, accepted, attempt, message):
    from api_agent.workflows import predicates
    projection, group = _guard_projection()
    original = predicates.component_decision

    def drift(controller, decision, current):
        if decision is accepted and current == attempt:
            return 'wrong'
        return original(controller, decision, current)

    monkeypatch.setattr(predicates, 'component_decision', drift)
    with pytest.raises(RenderError, match=message):
        _projection_source(projection, group)


def test_component_controller_without_retry_or_exhaustion_has_no_synthetic_routes():
    projection, group = _guard_projection(retry=False, exhaustion=False)

    source, _ = _projection_source(projection, group)

    assert b'EXHAUSTED' not in source
    assert b'..> n0' not in source


def test_nested_node_context_uses_nearest_component_budget():
    root = SimpleNamespace(logical_path='base/flow.md.html', stable_id='graph')
    outer_document = SimpleNamespace(logical_path='base/outer.md.html', stable_id='outer')
    inner_document = SimpleNamespace(logical_path='base/inner.md.html', stable_id='inner')
    outer = SimpleNamespace(id='outer', kind='component', parent=None,
                            declaration_path=outer_document.logical_path)
    inner = SimpleNamespace(id='inner', kind='component', parent='outer',
                            declaration_path=inner_document.logical_path)
    leaf = SimpleNamespace(id='leaf', kind='node', parent='inner',
                           declaration_path=inner_document.logical_path,
                           declaration_fragment='node-leaf')
    projection = SimpleNamespace(entry_path=root.logical_path,
                                 documents=(root, outer_document, inner_document),
                                 steps=(outer, inner, leaf),
                                 edges=(SimpleNamespace(owner='inner', source='START',
                                                        target='leaf', condition='always'),
                                        SimpleNamespace(owner='inner', source='leaf',
                                                        target='END', condition='always')),
                                 references=(), resources=(),
                                 controllers=(ViewerController('outer', '-', 1, False, False, False),
                                              ViewerController('inner', 'leaf', 2, True, True, True)))

    source, links = _projection_source(projection, inner_document)
    overview, _ = _projection_source(projection, root)

    assert b'Attempt context: attempt 1..2; max_passes 2' in source
    assert b'Attempt context: attempt 1..1' not in source
    assert b'Max passes: 2' in source
    assert any(link.endswith('inner.md.html#components') for link in links)
    assert b'outer' in overview
    assert b'leaf' not in overview


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


def test_svg_accepts_pinned_dashed_path_style():
    data = (b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
            b'viewBox="0 0 10 10"><path d="M0,0 L10,10" '
            b'style="stroke:#181818;stroke-width:1;stroke-dasharray:7,7;"/></svg>')

    assert b'stroke-dasharray:7,7' in validate_svg(data)


@pytest.mark.parametrize('dash', ['url(#bad)', '7,7,7', '0,0', '7, 7'])
def test_svg_rejects_other_css_dash_patterns(dash):
    data = (f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
            f'viewBox="0 0 10 10"><path d="M0,0 L10,10" '
            f'style="stroke:#181818;stroke-width:1;stroke-dasharray:{dash};"/></svg>').encode()

    with pytest.raises(RenderError, match='unsafe SVG style'):
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


def test_bundle_check_rejects_orphan_reference_page(tmp_path):
    root = tmp_path / 'project'
    current = root / '.private_resources/diagrams/flow.svg'
    orphan = root / '.private_resources/diagrams/contracts/stale.md.html'
    orphan.parent.mkdir(parents=True)
    current.write_bytes(b'current')
    orphan.write_bytes(b'<!-- api-agent-reference-v1 sha256:' + b'0' * 64 + b' -->\n')
    bundle = ViewBundle('r/base/flow.md.html',
                        (ViewFile('r/base/.private_resources/diagrams/flow.svg',
                                  'image/svg+xml', b'current'),))

    with pytest.raises(RenderError, match='orphaned workflow diagram'):
        check_view_bundle(bundle, roots={'base': root})


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
    reference_routes = [path for path in pages if '/contracts/' in path]
    assert len(reference_routes) == 2
    route = reference_routes[0]
    modified = tuple(replace(file, body=b'<html>changed</html>') if file.path == route else file
                     for file in bundle.files)
    forged = replace(bundle, files=modified,
                     routes=tuple(viewer_render.ViewRoute(file.path, file.content_type,
                                                          __import__('hashlib').sha256(file.body).hexdigest(),
                                                          len(file.body)) for file in modified))
    with pytest.raises(RenderError, match='reference page differs'):
        verify_bundle_proof(forged, selected)


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
    projection = SimpleNamespace(version=1, documents=(), resources=(), references=(),
                                 projection_sha256='0' * 64)
    source = Path(viewer_render.__file__).resolve().parents[3] / '.private_resources/viewer'
    monkeypatch.setattr(viewer_render, '_asset_root', lambda: tmp_path if installed else source)
    monkeypatch.setattr(viewer_render, '_read_view_file', lambda path: None)
    expected = 'reinstall api-agent' if installed else 'run npm ci from the repository root'
    with pytest.raises(RenderError, match=expected):
        build_view_bundle(projection, roots={}, renderer=object(), verify=False)


@pytest.mark.parametrize('fragment,expected', [
    ('node-leaf', b'group.md.html#node-leaf'),
    ('node leaf/#', b'group.md.html#node%20leaf%2F%23'),
    ('', b'group.md.html'),
])
def test_hosted_flow_preserves_selected_fragment(fragment, expected):
    raw = b'# Component: group\n\n## Flow\n\n1. [node leaf](original.md.html#ignored)\n'
    document = SimpleNamespace(logical_path='base/group.md.html', raw_bytes=raw,
                               executable_bytes=raw)
    step = SimpleNamespace(id='leaf', owner_path=document.logical_path, flow_line=5,
                           declaration_path=document.logical_path,
                           declaration_fragment=fragment)
    foreign = SimpleNamespace(id='other', owner_path='base/other.md.html')
    projection = SimpleNamespace(steps=(step, foreign), documents=(document,), resources=())

    hosted = viewer_render._hosted_declaration(projection, document)

    assert hosted == raw.replace(b'original.md.html#ignored', expected)
