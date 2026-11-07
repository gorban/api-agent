"""Delegated SDK spans attach to the active native workflow node."""
from uuid import uuid4

from api_agent.workflows.observations import WorkflowObservations
from api_agent.workflows.run_context import structural_scope
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.telemetry.tracing import TraceSpan
from api_agent.telemetry.tracing import TraceRuntime, TraceError
from api_agent.telemetry.operation import TraceLoss
from threading import Lock
import pytest


class Trace:
    active = True

    def __init__(self):
        self.calls = []

    def begin(self, name, structural, *, parent, payload):
        self.calls.append((name, structural, parent, payload))
        return object()


def test_delegated_child_uses_current_scope_and_explicit_parent():
    owner = object.__new__(WorkflowObservations)
    owner._closed = False
    owner.trace = Trace()
    scope = str(uuid4())
    node = object()
    owner._spans = {scope: node}
    with structural_scope(scope):
        child = owner.begin_child_trace('component delegated', {}, parent=None)
        assert owner.trace.calls == [('component delegated', {}, node, None)]
        owner.begin_child_trace('execute_tool', {'gen_ai.operation.name': 'execute_tool'}, parent=child)
    assert owner.trace.calls[-1][2] is child


def test_delegated_child_is_absent_after_observation_close():
    owner = object.__new__(WorkflowObservations)
    owner._closed = True
    owner.trace = Trace()
    assert owner.begin_child_trace('component delegated', {}) is None


def test_delegated_native_io_obeys_trace_content_policy():
    class SDKSpan:
        def __init__(self):
            self.attributes = {}

        def set_attribute(self, name, value):
            self.attributes[name] = value

    hidden, visible = SDKSpan(), SDKSpan()
    TraceSpan(hidden, EmissionBoundary(ContentPolicy(False))).native_io('input', {'prompt': 'hello'})
    TraceSpan(visible, EmissionBoundary(ContentPolicy(True))).native_io('input', {'prompt': 'hello'})
    assert hidden.attributes == {}
    assert any('hello' in value for value in visible.attributes.values())


def test_catalog_selection_admission_preserves_other_selected_models():
    runtime = object.__new__(TraceRuntime)
    runtime._model_lock = Lock()
    runtime._catalog_model_identities = frozenset({'first'})
    runtime._selected_model_identities = frozenset()
    runtime._response_model_identities = frozenset({'served'})
    runtime._model_identities = runtime._catalog_model_identities | runtime._response_model_identities
    runtime.losses = TraceLoss()
    runtime.admit_catalog_selection('second')
    runtime.admit_catalog_selection('second')
    runtime.bind_model_identities(('refreshed',))
    assert all(runtime.is_model_identity(value) for value in ('refreshed', 'second', 'served'))
    assert not runtime.is_model_identity('first')
    for invalid in ('', 'x' * 201, 'bad\nmodel', None):
        with pytest.raises(TraceError):
            runtime.admit_catalog_selection(invalid)
    assert runtime.is_model_identity('second')


def test_catalog_selection_capacity_records_loss_without_replacing_snapshot():
    runtime = object.__new__(TraceRuntime)
    runtime._model_lock = Lock()
    runtime._catalog_model_identities = frozenset({'catalog-model'})
    runtime._selected_model_identities = frozenset(f'model-{index}' for index in range(256))
    runtime._response_model_identities = frozenset()
    runtime._model_identities = runtime._catalog_model_identities | runtime._selected_model_identities
    runtime.losses = TraceLoss()
    runtime.admit_catalog_selection('model-0')
    runtime.admit_catalog_selection('extra')
    assert runtime.is_model_identity('model-0')
    assert not runtime.is_model_identity('extra')
    assert runtime.losses.count == 1
