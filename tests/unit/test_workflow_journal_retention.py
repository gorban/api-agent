import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

from api_agent.testing.collection import TestIdentity
from api_agent.testing.contracts import RunnerEvent, TestResult
from api_agent.testing.integration import IntegrationRunResult
from api_agent.testing.recorded import run_recorded_integration
from api_agent.workflows.journal import WorkflowJournal


def test_journal_crosses_runner_and_allure_with_manifest_hash(tmp_path):
    node = SimpleNamespace(id='Make', kind='node', parent=None)
    layout = SimpleNamespace(steps=[node], tables={'Edges': []})
    definition = SimpleNamespace(graph_id='Example', definition_id='a' * 64,
        entry_root='base', layout=layout, resources=[], replacements=[])
    journal = WorkflowJournal(definition, secrets=('needle',))
    graph_scope, node_scope = str(uuid4()), str(uuid4())
    journal.observe(dict(event='scope_start', scope_id=graph_scope, parent_id=None,
                         kind='graph', owner='GRAPH', started_ms=1000, input='needle'))
    journal.observe(dict(event='edge', owner='GRAPH', source='START', target='Make', implicit=True))
    journal.observe(dict(event='scope_start', scope_id=node_scope, parent_id=graph_scope,
                         kind='node', owner='Make', started_ms=1001))
    journal.record_call(visit_id=node_scope, usage={'input_tokens': 3, 'output_tokens': 2})
    journal.observe(dict(event='node', scope_id=node_scope, node='Make'))
    journal.observe(dict(event='scope_end', scope_id=node_scope, parent_id=graph_scope,
                         kind='node', owner='Make', started_ms=1001,
                         stopped_ms=1002, outcome='passed'))
    journal.observe(dict(event='edge', owner='GRAPH', source='Make', target='END',
                             implicit=True, visit_id=node_scope))
    journal.observe(dict(event='scope_end', scope_id=graph_scope, parent_id=None,
                         kind='graph', owner='GRAPH', started_ms=1000,
                         stopped_ms=1003, outcome='passed'))
    payload = journal.finalize()
    assert payload['completeness'] == 'complete'
    test = TestIdentity('owner::test.py::test_run', 'owner', 'test.py', 'test.py::test_run')
    context = SimpleNamespace(base_root=tmp_path, owner_root=tmp_path)
    config = {'TUI_MODE': 'integration', 'ALLURE_S3_BUCKET': 'synthetic',
              'ALLURE_RESULTS_DIR': tmp_path / 'reports'}

    class Storage:
        def persist(self, directory, manifest):
            pass

    def runner(context, tests, **hooks):
        hooks['before_run']()
        hooks['on_event'](RunnerEvent('workflow_evidence', 'owner', test,
                                     text=json.dumps(payload)))
        result = TestResult(test, 'passed', 0.01)
        hooks['on_result'](result)
        outcome = IntegrationRunResult(0, (result,), '', False)
        hooks['after_run'](outcome)
        return outcome

    record = run_recorded_integration(context, (test,), config, accepted=True,
                                      storage=Storage(), runner=runner)
    assert record.exit_code == 0
    files = record.manifest['files']
    result = json.loads(next(record.directory.glob('*-result.json')).read_text())
    assert result['steps'][0]['name'] == 'graph GRAPH'
    attachment = result['attachments'][0]['source']
    retained = json.loads((record.directory / attachment).read_text())
    from api_agent.testing.payloads import hydrate_journal, PayloadResolver
    resolver = PayloadResolver.local(record.directory, record.manifest)
    resolver.index(next(row['source'] for row in result['attachments'] if row['name'] == 'Payload index'))
    assert hydrate_journal(retained, resolver) == payload and 'needle' not in json.dumps(retained)
    assert hashlib.sha256((record.directory / attachment).read_bytes()).hexdigest() == files[attachment]
