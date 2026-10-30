import asyncio

import pytest

from api_agent.config.resolve import ConfigResult, ResolvedValue
from api_agent.telemetry.tracing import TraceRuntime


@pytest.fixture
def trace_sdk():
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    return encode_spans, TracerProvider, SimpleSpanProcessor, InMemorySpanExporter


@pytest.mark.parametrize('content', [False, True])
def test_serialized_arrays_respect_content_policy_and_keep_native_types(content, trace_sdk):
    encode_spans, TracerProvider, SimpleSpanProcessor, InMemorySpanExporter = trace_sdk
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    config = ConfigResult({'TRACE_MODE': 'OTEL', 'TRACELOOP_TRACE_CONTENT': content},
        {'TRACELOOP_TRACE_CONTENT': ResolvedValue(content, 'cli', True)}, (), (), ())
    runtime = TraceRuntime(config, factory=lambda *args: (provider, provider.get_tracer('test')))
    asyncio.run(runtime.astart())
    with runtime.span({'reasons': ['stop', 'length'], 'counts': [1, 2],
                       'flags': [True, False], 'ratios': [0.5, 1.0],
                       'empty': [], 'mixed': [1, 'two']}):
        pass

    request = encode_spans(exporter.get_finished_spans())
    attributes = {entry.key: entry.value for entry in
                  request.resource_spans[0].scope_spans[0].spans[0].attributes}
    if content:
        for key, field, expected in [('reasons', 'string_value', ['stop', 'length']),
                                     ('counts', 'int_value', [1, 2]),
                                     ('flags', 'bool_value', [True, False]),
                                     ('ratios', 'double_value', [0.5, 1.0])]:
            assert attributes[key].WhichOneof('value') == 'array_value'
            assert [getattr(item, field) for item in attributes[key].array_value.values] == expected
        assert attributes['mixed'].string_value == '[1, "two"]'
        assert attributes['empty'].WhichOneof('value') == 'array_value'
        assert not attributes['empty'].array_value.values
    else:
        assert not attributes
    assert runtime.shutdown()


def test_invalid_utf8_array_is_json_encoded_without_sdk_diagnostic(caplog, trace_sdk):
    encode_spans, TracerProvider, SimpleSpanProcessor, InMemorySpanExporter = trace_sdk
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    config = ConfigResult({'TRACE_MODE': 'OTEL', 'TRACELOOP_TRACE_CONTENT': True},
        {'TRACELOOP_TRACE_CONTENT': ResolvedValue(True, 'cli', True)}, (), (), ())
    runtime = TraceRuntime(config, factory=lambda *args: (provider, provider.get_tracer('test')))
    asyncio.run(runtime.astart())

    with runtime.span({'ordinary_key': ['safe\ud800bad']}):
        pass

    request = encode_spans(exporter.get_finished_spans())
    attribute, = request.resource_spans[0].scope_spans[0].spans[0].attributes
    assert attribute.value.string_value == '["safe\\ud800bad"]'
    assert b'ordinary_key' not in caplog.text.encode()
    assert runtime.shutdown()


def test_registered_secret_in_array_is_redacted_before_serialization(trace_sdk):
    encode_spans, TracerProvider, SimpleSpanProcessor, InMemorySpanExporter = trace_sdk
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    config = ConfigResult({'TRACE_MODE': 'OTEL', 'TRACELOOP_TRACE_CONTENT': True},
        {'TRACELOOP_TRACE_CONTENT': ResolvedValue(True, 'cli', True)}, (), (), ())
    runtime = TraceRuntime(config, factory=lambda *args: (provider, provider.get_tracer('test')))
    asyncio.run(runtime.astart())
    runtime.boundary.register_secret('hidden-value')

    with runtime.span({'payload': ['before hidden-value after']}):
        pass

    request = encode_spans(exporter.get_finished_spans())
    attribute, = request.resource_spans[0].scope_spans[0].spans[0].attributes
    assert attribute.value.array_value.values[0].string_value == 'before [REDACTED] after'
    assert b'hidden-value' not in request.SerializeToString()
    assert runtime.shutdown()


def test_encrypted_reasoning_carrier_is_removed_from_array_before_serialization(trace_sdk):
    encode_spans, TracerProvider, SimpleSpanProcessor, InMemorySpanExporter = trace_sdk
    exporter = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    config = ConfigResult({'TRACE_MODE': 'OTEL', 'TRACELOOP_TRACE_CONTENT': True},
        {'TRACELOOP_TRACE_CONTENT': ResolvedValue(True, 'cli', True)}, (), (), ())
    runtime = TraceRuntime(config, factory=lambda *args: (provider, provider.get_tracer('test')))
    asyncio.run(runtime.astart())

    with runtime.span({'payload': [{'type': 'redacted_thinking', 'text': 'hidden-thought'},
                                   {'type': 'text', 'text': 'visible'}]}):
        pass

    request = encode_spans(exporter.get_finished_spans())
    attribute, = request.resource_spans[0].scope_spans[0].spans[0].attributes
    assert attribute.value.string_value == '[{"type": "text", "text": "visible"}]'
    assert b'hidden-thought' not in request.SerializeToString()
    assert runtime.shutdown()
