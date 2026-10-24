"""The generic consumer has no transport-specific branch."""
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import replace
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
import time

import pytest
from jsonschema import Draft202012Validator
from referencing.exceptions import Unresolvable

from api_agent.providers.tools import PreparedCall, ToolResult, ToolSpec, TOOL_SCHEMA_REGISTRY, canonical
from api_agent.workflows.tool_gate import ToolGate, ToolGateError, VerifiedCall


SCHEMA = {'type': 'object', 'properties': {'query': {'type': 'string'}},
          'required': ['query'], 'additionalProperties': False}
OUTPUT = {'type': ['object', 'null']}


class Owner:
    def __init__(self, kind):
        self.kind = kind
        self.calls = []
        self.spec = ToolSpec('lookup', 'Read a record', SCHEMA, OUTPUT, sha256(kind.encode()).hexdigest())

    async def describe(self):
        self.calls.append('describe')
        return self.spec

    async def validate(self, prepared):
        self.calls.append('validate')
        if prepared.arguments['query'] == 'invalid':
            raise ValueError('Invalid query')

    async def invoke(self, prepared):
        self.calls.append('invoke')
        return ToolResult('success', {'source': self.kind})

    async def aclose(self):
        self.calls.append('close')


async def consume(owner, arguments=None):
    gate = ToolGate(owner, invocation_id='turn-1')
    prepared = await gate.prepare(arguments or {'query': 'read'})
    checked = await gate.verify(prepared, relevance=lambda _: {'relevant': True, 'grounded': True})
    result = await gate.execute(checked)
    return result, gate, checked


@pytest.mark.parametrize('kind', ['graphql', 'rest'])
def test_same_consumer_uses_graphql_or_rest_owner(kind):
    owner = Owner(kind)
    result, _, _ = asyncio.run(consume(owner))
    assert result.data == {'source': kind}
    assert owner.calls == ['describe', 'validate', 'invoke']


def test_same_consumer_discovers_distinct_rest_schema_without_graphql_branch():
    class RestOwner(Owner):
        def __init__(self):
            super().__init__('rest')
            self.spec = ToolSpec('rest.read', 'Read a resource',
                {'type': 'object', 'properties': {'path': {'type': 'string'}},
                 'required': ['path'], 'additionalProperties': False},
                {'type': 'object'}, sha256(b'rest-schema').hexdigest())
        async def validate(self, prepared):
            self.calls.append('validate')
            assert prepared.arguments == {'path': '/items'}
        async def invoke(self, prepared):
            self.calls.append('invoke')
            return ToolResult('success', {'items': ['Ada']})
    owner = RestOwner()
    result, _, _ = asyncio.run(consume(owner, {'path': '/items'}))
    assert result.data == {'items': ['Ada']}
    assert owner.calls == ['describe', 'validate', 'invoke']


def test_invalid_arguments_never_reach_owner_validation_or_execution():
    owner = Owner('rest')
    gate = ToolGate(owner, invocation_id='turn-1')
    with pytest.raises(ToolGateError, match='arguments'):
        asyncio.run(gate.prepare({'query': 'read', 'extra': True}))
    assert owner.calls == ['describe']


def test_owner_rejection_precedes_relevance_and_execution():
    owner = Owner('graphql')
    gate = ToolGate(owner, invocation_id='turn-1')
    async def run():
        prepared = await gate.prepare({'query': 'invalid'})
        with pytest.raises(ValueError, match='Invalid query'):
            await gate.verify(prepared, relevance=lambda _: pytest.fail('Relevance ran'))
    asyncio.run(run())
    assert owner.calls == ['describe', 'validate']


@pytest.mark.parametrize('verdict', [
    {'relevant': False, 'grounded': True},
    {'relevant': True, 'grounded': False},
])
def test_relevance_and_grounding_block_execution(verdict):
    owner = Owner('rest')
    gate = ToolGate(owner, invocation_id='turn-1')
    async def run():
        prepared = await gate.prepare({'query': 'read'})
        with pytest.raises(ToolGateError, match='relevance or grounding'):
            await gate.verify(prepared, relevance=lambda _: verdict)
    asyncio.run(run())
    assert owner.calls == ['describe', 'validate']


def test_changed_arguments_and_invocation_cannot_reuse_validation():
    owner = Owner('rest')
    gate = ToolGate(owner, invocation_id='turn-1')
    async def run():
        prepared = await gate.prepare({'query': 'read'})
        with pytest.raises(ToolGateError, match='arguments changed'):
            await gate.verify(replace(prepared, arguments={'query': 'other'}),
                              relevance=lambda _: {'relevant': True, 'grounded': True})
        with pytest.raises(ToolGateError, match='invocation changed'):
            await gate.verify(replace(prepared, invocation_id='turn-2'),
                              relevance=lambda _: {'relevant': True, 'grounded': True})
    asyncio.run(run())
    assert owner.calls == ['describe']


def test_grant_is_single_use_and_gate_local():
    owner = Owner('rest')
    result, gate, checked = asyncio.run(consume(owner))
    assert result.status == 'success'
    with pytest.raises(ToolGateError, match='already used'):
        asyncio.run(gate.execute(checked))
    other = ToolGate(owner, invocation_id='turn-1')
    with pytest.raises(ToolGateError, match='missing'):
        asyncio.run(other.execute(checked))
    assert owner.calls.count('invoke') == 1


@pytest.mark.parametrize('value', [float('nan'), object(), tuple([1]), 'x' * 10])
def test_canonical_rejects_non_json_or_oversize_values(value):
    with pytest.raises(ValueError, match='bounded JSON'):
        canonical(value, 2)


@pytest.mark.parametrize('change', [
    {'name': ''}, {'description': ''}, {'schema_fingerprint': 'wrong'},
    {'input_schema': []}, {'output_schema': []},
    {'input_schema': {'type': 'unrecognized'}},
])
def test_invalid_tool_spec_is_rejected(change):
    values = dict(name='lookup', description='Read', input_schema=SCHEMA,
                  output_schema=OUTPUT, schema_fingerprint=sha256(b'x').hexdigest())
    values.update(change)
    with pytest.raises(ValueError):
        ToolSpec(**values)


@pytest.mark.parametrize('schema_field', ['input_schema', 'output_schema'])
def test_stalled_remote_schema_reference_is_rejected_before_validation(schema_field):
    entered, release = Event(), Event()

    class StalledSchema(BaseHTTPRequestHandler):
        def do_GET(self):
            entered.set()
            release.wait(timeout=2)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), StalledSchema)
    server.timeout = .1
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    schema = {'$ref': f'http://127.0.0.1:{server.server_port}/stalled'}
    values = dict(name='lookup', description='Read', input_schema=SCHEMA,
                  output_schema=OUTPUT, schema_fingerprint=sha256(b'x').hexdigest())
    values[schema_field] = schema
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(ToolSpec, **values)
            with pytest.raises(ValueError, match='nonlocal reference'):
                future.result(timeout=.5)
        assert not entered.is_set()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('reference', [
    {'$id': 'http://example.invalid/schema'},
    {'$dynamicRef': 'http://example.invalid/schema'},
    {'$recursiveRef': 'http://example.invalid/schema'},
])
def test_tool_spec_rejects_other_reference_redirects(reference):
    with pytest.raises(ValueError, match='unsupported reference'):
        ToolSpec('lookup', 'Read', reference, OUTPUT, sha256(b'x').hexdigest())


@pytest.mark.parametrize('schema_field', ['input_schema', 'output_schema'])
@pytest.mark.parametrize('reserved', ['$id', '$ref', '$dynamicRef', '$recursiveRef'])
def test_reserved_property_names_are_data_not_schema_keywords(schema_field, reserved):
    schema = {'type': 'object', 'properties': {reserved: {'type': 'string'}}}
    values = dict(name='lookup', description='Read', input_schema=SCHEMA,
                  output_schema=OUTPUT, schema_fingerprint=sha256(b'x').hexdigest())
    values[schema_field] = schema

    spec = ToolSpec(**values)

    assert Draft202012Validator(getattr(spec, schema_field)).is_valid({reserved: 'value'})


@pytest.mark.parametrize('schema_field', ['input_schema', 'output_schema'])
def test_reserved_definition_name_and_literal_data_are_not_references(schema_field):
    literal = {'$ref': 'http://example.invalid/literal',
               '$id': 'http://example.invalid/id',
               '$dynamicRef': 'http://example.invalid/dynamic',
               '$recursiveRef': 'http://example.invalid/recursive'}
    schema = {'$defs': {'$id': {'const': literal}},
              'type': 'object',
              'properties': {
                  'constant': {'$ref': '#/$defs/$id'},
                  'choice': {'enum': [literal]},
                  'suggested': {'type': 'object', 'default': literal}}}
    values = dict(name='lookup', description='Read', input_schema=SCHEMA,
                  output_schema=OUTPUT, schema_fingerprint=sha256(b'x').hexdigest())
    values[schema_field] = schema

    spec = ToolSpec(**values)

    assert Draft202012Validator(getattr(spec, schema_field)).is_valid(
        {'constant': literal, 'choice': literal})


@pytest.mark.parametrize('schema_field', ['input_schema', 'output_schema'])
@pytest.mark.parametrize('subschema', [
    {'additionalProperties': {'$ref': 'http://example.invalid/schema'}},
    {'items': {'$ref': 'http://example.invalid/schema'}},
    {'prefixItems': [{'$ref': 'http://example.invalid/schema'}]},
    {'not': {'$ref': 'http://example.invalid/schema'}},
    {'allOf': [{'$ref': 'http://example.invalid/schema'}]},
    {'$defs': {'entry': {'$ref': 'http://example.invalid/schema'}}},
])
def test_actual_nested_remote_references_are_rejected(schema_field, subschema):
    values = dict(name='lookup', description='Read', input_schema=SCHEMA,
                  output_schema=OUTPUT, schema_fingerprint=sha256(b'x').hexdigest())
    values[schema_field] = subschema

    with pytest.raises(ValueError, match='nonlocal reference'):
        ToolSpec(**values)


def test_local_schema_reference_remains_usable():
    owner = Owner('rest')
    owner.spec = ToolSpec('lookup', 'Read',
        {'$defs': {'query': {'type': 'string'}}, 'type': 'object',
         'properties': {'query': {'$ref': '#/$defs/query'}}, 'required': ['query']},
        {'$defs': {'result': {'type': 'object'}}, '$ref': '#/$defs/result'},
        sha256(b'local').hexdigest())
    result, _, _ = asyncio.run(consume(owner))
    assert result.status == 'success'


def test_validation_registry_refuses_network_even_if_spec_admission_is_bypassed():
    validator = Draft202012Validator({'$ref': 'http://127.0.0.1:1/schema'},
        registry=TOOL_SCHEMA_REGISTRY)
    with pytest.raises(Unresolvable):
        validator.is_valid({})


@pytest.mark.parametrize('length', [100, 101, 200])
def test_gate_accepts_wire_valid_invocation_identity(length):
    gate = ToolGate(Owner('rest'), invocation_id='x' * length)
    assert gate.invocation_id == 'x' * length


def _expensive_local_schema(depth=13):
    definitions = {'D0': {'type': 'object'}}
    for index in range(1, depth + 1):
        definitions[f'D{index}'] = {'allOf': [
            {'$ref': f'#/$defs/D{index - 1}'},
            {'$ref': f'#/$defs/D{index - 1}'}]}
    return {'$defs': definitions, '$ref': f'#/$defs/D{depth}'}


@pytest.mark.parametrize('phase', ['prepare', 'execute'])
def test_expensive_local_references_obey_gate_timeout_and_keep_loop_responsive(phase, monkeypatch):
    owner = Owner('rest')
    expensive = _expensive_local_schema()
    assert len(canonical(expensive, 1024)) < 1024
    owner.spec = ToolSpec('lookup', 'Read',
        expensive if phase == 'prepare' else SCHEMA,
        expensive if phase == 'execute' else OUTPUT,
        sha256(b'expensive').hexdigest())
    real_spawn = asyncio.create_subprocess_exec
    processes = []

    async def track_spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', track_spawn)

    async def run():
        gate = ToolGate(owner, invocation_id='turn-1', timeout=5)
        if phase == 'prepare':
            gate.timeout = .1
            timer = asyncio.create_task(asyncio.sleep(.005))
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                await gate.prepare({})
        else:
            # Prepare and verify with a normal input schema before the output
            # schema enters its own timed worker.
            prepared = await gate.prepare({'query': 'read'})
            checked = await gate.verify(prepared, relevance=lambda _: {'relevant': True, 'grounded': True})
            gate.timeout = .1
            timer = asyncio.create_task(asyncio.sleep(.005))
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                await gate.execute(checked)
        await timer
        assert time.monotonic() - started < .25
        assert processes and all(process.returncode is not None for process in processes)

    asyncio.run(run())


@pytest.mark.parametrize('values', [
    ('unknown', {}, (), None), ('success', None, (), None),
    ('success', {}, [], None), ('failure', None, (), '\n'),
])
def test_invalid_tool_result_cannot_be_rendered(values):
    with pytest.raises(ValueError):
        ToolResult(*values)


def test_result_payload_limit_rejects_unbounded_content():
    with pytest.raises(ValueError, match='bounded JSON'):
        ToolResult('success', {'text': 'x' * 262144})


def test_invalid_owner_spec_and_argument_shape_stop_before_validation():
    owner = Owner('rest')
    owner.describe = lambda: invalid_spec()
    async def invalid_spec():
        return None
    gate = ToolGate(owner, invocation_id='turn-1')
    with pytest.raises(ToolGateError, match='invalid specification'):
        asyncio.run(gate.prepare({'query': 'read'}))
    with pytest.raises(ToolGateError, match='invocation identity'):
        ToolGate(Owner('rest'), invocation_id='')
    good = ToolGate(Owner('rest'), invocation_id='turn-1')
    with pytest.raises(ToolGateError, match='arguments must be'):
        asyncio.run(good.prepare('read'))


def test_cached_catalog_and_async_relevance_follow_same_order():
    owner = Owner('rest')
    gate = ToolGate(owner, invocation_id='turn-1')
    async def relevance(prepared):
        assert owner.calls == ['describe', 'validate']
        return {'relevant': True, 'grounded': True}
    async def run():
        first = await gate.prepare({'query': 'read'})
        second = await gate.prepare({'query': 'read'})
        assert first.digest == second.digest
        await gate.verify(second, relevance=relevance)
    asyncio.run(run())
    assert owner.calls == ['describe', 'validate']


def test_forged_selection_or_missing_grant_cannot_execute():
    owner = Owner('rest')
    gate = ToolGate(owner, invocation_id='turn-1')
    prepared = asyncio.run(gate.prepare({'query': 'read'}))
    with pytest.raises(ToolGateError, match='selection changed'):
        asyncio.run(gate.verify('read', relevance=lambda _: {'relevant': True, 'grounded': True}))
    with pytest.raises(ToolGateError, match='stale'):
        asyncio.run(gate.execute(VerifiedCall(prepared, gate._id)))
    assert owner.calls == ['describe']


def test_candidate_mutated_after_relevance_cannot_execute():
    owner = Owner('rest')
    gate = ToolGate(owner, invocation_id='turn-1')
    async def run():
        arguments = {'query': 'read'}
        prepared = await gate.prepare(arguments)
        verified = await gate.verify(prepared, relevance=lambda _: {'relevant': True, 'grounded': True})
        arguments['query'] = 'different'
        with pytest.raises(ToolGateError, match='arguments changed'):
            await gate.execute(verified)
    asyncio.run(run())
    assert owner.calls == ['describe', 'validate']


def test_one_invocation_cannot_execute_distinct_verified_candidates_concurrently():
    owner = Owner('rest')
    entered, release = asyncio.Event(), asyncio.Event()
    async def invoke(prepared):
        owner.calls.append(prepared.arguments['query'])
        entered.set()
        await release.wait()
        return ToolResult('success', {'query': prepared.arguments['query']})
    owner.invoke = invoke
    async def run():
        gate = ToolGate(owner, invocation_id='turn-1')
        first = await gate.prepare({'query': 'first'})
        second = await gate.prepare({'query': 'second'})
        approve = lambda _: {'relevant': True, 'grounded': True}
        verified_first = await gate.verify(first, relevance=approve)
        verified_second = await gate.verify(second, relevance=approve)
        pending = asyncio.create_task(gate.execute(verified_first))
        await entered.wait()
        with pytest.raises(ToolGateError, match='already used'):
            await gate.execute(verified_second)
        release.set()
        assert (await pending).data == {'query': 'first'}
    asyncio.run(run())
    assert owner.calls == ['describe', 'validate', 'validate', 'first']


def test_invalid_owner_protocol_is_rejected_before_side_effects():
    with pytest.raises(ToolGateError, match='Invalid tool owner'):
        ToolGate(object(), invocation_id='turn-1')


@pytest.mark.parametrize('result', [None, ToolResult('success', 'wrong')])
def test_bad_owner_result_fails_closed_after_exactly_one_invoke(result):
    owner = Owner('rest')
    async def invoke(prepared):
        owner.calls.append('invoke')
        return result
    owner.invoke = invoke
    async def run():
        gate = ToolGate(owner, invocation_id='turn-1')
        prepared = await gate.prepare({'query': 'read'})
        checked = await gate.verify(prepared, relevance=lambda _: {'relevant': True, 'grounded': True})
        with pytest.raises(ToolGateError):
            await gate.execute(checked)
    asyncio.run(run())
    assert owner.calls == ['describe', 'validate', 'invoke']
