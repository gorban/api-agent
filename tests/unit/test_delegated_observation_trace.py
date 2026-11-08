"""Delegated SDK spans attach to the active native workflow node."""
from uuid import uuid4

from api_agent.workflows.observations import WorkflowObservations
from api_agent.workflows.run_context import structural_scope
from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from api_agent.telemetry.tracing import TraceSpan, _bounded_native_json
from api_agent.telemetry.tracing import TraceRuntime, TraceError
from api_agent.telemetry.operation import TraceLoss
from threading import Lock
import pytest
import json


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


def test_oversized_native_io_never_exceeds_storage_tag_limit():
    class SDKSpan:
        def __init__(self):
            self.attributes = {}

        def set_attribute(self, name, value):
            self.attributes[name] = value

        def set_attributes(self, values):
            self.attributes.update(values)

    sdk = SDKSpan()
    boundary = EmissionBoundary(ContentPolicy(True))
    boundary.register_secret('known-secret')
    span = TraceSpan(sdk, boundary, operation='chat')
    span.native_io('input', {'messages': [{'role': 'user',
        'content': 'known-secret' + 'é' * 40000}]})
    assert sdk.attributes
    for key, value in sdk.attributes.items():
        if key in ('langfuse.observation.input', 'gen_ai.input.messages'):
            assert len(value.encode('utf-8')) < 65000
            assert json.loads(value)
    assert sdk.attributes['api_agent.capture.reason'] == 'truncated'
    admitted = json.loads(sdk.attributes['langfuse.observation.input'])
    assert admitted['messages'][0]['role'] == 'user'
    assert admitted['messages'][0]['content'].endswith('[truncated]')
    span.native_io('output', {'text': 'x' * 2032 + 'known-secret' + 'y' * 40000})
    boundary_preview = json.loads(sdk.attributes['langfuse.observation.output'])
    assert boundary_preview['text'].endswith('[truncated]')
    assert 'known-secret' not in str(sdk.attributes)


def test_native_io_limits_escaped_json_and_generated_message_alias(monkeypatch):
    class SDKSpan:
        def __init__(self):
            self.attributes = {}

        def set_attribute(self, name, value):
            self.attributes[name] = value

        def set_attributes(self, values):
            self.attributes.update(values)

    from api_agent.telemetry import trace_mapping
    monkeypatch.setattr(trace_mapping, 'genai_messages', lambda direction, values: [
        {'role': 'user', 'parts': [{'type': 'text', 'content': '\\' * 50000}]}])
    sdk = SDKSpan()
    span = TraceSpan(sdk, EmissionBoundary(ContentPolicy(True)), operation='chat')
    span.native_io('output', {'text': 'short'})
    assert sdk.attributes['langfuse.observation.output'] == '{"text": "short"}'
    alias = json.loads(sdk.attributes['gen_ai.output.messages'])
    assert alias[0]['role'] == 'user'
    assert alias[0]['parts'][0]['content'].endswith('[truncated]')
    assert sdk.attributes['api_agent.capture.reason'] == 'truncated'
    span.native_io('input', {'messages': [{'role': 'user', 'content': ('é\\\x00' * 12000)}]})
    for key in ('langfuse.observation.input', 'gen_ai.output.messages'):
        value = sdk.attributes[key]
        assert len(json.dumps(value).encode('utf-8')) < 65000
        assert json.loads(value)


@pytest.mark.parametrize('character', ['\x7f', '😀'])
def test_native_io_limits_del_character_after_storage_escaping(monkeypatch, character):
    class SDKSpan:
        def __init__(self):
            self.attributes = {}

        def set_attribute(self, name, value):
            self.attributes[name] = value

        def set_attributes(self, values):
            self.attributes.update(values)

    from api_agent.telemetry import trace_mapping
    monkeypatch.setattr(trace_mapping, 'genai_messages', lambda direction, values: [
        {'role': 'user', 'parts': [{'type': 'text', 'content': character * 16000}]}])
    sdk = SDKSpan()
    span = TraceSpan(sdk, EmissionBoundary(ContentPolicy(True)), operation='chat')
    span.native_io('input', {'messages': [{'role': 'user', 'content': character * 16000}]})
    span.native_io('output', {'text': 'short'})
    for key in ('langfuse.observation.input', 'gen_ai.output.messages'):
        value = sdk.attributes[key]
        assert len(json.dumps(value, ensure_ascii=True).encode()) <= 16_384
        bounded = json.loads(value)
        text = bounded['messages'][0]['content'] if key.startswith('langfuse') else bounded[0]['parts'][0]['content']
        assert text.endswith('[truncated]')


def test_bounded_native_io_preserves_unrelated_structured_fields():
    class SDKSpan:
        attributes = None

        def __init__(self):
            self.attributes = {}

        def set_attribute(self, name, value):
            self.attributes[name] = value

        def set_attributes(self, values):
            self.attributes.update(values)

    sdk = SDKSpan()
    TraceSpan(sdk, EmissionBoundary(ContentPolicy(True))).native_io('output', {
        'records': [{'kind': 'invoice', 'amount': 8666, 'detail': 'a' * 50000}],
        'status': {'ok': False, 'count': 1}})
    output = json.loads(sdk.attributes['langfuse.observation.output'])
    assert output['records'][0]['kind'] == 'invoice'
    assert output['records'][0]['amount'] == 8666
    assert output['records'][0]['detail'].endswith('[truncated]')
    assert output['status'] == {'ok': False, 'count': 1}
    assert sdk.attributes['api_agent.capture.reason'] == 'truncated'


def test_native_io_lone_surrogate_survives_sdk_otlp_encoding():
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer('native-io-test').start_as_current_span('native') as sdk_span:
        TraceSpan(sdk_span, EmissionBoundary(ContentPolicy(True))).native_io('output', {
            'text': '\ud800', 'kind': 'invoice'})
    encoded = encode_spans(exporter.get_finished_spans())
    attributes = {entry.key: entry.value for entry in
        encoded.resource_spans[0].scope_spans[0].spans[0].attributes}
    assert json.loads(attributes['langfuse.observation.output'].string_value) == {
        'text': '\ud800', 'kind': 'invoice'}
    provider.shutdown()


def test_native_json_shrinks_many_large_leaves_before_container_fallback():
    encoded, truncated = _bounded_native_json({'items': [
        {'kind': 'invoice', 'amount': index, 'detail': 'a' * 2000}
        for index in range(100)]})
    value = json.loads(encoded)
    assert truncated
    assert len(value['items']) == 100
    assert value['items'][99]['amount'] == 99
    assert value['items'][99]['detail'].endswith('[truncated]')
    assert len(json.dumps(encoded).encode()) <= 16_384


def test_native_json_falls_back_for_extremely_wide_container():
    encoded, truncated = _bounded_native_json({'items': [
        {'kind': 'invoice', 'amount': index} for index in range(1000)]})
    value = json.loads(encoded)
    assert truncated
    assert value['capture_reason'] == 'truncated'
    assert value['preview']
    assert len(json.dumps(encoded).encode()) <= 16_384


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
