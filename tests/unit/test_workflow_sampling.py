import asyncio
from copy import deepcopy
import pytest
from pathlib import Path

FACTORY_CONFIG = {'LLM_PROVIDER': str(Path(__file__).resolve().parents[2] / 'src/api_agent/llm_providers/gpt/factory.py') + ':factory'}
from api_agent.workflows.sampling import calibration, cover, lower_probability, validate_calibration, measured_calibration, DECLARED, FIRST, TARGETS, observed_targets, measurement

def structural_events(trial, route, *, retry=True):
    """Synthetic emitted lifecycles with independent scope and attempt identities."""
    events = []
    graph = f'graph-{trial}'
    def start(scope, parent, owner, kind, **values):
        row = dict(event='scope_start', scope_id=scope, parent_id=parent, owner=owner, kind=kind, **values)
        events.append(row)
        return row
    def end(row):
        events.append(dict(row, event='scope_end', outcome='passed'))
    graph_scope = start(graph, None, 'GRAPH', 'graph', attempt=1, max_passes=1)
    events.append(dict(event='edge', owner='GRAPH', source='START', target='first', implicit=True, attempt_id=None))
    for component in (['first'] if route == 'skip' else ['first', 'second']):
        scope = f'{graph}-{component}'
        component_scope = start(scope, graph, component, 'component', component=component, attempt=1, max_passes=2, attempt_id=None)
        for attempt in ((1, 2) if retry else (1,)):
            attempt_id = f'{scope}-attempt-{attempt}'
            events.append(dict(event='attempt', scope_id=scope, component=component, attempt=attempt, max_passes=2, attempt_id=attempt_id))
            for suffix in ('draft', 'format', 'guard'):
                node = f'{component}_{suffix}'
                node_scope = start(f'{attempt_id}-{node}', scope, node, 'node', component=component, node=node,
                                   attempt=attempt, max_passes=2, attempt_id=attempt_id)
                events.append(dict(event='node', scope_id=node_scope['scope_id'], node=node, component=component, attempt=attempt))
                end(node_scope)
        end(component_scope)
        target = ('END' if route == 'skip' else 'second') if component == 'first' else 'END'
        condition = ('first_draft.route == "skip"' if route == 'skip' else 'otherwise') if component == 'first' else 'always'
        events.append(dict(event='edge', owner='GRAPH', source=component, target=target, condition=condition,
                           implicit=component == 'second', visit_id=scope, attempt_id=None))
    end(graph_scope)
    return events





def test_exact_all_success_bound():
    assert lower_probability(10, 10, .05) == pytest.approx(.05 ** .1)
    assert lower_probability(0, 10, .05) == 0


@pytest.mark.parametrize('args', [(True, 2, .05), (3, 2, .05), (0, 0, .05), (0, 2, 1)])
def test_invalid_observations(args):
    with pytest.raises(ValueError):
        lower_probability(*args)



def records():
    rows = []
    for trial in range(1, 61):
        route = ('skip', 'next_a', 'next_b')[(trial - 1) % 3]
        rows.append(measurement(trial, {'first_draft': {'route': route}}, FIRST if route == 'skip' else DECLARED, structural_events(trial, route)))
    return rows

def test_calibration_recomputes_claims():
    report = measured_calibration(records(), DECLARED)
    report['model'] = 'synthetic'
    assert validate_calibration(report, DECLARED, {'model': 'synthetic'}) == report['max_runs']


@pytest.mark.parametrize('key,value', [('bounds', {'a': 1}), ('max_runs', 1), ('missing_node_bound', 0), ('hits', {'a': 60}), ('trials', True), ('model', 'stale')])
def test_tampered_calibration_rejected(key, value):
    report = measured_calibration(records(), DECLARED)
    report['model'] = 'synthetic'
    report[key] = value
    with pytest.raises(ValueError):
        validate_calibration(report, DECLARED, {'model': 'synthetic'})


@pytest.mark.parametrize('category', ['fingerprint_version', 'sampling_method', 'model',
    'dependency_versions', 'python_version', 'backend', 'provider_identity',
    'user_input_hash', 'effective_settings', 'sources'])
def test_stale_diagnostic_identifies_category_without_values(category, monkeypatch):
    from api_agent.workflows import sampling
    arithmetic = lambda *args: pytest.fail('Stale evidence reached statistical validation')
    monkeypatch.setattr(sampling, 'measured_calibration', arithmetic)
    with pytest.raises(ValueError) as caught:
        validate_calibration({category: 'old-private-value'}, DECLARED,
                             {category: 'new-private-value'})
    message = str(caught.value)
    assert f'Changed categories: {category}.' in message
    assert 'docs/workflow_calibration.md' in message
    assert 'real LLM calls' in message
    assert 'old-private-value' not in message
    assert 'new-private-value' not in message


@pytest.mark.parametrize('report', [None, [], 'private-value', {}])
def test_invalid_or_unknown_fingerprint_diagnostic_uses_fixed_fallback(report):
    with pytest.raises(ValueError) as caught:
        validate_calibration(report, DECLARED, {'private-field-name': 'private-value'})
    message = str(caught.value)
    assert 'Changed categories: report structure or additional fingerprint fields.' in message
    assert 'private-field-name' not in message
    assert 'private-value' not in message


def test_stale_diagnostic_lists_only_changed_categories_in_fixed_order():
    saved = {'model': 'same', 'sources': {'file': 'old'}, 'effective_settings': {'cache': None}}
    current = {'sources': {'file': 'new'}, 'model': 'same', 'effective_settings': {'cache': 'medium'}}
    with pytest.raises(ValueError) as caught:
        validate_calibration(saved, DECLARED, current)
    assert 'Changed categories: effective_settings, sources.' in str(caught.value)


@pytest.mark.parametrize('observations,required,ceiling', [([], {'a'}, 60), ([{'a'}], set(), 60), ([{'b'}], {'a'}, 60), ([{'a'}]*61, {'a'}, 60), ([{'a'}], {'a'}, 121), ([set()]*60, {'a'}, 60)])
def test_invalid_or_insufficient_pilot(observations, required, ceiling):
    with pytest.raises(ValueError):
        calibration(observations, required, ceiling=ceiling)


def test_coverage_stops_on_first_complete_graph():
    calls, events = [], []
    async def run(trial):
        calls.append(trial)
        return trial, {'a'} if trial == 1 else {'b'}
    summary, result = asyncio.run(cover(run, {'a', 'b'}, 60, events.append))
    assert calls == [1, 2]
    assert result == 2
    assert summary['stopping_reason'] == 'all_nodes_hit'
    assert [event['complete'] for event in events] == [False, True]


def test_coverage_fails_at_cap():
    async def run(trial):
        return None, {'a'}
    with pytest.raises(AssertionError):
        asyncio.run(cover(run, {'a', 'b'}, 2))


@pytest.mark.parametrize('required,maximum', [(set(), 2), ({'a'}, 0), ({'a'}, 61)])
def test_invalid_coverage(required, maximum):
    with pytest.raises(ValueError):
        asyncio.run(cover(None, required, maximum))


def test_unknown_coverage_node():
    async def run(trial):
        return None, {'unknown'}
    with pytest.raises(ValueError):
        asyncio.run(cover(run, {'a'}, 1))


@pytest.mark.parametrize('field,value', [('trial', True), ('trial', 2), ('route', 'unknown'),
    ('nodes', [1]), ('nodes', tuple(sorted(FIRST))), ('nodes', sorted(DECLARED))])
def test_measurements_reject_inconsistent_route_or_trials(field, value):
    rows = records()
    rows[0][field] = value
    with pytest.raises(ValueError):
        measured_calibration(rows, DECLARED)


@pytest.mark.parametrize('invalid', [None, []])
def test_measurements_reject_missing_records(invalid):
    with pytest.raises(ValueError):
        measured_calibration(invalid, DECLARED)


def test_measurements_derive_routes_and_hits():
    report=measured_calibration(records(),DECLARED)
    assert report['routes']=={'skip':20,'next_a':20,'next_b':20}
    assert report['hits']['second_guard']==40
    report['routes']={'skip':60}
    with pytest.raises(ValueError):
        validate_calibration(report,DECLARED,{})


@pytest.fixture
def workflow_driver():
    from api_agent.workflows import fixture_driver
    return vars(fixture_driver)


@pytest.mark.parametrize('setting,value', [('GPT_AUTH_MODE','api_key'),('GPT_BASE_URL','https://example.invalid/v1'),('GPT_API_MODE','completions'),('LLM_CACHE_POLICY','off')])
def test_fingerprint_changes_with_backend_settings(workflow_driver,setting,value):
    fingerprint=workflow_driver['fingerprint']
    assert fingerprint('synthetic',FACTORY_CONFIG) != fingerprint('synthetic',FACTORY_CONFIG | {setting:value})


def test_fingerprint_binds_user_input_and_driver(workflow_driver,monkeypatch):
    fingerprint=workflow_driver['fingerprint']
    before=fingerprint('synthetic',FACTORY_CONFIG)
    monkeypatch.setitem(fingerprint.__globals__,'USER_INPUT','different synthetic input')
    after=fingerprint('synthetic',FACTORY_CONFIG)
    assert before['user_input_hash'] != after['user_input_hash']
    assert 'src/api_agent/workflows/fixture_driver.py' in before['sources']


@pytest.mark.parametrize('source,before_text,after_text',[
    ('src/api_agent/llm_providers/gpt/inference.py',b'_LIMIT = 1048576',b'_LIMIT = 1048577'),
    ('src/api_agent/auth/bindings.py',b'sources[selected_role]',b'sources[next(iter(sources))]')])
def test_fingerprint_binds_request_affecting_code(workflow_driver,monkeypatch,source,before_text,after_text):
    from pathlib import Path
    fingerprint=workflow_driver['fingerprint']
    before=fingerprint('synthetic',FACTORY_CONFIG)
    original=Path.read_bytes
    def changed(path):
        return original(path).replace(before_text,after_text) if str(path).endswith(source) else original(path)
    monkeypatch.setattr(Path,'read_bytes',changed)
    after=fingerprint('synthetic',FACTORY_CONFIG)
    assert before['sources'][source] != after['sources'][source]


def test_all_skip_records_cannot_claim_second_component_success():
    rows = records()
    rows[0]['nodes'] = sorted(DECLARED)
    with pytest.raises(ValueError,match='route disagrees'):
        measured_calibration(rows,DECLARED)


def test_fingerprint_changes_when_installed_dependency_changes(workflow_driver,monkeypatch):
    fingerprint=workflow_driver['fingerprint']
    before=fingerprint('synthetic',FACTORY_CONFIG)
    original=fingerprint.__globals__['version']
    monkeypatch.setitem(fingerprint.__globals__,'version',lambda name: '999.0' if name=='langgraph' else original(name))
    after=fingerprint('synthetic',FACTORY_CONFIG)
    assert before['dependency_versions']['langgraph'] != after['dependency_versions']['langgraph']
    report=measured_calibration(records(),DECLARED)
    report.update(before)
    with pytest.raises(ValueError,match='stale'):
        validate_calibration(report,DECLARED,after)


def test_calibrated_cap_adds_four_runs_after_exact_minimum():
    report = measured_calibration(records(), DECLARED)
    minimum = report['minimum_runs']
    bounds = report['bounds'].values()
    assert set(report['hits']) == TARGETS
    assert report['safety_margin'] == 4 and report['max_runs'] == minimum + 4
    assert report['minimum_missing_node_bound'] == sum((1 - probability) ** minimum for probability in bounds)
    assert report['minimum_missing_node_bound'] <= .001
    assert sum((1 - probability) ** (minimum - 1) for probability in bounds) > .001
    assert validate_calibration(report, DECLARED, {}) == minimum + 4


@pytest.mark.parametrize('ceiling, minimum', [(13, 10), (60, 57), (120, 117)])
def test_required_margin_above_operating_ceiling_rejects_without_clamping(monkeypatch, ceiling, minimum):
    from api_agent.workflows import sampling
    probability = 1 - .001 ** (1 / (minimum - .5))
    monkeypatch.setattr(sampling, 'lower_probability', lambda *args: probability)
    assert (1 - probability) ** minimum <= .001
    assert (1 - probability) ** (minimum - 1) > .001
    with pytest.raises(ValueError, match='safety margin exceeds'):
        calibration([{'node'}], {'node'}, ceiling=ceiling)


@pytest.mark.parametrize('key, value', [('minimum_runs', 1), ('safety_margin', 0), ('minimum_missing_node_bound', 0.0)])
def test_buffer_policy_claims_are_recomputed_before_acceptance(key, value):
    report = measured_calibration(records(), DECLARED)
    report[key] = value
    with pytest.raises(ValueError, match='claims do not match'):
        validate_calibration(report, DECLARED, {})


@pytest.mark.parametrize('missing', ['internal_retry', 'skip', 'next_a', 'next_b'])
def test_all_nodes_do_not_complete_expanded_coverage(missing):
    async def run(trial):
        return trial, TARGETS - {missing}
    with pytest.raises(AssertionError, match=missing):
        asyncio.run(cover(run, TARGETS, 3))


def test_expanded_coverage_stops_only_after_routes_and_retry_observed():
    calls = []
    async def run(trial):
        calls.append(trial)
        return trial, DECLARED | ({'skip'} if trial == 1 else {'next_a'} if trial == 2 else {'next_b', 'internal_retry'})
    summary, result = asyncio.run(cover(run, TARGETS, 5))
    assert calls == [1, 2, 3] and result == 3
    assert summary['stopping_reason'] == 'all_targets_hit'


@pytest.mark.parametrize('change', ['missing_retry', 'missing_route', 'missing_attempt', 'reordered_attempt', 'wrong_scope'])
def test_measurement_tampering_cannot_qualify(change):
    rows = records()
    if change == 'missing_retry':
        for index, row in enumerate(rows):
            rows[index] = measurement(row['trial'], {'first_draft': {'route': row['route']}}, row['nodes'],
                                      structural_events(row['trial'], row['route'], retry=False))
    elif change == 'missing_route':
        for row in rows:
            if row['route'] == 'next_b':
                row['route'] = 'next_a'
    elif change == 'missing_attempt':
        rows[0]['attempts'].pop(0)
    elif change == 'reordered_attempt':
        rows[0]['attempts'].reverse()
    else:
        rows[0]['attempts'][1]['scope_id'] = 'unobserved'
    with pytest.raises(ValueError):
        measured_calibration(rows, DECLARED)


def test_execution_defect_propagates_before_coverage_retry():
    calls = []
    async def run(trial):
        calls.append(trial)
        raise RuntimeError('execution defect')
    with pytest.raises(RuntimeError, match='execution defect'):
        asyncio.run(cover(run, TARGETS, 60))
    assert calls == [1]


def test_legacy_node_only_records_cannot_qualify_expanded_policy():
    legacy = [{'trial': trial, 'route': 'next_a', 'nodes': sorted(DECLARED)} for trial in range(1, 61)]
    with pytest.raises(ValueError, match='schema'):
        measured_calibration(legacy, DECLARED)


@pytest.mark.parametrize('defect', ['no_edges', 'wrong_edge', 'orphan_attempt', 'wrong_parent', 'failed_scope', 'missing_retry_nodes'])
def test_targets_require_matching_executed_structural_proof(defect):
    row = records()[0]
    if defect == 'no_edges':
        row['proof'] = [event for event in row['proof'] if event['event'] != 'edge']
    elif defect == 'wrong_edge':
        edge = next(event for event in row['proof'] if event['event'] == 'edge' and event['source'] == 'first')
        edge.update(target='second', condition='otherwise')
    elif defect == 'orphan_attempt':
        for event in row['proof']:
            if event['event'] == 'attempt':
                event['scope_id'] = 'unexecuted-component'
        for event in row['attempts']:
            event['scope_id'] = 'unexecuted-component'
    elif defect == 'wrong_parent':
        for event in row['proof']:
            if event['event'] in {'scope_start', 'scope_end'} and event['kind'] == 'component':
                event['parent_id'] = 'unexecuted-graph'
    elif defect == 'failed_scope':
        row['proof'][-1]['outcome'] = 'failed'
    else:
        row['proof'] = [event for event in row['proof'] if not (event['event'] in {'node', 'scope_start', 'scope_end'}
                       and event.get('node') and event.get('attempt') == 2)]
    with pytest.raises(ValueError):
        observed_targets(row)


def test_independent_pilot_trials_cannot_reuse_executed_scope_identities():
    rows = records()
    rows[3]['proof'] = deepcopy(rows[0]['proof'])
    rows[3]['attempts'] = deepcopy(rows[0]['attempts'])
    with pytest.raises(ValueError, match='reuse executed scope'):
        measured_calibration(rows, DECLARED)


def test_structural_proof_survives_evidence_sanitization():
    from api_agent.workflows.evidence import sanitize
    row = records()[0]
    assert sanitize(row) == row


@pytest.mark.parametrize('proof', [None, [], [None], [{'event': 'unrecognized'}]])
def test_structural_proof_requires_an_observed_event_stream(proof):
    from api_agent.workflows.sampling import structural_targets
    row = records()[0]
    row['proof'] = proof
    with pytest.raises(ValueError, match='structural proof|structural event'):
        structural_targets(row)


@pytest.mark.parametrize('defect,message', [
    ('extra_field', 'Unexpected executed structural fields'),
    ('duplicate_scope', 'Duplicate or invalid executed scope'),
    ('wrong_graph_owner', 'Invalid executed graph scope'),
    ('node_attempt_identity', 'Successful node is not bound'),
    ('duplicate_node', 'Duplicate successful node'),
    ('root_attempt_identity', 'Root edge is not bound'),
    ('root_visit_identity', 'Root edge source disagrees'),
    ('extra_component', 'Executed components disagree'),
    ('returned_nodes', 'Executed successful nodes disagree'),
    ('returned_attempts', 'Observed attempts disagree'),
])
def test_structural_measurement_rejects_independent_correlation_defects(defect, message):
    from api_agent.workflows.sampling import structural_targets
    row = records()[0]
    if defect == 'extra_field':
        for event in (row['proof'][0], row['proof'][-1]):
            event['unobserved'] = True
    elif defect == 'duplicate_scope':
        row['proof'].insert(1, deepcopy(row['proof'][0]))
    elif defect == 'wrong_graph_owner':
        for event in (row['proof'][0], row['proof'][-1]):
            event['owner'] = 'different_graph'
    elif defect == 'node_attempt_identity':
        # Preserve node identity/count; only its admitted attempt binding changes.
        for event in row['proof']:
            if event['event'] in {'scope_start', 'scope_end'} and event.get('node') == 'first_draft' and event['attempt'] == 1:
                event['attempt_id'] = 'unexecuted-attempt'
    elif defect == 'duplicate_node':
        index = next(index for index, event in enumerate(row['proof']) if event['event'] == 'node')
        row['proof'].insert(index, deepcopy(row['proof'][index]))
    elif defect == 'root_attempt_identity':
        row['proof'][1]['attempt_id'] = 'component-attempt'
    elif defect == 'root_visit_identity':
        edge = next(event for event in row['proof'] if event['event'] == 'edge' and event['source'] == 'first')
        edge['visit_id'] = 'unexecuted-component'
    elif defect == 'extra_component':
        scope = dict(event='scope_start', scope_id='extra-component', parent_id=row['proof'][0]['scope_id'],
                     owner='first', kind='component', component='first', attempt=1, max_passes=2, attempt_id=None)
        row['proof'][-1:-1] = [scope, dict(scope, event='scope_end', outcome='passed')]
    elif defect == 'returned_nodes':
        row['nodes'].remove('first_draft')
    else:
        row['attempts'][0]['max_passes'] = 3
    with pytest.raises(ValueError, match=message):
        structural_targets(row)


@pytest.mark.parametrize('attempts,message', [
    (None, 'Invalid observed component attempts'),
    ([None], 'Invalid observed component attempt'),
    ([{}], 'Invalid observed component attempt'),
    ([], 'Missing observed component attempts'),
])
def test_observed_targets_rejects_unmeasured_component_attempts(attempts, message):
    row = records()[0]
    row['attempts'] = attempts
    with pytest.raises(ValueError, match=message):
        observed_targets(row)


@pytest.mark.parametrize('field,value', [('scope_id', ''), ('component', 'unknown'), ('component', 'second'),
    ('attempt', True), ('attempt', 0), ('max_passes', True), ('max_passes', 0)])
def test_observed_attempt_metadata_must_describe_a_legal_component_visit(field, value):
    row = records()[0]
    row['attempts'][0][field] = value
    with pytest.raises(ValueError, match='Invalid observed component attempt'):
        observed_targets(row)


@pytest.mark.parametrize('defect,message', [
    ('component_name', 'Executed component scope metadata disagrees with owner'),
    ('node_limit', 'Executed node pass limit disagrees with its component attempt'),
])
def test_scope_metadata_must_agree_with_executed_owner_and_pass_limit(defect, message):
    row = records()[0]
    for event in row['proof']:
        if event['event'] not in {'scope_start', 'scope_end'}:
            continue
        if defect == 'component_name' and event['kind'] == 'component':
            event['component'] = 'second'
        if defect == 'node_limit' and event['kind'] == 'node':
            event['max_passes'] = 3
    with pytest.raises(ValueError, match=message):
        observed_targets(row)
