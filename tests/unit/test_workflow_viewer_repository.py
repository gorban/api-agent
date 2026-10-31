"""Recorded repository identity and finite SVG navigation tests."""
from pathlib import Path

import pytest

from api_agent.workflows.viewer_repository import (
    RepositoryIdentity, identity_from_origin, read_repository_identity, repository_config_bytes,
)
from api_agent.workflows.viewer_render import _navigation_script, _strip_navigation, RenderError, validate_svg


@pytest.mark.parametrize("origin", ["https://github.com/example/agent.git", "git@github.com:example/agent.git", "ssh://git@github.com/example/agent"])
def test_explicit_origin_normalizes_to_recorded_scheme_relative_identity(origin):
    assert identity_from_origin(origin) == RepositoryIdentity("//github.com/example/agent")


@pytest.mark.parametrize("origin", ["https://user:secret@github.com/example/agent", "https://github.com/example/agent?token=x", "http://github.com/example/agent", "git@other.example:example/agent.git"])
def test_origin_rejects_credentials_and_unapproved_authorities(origin):
    with pytest.raises(ValueError):
        identity_from_origin(origin)


@pytest.mark.parametrize("prefix", ["../other", "/absolute", "a//b", "a%2fb", "a\\b", "a/"])
def test_repository_prefix_is_bounded_owner_relative(prefix):
    with pytest.raises(ValueError):
        RepositoryIdentity("//github.com/example/agent", prefix)


def test_recorded_identity_round_trip_and_exact_placeholder(tmp_path):
    identity = RepositoryIdentity("//github.com/example/agent", "packages/core")
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg/workflow-view.toml").write_bytes(repository_config_bytes(identity))
    assert read_repository_identity(tmp_path) == (identity, repository_config_bytes(identity))
    assert identity.link("workflows/a b.md.html") == "//github.com/example/agent/blob/_____switch_branch_____/packages/core/workflows/a%20b.md.html"


def test_missing_recorded_identity_has_no_discovery_or_writes(tmp_path):
    assert read_repository_identity(tmp_path) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("data", [b'version = 2\nrepository_url = "//github.com/example/agent"\n', b'version = true\nrepository_url = "//github.com/example/agent"\n', b'version = 1\nrepository_url = "//github.com/example/agent"\nextra = 1\n', b'version = 1\nrepository_url = 3\n'])
def test_recorded_configuration_rejects_unknown_schema(tmp_path, data):
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg/workflow-view.toml").write_bytes(data)
    with pytest.raises(ValueError):
        read_repository_identity(tmp_path)


def test_svg_envelope_contains_only_relative_roots_and_exact_removal(tmp_path):
    script = _navigation_script("base/flow.md.html", {"base": tmp_path}, {"base": RepositoryIdentity("//github.com/example/agent")})
    assert b'../../' in script
    assert str(tmp_path).encode() not in script
    graphic = b'<?xml version="1.0"?>\n<svg></svg>'
    wrapped = graphic[:-6] + script + b'</svg>'
    assert _strip_navigation(wrapped, script) == graphic
    with pytest.raises(RenderError):
        _strip_navigation(wrapped.replace(b'v1', b'v2'), script)


def test_svg_mapping_rejects_same_repository_for_distinct_roots(tmp_path):
    identity = RepositoryIdentity("//github.com/example/agent")
    with pytest.raises(RenderError, match="ambiguous"):
        _navigation_script("base/flow.md.html", {"base": tmp_path / "base", "pack": tmp_path / "pack"}, {"base": identity, "pack": identity})


def test_raw_svg_scripts_still_reject():
    with pytest.raises(RenderError):
        validate_svg(b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 10 10"><script href="trusted.js"/></svg>')


@pytest.mark.parametrize("target", ["", "../node", "a//b", "a?query", "a\\b", "a#fragment", "\x00"])
def test_repository_target_rejects_escaping_or_ambiguous_paths(target):
    with pytest.raises(ValueError):
        RepositoryIdentity("//github.com/example/agent").link(target)


@pytest.mark.parametrize("url", ["https://github.com/example/agent", "//other.example/example/agent", "//github.com/example/agent.git", "//github.com/example/agent?secret=x"])
def test_recorded_repository_url_is_canonical(url):
    with pytest.raises(ValueError):
        RepositoryIdentity(url)


@pytest.mark.parametrize("kind", ["fifo", "directory", "large", "symlink", "parent-symlink"])
def test_recorded_repository_rejects_special_or_unbounded_files(tmp_path, kind):
    import os
    directory = tmp_path / "cfg"
    directory.mkdir()
    path = directory / "workflow-view.toml"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    elif kind == "large":
        path.write_bytes(b" " * 4097)
    elif kind == "symlink":
        path.symlink_to(tmp_path / "missing")
    else:
        directory.rmdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        directory.symlink_to(outside, target_is_directory=True)
    expected = "symlink rejected" if "symlink" in kind else "bounded regular file"
    with pytest.raises(ValueError, match=expected):
        read_repository_identity(tmp_path)


def test_config_inode_swap_does_not_follow_final_symlink(tmp_path, monkeypatch):
    from api_agent.workflows import viewer_repository
    directory = tmp_path / "cfg"
    directory.mkdir()
    path = directory / "workflow-view.toml"
    path.write_bytes(repository_config_bytes(RepositoryIdentity("//github.com/example/agent")))
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    original = viewer_repository.os.open

    def swap_then_open(target, flags, **kwargs):
        if target == path.name:
            path.unlink()
            path.symlink_to(outside)
        return original(target, flags, **kwargs)

    monkeypatch.setattr(viewer_repository.os, "open", swap_then_open)
    with pytest.raises(OSError):
        read_repository_identity(tmp_path)


def test_capture_repository_sources_deduplicates_alias_roots(tmp_path):
    from api_agent.workflows.viewer_render import capture_repository_sources
    identity = RepositoryIdentity("//github.com/example/agent")
    (tmp_path / "cfg").mkdir()
    data = repository_config_bytes(identity)
    (tmp_path / "cfg/workflow-view.toml").write_bytes(data)
    identities, sources = capture_repository_sources({"base": tmp_path, "pack": tmp_path})
    assert identities == {"base": identity, "pack": identity}
    assert len(sources) == 1
    assert sources[0].logical_path == "base/cfg/workflow-view.toml"
    assert sources[0].served is False


@pytest.mark.parametrize("content", [b"", b"<title>Diagram</title>"])
def test_svg_navigation_provenance_rejects_script_change_with_recomputed_digest(tmp_path, content):
    from hashlib import sha256
    import json
    import re
    from api_agent.workflows.viewer_build import _projection
    from api_agent.workflows.viewer_render import build_view_bundle, _physical_file
    projection = _projection(Path(__file__).resolve().parents[2])
    repositories = {"base": RepositoryIdentity("//github.com/example/agent")}

    class Renderer:
        def render(self, source, *, allowed_links):
            assert all(link.startswith("//github.com/example/agent/blob/_____switch_branch_____/") for link in allowed_links)
            return b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" viewBox="0 0 1 1">' + content + b'</svg>'

    roots = {"base": tmp_path, "pack": tmp_path}
    bundle = build_view_bundle(projection, roots=roots, repositories=repositories, renderer=Renderer(), verify=False, hosted_selected=False)
    for file in bundle.files:
        path = _physical_file(file.path, roots)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file.body)
    build_view_bundle(projection, roots=roots, repositories=repositories)
    svg = next(_physical_file(file.path, roots) for file in bundle.files if file.path.endswith(".svg"))
    data = svg.read_bytes().replace(b'data-workflow-links="v1"', b'data-workflow-links="v2"')
    marker = re.search(rb'<!-- api-agent-view-v1 (\{[^\n]+\}) -->\n', data)
    metadata = json.loads(marker[1])
    graphic = data[:marker.start()] + data[marker.end():]
    metadata["graphic_sha256"] = sha256(graphic).hexdigest()
    svg.write_bytes(data[:marker.start()] + b'<!-- api-agent-view-v1 ' + json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode() + b' -->\n' + data[marker.end():])
    with pytest.raises(RenderError, match="navigation envelope"):
        build_view_bundle(projection, roots=roots, repositories=repositories)


@pytest.fixture
def empty_generation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from api_agent.workflows import viewer_build
    from api_agent.workflows.viewer_render import ViewBundle
    monkeypatch.setattr(viewer_build, "discover_view_entries", lambda root: (root / "flow.md.html",))
    monkeypatch.setattr(viewer_build, "_projection", lambda root: SimpleNamespace(documents=(), resources=()))
    monkeypatch.setattr(viewer_build, "build_view_bundle", lambda *args, **kwargs: ViewBundle("", ()))
    return viewer_build


def test_explicit_generation_initializes_recorded_config_without_staging(tmp_path, monkeypatch, empty_generation):
    monkeypatch.setattr(empty_generation.subprocess, "run", lambda *args, **kwargs: pytest.fail("explicit identity must not inspect origin"))
    paths = empty_generation.generate_views(tmp_path, renderer=object(), repository_url="//github.com/example/agent")
    assert paths == (tmp_path / "cfg/workflow-view.toml",)
    assert read_repository_identity(tmp_path)[0] == RepositoryIdentity("//github.com/example/agent")
    assert empty_generation.generate_views(tmp_path, renderer=object()) == ()


def test_explicit_generation_initializes_only_from_safe_origin(tmp_path, monkeypatch, empty_generation):
    from types import SimpleNamespace
    monkeypatch.setattr(empty_generation.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="git@github.com:example/agent.git\n"))
    empty_generation.generate_views(tmp_path, renderer=object())
    assert read_repository_identity(tmp_path)[0] == RepositoryIdentity("//github.com/example/agent")


def test_explicit_generation_missing_origin_requires_identity(tmp_path, monkeypatch, empty_generation):
    from types import SimpleNamespace
    monkeypatch.setattr(empty_generation.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    with pytest.raises(RenderError, match="supply --repository-url"):
        empty_generation.generate_views(tmp_path, renderer=object())
    assert not (tmp_path / "cfg/workflow-view.toml").exists()


def test_repository_mapping_rejects_unadmitted_owner(tmp_path):
    with pytest.raises(RenderError, match="unadmitted"):
        _navigation_script("base/flow.md.html", {"base": tmp_path}, {"other": RepositoryIdentity("//github.com/example/agent")})


def test_repository_mapping_rejects_overlapping_distinct_roots(tmp_path):
    with pytest.raises(RenderError, match="overlapping"):
        _navigation_script("base/flow.md.html", {"base": tmp_path, "pack": tmp_path / "pack"}, {"base": RepositoryIdentity("//github.com/example/agent"), "pack": RepositoryIdentity("//github.com/example/agent", "nested")})


def test_repository_mapping_allows_matching_nested_root_and_prefix(tmp_path):
    roots = {"base": tmp_path, "pack": tmp_path / "pack"}
    base = RepositoryIdentity("//github.com/example/agent")
    matching = RepositoryIdentity("//github.com/example/agent", "pack")
    script = _navigation_script("base/flow.md.html", roots, {"base": base, "pack": matching})
    assert base.link_prefix.encode() in script
    assert matching.link_prefix.encode() in script
    with pytest.raises(RenderError, match="overlapping"):
        _navigation_script("base/flow.md.html", roots, {"base": base,
                                                     "pack": RepositoryIdentity("//github.com/example/agent", "other")})
    hosted = _navigation_script("base/flow.md.html", {"base": Path("/r/base"), "pack": Path("/r/pack")},
                                {"base": base, "pack": matching}, admission_roots=roots)
    assert matching.link_prefix.encode() in hosted
    assert b"../../../pack/" in hosted
    with pytest.raises(RenderError, match="unadmitted"):
        _navigation_script("base/flow.md.html", roots, {"base": base}, admission_roots={})


def test_repository_mapping_checks_nested_prefixes_in_reverse_owner_order(tmp_path):
    roots = {"a": tmp_path / "pack", "z": tmp_path}
    base = RepositoryIdentity("//github.com/example/agent")
    matching = RepositoryIdentity("//github.com/example/agent", "pack")
    script = _navigation_script("z/flow.md.html", roots, {"a": matching, "z": base})
    assert matching.link_prefix.encode() in script
    assert base.link_prefix.encode() in script
    with pytest.raises(RenderError, match="overlapping"):
        _navigation_script("z/flow.md.html", roots, {"a": RepositoryIdentity("//github.com/example/agent", "other"),
                                                   "z": base})


def test_repository_mapping_rejects_nested_url_for_non_nested_physical_root(tmp_path):
    with pytest.raises(RenderError, match="overlapping"):
        _navigation_script("base/flow.md.html", {"base": tmp_path / "base", "pack": tmp_path / "pack"},
                           {"base": RepositoryIdentity("//github.com/example/agent"),
                            "pack": RepositoryIdentity("//github.com/example/agent", "pack")})


def test_projection_rejects_missing_owner_identity():
    from api_agent.workflows.viewer_build import _projection
    from api_agent.workflows.viewer_render import _projection_source
    projection = _projection(Path(__file__).resolve().parents[2])
    with pytest.raises(RenderError, match="missing workflow repository"):
        _projection_source(projection, projection.documents[0], repositories={"pack": RepositoryIdentity("//github.com/example/agent")})


def test_reader_detects_inode_size_change(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from api_agent.workflows import viewer_repository
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg/workflow-view.toml").write_bytes(repository_config_bytes(RepositoryIdentity("//github.com/example/agent")))
    original = viewer_repository.os.fstat
    def changed_size(descriptor):
        info = original(descriptor)
        return SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size + 1)
    monkeypatch.setattr(viewer_repository, 'os', SimpleNamespace(**(vars(viewer_repository.os) | {'fstat': changed_size})))
    with pytest.raises(ValueError, match="changed during read"):
        read_repository_identity(tmp_path)


def test_generation_rejects_conflicting_explicit_identity(tmp_path, empty_generation):
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg/workflow-view.toml").write_bytes(repository_config_bytes(RepositoryIdentity("//github.com/example/agent")))
    with pytest.raises(RenderError, match="differs from recorded"):
        empty_generation.generate_views(tmp_path, renderer=object(), repository_url="//github.com/example/other")


def test_generation_detects_recorded_config_change_during_capture(tmp_path, monkeypatch, empty_generation):
    (tmp_path / "cfg").mkdir()
    path = tmp_path / "cfg/workflow-view.toml"
    path.write_bytes(repository_config_bytes(RepositoryIdentity("//github.com/example/agent")))
    original = empty_generation._repository_inputs
    def changing_capture(root):
        result = original(root)
        path.write_bytes(repository_config_bytes(RepositoryIdentity("//github.com/example/other")))
        return result
    monkeypatch.setattr(empty_generation, "_repository_inputs", changing_capture)
    with pytest.raises(RenderError, match="changed during admission"):
        empty_generation.generate_views(tmp_path, renderer=object())


def test_new_recorded_config_rolls_back_with_later_output_failure(tmp_path, monkeypatch, empty_generation):
    from api_agent.workflows.viewer_render import ViewBundle, ViewFile
    monkeypatch.setattr(empty_generation, "build_view_bundle", lambda *args, **kwargs: ViewBundle("", (ViewFile("r/base/.private_resources/diagrams/a.svg", "image/svg+xml", b"new"),)))
    original = empty_generation._publish_link
    def fail_diagram(source, target):
        if str(target).endswith("a.svg"):
            raise OSError("controlled publication failure")
        return original(source, target)
    monkeypatch.setattr(empty_generation, "_publish_link", fail_diagram)
    with pytest.raises(OSError, match="controlled publication"):
        empty_generation.generate_views(tmp_path, renderer=object(), repository_url="//github.com/example/agent")
    assert not (tmp_path / "cfg/workflow-view.toml").exists()
    assert not (tmp_path / ".private_resources/diagrams/a.svg").exists()


@pytest.mark.parametrize("parent", [False, True])
def test_generation_output_snapshot_rejects_configuration_symlinks(tmp_path, parent):
    from api_agent.workflows.viewer_build import _output_snapshot
    directory = tmp_path / "cfg"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "workflow-view.toml").write_bytes(b"external")
    if parent:
        directory.symlink_to(outside, target_is_directory=True)
    else:
        directory.mkdir()
        (directory / "workflow-view.toml").symlink_to(outside / "workflow-view.toml")
    with pytest.raises(RenderError, match="symlink"):
        _output_snapshot(tmp_path)


def test_nested_repository_prefixes_cannot_alias_the_same_local_root(tmp_path):
    with pytest.raises(RenderError, match="overlapping"):
        _navigation_script("base/flow.md.html", {"base": tmp_path, "pack": tmp_path}, {
            "base": RepositoryIdentity("//github.com/example/agent"),
            "pack": RepositoryIdentity("//github.com/example/agent", "nested")})


def test_alias_capture_reads_one_immutable_repository_snapshot(tmp_path, monkeypatch):
    from api_agent.workflows import viewer_render
    (tmp_path / "cfg").mkdir()
    path = tmp_path / "cfg/workflow-view.toml"
    original_identity = RepositoryIdentity("//github.com/example/original")
    changed_identity = RepositoryIdentity("//github.com/example/changed")
    path.write_bytes(repository_config_bytes(original_identity))
    read = viewer_render.read_repository_identity
    calls = []
    def change_after_read(root):
        result = read(root)
        calls.append(root)
        path.write_bytes(repository_config_bytes(changed_identity))
        return result
    monkeypatch.setattr(viewer_render, "read_repository_identity", change_after_read)
    identities, sources = viewer_render.capture_repository_sources({"base": tmp_path, "pack": tmp_path})
    assert calls == [tmp_path]
    assert identities["base"] == identities["pack"] == original_identity
    assert len(sources) == 1
    with pytest.raises(TypeError):
        identities["base"] = changed_identity
