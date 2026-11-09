"""Scenario execution for artifact acceptance, run in a fresh interpreter.

Source mode builds the engine from declarations exactly as existing callers do.
Compiled mode uses only ``artifact_acceptance_adapter.load``. Both emit one
normalized JSON trace so the parent test can compare them event for event.
"""
import asyncio
import importlib
import json
from pathlib import Path
import re

_VOLATILE = frozenset(('scope_id', 'parent_id', 'visit_id', 'event_id', 'turn_id', 'attempt_id',
                       'run_id', 'span_id', 'trace_id', 'elapsed', 'duration', 'started', 'stopped',
                       'timestamp', 'run_ids', 'parent_ids', 'effective_timeout'))
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_ADDRESS = re.compile(r' at 0x[0-9a-f]+')
V3_INITIAL = {'messages': [], 'mode': 'ok', 'attempts': 0, 'feedback': '', 'trail': [], 'outcome': ''}
V3_NODES = frozenset(('intake', 'review', 'work', 'finish'))


def normalize(value):
    if isinstance(value, dict):
        return {key: '<volatile>' if key in _VOLATILE or key.endswith('_ms') else normalize(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    if isinstance(value, str):
        return _ADDRESS.sub(' at <addr>', _UUID.sub('<uuid>', value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return normalize(repr(value))


def _message(message):
    return [type(message).__name__, message.content, message.id]


def _state(state):
    return {key: [_message(item) for item in value] if key == 'messages' else value
            for key, value in state.items()}


def _error(error):
    return [type(error).__name__, str(error)]


class SourceEngine:
    def __init__(self, spec):
        entry, base, pack = (Path(spec[key]) for key in ('entry', 'base_root', 'pack_root'))
        entry_root = spec.get('entry_root')
        if spec['corpus'] == 'v3':
            from api_agent.workflows.native import (load_native_bindings)
            from api_agent.workflows.definition import (load_definition)
            self.definition = load_definition(entry, base_root=base, pack_root=pack, entry_root=entry_root)
            self._catalog = load_native_bindings(self.definition)
        else:
            from api_agent.workflows.definition import load_definition
            self.definition = load_definition(entry, base_root=base, pack_root=pack, entry_root=entry_root)
        self.definition_id = self.definition.definition_id
        self.graph_id = self.definition.graph_id

    def workflow(self, **options):
        from api_agent.workflows.execution import (Workflow)
        return Workflow(self.definition, catalog=self._catalog, **options)

    def contrived(self, *, llm, trial, observe, config):
        from api_agent.workflows import contrived
        return contrived.build(Path(self._root()), llm=llm, selection='synthetic', trial=trial,
                               observe=observe, config=config)

    def _root(self):
        return self.definition.roots['base']

    def journal(self):
        from api_agent.workflows.journal import WorkflowJournal
        return WorkflowJournal(self.definition)

    def identity(self):
        return {'definition_id': self.definition_id, 'graph_id': self.graph_id,
                'topology': [[step.id, step.kind, step.parent] for step in self.definition.layout.steps],
                'tool_usages': sorted([usage.scope, usage.port, list(usage.operations)]
                                      for usage in self.definition.tool_usages),
                'resources': sorted([item.kind, item.logical_path, item.sha256]
                                    for item in self.definition.resources)}


class CompiledEngine:
    def __init__(self, spec):
        adapter = importlib.import_module('artifact_acceptance_adapter')
        self._loaded = adapter.load(Path(spec['plan']).read_bytes())
        self.definition_id = self._loaded.definition_id
        self.graph_id = self._loaded.graph_id
        self._schema_path = spec.get('output_schema_path')

    def workflow(self, **options):
        return self._loaded.workflow(**options)

    def contrived(self, *, llm, trial, observe, config):
        schema = json.loads(self._loaded.resource(self._schema_path))
        services = dict(llm=llm, selection='synthetic', trial=trial, output_schema=schema, observe=observe,
                        llm_timeout=(config or {}).get('WORKFLOW_LLM_TIMEOUT_SECONDS', 120))
        return self._loaded.workflow(config=config, services=services)

    def journal(self):
        return self._loaded.journal()

    def identity(self):
        return {'definition_id': self.definition_id, 'graph_id': self.graph_id,
                'topology': [list(row) for row in self._loaded.topology()],
                'tool_usages': sorted([scope, port, list(operations)]
                                      for scope, port, operations in self._loaded.tool_usages()),
                'resources': sorted(list(row) for row in self._loaded.resources())}


def _reducers():
    from langgraph.graph.message import add_messages
    return {'messages': add_messages}


async def _v3_run(engine, update, thread_id, services=None):
    observed, journal = [], engine.journal()
    workflow = engine.workflow(reducers=_reducers())
    try:
        result = {'state': _state(await workflow.arun(
            update, thread_id=thread_id, services={'observe': observed.append, **(services or {})},
            journal=journal))}
    except Exception as error:
        result = {'error': _error(error)}
    try:
        envelope = journal.finalize()
        result['journal'] = {'completeness': envelope['completeness'], 'events': envelope['events']}
    except Exception as error:
        result['journal'] = {'error': _error(error)}
    result['observed'] = observed
    return result


async def _v3_scenario(engine, scenario):
    if scenario == 'complete':
        return await _v3_run(engine, dict(V3_INITIAL), 'complete')
    if scenario == 'failure':
        return await _v3_run(engine, {**V3_INITIAL, 'mode': 'fail'}, 'failure')
    if scenario == 'fresh':
        workflow = engine.workflow(reducers=_reducers())
        first = await workflow.arun(dict(V3_INITIAL), thread_id='one')
        other = await workflow.arun(dict(V3_INITIAL), thread_id='two')
        again = await workflow.arun({'mode': 'ok'}, thread_id='one')
        return {'first': _state(first), 'other': _state(other), 'second_turn': _state(again)}
    if scenario == 'stream':
        workflow = engine.workflow(reducers=_reducers())
        events = [[event['event'], event['name']] async for event in workflow.astream_events(
            dict(V3_INITIAL), thread_id='stream') if event['event'] in ('on_chain_start', 'on_chain_end')
            and event['name'] in V3_NODES]
        return {'stream': events}
    if scenario == 'cancel':
        workflow, observed = engine.workflow(reducers=_reducers()), []
        gate, started = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(workflow.arun(
            {**V3_INITIAL, 'mode': 'block'}, thread_id='cancel',
            services={'observe': observed.append, 'gate': gate, 'started': started}))
        await started.wait()
        task.cancel()
        try:
            await task
            cancelled = False
        except asyncio.CancelledError:
            cancelled = True
        recovered = await workflow.arun(dict(V3_INITIAL), thread_id='cancel')
        return {'cancelled': cancelled, 'observed': observed, 'recovered': _state(recovered)}
    raise ValueError(scenario)


def _trial(route='skip', *, first_ready=True, second_ready=True):
    last = {'skip': '0', 'next_a': '5', 'next_b': 'a'}[route]
    return '00000000-0000-4000-8000-000000000' + ('0' if second_ready else '8') + ('0' if first_ready else '8') + last


def _reply(request, **changes):
    from api_agent.providers.llm import LLMResult, Message, TextContent
    metadata = next(json.loads(message.content[0].text) for message in reversed(request.payload.messages)
                    if message.role == 'user' and '"decisions"' in message.content[0].text)
    source = request.payload.messages[1].content[0].text
    value = {'original_input': source, 'final_output': source,
             'route': metadata['decisions']['route'], 'ready': metadata['decisions']['ready'], **changes}
    return LLMResult(Message('assistant', (TextContent(json.dumps(value)),)))


async def _v2_scenario(engine, scenario):
    requests, observed = [], []
    if scenario == 'repair':
        async def llm(request):
            requests.append(request)
            return _reply(request, **({'route': 'next_a'} if len(requests) == 1 else {}))
        trial, config = _trial('skip'), {'WORKFLOW_MAX_PASSES': 2}
    elif scenario == 'guard':
        async def llm(request):
            requests.append(request)
            return _reply(request)
        trial, config = _trial('next_a', first_ready=False, second_ready=True), None
    elif scenario == 'exhaust':
        async def llm(request):
            requests.append(request)
            return _reply(request, route='next_b')
        trial, config = _trial(), None
    else:
        raise ValueError(scenario)
    workflow = engine.contrived(llm=llm, trial=trial, observe=observed.append, config=config)
    try:
        result = {'output': await workflow.arun({'user_input': '  Original λ\n'})}
    except Exception as error:
        result = {'error': _error(error)}
    result['requests'] = [[[message.role, [getattr(part, 'text', repr(part)) for part in message.content]]
                           for message in request.payload.messages] for request in requests]
    result['observed'] = observed
    return result


def run(spec):
    """Run one scenario; rejection outcomes are data, not crashes."""
    if spec['scenario'] == 'load':
        adapter = importlib.import_module('artifact_acceptance_adapter')
        try:
            engine = CompiledEngine(spec)
        except Exception as error:
            return {'rejected': type(error).__name__,
                    'is_artifact_rejected': isinstance(error, adapter.ArtifactRejected), 'message': str(error)}
        return {'loaded': True, 'definition_id': engine.definition_id}
    engine = (CompiledEngine if spec['mode'] == 'compiled' else SourceEngine)(spec)
    if spec['scenario'] == 'identity':
        trace = engine.identity()
    elif spec['corpus'] == 'v3':
        trace = asyncio.run(_v3_scenario(engine, spec['scenario']))
    else:
        trace = asyncio.run(_v2_scenario(engine, spec['scenario']))
    return {'definition_id': engine.definition_id, 'graph_id': engine.graph_id, 'trace': normalize(trace)}
