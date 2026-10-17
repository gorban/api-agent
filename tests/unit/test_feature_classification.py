"""Feature declarations survive real collection and immutable recording."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from api_agent.testing.classification import collected_features, validate_features
from api_agent.testing.collection import CollectionError, TestIdentity, collect_tests
from api_agent.testing.contracts import TestResult
from api_agent.testing.recording import RunIdentity, RunRecorder
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from test_collection import context


@pytest.mark.parametrize('values', [(), [], ['A', 'A', 'B']])
def test_identity_features_are_immutable_and_deduplicated(values):
    identity = TestIdentity('r::test', 'r', 'test.py', 'test.py::test', features=values)
    assert identity.features == tuple(sorted(set(values)))
    assert isinstance(identity.features, tuple)


@pytest.mark.parametrize('values', [None, 'feature', [None], [''], [' '], ['x' * 101], ['line\nbreak'], ['A'] * 17])
def test_invalid_feature_values_are_rejected(values):
    with pytest.raises(ValueError, match='Features require'):
        validate_features(values)


def test_feature_limits_and_ordinary_tags_are_preserved():
    values = [str(index) + 'x' * 98 for index in range(16)]
    identity = TestIdentity('r::test', 'r', 'test.py', 'test.py::test', {'requires_llm'}, features=values)
    assert len(identity.features) == 16
    assert identity.tags == frozenset({'requires_llm'})


@pytest.mark.parametrize('args,kwargs', [((), {}), (('A', 'B'), {}), (('A',), {'extra': 'B'})])
def test_marker_requires_one_positional_string(args, kwargs):
    item = SimpleNamespace(iter_markers=lambda **_: [SimpleNamespace(args=args, kwargs=kwargs)])
    with pytest.raises(ValueError, match='exactly one positional'):
        collected_features(item)


@pytest.mark.parametrize('names,expected', [([], ()), (['B', 'A', 'A'], ('A', 'B'))])
def test_collected_features_include_all_inherited_markers(names, expected):
    def iter_features(*, name):
        assert name == 'feature'
        return [SimpleNamespace(args=(value,), kwargs={}) for value in names]
    assert collected_features(SimpleNamespace(iter_markers=iter_features)) == expected


@pytest.mark.parametrize('mode', ['integration', 'e2e'])
def test_native_collection_keeps_inherited_parameterized_features_without_execution(tmp_path, mode):
    folder = tmp_path / 'tests' / mode
    folder.mkdir(parents=True)
    (folder / 'test_feature.py').write_text('''import pytest
pytestmark = pytest.mark.feature('Shared capability')
@pytest.mark.feature('Class capability')
class TestFeature:
    @pytest.mark.feature('Case capability')
    @pytest.mark.parametrize('value', [1, 2])
    def test_case(self, value):
        raise AssertionError('Collection must not execute tests')
''')

    tests = collect_tests(context(tmp_path, modes={mode: (folder,)}), mode)

    assert len(tests) == 2
    assert tests[0].id != tests[1].id
    assert all(test.features == ('Case capability', 'Class capability', 'Shared capability') for test in tests)
    assert all('parametrize' in test.tags for test in tests)


def test_native_collection_rejects_invalid_feature_declaration(tmp_path):
    folder = tmp_path / 'tests/integration'
    folder.mkdir(parents=True)
    (folder / 'test_feature.py').write_text("import pytest\npytestmark=pytest.mark.feature('')\ndef test_case(): pass\n")
    with pytest.raises(CollectionError, match='Features require'):
        collect_tests(context(tmp_path, modes={'integration': (folder,)}), 'integration')


@pytest.mark.parametrize('status', ['passed', 'failed', 'broken', 'skipped'])
def test_feature_labels_preserve_history_status_and_retry_identity(tmp_path, status):
    identity = TestIdentity('example::test.py::test_case', 'example', 'test.py', 'test.py::test_case',
                            features=('Chat', 'Discovery'))
    recorder = RunRecorder(tmp_path, RunIdentity.create('example', 'integration'))
    outcome = TestResult(identity, status, .1)
    first = json.loads((tmp_path / recorder.record_test(outcome)).read_bytes())
    second = json.loads((tmp_path / recorder.record_test(replace(outcome, identity=replace(identity, features=())), attempt=2)).read_bytes())

    assert first['labels'][:3] == second['labels']
    assert first['labels'][3:] == [{'name': 'feature', 'value': 'Chat'}, {'name': 'feature', 'value': 'Discovery'}]
    assert first['status'] == second['status'] == status
    assert first['historyId'] == second['historyId']
    assert first['uuid'] != second['uuid']
    assert second['parameters'] == [{'name': 'attempt', 'value': '2', 'excluded': True}]


def test_feature_label_uses_credential_boundary(tmp_path):
    boundary = EmissionBoundary(ContentPolicy(True), secrets=('SYNTHETIC-CREDENTIAL',))
    recorder = RunRecorder(tmp_path, RunIdentity.create('example', 'integration'), evidence_boundary=boundary)
    identity = TestIdentity('example::test.py::test_case', 'example', 'test.py', 'test.py::test_case',
                            features=('Capability SYNTHETIC-CREDENTIAL',))

    path = tmp_path / recorder.record_test(TestResult(identity, 'passed'))

    assert 'SYNTHETIC-CREDENTIAL' not in path.read_text()
    assert '[REDACTED]' in path.read_text()


@pytest.mark.parametrize('jobs', [0, 2])
def test_real_recorded_workers_preserve_feature_labels(tmp_path, jobs):
    from test_integration_runner import fixture_repo, context as runner_context
    from test_recorded_integration import Storage
    from api_agent.testing.recorded import run_recorded_integration
    base = fixture_repo(tmp_path / 'base', "import pytest\npytestmark=pytest.mark.feature('Native worker')\n"
                        "@pytest.mark.parametrize('value',[1,2])\ndef test_case(value): assert value > 0\n")
    ctx = runner_context(base)
    tests = collect_tests(ctx, 'integration')
    config = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic',
              'ALLURE_RESULTS_DIR': tmp_path / 'reports', 'TEST_PARALLEL_JOBS': jobs}

    result = run_recorded_integration(ctx, tests, config, accepted=True, storage=Storage())

    assert result.exit_code == 0
    assert all(outcome.identity.features == ('Native worker',) for outcome in result.execution.tests)
    results = [json.loads((result.directory / name).read_bytes())
               for name in result.manifest['files'] if name.endswith('-result.json')]
    assert len(results) == 2
    assert all({'name': 'feature', 'value': 'Native worker'} in row['labels'] for row in results)
