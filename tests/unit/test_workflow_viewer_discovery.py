"""Recursive static admission and whole-inventory publication controls."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from api_agent.workflows import viewer_build as vb
from api_agent.workflows.viewer_render import RenderError, ViewBundle, ViewFile


def entry(root, relative="flow.md.html", heading="# Workflow v2"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(heading + "\n")
    return path


def test_repository_inventory_includes_permanent_and_integration_fixtures():
    root = Path(__file__).resolve().parents[2]
    expected = {"flow.md.html", "demo/simple/flow.md.html", "demo/contrived/flow.md.html",
                "tests/integration/fixtures/workflow_preferences/flow.md.html",
                "tests/integration/fixtures/tool_instances/shared_primary/flow.md.html",
                "tests/integration/fixtures/tool_instances/independent/flow.md.html"}
    assert {p.relative_to(root).as_posix() for p in vb.discover_view_entries(root)} == expected
    assert {p.graph_id: p.entry_path for p in vb._projections(root)} == {
        "simple_chat": "base/flow.md.html",
        "useful_api_demo": "base/demo/simple/flow.md.html",
        "contrived_qualification": "pack/contrived/flow.md.html",
        "workflow_preferences": "base/flow.md.html",
        "independent_tools": "base/tests/integration/fixtures/tool_instances/independent/flow.md.html",
        "shared_primary_tools": "base/tests/integration/fixtures/tool_instances/shared_primary/flow.md.html",
    }


@pytest.mark.parametrize("directory", sorted(vb._DISCOVERY_EXCLUDED) + [".cache", ".private_resources"])
def test_discovery_ignores_explicit_dependency_and_runtime_trees(tmp_path, directory):
    root = entry(tmp_path)
    entry(tmp_path, directory + "/flow.md.html", "broken ignored content")
    assert vb.discover_view_entries(tmp_path) == (root,)


def test_foreign_repositories_are_excluded(tmp_path):
    root = entry(tmp_path)
    entry(tmp_path, "foreign/flow.md.html", "broken ignored content")
    (tmp_path / "foreign/.git").write_text("gitdir: elsewhere")
    assert vb.discover_view_entries(tmp_path) == (root,)


@pytest.mark.parametrize("heading", ["# Component v2: child", "# Node v2: child",
                                      "# Component v3: child", "# Node v3: child"])
def test_named_component_and_node_documents_are_not_graph_roots(tmp_path, heading):
    root = entry(tmp_path)
    entry(tmp_path, "nested/flow.md.html", heading)
    assert vb.discover_view_entries(tmp_path) == (root,)


def test_discovery_rejects_malformed_nested_graph(tmp_path):
    entry(tmp_path)
    entry(tmp_path, "nested/flow.md.html", "# Workflow v999")
    with pytest.raises(RenderError, match="invalid entry heading"):
        vb.discover_view_entries(tmp_path)


@pytest.mark.parametrize("linked_directory", [False, True])
def test_discovery_rejects_symlinks(tmp_path, linked_directory):
    root = tmp_path / "root"
    root.mkdir()
    source = entry(tmp_path, "outside/flow.md.html")
    target = root / ("linked" if linked_directory else "flow.md.html")
    target.symlink_to(source.parent if linked_directory else source, target_is_directory=linked_directory)
    with pytest.raises(RenderError, match="symlink rejected"):
        vb.discover_view_entries(root)


@pytest.mark.parametrize(("limit", "value", "message"), [
    ("_MAX_DISCOVERY_ENTRIES", 0, "entry limit"),
    ("_MAX_DISCOVERY_DEPTH", 0, "depth limit"),
    ("_MAX_GRAPH_ENTRIES", 0, "graph limit"),
    ("_MAX_VIEW_FILE", 1, "bounded regular"),
])
def test_discovery_enforces_bounds(tmp_path, monkeypatch, limit, value, message):
    entry(tmp_path, "nested/flow.md.html")
    monkeypatch.setattr(vb, limit, value)
    with pytest.raises(RenderError, match=message):
        vb.discover_view_entries(tmp_path)


def test_nested_only_inventory_is_supported(tmp_path, monkeypatch):
    nested = entry(tmp_path, "nested/flow.md.html")
    monkeypatch.setattr(vb, "_projection", lambda root, path=None: path)
    assert vb._projections(tmp_path) == (nested,)


def test_empty_inventory_fails_clearly(tmp_path):
    with pytest.raises(RenderError, match="no graph entries"):
        vb._projections(tmp_path)


def test_shared_outputs_deduplicated_only_when_bytes_agree(tmp_path, monkeypatch):
    first = ViewFile("r/base/shared.svg", "image/svg+xml", b"same")
    monkeypatch.setattr(vb, "build_view_bundle", lambda *a, **k: ViewBundle("", (first,)))
    assert vb._bundle_files((object(), object()), roots={"base": tmp_path}) == (first,)


def test_shared_output_conflict_fails_without_writes(tmp_path, monkeypatch):
    source = entry(tmp_path)
    original = source.read_bytes()
    projections = (SimpleNamespace(documents=(), resources=()),) * 2
    monkeypatch.setattr(vb, "_projections", lambda root: projections)
    counter = iter((b"first", b"second"))
    monkeypatch.setattr(vb, "build_view_bundle", lambda *a, **k: ViewBundle("", (
        ViewFile("r/base/.private_resources/diagrams/shared.svg", "image/svg+xml", next(counter)),)))
    with pytest.raises(RenderError, match="disagree on shared output"):
        vb.generate_views(tmp_path, renderer=object(), repository_url="//github.com/example/agent")
    assert list(tmp_path.iterdir()) == [source]
    assert source.read_bytes() == original


def test_check_processes_nested_inventory_without_writes(tmp_path, monkeypatch):
    root = entry(tmp_path)
    nested = entry(tmp_path, "nested/flow.md.html")
    monkeypatch.setattr(vb, "_projection", lambda root, path=None: path or root / "flow.md.html")
    calls = []
    def check(projection, **kwargs):
        assert kwargs["verify"] is True
        calls.append(projection)
        if projection == nested:
            raise RenderError("nested saved view stale")
        return ViewBundle("", ())
    monkeypatch.setattr(vb, "build_view_bundle", check)
    before = {path: path.read_bytes() for path in (root, nested)}
    with pytest.raises(RenderError, match="nested saved view stale"):
        vb.check_views(tmp_path)
    assert calls == [root, nested]
    assert {path: path.read_bytes() for path in before} == before


def minimal_graph(root, directory=""):
    prefix = "../" * len(Path(directory).parts)
    base = root / directory
    base.mkdir(parents=True, exist_ok=True)
    (root / "native.py").write_text("raise RuntimeError('must never execute')\n")
    (root / "schema.json").write_text('{"type":"object"}')
    (base / "flow.md.html").write_text(
        "# Workflow v2\n\n## Graph ID\n\n| ID |\n| --- |\n| example |\n\n"
        "## Flow\n\n1.  [node work](work.md.html)\n")
    (base / "work.md.html").write_text(
        "# Node v2: work\n\n## Native\n\n| Registry | Kind | Source |\n| --- | --- | --- |\n"
        f"| work | deterministic | [Python]({prefix}native.py) |\n\n"
        "## Functions\n\n| ID | Registry | Input | Output |\n| --- | --- | --- | --- |\n"
        f"| work | work | [input]({prefix}schema.json) | [output]({prefix}schema.json) |\n")
    return base / "flow.md.html"


class TinyRenderer:
    def render(self, source, *, allowed_links=frozenset()):
        return (b'<?xml version="1.0"?>\n'
                b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" viewBox="0 0 1 1"/>')


def test_nested_projection_keeps_repository_relative_shared_resources(tmp_path):
    source = minimal_graph(tmp_path, "nested")
    projection = vb._projection(tmp_path, source)
    assert projection.entry_path == "base/nested/flow.md.html"
    assert {resource.logical_path for resource in projection.resources} >= {
        "base/native.py", "base/schema.json"}


def test_whole_inventory_generate_check_and_nested_staleness(tmp_path):
    root = minimal_graph(tmp_path)
    nested = minimal_graph(tmp_path, "nested")
    vb.generate_views(tmp_path, renderer=TinyRenderer(), repository_url="//github.com/example/agent")
    vb.check_views(tmp_path)
    diagrams = tmp_path / ".private_resources/diagrams"
    assert (diagrams / "flow.svg").is_file()
    assert (diagrams / "nested/flow.svg").is_file()
    original = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert vb.generate_views(tmp_path, renderer=TinyRenderer()) == ()
    assert {p: p.read_bytes() for p in original} == original
    assert b".private_resources/viewer/" in root.read_bytes()
    assert b"../.private_resources/viewer/" in nested.read_bytes()
    nested.write_bytes(nested.read_bytes().replace(b"| example |", b"| changed |"))
    with pytest.raises(RenderError):
        vb.check_views(tmp_path)


def test_new_graph_during_render_fails_before_writing(tmp_path, monkeypatch):
    source = entry(tmp_path)
    before = source.read_bytes()
    projection = SimpleNamespace(documents=(), resources=())
    monkeypatch.setattr(vb, "_projections", lambda root: (projection,))
    def render(*args, **kwargs):
        entry(tmp_path, "new/flow.md.html")
        return ViewBundle("", ())
    monkeypatch.setattr(vb, "build_view_bundle", render)
    with pytest.raises(RenderError, match="inventory changed"):
        vb.generate_views(tmp_path, renderer=object(), repository_url="//github.com/example/agent")
    assert source.read_bytes() == before
    assert not (tmp_path / "cfg").exists()
    assert not (tmp_path / ".private_resources").exists()


def test_managed_canonical_selection_accepts_independent_artifacts_but_checks_selected_bytes(tmp_path, monkeypatch):
    from api_agent.runtime.view_flow import _prepare_view
    from api_agent.workflows.definition import load_definition
    import importlib
    runtime = importlib.import_module("api_agent.runtime.view_flow")
    minimal_graph(tmp_path)
    minimal_graph(tmp_path, "nested")
    vb.generate_views(tmp_path, renderer=TinyRenderer(), repository_url="//github.com/example/agent")
    monkeypatch.setattr(runtime, "load_selected_definition", lambda context: load_definition(
        tmp_path / "flow.md.html", base_root=tmp_path, pack_root=tmp_path))
    context = SimpleNamespace(base_root=tmp_path, workflow_base_root=None)

    prepared = _prepare_view(context)

    assert prepared[2] is True
    assert all("nested" not in file.path for file in prepared[3].files)
    selected = tmp_path / ".private_resources/diagrams/flow.svg"
    selected.write_bytes(selected.read_bytes().replace(b'width="1"', b'width="2"'))
    with pytest.raises(RenderError):
        _prepare_view(context)


def test_source_inventory_check_still_rejects_unowned_orphan(tmp_path):
    minimal_graph(tmp_path)
    minimal_graph(tmp_path, "nested")
    vb.generate_views(tmp_path, renderer=TinyRenderer(), repository_url="//github.com/example/agent")
    orphan = tmp_path / ".private_resources/diagrams/orphan.svg"
    orphan.write_bytes(b"unknown human output")
    with pytest.raises(RenderError, match="orphaned workflow diagram"):
        vb.check_views(tmp_path)
    assert orphan.read_bytes() == b"unknown human output"


def test_discovery_rejects_fifo_entry_without_blocking(tmp_path):
    import os
    os.mkfifo(tmp_path / "flow.md.html")
    with pytest.raises(RenderError, match="bounded regular file"):
        vb.discover_view_entries(tmp_path)


@pytest.mark.parametrize("heading", ["# Component v2: child", "# Node v2: child",
                                      "# Component v3: child", "# Node v3: child"])
def test_top_level_component_does_not_mask_nested_graph(tmp_path, heading):
    entry(tmp_path, heading=heading)
    nested = entry(tmp_path, "nested/flow.md.html")
    assert vb.discover_view_entries(tmp_path) == (nested,)


def test_conflicting_shared_source_admission_fails_before_any_output(tmp_path, monkeypatch):
    source = entry(tmp_path)
    before = source.read_bytes()
    projections = tuple(SimpleNamespace(documents=(SimpleNamespace(
        logical_path="base/flow.md.html", raw_bytes=body),), resources=())
        for body in (before, b"different admitted source"))
    monkeypatch.setattr(vb, "_projections", lambda root: projections)
    with pytest.raises(RenderError, match="disagree on admitted source"):
        vb.generate_views(tmp_path, renderer=object())
    assert source.read_bytes() == before
    assert list(tmp_path.iterdir()) == [source]


def roots_manifest(root, entries):
    import json
    target = root / vb.VIEW_ROOTS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"version": 1, "entries": entries}))
    return target


def mapped_graphs(root):
    minimal_graph(root)
    minimal_graph(root, "pack/nested")
    fixture = root / "fixture"
    minimal_graph(fixture)
    roots_manifest(root, {
        "pack/nested/flow.md.html": {"base_root": ".", "pack_root": "pack", "entry_root": "pack"},
        "fixture/flow.md.html": {"base_root": "fixture", "pack_root": "fixture", "entry_root": "base"},
    })
    return root / "pack", fixture


def test_mapped_owner_envelopes_and_identities_match_actual_loaders(tmp_path):
    from api_agent.workflows.definition import load_definition
    from api_agent.workflows.viewer_projection import project_view
    from api_agent.workflows.viewer_render import build_view_bundle, capture_repository_sources
    pack, fixture = mapped_graphs(tmp_path)

    vb.generate_views(tmp_path, renderer=TinyRenderer(), repository_url="//github.com/example/agent")
    vb.check_views(tmp_path)

    for entry, base, selected_pack in ((pack / "nested/flow.md.html", tmp_path, pack),
                                       (fixture / "flow.md.html", fixture, fixture)):
        definition = load_definition(entry, base_root=base, pack_root=selected_pack)
        repositories, sources = capture_repository_sources(definition.roots)
        assert repositories[definition.entry_root].path_prefix == selected_pack.relative_to(tmp_path).as_posix()
        bundle = build_view_bundle(project_view(definition), roots=definition.roots, verify=True,
                                   repositories=repositories, repository_sources=sources)
        assert bundle.files
    assert (pack / ".private_resources/diagrams/nested/flow.svg").is_file()
    assert (fixture / ".private_resources/diagrams/flow.svg").is_file()
    assert not (tmp_path / ".private_resources/diagrams/pack").exists()
    assert b"../.private_resources/viewer/" in (pack / "nested/flow.md.html").read_bytes()
    assert b'".private_resources/viewer/' in (fixture / "flow.md.html").read_bytes()
    assert vb.generate_views(tmp_path, renderer=TinyRenderer()) == ()


@pytest.mark.parametrize("document", [[], {"version": True, "entries": {}},
    {"version": 2, "entries": {}}, {"version": 1, "entries": []},
    {"version": 1, "entries": {}}, {"version": 1, "entries": {}, "extra": 1}])
def test_roots_manifest_rejects_invalid_top_level(tmp_path, document):
    import json
    path = roots_manifest(tmp_path, {})
    path.write_text(json.dumps(document))
    with pytest.raises(RenderError, match="configuration"):
        vb.read_view_roots(tmp_path)


@pytest.mark.parametrize("row", [None, {},
    {"base_root": ".", "pack_root": ".", "entry_root": "unknown"},
    {"base_root": ".", "pack_root": ".", "entry_root": "base", "extra": 1},
    {"base_root": ".", "pack_root": ".", "entry_root": "base"},
])
def test_roots_manifest_rejects_invalid_or_default_entry(tmp_path, row):
    entry(tmp_path)
    roots_manifest(tmp_path, {"flow.md.html": row})
    with pytest.raises(RenderError, match="entry|nondefault"):
        vb.read_view_roots(tmp_path)


@pytest.mark.parametrize("relative", ["../outside", "/absolute", "a//b", "a/./b", "a/../b", "a\\b", "a?b", "a#b", "a\x00b", "", 1])
def test_roots_manifest_rejects_noncanonical_paths(tmp_path, relative):
    entry(tmp_path)
    roots_manifest(tmp_path, {"flow.md.html": {"base_root": relative, "pack_root": ".", "entry_root": "base"}})
    with pytest.raises(RenderError, match="roots path"):
        vb.read_view_roots(tmp_path)


def test_roots_manifest_rejects_stale_and_unknown_graph_entries(tmp_path):
    entry(tmp_path)
    roots_manifest(tmp_path, {"missing/flow.md.html": {"base_root": ".", "pack_root": ".", "entry_root": "pack"}})
    with pytest.raises(RenderError, match="unknown or stale"):
        vb.read_view_roots(tmp_path)


def test_roots_manifest_rejects_duplicate_json_keys(tmp_path):
    path = roots_manifest(tmp_path, {})
    path.write_text('{"version":1,"entries":{},"entries":{}}')
    with pytest.raises(RenderError, match="duplicate"):
        vb.read_view_roots(tmp_path)


def test_roots_manifest_rejects_entry_outside_declared_owner(tmp_path):
    entry(tmp_path)
    (tmp_path / "other").mkdir()
    roots_manifest(tmp_path, {"flow.md.html": {"base_root": "other", "pack_root": ".", "entry_root": "base"}})
    with pytest.raises(RenderError, match="outside mapped owner"):
        vb.read_view_roots(tmp_path)


def test_roots_manifest_rejects_linked_owner_directory(tmp_path):
    graph = entry(tmp_path)
    (tmp_path / "linked").symlink_to(tmp_path, target_is_directory=True)
    roots_manifest(tmp_path, {"flow.md.html": {"base_root": ".", "pack_root": "linked", "entry_root": "base"}})
    with pytest.raises(RenderError, match="symlink"):
        vb.read_view_roots(tmp_path, (graph,))


def test_roots_manifest_rejects_linked_or_oversized_config(tmp_path):
    path = roots_manifest(tmp_path, {})
    original = tmp_path / "original.json"
    path.rename(original)
    path.symlink_to(original)
    with pytest.raises(RenderError, match="symlink"):
        vb.read_view_roots(tmp_path)
    path.unlink()
    path.write_bytes(b" " * (vb._MAX_ROOTS_FILE + 1))
    with pytest.raises(RenderError, match="size limit"):
        vb.read_view_roots(tmp_path)


def test_mapping_edit_during_render_fails_without_outputs(tmp_path, monkeypatch):
    mapped_graphs(tmp_path)
    original = vb.build_view_bundle
    first = True
    def edit_mapping(projection, **kwargs):
        nonlocal first
        bundle = original(projection, **kwargs)
        if first:
            first = False
            path = tmp_path / vb.VIEW_ROOTS_PATH
            path.write_bytes(path.read_bytes() + b"\n")
        return bundle
    monkeypatch.setattr(vb, "build_view_bundle", edit_mapping)
    with pytest.raises(RenderError, match="inventory changed"):
        vb.generate_views(tmp_path, renderer=TinyRenderer(), repository_url="//github.com/example/agent")
    assert not (tmp_path / "cfg/workflow-view.toml").exists()
    assert not (tmp_path / "pack/.private_resources").exists()
    assert not (tmp_path / "fixture/.private_resources").exists()


def test_failure_after_nested_owner_publish_rolls_back_every_owner(tmp_path, monkeypatch):
    pack, fixture = mapped_graphs(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    prior_recovery = set(tmp_path.parent.glob(".workflow-view-recovery-*"))
    actual = vb._apply_output
    applied = []
    def fail_after_nested(root, stage, recovery):
        actual(root, stage, recovery)
        applied.append(stage.path)
        if stage.path == fixture / "flow.md.html":
            raise RenderError("controlled nested publication failure")
    monkeypatch.setattr(vb, "_apply_output", fail_after_nested)
    with pytest.raises(RenderError, match="controlled nested publication failure"):
        vb.generate_views(tmp_path, renderer=TinyRenderer(), repository_url="//github.com/example/agent")
    assert tmp_path / "cfg/workflow-view.toml" in applied
    assert fixture / "cfg/workflow-view.toml" in applied
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert set(tmp_path.parent.glob(".workflow-view-recovery-*")) == prior_recovery


def test_owner_local_orphan_requires_confirmation_without_deleting_other_graphs(tmp_path):
    pack, fixture = mapped_graphs(tmp_path)
    vb.generate_views(tmp_path, renderer=TinyRenderer(), repository_url="//github.com/example/agent")
    other = pack / ".private_resources/diagrams/nested/flow.svg"
    before = other.read_bytes()
    body = b"# old generated output\n"
    from hashlib import sha256
    orphan = fixture / ".private_resources/diagrams/orphan.md"
    orphan.write_bytes(b"<!-- api-agent-companion-v1 sha256:" + sha256(body).hexdigest().encode() + b" -->\n" + body)
    with pytest.raises(RenderError, match="orphaned workflow diagram"):
        vb.check_views(tmp_path)
    with pytest.raises(RenderError, match="orphaned or edited"):
        vb.generate_views(tmp_path, renderer=TinyRenderer(), fix_orphans=True)
    assert orphan.exists()
    assert vb.generate_views(tmp_path, renderer=TinyRenderer(), fix_orphans=True, confirmed_orphans=(orphan,)) == (orphan,)
    assert other.read_bytes() == before
    vb.check_views(tmp_path)


def test_explicit_owner_roots_union_is_deterministic(tmp_path):
    pack, fixture = mapped_graphs(tmp_path)
    assert vb.viewer_owner_roots(tmp_path) == tuple(sorted((tmp_path, pack, fixture)))


def test_roots_manifest_rejects_dot_as_a_graph_file(tmp_path):
    roots_manifest(tmp_path, {".": {"base_root": ".", "pack_root": ".", "entry_root": "pack"}})
    with pytest.raises(RenderError, match="must be a file"):
        vb.read_view_roots(tmp_path)


def test_mapping_projection_inventory_mismatch_fails_before_writes(tmp_path, monkeypatch):
    mapped_graphs(tmp_path)
    monkeypatch.setattr(vb, "_projections", lambda root: ())
    with pytest.raises(RenderError, match="projection inventory changed"):
        vb.generate_views(tmp_path, renderer=object(), repository_url="//github.com/example/agent")
    assert not (tmp_path / "cfg/workflow-view.toml").exists()


def test_rebased_bundle_cannot_publish_outside_repository(tmp_path):
    source = ViewFile("r/base/file.svg", "image/svg+xml", b"graphic")
    outside = tmp_path / "outside"
    inside = tmp_path / "inside"
    outside.mkdir()
    inside.mkdir()
    with pytest.raises(RenderError, match="output outside repository"):
        vb._rebase_file(source, {"base": outside}, inside)


def test_mapped_check_requires_recorded_repository_identity(tmp_path):
    mapped_graphs(tmp_path)
    with pytest.raises(RenderError, match="identity missing for mapped roots"):
        vb.check_views(tmp_path)


def test_missing_owner_identity_is_not_silently_initialized_by_check(tmp_path):
    from api_agent.workflows.viewer_repository import RepositoryIdentity, repository_config_bytes
    pack, fixture = mapped_graphs(tmp_path)
    config = tmp_path / "cfg/workflow-view.toml"
    config.write_bytes(repository_config_bytes(RepositoryIdentity("//github.com/example/agent")))
    with pytest.raises(RenderError, match="owner repository identity missing"):
        vb.check_views(tmp_path)
    assert not (pack / "cfg/workflow-view.toml").exists()
    assert not (fixture / "cfg/workflow-view.toml").exists()


def test_owner_local_identity_must_match_recorded_repository_prefix(tmp_path):
    from api_agent.workflows.viewer_repository import RepositoryIdentity, repository_config_bytes
    pack, fixture = mapped_graphs(tmp_path)
    path = pack / "cfg/workflow-view.toml"
    path.parent.mkdir()
    path.write_bytes(repository_config_bytes(RepositoryIdentity("//github.com/example/agent", "wrong")))
    root_identity = RepositoryIdentity("//github.com/example/agent")
    with pytest.raises(RenderError, match="identity differs from repository"):
        vb._owner_records(tmp_path, (tmp_path, pack), (root_identity, repository_config_bytes(root_identity)), initialize=True)


def test_distinct_contexts_cannot_disagree_on_same_physical_output(tmp_path, monkeypatch):
    minimal_graph(tmp_path)
    minimal_graph(tmp_path, "pack/nested")
    roots_manifest(tmp_path, {"pack/nested/flow.md.html": {
        "base_root": ".", "pack_root": "pack", "entry_root": "pack"}})
    counter = iter((b"first", b"second"))
    def conflicting_bundle(projection, **kwargs):
        return ViewBundle("", (ViewFile("r/base/shared.svg", "image/svg+xml", next(counter)),))
    monkeypatch.setattr(vb, "build_view_bundle", conflicting_bundle)
    with pytest.raises(RenderError, match="disagree on shared output"):
        vb.generate_views(tmp_path, renderer=object(), repository_url="//github.com/example/agent")
    assert not (tmp_path / "shared.svg").exists()
    assert not (tmp_path / "cfg/workflow-view.toml").exists()


@pytest.mark.parametrize("link", [".private_resources", ".private_resources/viewer", ".private_resources/diagrams"])
def test_output_inventory_rejects_linked_managed_directory(tmp_path, link):
    outside = tmp_path / "outside"
    outside.mkdir()
    path = tmp_path / link
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RenderError, match="output symlink rejected"):
        vb._output_snapshot(tmp_path)


@pytest.mark.parametrize("directory", sorted(vb._DISCOVERY_EXCLUDED) + [".hidden"])
def test_mapped_owner_cannot_enter_ignored_dependency_or_runtime_tree(tmp_path, directory):
    graph = entry(tmp_path)
    (tmp_path / directory).mkdir()
    roots_manifest(tmp_path, {"flow.md.html": {"base_root": ".", "pack_root": directory, "entry_root": "base"}})
    with pytest.raises(RenderError, match="ignored tree"):
        vb.read_view_roots(tmp_path, (graph,))


def test_mapped_owner_cannot_enter_foreign_repository(tmp_path):
    graph = entry(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / ".git").write_text("gitdir: elsewhere")
    roots_manifest(tmp_path, {"flow.md.html": {"base_root": ".", "pack_root": "foreign", "entry_root": "base"}})
    with pytest.raises(RenderError, match="foreign repository"):
        vb.read_view_roots(tmp_path, (graph,))


def test_mapping_missing_owner_directory_fails_clearly(tmp_path):
    entry(tmp_path)
    roots_manifest(tmp_path, {"flow.md.html": {"base_root": ".", "pack_root": "missing", "entry_root": "base"}})
    with pytest.raises(RenderError, match="directory is missing or inaccessible"):
        vb.read_view_roots(tmp_path)


def test_mapping_existing_component_document_is_not_a_source_graph_entry(tmp_path):
    entry(tmp_path)
    entry(tmp_path, "nested/flow.md.html", "# Component v2: child")
    roots_manifest(tmp_path, {"nested/flow.md.html": {"base_root": ".", "pack_root": ".", "entry_root": "pack"}})
    with pytest.raises(RenderError, match="unknown or stale"):
        vb.read_view_roots(tmp_path)
