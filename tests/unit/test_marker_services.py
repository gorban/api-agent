"""Markers a pack declares under tests.prerequisites select services held for a unit run."""
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from api_agent.runtime.resource_pack import ResourcePackError, load_launch_context
from api_agent.testing.collection import CollectionError, collect_tests, repository_label
from api_agent.testing.marker_services import acquired_test_services
from api_agent.testing.runner import run_unit_tests
from test_resource_pack import make_pack

PROVIDER = '''
from contextlib import contextmanager
from pathlib import Path

@contextmanager
def acquire(services):
    Path(services["owner_root"], "events.log").open("a").write("enter\\n")
    try:
        yield {"SERVICE_URL": "http://127.0.0.1:1/" + services["environment"].get("SUFFIX", "")}
    finally:
        Path(services["owner_root"], "events.log").open("a").write("exit\\n")
'''


def owner_with_marker(root, provider=PROVIDER, *, marked=True):
    (root / 'src').mkdir(parents=True)
    (root / 'src/tiny.py').write_text('VALUE = 1\n')
    (root / 'tests/unit').mkdir(parents=True)
    (root / 'tests/services').mkdir(parents=True)
    (root / 'tests/services/provider.py').write_text(provider)
    (root / 'pytest.ini').write_text('[pytest]\nmarkers =\n    needs_service: needs a service\n')
    (root / 'tests/unit/test_tiny.py').write_text(
        'import os\nimport pytest\nfrom tiny import VALUE\n'
        'def test_plain():\n    assert VALUE == 1\n    assert "SERVICE_URL" not in os.environ\n'
        + ('@pytest.mark.needs_service\n' if marked else '')
        + 'def test_with_service():\n    assert os.environ["SERVICE_URL"] == "http://127.0.0.1:1/x"\n')
    return root


def context(base, owner, **layer):
    values = {'unit_exclude_markers': ('needs_service',),
              'test_prerequisites': (('needs_service', owner / 'tests/services/provider.py', 'acquire'),), **layer}
    return SimpleNamespace(base_root=base, owner_root=owner, **values,
                           suite_paths=lambda mode: (owner / 'tests/unit',))


@pytest.fixture
def pair(tmp_path):
    base = tmp_path / 'base'
    (base / 'tests/unit').mkdir(parents=True)
    (base / 'tests/unit/test_base.py').write_text('def test_base():\n    pass\n')
    (base / 'pytest.ini').write_text('[pytest]\n')
    return base, owner_with_marker(tmp_path / 'pack')


def events(owner):
    path = owner / 'events.log'
    return path.read_text().split() if path.exists() else []


def test_declared_marker_is_excluded_until_a_tag_selector_or_the_runner_names_it(pair):
    base, owner = pair
    ctx = context(base, owner)

    assert [test.node_id for test in collect_tests(ctx, 'unit') if test.repo != 'base'] == [
        'tests/unit/test_tiny.py::test_plain']
    for selector in ('tag:needs_service', 'pack::tag:needs_service'):
        assert [test.node_id for test in collect_tests(ctx, 'unit', [selector])] == [
            'tests/unit/test_tiny.py::test_with_service']
    assert len(collect_tests(ctx, 'unit', include_marked=True)) == 2
    assert [test.node_id for test in collect_tests(ctx, 'unit', ['tests/unit/test_tiny.py'])] == [
        'tests/unit/test_tiny.py::test_plain']


def test_plain_exclusions_are_not_released_by_their_tag(pair):
    base, owner = pair
    ctx = context(base, owner, test_prerequisites=())
    with pytest.raises(CollectionError):
        collect_tests(ctx, 'unit', ['tag:needs_service'])


def test_selected_marker_holds_its_service_for_the_run_and_exports_its_environment(pair):
    base, owner = pair
    ctx = context(base, owner)
    tests = collect_tests(ctx, 'unit', ['tag:needs_service'])

    result = run_unit_tests(ctx, tests, test_environment={'SUFFIX': 'x', 'UNUSED': None}, require_full_coverage=False)

    assert result.exit_code == 0, result.output
    assert [test.status for test in result.tests] == ['passed']
    assert events(owner) == ['enter', 'exit']


def test_unmarked_selection_never_loads_the_provider(pair):
    base, owner = pair
    (owner / 'tests/services/provider.py').write_text('raise RuntimeError("provider imported")\n')
    ctx = context(base, owner)

    result = run_unit_tests(ctx, collect_tests(ctx, 'unit', ['tests/unit/test_tiny.py']),
                            require_full_coverage=False)

    assert result.exit_code == 0 and [test.status for test in result.tests] == ['passed']


def test_service_stops_when_the_run_fails(pair):
    base, owner = pair
    ctx = context(base, owner)
    tests = collect_tests(ctx, 'unit', ['tag:needs_service'])

    result = run_unit_tests(ctx, tests, test_environment={'SUFFIX': 'wrong'}, require_full_coverage=False)

    assert result.exit_code == 1
    assert events(owner) == ['enter', 'exit']


def identity(repo, *tags):
    return SimpleNamespace(repo=repo, tags=frozenset(tags))


def provider(tmp_path, name, body):
    path = tmp_path / f'{name}.py'
    path.write_text('from contextlib import contextmanager\n@contextmanager\ndef acquire(services):\n' + body)
    return path


def test_each_selected_marker_starts_once_in_order_and_stops_in_reverse(tmp_path):
    log = tmp_path / 'log'
    body = ('    open(%r, "a").write("start-%s\\n")\n    try:\n        yield {"%s": "1"}\n'
            '    finally:\n        open(%r, "a").write("stop-%s\\n")\n')
    first = provider(tmp_path, 'first', body % (str(log), 'a', 'A', str(log), 'a'))
    second = provider(tmp_path, 'second', body % (str(log), 'b', 'B', str(log), 'b'))
    ctx = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, test_prerequisites=(
        ('one', first, 'acquire'), ('two', second, 'acquire'), ('three', first, 'acquire')))
    label = repository_label(ctx, tmp_path)

    with acquired_test_services(ctx, [identity(label, 'one'), identity(label, 'two', 'one')], {}) as values:
        assert values == {'A': '1', 'B': '1'}
        assert log.read_text().split() == ['start-a', 'start-b']
    assert log.read_text().split() == ['start-a', 'start-b', 'stop-b', 'stop-a']


def test_providers_receive_owner_environment_scratch_and_output(tmp_path):
    seen = tmp_path / 'seen'
    path = provider(tmp_path, 'seen', (
        '    from pathlib import Path\n'
        '    services["output"]("hello")\n'
        '    Path(%r).write_text(repr((str(services["owner_root"]), services["environment"]["K"],'
        ' Path(services["directory"]).is_dir())))\n    yield {}\n') % str(seen))
    ctx = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, test_prerequisites=(('m', path, 'acquire'),))
    label = repository_label(ctx, tmp_path)
    lines = []

    with acquired_test_services(ctx, [identity(label, 'm')], {'K': 'v'}, output=lines.append):
        pass

    assert lines == ['hello']
    assert seen.read_text() == repr((str(tmp_path.resolve()), 'v', True))


def test_provider_output_is_dropped_when_the_caller_shows_none(tmp_path):
    path = provider(tmp_path, 'quiet', '    services["output"]("hello")\n    yield {"A": "1"}\n')
    ctx = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, test_prerequisites=(('m', path, 'acquire'),))
    label = repository_label(ctx, tmp_path)

    with acquired_test_services(ctx, [identity(label, 'm')], {}) as values:
        assert values == {'A': '1'}


def test_tests_of_another_owner_do_not_select_a_providers_service(tmp_path):
    path = provider(tmp_path, 'never', '    raise RuntimeError("started")\n    yield {}\n')
    ctx = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, test_prerequisites=(('m', path, 'acquire'),))

    with acquired_test_services(ctx, [identity('someone-else', 'm')], {}) as values:
        assert values == {}


@pytest.mark.parametrize('value', ['None', '{1: "x"}', '{"bad-name": "x"}', '{"A": 1}', '{"A": "x\\ny"}'])
def test_invalid_service_environment_is_rejected_and_the_service_stops(tmp_path, value):
    stopped = tmp_path / 'stopped'
    path = provider(tmp_path, 'bad', f'    try:\n        yield {value}\n    finally:\n        open({str(stopped)!r}, "w").write("x")\n')
    ctx = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, test_prerequisites=(('m', path, 'acquire'),))
    label = repository_label(ctx, tmp_path)

    with pytest.raises(ValueError, match='Test services for m must return'):
        with acquired_test_services(ctx, [identity(label, 'm')], {}):
            pass
    assert stopped.exists()


def test_two_services_cannot_redefine_the_same_variable(tmp_path):
    first = provider(tmp_path, 'first', '    yield {"A": "1"}\n')
    second = provider(tmp_path, 'second', '    yield {"A": "2"}\n')
    ctx = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path,
                          test_prerequisites=(('one', first, 'acquire'), ('two', second, 'acquire')))
    label = repository_label(ctx, tmp_path)

    with pytest.raises(ValueError, match='Test services for two redefine'):
        with acquired_test_services(ctx, [identity(label, 'one', 'two')], {}):
            pass


def test_manifest_declares_provider_for_an_excluded_marker(tmp_path):
    base, pack = tmp_path / 'base', tmp_path / 'external'
    base.mkdir()
    make_pack(pack)
    (pack / 'services').mkdir()
    (pack / 'services/db.py').write_text('')
    manifest = pack / 'pack.toml'
    manifest.write_text(manifest.read_text().replace(
        '[tests]\n', '[tests]\nunit_exclude_markers = ["needs_db"]\nprerequisites = {needs_db = "services/db.py:acquire"}\n'))

    assert load_launch_context(base, pack).test_prerequisites == (
        ('needs_db', pack / 'services/db.py', 'acquire'),)
    make_pack(pack)
    assert load_launch_context(base, pack).test_prerequisites == ()


@pytest.mark.parametrize('declaration', [
    'prerequisites = 7', 'prerequisites = {needs_db = 7}', 'prerequisites = {needs_db = "services/db.py"}',
    'prerequisites = {needs_db = "services/db.py:bad-name"}', 'prerequisites = {needs_db = "services/db.txt:acquire"}',
    'prerequisites = {needs_db = "/etc/db.py:acquire"}', 'prerequisites = {needs_db = "../db.py:acquire"}',
    'prerequisites = {needs_db = "services/missing.py:acquire"}', 'prerequisites = {needs_db = "services/link.py:acquire"}',
    'prerequisites = {other = "services/db.py:acquire"}',
])
def test_invalid_marker_providers_are_rejected(tmp_path, declaration):
    base, pack = tmp_path / 'base', tmp_path / 'external'
    base.mkdir()
    make_pack(pack)
    (pack / 'services').mkdir()
    (pack / 'services/db.py').write_text('')
    (pack / 'services/link.py').symlink_to(pack / 'services/db.py')
    manifest = pack / 'pack.toml'
    manifest.write_text(manifest.read_text().replace(
        '[tests]\n', '[tests]\nunit_exclude_markers = ["needs_db"]\n' + declaration + '\n'))

    with pytest.raises(ResourcePackError, match='prerequisites'):
        load_launch_context(base, pack)


WAIT_FOR_CANCEL = (
    '    import time\n    from pathlib import Path\n'
    '    try:\n'
    '        while not services["cancelled"]():\n            time.sleep(0.01)\n'
    '        raise RuntimeError("stopped waiting")\n'
    '    finally:\n        Path(%r).write_text("cleaned")\n    yield {}\n')


def waiting_context(tmp_path, body):
    path = provider(tmp_path, 'waiting', body % str(tmp_path / 'cleaned'))
    ctx = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, test_prerequisites=(('m', path, 'acquire'),))
    return ctx, [identity(repository_label(ctx, tmp_path), 'm')]


def test_providers_receive_the_startup_deadline_and_a_cancellation_check(tmp_path):
    seen = tmp_path / 'seen'
    path = provider(tmp_path, 'budget', (
        '    import time\n    from pathlib import Path\n'
        '    Path(%r).write_text(repr((services["deadline"] - time.monotonic(), services["cancelled"]())))\n'
        '    yield {}\n') % str(seen))
    ctx = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, test_prerequisites=(('m', path, 'acquire'),))

    with acquired_test_services(ctx, [identity(repository_label(ctx, tmp_path), 'm')], {}, timeout=30):
        pass

    remaining, cancelled = eval(seen.read_text())
    assert 20 < remaining <= 30 and cancelled is False


def test_startup_budget_defaults_to_the_declared_setting(tmp_path):
    seen = tmp_path / 'seen'
    path = provider(tmp_path, 'budget', (
        '    import time\n    from pathlib import Path\n'
        '    Path(%r).write_text(repr(services["deadline"] - time.monotonic()))\n    yield {}\n') % str(seen))
    ctx = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, test_prerequisites=(('m', path, 'acquire'),))

    with acquired_test_services(ctx, [identity(repository_label(ctx, tmp_path), 'm')], {}):
        pass

    assert 590 < float(seen.read_text()) <= 600


def test_a_service_that_outlives_the_startup_budget_is_stopped_and_reported(tmp_path):
    ctx, tests = waiting_context(tmp_path, WAIT_FOR_CANCEL)

    with pytest.raises(TimeoutError, match='Test services for m did not start within 0.2 seconds'):
        with acquired_test_services(ctx, tests, {}, timeout=0.2):
            pytest.fail('the service must not be reported ready')

    assert (tmp_path / 'cleaned').read_text() == 'cleaned'


def test_a_service_that_ignores_the_budget_is_stopped_once_it_returns(tmp_path):
    ctx, tests = waiting_context(tmp_path, (
        '    import time\n    from pathlib import Path\n'
        '    time.sleep(0.3)\n'
        '    try:\n        yield {}\n    finally:\n        Path(%r).write_text("cleaned")\n'))

    with pytest.raises(TimeoutError, match='did not start within 0.1 seconds'):
        with acquired_test_services(ctx, tests, {}, timeout=0.1):
            pytest.fail('the service must not be reported ready')

    assert (tmp_path / 'cleaned').read_text() == 'cleaned'


def test_cancelling_the_run_stops_startup_and_cleans_up(tmp_path):
    ctx, tests = waiting_context(tmp_path, WAIT_FOR_CANCEL)
    cancel = threading.Event()
    threading.Timer(0.1, cancel.set).start()

    with pytest.raises(InterruptedError, match='Test services for m were cancelled during startup'):
        with acquired_test_services(ctx, tests, {}, timeout=30, cancel_event=cancel):
            pytest.fail('the service must not be reported ready')

    assert (tmp_path / 'cleaned').read_text() == 'cleaned'


def test_a_failure_unrelated_to_the_budget_surfaces_unchanged(tmp_path):
    path = provider(tmp_path, 'broken', '    raise RuntimeError("broken")\n    yield {}\n')
    ctx = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path, test_prerequisites=(('m', path, 'acquire'),))

    with pytest.raises(RuntimeError, match='broken'):
        with acquired_test_services(ctx, [identity(repository_label(ctx, tmp_path), 'm')], {}, cancel_event=threading.Event()):
            pass


def test_the_run_passes_its_budget_and_cancellation_to_the_services(pair, monkeypatch):
    from api_agent.testing import marker_services
    base, owner = pair
    ctx = context(base, owner)
    tests = collect_tests(ctx, 'unit', ['tag:needs_service'])
    seen = {}
    real = marker_services.acquired_test_services
    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)
    monkeypatch.setattr(marker_services, 'acquired_test_services', spy)
    cancel = threading.Event()

    run_unit_tests(ctx, tests, test_environment={'SUFFIX': 'x'}, services_timeout=45.0, cancel_event=cancel,
                   require_full_coverage=False)

    assert seen['timeout'] == 45.0 and seen['cancel_event'] is cancel
