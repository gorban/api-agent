"""Synthetic races at final workflow publication and cleanup boundaries."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from api_agent.workflows import viewer_build as vb
from api_agent.workflows.viewer_render import ViewBundle, ViewFile

@pytest.fixture
def controlled(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "cfg").mkdir()
    (root / "cfg/workflow-view.toml").write_text(
        'version = 1\nrepository_url = "//github.com/example/agent"\n'
    )
    flow = root / "flow.md.html"
    flow.write_bytes(b"owned declaration")
    diagrams = root / ".private_resources/diagrams"
    diagrams.mkdir(parents=True)
    first = diagrams / "first.svg"
    first.write_bytes(b"old first")
    second = diagrams / "second.svg"
    second.write_bytes(b"old second")
    document = SimpleNamespace(
        logical_path="base/flow.md.html", raw_bytes=flow.read_bytes()
    )
    projection = SimpleNamespace(documents=(document,), resources=())
    bundle = ViewBundle(
        "r/base/flow.md.html",
        (
            ViewFile("r/base/.private_resources/diagrams/first.svg", "image/svg+xml", b"new first"),
            ViewFile("r/base/.private_resources/diagrams/second.svg", "image/svg+xml", b"new second"),
        ),
    )
    monkeypatch.setattr(vb, "_projection", lambda root: projection)
    monkeypatch.setattr(vb, "build_view_bundle", lambda *args, **kwargs: bundle)
    return root, diagrams, first, second


def test_moved_prior_verification_preserves_final_human_edit(controlled, monkeypatch):
    root, diagrams, first, second = controlled
    rename = vb.os.rename

    def race(source, target, **kwargs):
        if source == first.name:
            first.write_bytes(b'concurrent human edit')
        return rename(source, target, **kwargs)
    monkeypatch.setattr(vb.os, 'rename', race)
    with pytest.raises((OSError, ValueError)):
        vb.generate_views(root, renderer=object())
    assert first.read_bytes() == b'concurrent human edit'
    assert second.read_bytes() == b'old second'

def test_pinned_staging_parent_swap_preserves_external_files(controlled, monkeypatch):
    root, diagrams, first, second = controlled
    outside = root.parent / 'outside'
    outside.mkdir()
    (outside / 'first.svg').write_bytes(b'old first')
    (outside / 'second.svg').write_bytes(b'old second')
    open_ = vb.os.open
    swapped = False

    def race(target, flags, *args, **kwargs):
        nonlocal swapped
        if str(target).startswith('.workflow-view-') and flags & vb.os.O_CREAT and (not swapped):
            swapped = True
            diagrams.rename(diagrams.with_name('kept-diagrams'))
            diagrams.symlink_to(outside, target_is_directory=True)
        return open_(target, flags, *args, **kwargs)
    monkeypatch.setattr(vb.os, 'open', race)
    with pytest.raises((OSError, ValueError)):
        vb.generate_views(root, renderer=object())
    assert swapped
    assert (outside / 'first.svg').read_bytes() == b'old first'
    assert (outside / 'second.svg').read_bytes() == b'old second'
    assert (diagrams.with_name('kept-diagrams') / 'first.svg').read_bytes() == b'old first'

def test_exclusive_initialization_preserves_concurrent_config(tmp_path, monkeypatch):
    monkeypatch.setattr(vb, '_projection', lambda root: SimpleNamespace(documents=(), resources=()))
    monkeypatch.setattr(vb, 'build_view_bundle', lambda *a, **kw: ViewBundle('', ()))
    config = tmp_path / 'cfg/workflow-view.toml'
    publish = vb._publish_link

    def race(stage, path):
        if path == config:
            config.write_bytes(b'concurrent human configuration')
        return publish(stage, path)
    monkeypatch.setattr(vb, '_publish_link', race)
    with pytest.raises((OSError, ValueError)):
        vb.generate_views(tmp_path, renderer=object(), repository_url='//github.com/example/agent')
    assert config.read_bytes() == b'concurrent human configuration'

def test_rollback_final_swap_preserves_human_bytes(controlled, monkeypatch):
    root, diagrams, first, second = controlled
    publish = vb._publish_link

    def fail_second(stage, path):
        if path == second:
            raise OSError('controlled second publication failure')
        return publish(stage, path)
    monkeypatch.setattr(vb, '_publish_link', fail_second)
    rename = vb.os.rename
    calls = 0

    def race(source, target, **kwargs):
        nonlocal calls
        if source == first.name:
            calls += 1
            if calls == 2:
                first.write_bytes(b'rollback concurrent human edit')
        return rename(source, target, **kwargs)
    monkeypatch.setattr(vb.os, 'rename', race)
    with pytest.raises(ValueError, match='rollback conflict'):
        vb.generate_views(root, renderer=object())
    assert first.read_bytes() == b'rollback concurrent human edit'
    assert second.read_bytes() == b'old second'

def test_preexisting_temporary_collision_is_preserved(controlled, monkeypatch):
    root, diagrams, first, second = controlled
    monkeypatch.setattr(vb.secrets, 'token_hex', lambda count: 'collision')
    existing = diagrams / '.workflow-view-collision'
    existing.write_bytes(b'unrelated temporary file')
    recovery = root.parent / 'collision-recovery'
    recovery.mkdir()
    recovery_fd = vb.os.open(recovery, vb.os.O_RDONLY | vb.os.O_DIRECTORY)
    try:
        with pytest.raises((OSError, ValueError)):
            vb._stage_output(root, first, b'old first', b'new first', recovery_fd)
    finally:
        vb.os.close(recovery_fd)
    assert existing.read_bytes() == b'unrelated temporary file'

def test_final_cleanup_preserves_swapped_temporary_name(controlled, monkeypatch):
    root, diagrams, first, second = controlled
    publish = vb._publish_link
    unknown = []

    def race(stage, path):
        result = publish(stage, path)
        temporary = stage.path.parent / stage.temporary
        temporary.unlink()
        temporary.write_bytes(b'unrelated replacement of temporary')
        unknown.append(temporary)
        return result
    monkeypatch.setattr(vb, '_publish_link', race)
    try:
        vb.generate_views(root, renderer=object())
    except (OSError, ValueError):
        pass
    assert unknown
    assert all((path.read_bytes() == b'unrelated replacement of temporary' for path in unknown))

def test_final_recovery_cleanup_preserves_replaced_directory(controlled, monkeypatch):
    root, diagrams, first, second = controlled
    cleanup = vb._cleanup_recovery
    human = []

    def race(path, descriptor, stages):
        path.rename(path.with_name(path.name + '-owned'))
        path.mkdir()
        file = path / 'human.txt'
        file.write_bytes(b'unrelated recovery-directory replacement')
        human.append(file)
        return cleanup(path, descriptor, stages)
    monkeypatch.setattr(vb, '_cleanup_recovery', race)
    with pytest.raises(ValueError, match='cleanup incomplete; retained at'):
        vb.generate_views(root, renderer=object())
    assert human[0].read_bytes() == b'unrelated recovery-directory replacement'

@pytest.fixture
def anchored_stage(tmp_path):
    root = tmp_path / 'owner'
    root.mkdir()
    path = root / 'output.svg'
    path.write_bytes(b'prior')
    recovery = tmp_path / 'recovery'
    recovery.mkdir()
    descriptor = vb.os.open(recovery, vb.os.O_RDONLY | vb.os.O_DIRECTORY)
    vb._RECOVERY_SLOTS[descriptor] = {}
    stage = vb._stage_output(root, path, b'prior', b'new', descriptor)
    yield (root, stage, recovery, descriptor)
    vb.os.close(stage.parent)
    vb.os.close(descriptor)
    vb._RECOVERY_SLOTS.pop(descriptor, None)

def test_pinned_reader_rejects_changed_inode_size(anchored_stage, monkeypatch):
    root, stage, recovery, descriptor = anchored_stage
    original = vb.os.fstat

    def changed_size(fd):
        info = original(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_size=info.st_size + 1)
    monkeypatch.setattr(vb.os, 'fstat', changed_size)
    with pytest.raises(ValueError, match='changed during read'):
        vb._output_at(stage.parent, stage.path.name)

def test_parent_verification_rejects_replaced_directory(anchored_stage):
    root, stage, recovery, descriptor = anchored_stage
    root.rename(root.with_name('retained'))
    root.mkdir()
    with pytest.raises(ValueError, match='parent changed'):
        vb._same_output_parent(root, stage)

def test_staging_rejects_changed_target_before_temporary_creation(anchored_stage):
    root, stage, recovery, descriptor = anchored_stage
    with pytest.raises(ValueError, match='changed during generation'):
        vb._stage_output(root, stage.path, b'different', b'new', descriptor)

def test_staging_failure_removes_only_its_created_temporary(anchored_stage, monkeypatch):
    root, stage, recovery, descriptor = anchored_stage
    before = set(root.iterdir())
    monkeypatch.setattr(vb.os, 'fsync', lambda fd: (_ for _ in ()).throw(OSError('controlled flush failure')))
    with pytest.raises(OSError, match='controlled flush'):
        vb._stage_output(root, stage.path, b'prior', b'other', descriptor)
    assert set(root.iterdir()) == before

def test_recovery_slot_collision_allocates_a_distinct_exclusive_directory(anchored_stage, monkeypatch):
    root, stage, recovery, descriptor = anchored_stage
    monkeypatch.setattr(vb.secrets, 'token_hex', lambda count: 'same')
    assert vb._recovery_slot(descriptor) == 'same-0/file'
    assert vb._recovery_slot(descriptor) == 'same-1/file'

def test_recovery_cleanup_preserves_changed_backup(anchored_stage):
    root, stage, recovery, descriptor = anchored_stage
    vb._apply_output(root, stage, descriptor)
    (recovery / stage.backup).write_bytes(b'human backup edit')
    assert vb._cleanup_recovery(recovery, descriptor, [stage]) is False
    assert (recovery / stage.backup).read_bytes() == b'human backup edit'

def test_recovery_cleanup_preserves_unknown_entry(anchored_stage):
    root, stage, recovery, descriptor = anchored_stage
    (recovery / 'human.txt').write_bytes(b'human')
    assert vb._cleanup_recovery(recovery, descriptor, []) is False
    assert (recovery / 'human.txt').read_bytes() == b'human'

def test_recovery_cleanup_preserves_replaced_slot_directory(anchored_stage):
    root, stage, recovery, descriptor = anchored_stage
    slot = vb._recovery_slot(descriptor).split('/')[0]
    (recovery / slot).rename(recovery / 'retained')
    (recovery / slot).mkdir()
    retained = recovery / 'retained'
    retained.rmdir()
    assert vb._cleanup_recovery(recovery, descriptor, []) is False
    assert (recovery / slot).is_dir()

def test_recovery_cleanup_preserves_nonempty_owned_slot(anchored_stage):
    root, stage, recovery, descriptor = anchored_stage
    slot = vb._recovery_slot(descriptor).split('/')[0]
    human = recovery / slot / 'human.txt'
    human.write_bytes(b'human')
    assert vb._cleanup_recovery(recovery, descriptor, []) is False
    assert human.read_bytes() == b'human'

def test_recovery_cleanup_handles_missing_outer_name(anchored_stage):
    root, stage, recovery, descriptor = anchored_stage
    recovery.rename(recovery.with_name('retained'))
    assert vb._cleanup_recovery(recovery, descriptor, []) is False

def test_recovery_cleanup_rmdir_cannot_remove_a_new_nonempty_directory(anchored_stage, monkeypatch):
    root, stage, recovery, descriptor = anchored_stage
    original = vb.os.rmdir

    def add_before_remove(name, **kwargs):
        (recovery / 'human.txt').write_bytes(b'human')
        return original(name, **kwargs)
    monkeypatch.setattr(vb.os, 'rmdir', add_before_remove)
    assert vb._cleanup_recovery(recovery, descriptor, []) is False
    assert (recovery / 'human.txt').read_bytes() == b'human'

def test_missing_temporary_cleanup_is_harmless(anchored_stage):
    root, stage, recovery, descriptor = anchored_stage
    assert vb._remove_owned_temporary(stage.parent, 'missing', (0, 0), descriptor) is True

def test_unknown_temporary_restore_conflict_retains_both_files(anchored_stage, monkeypatch):
    root, stage, recovery, descriptor = anchored_stage
    temporary = root / stage.temporary
    temporary.unlink()
    temporary.write_bytes(b'unknown temporary')
    original = vb.os.link

    def appear_before_restore(source, target, **kwargs):
        temporary.write_bytes(b'concurrent name')
        return original(source, target, **kwargs)
    monkeypatch.setattr(vb.os, 'link', appear_before_restore)
    assert vb._remove_owned_temporary(stage.parent, stage.temporary, stage.temporary_identity, descriptor) is False
    assert temporary.read_bytes() == b'concurrent name'
    assert next(recovery.rglob('file')).read_bytes() == b'unknown temporary'

@pytest.mark.parametrize('missing', [False, True])
def test_publication_rejects_changed_staged_bytes(anchored_stage, missing):
    root, stage, recovery, descriptor = anchored_stage
    temporary = root / stage.temporary
    temporary.unlink()
    if not missing:
        temporary.write_bytes(b'changed')
    with pytest.raises(ValueError, match='staged output changed'):
        vb._apply_output(root, stage, descriptor)
    assert (recovery / stage.backup).read_bytes() == b'prior'

def test_publication_detects_edit_at_exclusive_link_boundary(anchored_stage, monkeypatch):
    root, stage, recovery, descriptor = anchored_stage
    original = vb._publish_link

    def edit_after_link(staged, path):
        original(staged, path)
        path.write_bytes(b'human edit')
    monkeypatch.setattr(vb, '_publish_link', edit_after_link)
    with pytest.raises(ValueError, match='changed during publication'):
        vb._apply_output(root, stage, descriptor)
    assert stage.path.read_bytes() == b'human edit'

def test_rollback_preserves_missing_published_name(anchored_stage):
    root, stage, recovery, descriptor = anchored_stage
    vb._apply_output(root, stage, descriptor)
    stage.path.unlink()
    assert vb._rollback_output(stage, descriptor) is False
    assert (recovery / stage.backup).read_bytes() == b'prior'

def test_rollback_restore_conflict_preserves_displaced_and_new_human_files(anchored_stage, monkeypatch):
    root, stage, recovery, descriptor = anchored_stage
    vb._apply_output(root, stage, descriptor)
    stage.path.write_bytes(b'human published edit')
    original = vb.os.link

    def appear_before_restore(source, target, **kwargs):
        stage.path.write_bytes(b'concurrent human file')
        return original(source, target, **kwargs)
    monkeypatch.setattr(vb.os, 'link', appear_before_restore)
    assert vb._rollback_output(stage, descriptor) is False
    assert stage.path.read_bytes() == b'concurrent human file'
    assert b'human published edit' in [p.read_bytes() for p in recovery.rglob('file')]


@pytest.fixture
def legacy_presentations(tmp_path):
    from pathlib import PurePosixPath
    from api_agent.workflows import document_format
    from api_agent.workflows.viewer_render import _navigation, _physical_file, _route, _svg_route

    source = Path(__file__).resolve().parents[2]
    admitted = vb._projection(source)
    roots = {"base": tmp_path, "pack": tmp_path}
    for resource in admitted.resources:
        path = _physical_file(_route(resource.logical_path), roots)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resource.content_bytes)
    for document in admitted.documents:
        depth = len(PurePosixPath(document.logical_path).parts) - 2
        assets = ("../" * depth + ".private_resources/viewer/").encode()
        fallback = (document_format._ROOT_FALLBACK_V1
                    if document.logical_path == admitted.entry_path else b"")
        saved = (
            document_format._HEAD_V1.replace(b"{assets}", assets)
            + fallback + document_format._CARRIER_V1 + document.executable_bytes
            + document_format._GENERATED_START + _navigation(admitted, document)
            + document_format._GENERATED_END
        )
        path = _physical_file(_route(document.logical_path), roots)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(saved)
        graphic = _physical_file(_svg_route(document.logical_path), roots)
        graphic.parent.mkdir(parents=True, exist_ok=True)
        graphic.write_bytes(b"prior diagram bytes")
    (tmp_path / "cfg").mkdir(exist_ok=True)
    (tmp_path / "cfg/workflow-view.toml").write_text(
        'version = 1\nrepository_url = "//github.com/example/agent"\n'
    )

    class Renderer:
        def render(self, source, *, allowed_links):
            return (
                b'<?xml version="1.0"?>\n'
                b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" '
                b'viewBox="0 0 1 1"><title>Updated diagram</title></svg>'
            )

    return admitted, roots, Renderer


def _owner_file_bytes(root):
    return {path.relative_to(root): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def test_generation_upgrades_root_and_nested_presentations_with_changed_graphics(legacy_presentations):
    from api_agent.workflows import document_format
    from api_agent.workflows.viewer_render import _physical_file, _route, _svg_route
    admitted, roots, renderer = legacy_presentations
    root = roots["base"]

    changed = vb.generate_views(root, renderer=renderer())
    vb.check_views(root)

    assert len(admitted.documents) > 1
    for document in admitted.documents:
        path = _physical_file(_route(document.logical_path), roots)
        assert path in changed
        assert b'content="v2"' in path.read_bytes()
        assert document_format.canonical_executable_bytes(path.read_bytes(), path=path) == document.executable_bytes
        graphic = _physical_file(_svg_route(document.logical_path), roots)
        assert graphic in changed
        assert b"Updated diagram" in graphic.read_bytes()


def test_failed_nested_upgrade_restores_all_original_files_and_cleans_owned_recovery(legacy_presentations, monkeypatch):
    from api_agent.workflows.viewer_render import _physical_file, _route
    admitted, roots, renderer = legacy_presentations
    root = roots["base"]
    original_files = _owner_file_bytes(root)
    recoveries = set(root.parent.glob(".workflow-view-recovery-*"))
    nested_document = next(document for document in admitted.documents
                           if document.logical_path != admitted.entry_path)
    nested_path = _physical_file(_route(nested_document.logical_path), roots)
    publish = vb._publish_link
    def fail_after_root(stage, path):
        if path == nested_path:
            raise OSError("controlled nested publication failure")
        return publish(stage, path)
    monkeypatch.setattr(vb, "_publish_link", fail_after_root)

    with pytest.raises(OSError, match="controlled nested publication"):
        vb.generate_views(root, renderer=renderer())

    assert _owner_file_bytes(root) == original_files
    assert set(root.parent.glob(".workflow-view-recovery-*")) == recoveries


def test_inventory_failure_after_root_upgrade_restores_files_and_cleans_recovery(legacy_presentations, monkeypatch):
    admitted, roots, renderer = legacy_presentations
    root = roots["base"]
    original_files = _owner_file_bytes(root)
    recoveries = set(root.parent.glob(".workflow-view-recovery-*"))
    verify = vb._assert_output_unchanged
    def reject_after_root_published(snapshot, owner, *, exclude=()):
        verify(snapshot, owner, exclude=exclude)
        if b'content="v2"' in (root / "flow.md.html").read_bytes():
            raise ValueError("controlled inventory failure after root publication")
    monkeypatch.setattr(vb, "_assert_output_unchanged", reject_after_root_published)

    with pytest.raises(ValueError, match="controlled inventory failure"):
        vb.generate_views(root, renderer=renderer())

    assert _owner_file_bytes(root) == original_files
    assert set(root.parent.glob(".workflow-view-recovery-*")) == recoveries


def test_successful_publication_disposes_all_owned_recovery(controlled):
    root, diagrams, first, second = controlled
    before = set(root.parent.glob(".workflow-view-recovery-*"))

    vb.generate_views(root, renderer=object())

    assert first.read_bytes() == b"new first"
    assert second.read_bytes() == b"new second"
    assert set(root.parent.glob(".workflow-view-recovery-*")) == before


def test_incomplete_cleanup_reports_recovery_after_closing_descriptor(controlled, monkeypatch):
    root, diagrams, first, second = controlled
    captured = []
    def retain_recovery(path, descriptor, stages):
        captured.append((path, descriptor))
        return False
    monkeypatch.setattr(vb, "_cleanup_recovery", retain_recovery)

    with pytest.raises(ValueError, match="cleanup incomplete; retained at") as error:
        vb.generate_views(root, renderer=object())

    path, descriptor = captured[0]
    assert str(path) in str(error.value)
    assert path.is_dir()
    assert descriptor not in vb._RECOVERY_SLOTS
    with pytest.raises(OSError):
        vb.os.fstat(descriptor)


def test_cleanup_diagnostic_retains_original_publication_error(controlled, monkeypatch):
    root, diagrams, first, second = controlled
    captured = []
    def fail_publication(stage, path):
        raise OSError("controlled publication failure")
    def retain_recovery(path, descriptor, stages):
        captured.append(descriptor)
        return False
    monkeypatch.setattr(vb, "_publish_link", fail_publication)
    monkeypatch.setattr(vb, "_cleanup_recovery", retain_recovery)

    with pytest.raises(ValueError, match="cleanup incomplete; retained at") as error:
        vb.generate_views(root, renderer=object())

    assert isinstance(error.value.__cause__, OSError)
    assert "controlled publication failure" in str(error.value.__cause__)
    with pytest.raises(OSError):
        vb.os.fstat(captured[0])


def test_recovery_cleanup_exception_reports_location_and_closes_descriptor(controlled, monkeypatch):
    root, diagrams, first, second = controlled
    captured = []
    def fail_cleanup(path, descriptor, stages):
        captured.append(descriptor)
        raise OSError("controlled recovery cleanup failure")
    monkeypatch.setattr(vb, "_cleanup_recovery", fail_cleanup)

    with pytest.raises(ValueError, match="cleanup incomplete; retained at") as error:
        vb.generate_views(root, renderer=object())

    assert "controlled recovery cleanup failure" in str(error.value.__cause__)
    with pytest.raises(OSError):
        vb.os.fstat(captured[0])


def test_temporary_cleanup_exception_closes_every_staged_parent(controlled, monkeypatch):
    root, diagrams, first, second = controlled
    parents = []
    original = vb._stage_output
    def capture_stage(*args):
        stage = original(*args)
        parents.append(stage.parent)
        return stage
    def fail_cleanup(*args):
        raise OSError("controlled temporary cleanup failure")
    monkeypatch.setattr(vb, "_stage_output", capture_stage)
    monkeypatch.setattr(vb, "_remove_owned_temporary", fail_cleanup)

    with pytest.raises(ValueError, match="cleanup incomplete; retained at"):
        vb.generate_views(root, renderer=object())

    for descriptor in parents:
        with pytest.raises(OSError):
            vb.os.fstat(descriptor)
    assert first.read_bytes() == b"old first"
    assert second.read_bytes() == b"old second"


def test_recovery_cleanup_refreshes_stale_directory_enumeration_without_reopening_its_path(anchored_stage, monkeypatch):
    root, stage, recovery, descriptor = anchored_stage
    vb._recovery_slot(descriptor)
    original = vb.os.listdir
    def stale_original_handle(directory):
        if directory == descriptor:
            return []
        return original(directory)
    monkeypatch.setattr(vb.os, "listdir", stale_original_handle)

    assert vb._cleanup_recovery(recovery, descriptor, []) is True
    assert not recovery.exists()
