"""Successful empty JSON retains provenance distinct from missing capture."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.workflows.capture import capture_reason, project_values
from api_agent.workflows.capture_authority import capture_node
from api_agent.workflows.observations import WorkflowObservationFactory
from test_graph_capture_authority import definition


@pytest.mark.parametrize('value,kind', [({}, 'object'), ([], 'array'), ('', 'string'),
    (0, 'integer'), (False, 'boolean'), (None, 'null')])
def test_successfully_projected_empty_json_is_captured(value, kind):
    report = set()
    safe = project_values(value, {'type': kind}, EmissionBoundary(ContentPolicy(True)), report=report)
    assert type(safe) is type(value) and safe == value
    assert capture_reason(safe, report) == 'captured'


@pytest.mark.parametrize('log,trace', [(False, False), (False, True), (True, False), (True, True)])
def test_empty_node_input_is_captured_for_each_enabled_destination(log, trace):
    from api_agent.config.resolve import ConfigResult, ResolvedValue
    async def run():
        boundary = EmissionBoundary(ContentPolicy(log))
        authority = EmissionBoundary(ContentPolicy(True))
        rows, events = [], []
        class Span:
            def event(self, row): events.append(row)
            def attributes(self, row): pass
            def end(self, **kwargs): pass
        tracer = SimpleNamespace(boundary=boundary.for_policy(ContentPolicy(trace)),
            is_model_identity=lambda _: False, begin=lambda *args, **kwargs: Span())
        values = {'LOG_CONTENT': log, 'TRACELOOP_TRACE_CONTENT': trace}
        config = ConfigResult(values, {key: ResolvedValue(value, 'cli', True)
            for key, value in values.items()}, (), (), ())
        record = WorkflowObservationFactory(config, boundary=boundary, sink=rows.append, trace=tracer).create(definition())
        record.observe(dict(event='scope_start', scope_id='visit', owner='Make', kind='node'))
        @asynccontextmanager
        async def capability():
            lease = authority.begin_evidence_lease()
            try:
                yield authority.snapshot_content()[1]
            finally:
                lease.release()
        await capture_node(record, {'capture_authority': capability}, visit_id='visit', direction='input',
            values={}, schema={'type': 'object', 'properties': {}})
        retained = next(row for row in record.finalize()['events'] if row['event'] == 'capture')
        live = next(row for row in rows if row['event'] == 'capture')
        for row in (retained, live):
            assert row['reason'] == ('captured' if log else 'withheld')
            assert ('values' in row) is log
            if log: assert row['values'] == {}
        assert len(events) == 1
        if trace:
            assert events[0]['reason'] == 'captured' and events[0]['values'] == {}
        else:
            assert events[0] == {'values': {}}
    asyncio.run(run())


@pytest.mark.parametrize('missing_stage', [1, 2])
def test_export_omission_cannot_become_successful_empty_capture(missing_stage):
    class Boundary:
        def __init__(self): self.calls = 0
        def content_export(self, value):
            self.calls += 1
            return {} if self.calls == missing_stage else value
    report = set()
    safe = project_values({}, {'type': 'object'}, Boundary(), report=report)
    assert safe == {} and 'valid_empty' not in report
    assert capture_reason(safe, report) == 'unavailable'


@pytest.mark.parametrize('value,schema,options,expected', [
    (object(), {}, {}, 'unavailable'),
    ({'hidden': 1}, {'type': 'object', 'properties': {}}, {}, 'unavailable'),
    ({'text': 'long text'}, {'type': 'object'}, {'max_bytes': 2}, 'truncated'),
    ('', {'type': 'object'}, {}, 'unavailable'),
])
def test_loss_cannot_become_successful_empty_capture(value, schema, options, expected):
    report = set()
    safe = project_values(value, schema, EmissionBoundary(ContentPolicy(True)), report=report, **options)
    assert safe == {} and 'valid_empty' not in report
    assert capture_reason(safe, report) == expected


@pytest.mark.parametrize('upstream,expected', [('unavailable', 'unavailable'), ('truncated', 'truncated'),
    ('partial', 'partial'), (None, 'captured')])
def test_successful_empty_does_not_upgrade_upstream_loss(upstream, expected):
    report = set()
    safe = project_values({}, {'type': 'object'}, EmissionBoundary(ContentPolicy(True)), report=report)
    assert capture_reason(safe, report, upstream) == expected


def test_successful_empty_marker_does_not_hide_other_projection_loss():
    assert capture_reason({}, {'valid_empty', 'unsupported'}) == 'unavailable'
    assert capture_reason({'text': 'safe'}, {'valid_empty', 'unsupported'}) == 'partial'


@pytest.mark.parametrize('invalid_stage', [1, 2])
@pytest.mark.parametrize('invalid', [None, ['values'], 'values'])
def test_nonmapping_export_is_unavailable_without_raising(invalid_stage, invalid):
    class Boundary:
        def __init__(self): self.calls = 0
        def content_export(self, value):
            self.calls += 1
            return invalid if self.calls == invalid_stage else value
    report = set()
    safe = project_values({}, {'type': 'object'}, Boundary(), report=report)
    assert safe == {} and report == {'unsupported'}
    assert capture_reason(safe, report) == 'unavailable'
