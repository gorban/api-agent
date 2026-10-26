"""Exact selected subsets retain run binding and immutable route identities."""
from copy import deepcopy
import hashlib
import json
from uuid import uuid4

import pytest

from api_agent.testing.allure_selection import selected_snapshot
from api_agent.testing.allure_source import PreviewSnapshot
from api_agent.testing.allure_viewer import ManagedAllureViewer
from test_allure_viewer import run, cli, history_cli, port, Browser, async_test, generated_root


@pytest.fixture
def attempts(run):
    manifest, files, name = run
    copies = []
    for number in range(3):
        row = json.loads(files[name])
        row.update(uuid=str(uuid4()), name=f'test_{number}', historyId=f'history-{number}')
        filename = row['uuid'] + '-result.json'
        artifacts = {filename: json.dumps(row).encode(), 'image.png': files['image.png']}
        binding = {key: value for key, value in manifest.items() if key not in {'finished_at', 'status'}}
        binding.update(preview_version=number + 1,
                       files={key: hashlib.sha256(body).hexdigest() for key, body in artifacts.items()})
        copies.append((f'test-{number}', PreviewSnapshot(artifacts, binding, {f'test-{number}': filename})))
    return copies


def test_live_selection_combines_exact_attempts_without_mutating_inputs(attempts):
    before = deepcopy(attempts)
    selected = selected_snapshot((attempts[0], attempts[2]))
    assert selected.result_files == {key: snapshot.result_files[key] for key, snapshot in (attempts[0], attempts[2])}
    assert len([name for name in selected.files if name.endswith('-result.json')]) == 2
    assert selected.manifest['preview_version'] == 3
    assert attempts == before


@pytest.mark.parametrize('change', ['run_id', 'repo', 'mode', 'started_at', 'schema', 'final', 'collision', 'digest', 'missing'])
def test_incoherent_or_damaged_live_selection_is_rejected(attempts, change):
    first, second = attempts[:2]
    snapshot = second[1]
    if change == 'final':
        snapshot = PreviewSnapshot(snapshot.files, snapshot.manifest, snapshot.result_files, False)
    elif change == 'collision':
        snapshot.files['image.png'] = b'conflicting image'
        snapshot.manifest['files']['image.png'] = hashlib.sha256(b'conflicting image').hexdigest()
    elif change == 'missing':
        snapshot.result_files.clear()
    elif change == 'digest':
        snapshot.files['image.png'] = b'changed image'
    else:
        snapshot.manifest[change] = {'run_id': str(uuid4()), 'repo': 'other', 'mode': 'e2e',
                                     'started_at': '2026-01-02T00:00:00Z', 'schema': 2}[change]
    with pytest.raises(ValueError):
        selected_snapshot((first, (second[0], snapshot)))


@async_test
async def test_subset_routes_are_order_independent_distinct_and_preserve_single_deeplink(attempts, cli, port):
    snapshot = selected_snapshot(tuple(attempts))
    names = tuple(snapshot.result_files.values())
    owner = ManagedAllureViewer(port, browser_factory=Browser)
    try:
        subset = await owner.show_preview(snapshot, names[:2])
        reverse = await owner.show_preview(snapshot, tuple(reversed(names[:2])))
        all_tests = await owner.show_preview(snapshot, names)
        single = await owner.show_preview(snapshot, names[0])
        assert subset.url == reverse.url
        assert len({subset.url, all_tests.url, single.url}) == 3
        assert '#testresult/' in single.url and '#' not in subset.url
        assert len(list((generated_root(owner, subset) / 'data/test-cases').glob('*.json'))) == 2
        assert len(list((generated_root(owner, all_tests) / 'data/test-cases').glob('*.json'))) == 3
    finally:
        await owner.aclose()


@pytest.mark.parametrize('unavailable', [None, 'pending', 'running', 'missing', 'budget', 'other-run'])
@async_test
async def test_table_range_opens_every_completed_attempt_or_rejects_entire_selection(tmp_path, attempts, unavailable):
    from textual.widgets import DataTable
    from test_tui import fixture
    from test_allure_navigation import Viewer
    app = fixture(tmp_path)
    app.allure_viewer = Viewer()
    async with app.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        app.mode = 'integration'
        app.unit_rows = {key: ('PASSED', key, '1s') for key, _ in attempts}
        if unavailable in {'pending', 'running'}:
            app.unit_rows['test-1'] = (unavailable.upper(), 'test-1', '')
        app.show('test-results', 'Results', rows=list(app.unit_rows.items()))
        await pilot.pause()
        for key, snapshot in attempts:
            if key == 'test-1' and unavailable in {'missing', 'budget'}:
                continue
            if key == 'test-1' and unavailable == 'other-run':
                snapshot.manifest['run_id'] = str(uuid4())
            app.receive_allure_snapshot(snapshot, app._inspection_generation)
        if unavailable == 'budget':
            app._allure_budget_unavailable.add('test-1')
        table = app.screen.query_one(DataTable)
        table.action_extend_range(2)
        selected = app.selected_allure_result(app.screen)
        if unavailable is None:
            assert set(selected[1]) == {snapshot.result_files[key] for key, snapshot in attempts}
            await app.open_selected_allure()
            assert app.allure_viewer.calls == [selected]
        else:
            assert selected is None if unavailable in {'pending', 'running'} else selected == (None, None)
            await app.open_selected_allure()
            assert app.allure_viewer.calls == []


def test_shared_final_snapshot_preserves_authoritative_manifest(attempts):
    live = selected_snapshot(tuple(attempts))
    final = PreviewSnapshot(live.files, live.manifest, live.result_files, False)
    assert selected_snapshot(tuple((key, final) for key in live.result_files)) is final


@pytest.mark.parametrize('binding', ['duplicate-key', 'duplicate-file', 'non-result', 'missing-file'])
def test_selection_rejects_conflicting_identity_bindings(attempts, binding):
    first, second = attempts[:2]
    if binding == 'duplicate-key':
        selection = (first, first)
    elif binding == 'duplicate-file':
        snapshot = first[1]
        snapshot.result_files[second[0]] = snapshot.result_files[first[0]]
        selection = (first, (second[0], snapshot))
    else:
        second[1].result_files[second[0]] = 'image.png' if binding == 'non-result' else 'missing-result.json'
        selection = (first, second)
    with pytest.raises(ValueError, match='identity bindings conflict'):
        selected_snapshot(selection)


@pytest.mark.parametrize('selection', ['duplicate', 'empty', 'missing'])
@async_test
async def test_viewer_rejects_incomplete_or_duplicate_subset_before_render(attempts, cli, port, selection):
    from api_agent.testing.allure_source import ViewerError
    snapshot = selected_snapshot(tuple(attempts))
    name = next(iter(snapshot.result_files.values()))
    names = {'duplicate': (name, name), 'empty': (), 'missing': (name, 'missing-result.json')}[selection]
    owner = ManagedAllureViewer(port, browser_factory=Browser)
    try:
        with pytest.raises(ViewerError, match='exact selected Allure result is unavailable'):
            await owner.show_preview(snapshot, names)
        assert owner._bindings == {} and owner._browser is None
    finally:
        await owner.aclose()


@async_test
async def test_bindings_do_not_aggregate_and_opener_freezes_retained_selection(tmp_path, attempts, cli, port, monkeypatch):
    import threading
    from textual.widgets import DataTable
    from test_tui import fixture
    from api_agent.testing import allure_selection
    app = fixture(tmp_path)
    owner = ManagedAllureViewer(port, browser_factory=Browser)
    app.allure_viewer = owner
    original = allure_selection.selected_snapshot
    calls = []
    ui_thread = threading.get_ident()
    def aggregate(selection):
        calls.append((threading.get_ident(), selection))
        return original(selection)
    monkeypatch.setattr(allure_selection, 'selected_snapshot', aggregate)
    async def replace_after_click():
        app.clear_allure_snapshots()
    monkeypatch.setattr(app, 'prepare_allure_reporting', replace_after_click)
    monkeypatch.setattr(app, 'allure_source', lambda: (_ for _ in ()).throw(ValueError('No history')))
    async with app.run_test(size=(110, 35)) as pilot:
        await pilot.pause()
        app.mode = 'integration'
        app.unit_rows = {key: ('PASSED', key, '1s') for key, _ in attempts}
        app.show('test-results', 'Results', rows=list(app.unit_rows.items()))
        await pilot.pause()
        for _, snapshot in attempts:
            app.receive_allure_snapshot(snapshot, app._inspection_generation)
        app.screen.query_one(DataTable).action_extend_range(2)
        assert app.screen.check_action('view_allure', ()) is True
        app.screen.refresh_bindings()
        assert calls == []
        await app.open_selected_allure()
        assert len(calls) == 1 and calls[0][0] != ui_thread
        assert calls[0][1] == tuple(attempts)
        assert app.allure_snapshots == {}
        assert len(owner._bindings) == 1
    await owner.aclose()


@async_test
async def test_same_count_substitution_cannot_publish_selected_report(attempts, cli, port):
    from api_agent.testing.allure_source import ViewerError
    cli.write_text(cli.read_text() + "\n for case in cases.glob('*.json'):\n  row=json.loads(case.read_bytes()); row['labels']=[{'name':'api-agent.selected-result','value':'unselected'}]; case.write_text(json.dumps(row))\n")
    snapshot = selected_snapshot(tuple(attempts))
    owner = ManagedAllureViewer(port, browser_factory=Browser)
    try:
        with pytest.raises(ViewerError, match='did not preserve the exact selected attempt'):
            await owner.show_preview(snapshot, tuple(snapshot.result_files.values()))
        assert not owner._bindings and owner._browser is None
    finally:
        await owner.aclose()


def test_derived_marker_replaces_source_marker_and_preserves_history_identity():
    from api_agent.testing.allure_selection import marked_result
    row = {'uuid': 'attempt', 'historyId': 'stable', 'parameters': [{'name': 'attempt', 'value': '2'}],
           'labels': [{'name': 'api-agent.selected-result', 'value': 'spoof'}, {'name': 'suite', 'value': 'sample'}]}
    body = json.dumps(row).encode()
    marked, marker = marked_result('attempt-result.json', body)
    derived = json.loads(marked)
    assert derived['historyId'] == row['historyId'] and derived['parameters'] == row['parameters']
    assert derived['labels'] == [{'name': 'suite', 'value': 'sample'}, {'name': 'api-agent.selected-result', 'value': marker}]
    assert marker == hashlib.sha256(b'attempt-result.json\0' + body).hexdigest()
    assert 'spoof' in body.decode()


@pytest.mark.parametrize('labels', [[], [{'name': 'api-agent.selected-result', 'value': 'wrong'}],
    [{'name': 'api-agent.selected-result', 'value': 'expected'}] * 2, None, ['invalid']])
def test_generated_case_requires_one_exact_marker(tmp_path, labels):
    from api_agent.testing.allure_selection import verify_selected_cases
    from api_agent.testing.allure_source import ViewerError
    case = tmp_path / 'case.json'
    case.write_text(json.dumps({'labels': labels}))
    with pytest.raises(ViewerError, match='did not preserve'):
        verify_selected_cases([case], ['expected'])


def test_shared_snapshot_binding_remains_a_cheap_frozen_binding(attempts):
    from api_agent.testing.allure_selection import bind_selection
    combined = selected_snapshot(tuple(attempts))
    selection = tuple((key, combined) for key in combined.result_files)
    bound, names = bind_selection(selection)
    assert bound.selection == selection
    assert names == tuple(combined.result_files.values())


@pytest.mark.parametrize('labels', [None, ['invalid']])
def test_invalid_source_labels_cannot_be_marked(labels):
    from api_agent.testing.allure_selection import marked_result
    from api_agent.testing.allure_source import ViewerError
    with pytest.raises(ViewerError, match='exact selected Allure result is unavailable'):
        marked_result('one-result.json', json.dumps({'labels': labels}).encode())


@async_test
async def test_bound_conflicting_artifacts_fail_before_viewer_resources(attempts, cli, port):
    from api_agent.testing.allure_selection import bind_selection
    from api_agent.testing.allure_source import ViewerError
    first, second = attempts[:2]
    second[1].files['image.png'] = b'conflicting retained image'
    second[1].manifest['files']['image.png'] = hashlib.sha256(b'conflicting retained image').hexdigest()
    bound, names = bind_selection((first, second))
    owner = ManagedAllureViewer(port, browser_factory=Browser)
    try:
        with pytest.raises(ViewerError, match='The exact selected Allure result is unavailable'):
            await owner.show_preview(bound, names)
        assert owner._server is None and owner._temporary is None
        assert owner._bindings == {} and owner._browser is None
        assert not owner._operations
    finally:
        await owner.aclose()
