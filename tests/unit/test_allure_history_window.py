"""Configured history retains native identities and measured status proportions."""
import json
import asyncio
import threading
from types import SimpleNamespace

import pytest

from api_agent.testing import allure_history_window as window
from api_agent.testing.allure_source import ViewerError
from api_agent.testing.allure_viewer import ManagedAllureViewer
from test_allure_viewer import Browser, async_test, cli, port, run, save
from api_agent.testing.allure_source import RunSource


def entry(items):
    counts = {status: sum(row['status'] == status for row in items) for status in window.STATUSES}
    return {'statistic': dict(counts, total=len(items)), 'items': items}


def items(count):
    return [{'uid': str(n), 'status': 'failed' if n % 3 == 0 else 'passed',
             'reportUrl': f'http://127.0.0.1/run/{n}'} for n in range(count)]


@pytest.fixture
def expanded(tmp_path):
    (tmp_path / 'history').mkdir()
    (tmp_path / 'widgets').mkdir()
    previous = items(24)
    current = {'uid': 'current', 'status': 'passed'}
    complete = entry([current, *previous])
    native = dict(complete, items=complete['items'][:20])
    (tmp_path / 'history/history.json').write_text(json.dumps({'test': native, 'new': entry([current])}))
    prior = {'history.json': json.dumps({'test': entry(previous)}).encode()}
    return tmp_path, prior, complete


def test_restores_truncated_native_test_history_without_changing_statistics(expanded):
    root, prior, complete = expanded
    window.extend_history(root, prior, 25)
    result = json.loads((root / 'history/history.json').read_bytes())
    assert result['test'] == complete
    assert result['new'] == entry([{'uid': 'current', 'status': 'passed'}])


@pytest.mark.parametrize('count', [1, 3, 25])
def test_prior_history_statistics_follow_the_retained_observations(count):
    original = {'history.json': json.dumps({'test': entry(items(40))}).encode(),
                'history-trend.json': json.dumps(list(range(40))).encode()}
    result = window.prepare_history(original, count)
    assert json.loads(result['history.json']) == {'test': entry(items(40)[:count - 1])}
    assert json.loads(result['history-trend.json']) == list(range(count - 1))
    assert len(json.loads(original['history.json'])['test']['items']) == 40


def test_sparse_test_history_is_not_duplicated_or_lost(tmp_path):
    (tmp_path / 'history').mkdir()
    value = {'sparse': entry(items(24))}
    prior = {'history.json': json.dumps(value).encode()}
    (tmp_path / 'history/history.json').write_bytes(prior['history.json'])
    window.extend_history(tmp_path, prior, 25)
    assert json.loads((tmp_path / 'history/history.json').read_bytes()) == value


@pytest.mark.parametrize('body', [[], {'x': None}, {'x': {'items': None}},
    {'x': {'items': [None]}}, {'x': {'items': [{'status': 'other'}]}},
    {'x': {'items': [], 'statistic': None}},
    {'x': {'items': [], 'statistic': dict(entry([])['statistic'], passed=True)}},
    {'x': {'items': [], 'statistic': dict(entry([])['statistic'], passed=-1)}},
    {'x': {'items': [], 'statistic': dict(entry([])['statistic'], total=1)}}])
def test_malformed_test_history_cannot_produce_invented_statistics(body):
    with pytest.raises(ViewerError, match='history'):
        window.prepare_history({'history.json': json.dumps(body).encode()}, 25)


def test_malformed_trend_input_is_rejected():
    with pytest.raises(ViewerError, match='Trend'):
        window.prepare_history({'history-trend.json': b'{}'}, 25)


@pytest.mark.parametrize('limit', [1, 60])
def test_history_input_and_expansion_remain_byte_bounded(monkeypatch, limit):
    monkeypatch.setattr(window, 'MAX_ARTIFACT', limit)
    with pytest.raises(ViewerError, match='size limit'):
        window.prepare_history({'history.json': b'{"a":{"items":[],"statistic":{"failed":0,"broken":0,"skipped":0,"passed":0,"unknown":0,"total":0}}}'}, 25)


def test_encoded_expanded_history_remains_byte_bounded(expanded, monkeypatch):
    root, prior, complete = expanded
    size = max(len(prior['history.json']), (root / 'history/history.json').stat().st_size)
    monkeypatch.setattr(window, 'MAX_ARTIFACT', size)
    with pytest.raises(ViewerError, match='size limit'):
        window.extend_history(root, prior, 25)


def test_symlinked_generated_history_directory_is_not_written(expanded, tmp_path):
    root, prior, complete = expanded
    (root / 'history').rename(root / 'outside')
    (root / 'history').symlink_to(root / 'outside', target_is_directory=True)
    original = (root / 'outside/history.json').read_bytes()
    with pytest.raises(ViewerError, match='Symlinked'):
        window.extend_history(root, prior, 25)
    assert (root / 'outside/history.json').read_bytes() == original


@pytest.mark.parametrize('problem', ['not_list', 'empty', 'overlap', 'widget'])
def test_inconsistent_generated_trends_are_not_published(tmp_path, problem):
    (tmp_path / 'history').mkdir()
    (tmp_path / 'widgets').mkdir()
    old = {'data': {'passed': 1}}
    current = {'data': {'failed': 1}}
    rows = [current, old]
    prior = {'history-trend.json': json.dumps([old]).encode()}
    if problem == 'not_list':
        prior['history-trend.json'] = b'{}'
    elif problem == 'empty':
        rows = []
    elif problem == 'overlap':
        rows = [current, current]
    path = tmp_path / 'history/history-trend.json'
    path.write_text(json.dumps(rows))
    (tmp_path / 'widgets/history-trend.json').write_text(json.dumps([old]))
    with pytest.raises(ViewerError, match='Conflicting'):
        window.extend_history(tmp_path, prior, 25)


def test_extended_native_trends_do_not_require_a_widget(tmp_path):
    (tmp_path / 'history').mkdir()
    path = tmp_path / 'history/history-trend.json'
    path.write_text('[{"data":{"passed":1}}]')
    window.extend_history(tmp_path, {}, 25)
    assert json.loads(path.read_bytes()) == [{'data': {'passed': 1}}]


@pytest.mark.parametrize('change', ['overlap', 'negative', 'missing_current'])
def test_native_history_conflicts_are_rejected(expanded, change):
    root, prior, complete = expanded
    path = root / 'history/history.json'
    value = json.loads(path.read_bytes())
    if change == 'overlap':
        value['test']['items'][1]['uid'] = 'wrong'
    elif change == 'negative':
        value['test']['statistic'] = entry([])['statistic']
    else:
        value['new']['statistic'] = entry(items(21))['statistic']
    path.write_text(json.dumps(value))
    with pytest.raises(ViewerError, match='history'):
        window.extend_history(root, prior, 25)


@pytest.mark.parametrize('count', [0, -1, True, 1.5, '25'])
def test_invalid_history_counts_fail_before_browser_or_server_setup(count):
    with pytest.raises(ValueError, match='positive integer'):
        ManagedAllureViewer(history_count=count)


@pytest.mark.parametrize('mode', ['integration', 'e2e', 'allure'])
def test_display_count_is_review_visible_and_persists_normally(tmp_path, mode):
    from api_agent.config import load_shared_schema, read_dotenv
    from api_agent.config.session import ConfigSession
    context = SimpleNamespace(base_root=tmp_path, pack_root=tmp_path,
        dotenv_path=tmp_path / '.env', external_pack=False,
        anchors={'base_root': tmp_path, 'pack_root': tmp_path, 'owner_root': tmp_path})
    session = ConfigSession(context, load_shared_schema(), environ={'TUI_MODE': mode})
    assert {row.name: row.value for row in session.default_candidates()}['ALLURE_HISTORY_DISPLAY_COUNT'] == 20
    session.save_defaults(['ALLURE_HISTORY_DISPLAY_COUNT'])
    assert read_dotenv(context.dotenv_path)['ALLURE_HISTORY_DISPLAY_COUNT'] == '20'
    session.set_value('ALLURE_HISTORY_DISPLAY_COUNT', '25', persist=True)
    assert session.result().values['ALLURE_HISTORY_DISPLAY_COUNT'] == 25


@pytest.mark.parametrize('value', ['0', '-1', '1.5', 'true'])
def test_invalid_configured_count_is_rejected_before_execution(value):
    from api_agent.config import load_shared_schema, resolve_config
    result = resolve_config(load_shared_schema(), environ={
        'TUI_MODE': 'allure', 'ALLURE_HISTORY_DISPLAY_COUNT': value})
    assert any('ALLURE_HISTORY_DISPLAY_COUNT' in error for error in result.errors)


@pytest.mark.parametrize('stage', ['prepare_history', 'extend_history'])
@async_test
async def test_cancellation_drains_history_processing_before_owned_staging_disposal(tmp_path, run, cli, port, monkeypatch, stage):
    manifest, files, _ = run
    save(tmp_path, manifest, files)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    def blocked(*args):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), 'History processing was not released by the fixture'
        return {}
    monkeypatch.setattr(window, stage, blocked)
    owner = ManagedAllureViewer(port, browser_factory=Browser, history_count=25)
    operation = asyncio.create_task(owner.show_history(RunSource('sample', tmp_path)))
    cleanup = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        operation.cancel()
        cleanup = asyncio.create_task(owner.aclose())
        await asyncio.sleep(0)
        assert not operation.done(), 'Cancellation returned before history processing drained'
        assert not cleanup.done() and owner._temporary is not None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        await cleanup
        assert owner._temporary is None and not owner._operations
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)
        if cleanup is not None:
            await cleanup
        await owner.aclose()
