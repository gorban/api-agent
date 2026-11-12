"""LangGraph compilation, owned nested pass budgets and strict node contracts."""
from .run_context import recording_enabled
from .run_context import current_structural_scope, structural_scope
import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextlib import nullcontext
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, replace
import inspect
import json
import logging
import os
import re
import time
from typing import Annotated, TypedDict, get_args, get_origin, get_type_hints
from typing_extensions import is_typeddict
from types import MappingProxyType
from threading import Lock
from langgraph.checkpoint.memory import InMemorySaver
from .model import LayoutError
from .model import NativeCatalog
from . import session as session_contract
from .prompt_slots import fill_slots, slot_text
from .configuration import resolve_timeouts, resolve_component_passes, resolve_state_limits, resolve_state_timeouts
from api_agent.runtime.tasks import settle
from api_agent.providers.progress import bind_timeout_budget
from uuid import uuid4

from jsonschema import Draft202012Validator
from langgraph.graph import END, START, StateGraph

from api_agent.providers.progress import component_timeout
from api_agent.runtime.tasks import offload
from .predicates import (component_controller, component_decision, edge_expiry, edge_source, entry_source, loop_edges,
                         MissingPredicateField, native_condition_id, parse_predicate, predicate_fields, state_predicate_fields,
                         unbounded_edge_message)


_PROMPT_SLOT = re.compile(r'\{\{([^{}\n]{1,64})\}\}')


class WorkflowError(RuntimeError):
    pass


class _SchemaRejection(WorkflowError):
    def __init__(self, node, direction, errors, schema, value):
        self.errors = errors
        path = _schema_facts(schema, value, errors)[0]['path']
        super().__init__(f'Node {node} returned invalid {direction} structure at {path}')


class CandidateCorrection(WorkflowError):
    """Explicit rejection of current-node candidate data, never execution failure.

    Authors supply safe facts, not exception/provider text, and mark arbitrary
    effects before dispatch through NodeContext.mark_effect().
    """
    def __init__(self, feedback, *, rejected=None):
        if type(feedback) is not dict or not feedback:
            raise ValueError('Candidate correction requires feedback facts')
        try:
            encoded = json.dumps({'feedback': feedback, 'rejected': rejected}, allow_nan=False)
            value = json.loads(encoded)
            if value != {'feedback': feedback, 'rejected': rejected} or len(encoded.encode()) > 1024 * 1024:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError('Invalid candidate correction data') from None
        self.feedback, self.rejected = value['feedback'], value['rejected']
        super().__init__('Candidate requires correction')


def _schema_facts(schema, value, errors):
    numeric = {'minLength', 'maxLength', 'minItems', 'maxItems', 'minProperties',
               'maxProperties', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf'}
    facts = []
    for error in errors[:16]:
        rule, path = schema, []
        candidate = error.instance
        for part in list(error.path)[:16]:
            if type(part) is str and part in rule.get('properties', {}):
                path.append(part)
                rule = rule['properties'][part]
            elif type(part) is int and part >= 0:
                path.append(part)
                rule = rule.get('items', {})
            else:
                path.append('*')
                rule = {}
        fact = {'path': path, 'keyword': error.validator}
        if error.validator in numeric and type(error.validator_value) in (int, float):
            fact['constraint'] = error.validator_value
            if type(candidate) in (str, list, dict):
                fact['actual'] = len(candidate)
        facts.append(fact)
    return facts


def scrub_correction(feedback, *, boundary=None):
    """Bound JSON facts, sharing active authority or an explicit empty authority."""
    from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
    admitted = CandidateCorrection(feedback if feedback != {} else {'reason': 'candidate_rejected'}).feedback
    if feedback == {}:
        admitted = {}
    if boundary is None:
        boundary = EmissionBoundary(ContentPolicy(True))
    if type(boundary) is not EmissionBoundary:
        raise WorkflowError('Correction credential authority unavailable')
    lease = boundary.begin_evidence_lease()
    try:
        return boundary.for_policy(ContentPolicy(True)).content_export(admitted)
    finally:
        lease.release()


async def project_correction(feedback, *, services=None):
    """Borrow the live task-local credential authority even without a journal."""
    if services is None:
        from .attribution import current_tool_capture_services
        services = current_tool_capture_services() or {}
    capability = services.get('capture_authority')
    if capability is not None:
        try:
            async with component_timeout('Workflow correction authority'):
                async with capability() as boundary:
                    return scrub_correction(feedback, boundary=boundary)
        except TimeoutError:
            raise
        except Exception:
            raise WorkflowError('Correction credential authority unavailable') from None
    return scrub_correction(feedback, boundary=services.get('emissions'))


@dataclass
class _RepairFrame:
    workflow: object
    component: str
    maximum: int
    budget: object
    attempt: int = 1
    guard_attempt: int = 1
    effect: bool = False


_REPAIRS = ContextVar('workflow_repair_frames', default=())
_ACTIVE_NODES = ContextVar('workflow_repair_node_effect_owners', default=())


@contextmanager
def _node_effect_owner(context):
    token = _ACTIVE_NODES.set((*_ACTIVE_NODES.get(), context))
    try:
        yield
    finally:
        _ACTIVE_NODES.reset(token)


def mark_active_effect():
    """Record a generic dispatch on its active native-node ownership chain."""
    contexts = _ACTIVE_NODES.get()
    if contexts:
        contexts[-1].mark_effect()


@asynccontextmanager
async def _repair_owner(frame):
    token = _REPAIRS.set((*_REPAIRS.get(), frame) if frame is not None else _REPAIRS.get())
    try:
        yield
    finally:
        _REPAIRS.reset(token)


_CURRENT_JOURNAL = ContextVar('workflow_journal', default=None)


@dataclass(frozen=True)
class NodeFunction:
    invoke: object
    kind: str = 'deterministic'

    # Synchronous functions must finish in bounded time: cancellation drains
    # their owned thread. Async functions must honor cancellation; a swallowed
    # timeout still fails deadline validation before any result is accepted.


@dataclass(frozen=True)
class GuardResult:
    accepted: bool
    feedback: dict

    @classmethod
    def from_output(cls, value):
        if type(value) is not dict or type(value.get('accepted')) is not bool or type(value.get('feedback')) is not dict:
            raise WorkflowError('Guard must return accepted:boolean and feedback:object')
        return cls(value['accepted'], value['feedback'])


@dataclass(frozen=True)
class NodeContext:
    node_id: str
    ancestry: tuple
    prompts: tuple[str, ...]
    services: dict
    visit_id: str | None = None
    attempt_id: str | None = None
    response_contract: dict | None = None
    correction: dict | None = None
    rejected: object = None
    correction_attempt: int = 0
    guard_attempt: int = 1
    _repair_limit: tuple | None = None
    _effects: list = field(default_factory=list, repr=False, compare=False)
    request: object = None
    resources: object = None
    prompt_texts: object = field(default_factory=lambda: MappingProxyType({}))
    _tools: object = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        from api_agent.tooling.runtime import capture_node_tools
        object.__setattr__(self, '_tools', capture_node_tools(self.node_id))

    def mark_effect(self):
        """Mark arbitrary native dispatch before awaiting it; replay is unsafe."""
        for context in (*_ACTIVE_NODES.get(), self):
            if not context._effects:
                context._effects.append(True)
        for frame in _REPAIRS.get():
            frame.effect = True

    def _dispatched(self):
        invocation = self._tools._invocation if self._tools is not None else None
        return bool(self._effects or (invocation is not None and self.node_id in getattr(invocation, 'effects', ())))

    def response_request(self, messages, *, tools=(), generation=None, strict=True):
        """Explicitly bind a request to this node's generated-response contract."""
        from api_agent.providers.llm import CompletionRequest, GenerationOptions, ResponsePolicy, StructuredOutput
        row = self.response_contract
        if row is None or row.get('Output mode', '-') == '-':
            raise WorkflowError('Node has no declared generated-response contract')
        if type(strict) is not bool or (row['Output mode'] == 'text' and not strict):
            raise WorkflowError('Invalid generated-response strictness')
        schema = None
        if row['Output mode'] != 'text':
            schema = StructuredOutput(self.node_id, json.dumps(row['Response schema value'], default=dict), strict=strict)
        return CompletionRequest(tuple(messages), tuple(tools), generation or GenerationOptions(),
                                 response=ResponsePolicy(row['Output mode'], row['Fallback'], schema))

    @property
    def tools(self):
        if self._tools is None:
            raise ValueError('Node tools require an active workflow invocation')
        self._tools._invocation.check()
        return self._tools

    def tool(self, port):
        # Compatibility for fixed-operation tools; only this node's declared ports resolve.
        invocation = self._tools._invocation if self._tools is not None else None
        if invocation is None or invocation.resolver is None:
            raise WorkflowError('Workflow has no admitted tool bindings')
        from api_agent.providers.contracts import ContractError
        invocation.check()
        if not any(use.scope_kind == 'node' and use.scope == self.node_id and use.port == port
                   for use in getattr(invocation.definition, 'tool_usages', ())):
            raise ContractError('Node tool port is not declared')
        borrowed = invocation.resolver.for_node(self.node_id).borrow(port)
        timeout = invocation.timeouts.get(f'{self.node_id}.{port}')
        context = self
        class EffectTool:
            def __getattr__(self, name):
                if name in ('call', 'view', 'owner'):
                    raise ContractError(f'Tool attribute {name} is not available to nodes')
                return getattr(borrowed, name)
            async def invoke(self, prepared):
                context.mark_effect()
                async with component_timeout('Tool execution', timeout):
                    return await borrowed.invoke(prepared)
        return EffectTool()

    def record_call(self, *, usage=None, selection=None, elapsed=None, outcome='completed', call_id=None):
        """Attribute one completed provider/tool leaf call to this node visit."""
        journal = _CURRENT_JOURNAL.get() or self.services.get('journal')
        if journal is None:
            return None
        return journal.record_call(visit_id=self.visit_id, attempt_id=self.attempt_id,
                                   usage=usage, selection=selection, elapsed=elapsed,
                                   outcome=outcome, call_id=call_id)

    @property
    def attempt(self):
        return self._repair_limit[0] if self._repair_limit else self.ancestry[-1][1] if self.ancestry else 1

    @property
    def max_passes(self):
        return self._repair_limit[1] if self._repair_limit else self.ancestry[-1][2] if self.ancestry else 1


class _SessionState(TypedDict):
    session: dict


def _thaw(value):
    """Copy an admitted frozen declaration value back to plain JSON containers."""
    if isinstance(value, (dict, MappingProxyType)):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _latest_turn_value(runs, field):
    """The value of ``field`` written by the most recently completed run that wrote it.

    ``runs`` lists ``(node, output)`` for every completed run of the turn in completion
    order, without the runs a rejected guarded pass discarded. A run that omits the field
    writes nothing, and a run an engine re-entry superseded still counts.
    """
    for _, output in reversed(runs):
        if field in output:
            return True, output[field]
    return False, None


def _started(starts, step_id):
    """Count one more start of ``step_id`` in this turn, for ``<step>.attempt`` conditions."""
    return {**starts, step_id: starts.get(step_id, 0) + 1}


def _entered(state, step_id):
    """``state`` as ``step_id`` starts: one more start counted and its expiry flag cleared."""
    return dict(state, starts=_started(state.get('starts', {}), step_id),
                expired={key: value for key, value in state.get('expired', {}).items() if key != step_id})


class _SessionCommit:
    """One turn's session write, and the means to take it back.

    ``merged`` is set when the graph finishes. The write belongs to the graph scope,
    so a slow or failing store fails the turn; ``restore`` undoes it if anything after the
    scope (journal finalization, evidence publication) fails the same turn.
    """

    def __init__(self, session, config, fields):
        self.session, self.config, self.fields = session, config, fields
        self.before, self.merged, self.saved = {}, None, False

    async def load(self):
        """The stored session as this turn starts, minus fields the Session table dropped."""
        snapshot = await self.session.aget_state(self.config)
        stored = snapshot.values.get('session', {})
        self.before = deepcopy({field: value for field, value in stored.items() if field in self.fields})
        return deepcopy(self.before)

    async def save(self):
        await self.session.ainvoke({'session': self.merged}, self.config)
        self.saved = True

    async def restore(self):
        if self.saved:
            self.saved = False
            await self.session.ainvoke({'session': self.before}, self.config)


class _State(TypedDict):
    values: dict
    ancestry: tuple
    feedback: dict
    previous: dict
    prompts: dict
    visit_id: str | None
    attempt_id: str | None
    starts: dict
    expired: dict






def _source_manifest(definition, packaged_proof=None):
    """Produce source or compiled-plan provenance before native effects."""
    if getattr(definition, 'origin', 'source') == 'compiled':
        from .artifact import compiled_provenance
        return compiled_provenance(definition)
    from .provenance import verified_documents
    return verified_documents(definition, packaged_proof=packaged_proof)


async def _project_capture(journal, projection, direction, visit_id, *args):
    """An optional observation projection cannot fail application execution."""
    try:
        values = projection(direction, *args)
        if inspect.isawaitable(values):
            values = await values
        if type(values) is not dict:
            raise TypeError('I/O projection must return a dictionary')
        return values
    except Exception:
        logging.getLogger(__name__).warning('Workflow I/O capture unavailable: projection failed')
        journal.observe(dict(event='capture', visit_id=visit_id, direction=direction,
                             reason='unavailable'))
        return None


class Workflow:
    """The admitted LangGraph engine for bindings and typed continuation state."""

    def __init__(self, layout, registry=None, conditions=None, *, catalog=None, reducers=None,
                 config=None, services=None, journal=None, checkpointer=None):
        if not hasattr(layout, 'definition_id'):
            raise LayoutError('Execution requires an admitted workflow definition')
        self.typed_state = getattr(layout, 'schema_source', None) is not None
        if self.typed_state:
            if conditions is not None:
                raise LayoutError('Typed state uses declared routers, not external conditions')
            self._init_state(layout, catalog=catalog, reducers=reducers or {}, services=services,
                             journal=journal, checkpointer=checkpointer, config=config)
            self.layout = layout.layout
            self.component_passes = {}
            self.session_rows, self.session_commit = (), ('passed',)
        else:
            sessions = bool(layout.layout.tables.get('Session'))
            if catalog is not None or reducers or (checkpointer is not None and not sessions):
                raise LayoutError('Checkpoint state requires a declared State or Session contract')
            self._init_bindings(layout, registry or {}, conditions, config=config, services=services)
        self._request = ContextVar('workflow_request', default=None)
        self._services = ContextVar('workflow_services', default=self.services)
        self._capture_services_context = ContextVar('workflow_capture_services', default=self.services)
        self._journal = ContextVar('workflow_journal', default=journal)
        self._turn_state = ContextVar('workflow_turn', default=None)
        self.journal = journal
        self._active_threads = set()
        self._thread_guard = Lock()
        self._parents = {step.id: step.parent for step in self.layout.steps}
        saver = (checkpointer if checkpointer is not None else InMemorySaver()) if self.typed_state else False
        self.graph = self._compile(None, checkpointer=saver)
        self._session = self._compile_session(checkpointer) if self.session_rows else None

    def _init_bindings(self, layout, registry, conditions=None, *, config=None, services=None):
        self.definition = layout
        layout = self.definition.layout
        self.layout, self.registry = layout, registry
        self.conditions = conditions or {}
        self.config, self.services = config or {}, services or {}
        self._predicates = {}
        self.functions = {row['ID']: row for row in layout.tables['Functions']}
        self.session_rows = tuple(layout.tables.get('Session', ()))
        self.session_commit = session_contract.commit_outcomes(layout.tables)
        self.components = {row['ID']: row for row in layout.tables['Components']}
        self.timeouts, self.timeout_keys = resolve_timeouts(layout, self.config, self.definition)
        self.limit_values = resolve_state_limits(self.definition, self.config)
        self.component_passes, self.pass_constants = resolve_component_passes(layout, self.config)
        steps = {step.id: step for step in layout.steps}
        self.definition.verify_sources()
        for row in self.functions.values():
            function = registry.get(row['Registry'])
            if (not isinstance(function, NodeFunction) or function.kind not in ('llm', 'deterministic')
                    or not callable(function.invoke)
                    or function.kind != row['Kind']):
                layout.row_error(row, 'unknown or invalid registered node function')
            if 'Output mode' in row and ((function.kind == 'deterministic') != (row['Output mode'] == '-')):
                layout.row_error(row, 'response declaration must match registered node kind')
            rejected = self.definition.native_rejection(function.invoke, steps[row['ID']])
            if rejected is not None:
                layout.row_error(row, rejected)
        for row in (*layout.tables['Edges'], *layout.tables['Prompts']):
            when = row['When']
            if when == 'always' or (when == 'otherwise' and row in layout.tables['Edges']):
                continue
            if native_condition_id(when) and callable(self.conditions.get(when)):
                continue
            if row in layout.tables['Prompts']:
                layout.row_error(row, 'unknown registered condition')
            source, entering = edge_source(row)
            try:
                self._predicates[source, when] = parse_predicate(when, predicate_fields(layout, row['From'], entering))
            except ValueError as error:
                layout.row_error(row, str(error))
        self._recursion_limit = self._loop_recursion_limit(layout)
        self._expiry = {}
        for source, when in self._predicates:
            if edge_expiry(when, source) is not None:
                self._expiry.setdefault(source, set()).add(when)
        self.schema = _State

    def _loop_recursion_limit(self, layout):
        """Supersteps one graph run may need: every step once, plus each bounded loop's re-entries.

        An edge bounded by ``target.attempt < LIMIT`` re-enters at most LIMIT times (one more
        for ``<=``), and a re-entry can re-run every step of the graph.
        """
        reentries = 0
        for row, bound in loop_edges(layout):
            if bound is None:
                layout.row_error(row, unbounded_edge_message(row['To']))
            reentries += self.limit_values[bound[0]] + bound[1]
        return len(layout.steps) * (1 + reentries) + 1


    def _init_state(self, definition, *, catalog, reducers, services=None, journal=None, checkpointer=None,
                 config=None):
        definition.verify_sources()
        if not isinstance(catalog, NativeCatalog) or catalog.definition_id != definition.definition_id:
            raise LayoutError('catalog does not match admitted definition')
        self.definition = definition
        self.services = MappingProxyType(dict(services or {}))
        self.config = MappingProxyType(dict(config or {}))
        requested_timeouts, requested_keys = resolve_state_timeouts(definition, self.config)
        if requested_timeouts != catalog.timeout_values or requested_keys != catalog.timeout_keys:
            raise LayoutError('catalog timeout configuration does not match selected values')
        self.timeouts = catalog.timeout_values
        self.timeout_keys = catalog.timeout_keys
        self.journal = journal
        self.catalog = catalog
        self.reducers = reducers
        schema = self._binding(definition.schema_source, definition.schema_name)
        if not is_typeddict(schema):
            raise LayoutError('state registry must name a TypedDict')
        hints = get_type_hints(schema, include_extras=True)
        for key, hint in hints.items():
            if get_origin(hint) is Annotated:
                metadata = get_args(hint)[1:]
                if len(metadata) != 1 or reducers.get(key) is not metadata[0]:
                    raise LayoutError(f'unregistered state reducer: {key}')
        if any(key not in hints for key in reducers):
            raise LayoutError('reducer references undeclared state field')
        from .capture import annotation_schema
        self.capture_schema = {'type': 'object', 'properties': {
            key: annotation_schema(hint) for key, hint in hints.items()}}
        self._typed_functions = {row['ID']: row for row in getattr(definition, 'functions', ())}
        for row in self._typed_functions.values():
            unknown = {name for column in ('Input schema', 'Output schema')
                       for name in row[column].get('properties', {})} - set(hints)
            if unknown:
                raise LayoutError(f'{definition.entry}:{row["line"]}: Functions schema names no State field: {min(unknown)}')
        self.limit_values = resolve_state_limits(definition, self.config)
        self._expiry = {}
        for row in definition.timeouts:
            if 'Expired' in row:
                where = f'{row.get("_owner", definition.entry)}:{row["line"]}'
                for field, kind, article in ((row['Expired'], 'string', 'a string'),
                                             (row['Expirations'], 'integer', 'an integer')):
                    if self.capture_schema['properties'].get(field) != {'type': kind}:
                        raise LayoutError(f'{where}: expiry field {field} must be {article} State field')
                self._expiry[row['Owner']] = (row['Expired'], row['Expirations'])
        fields = state_predicate_fields(self.capture_schema['properties'], self.limit_values)
        self._state_predicates = {}
        for row in (*definition.edges, *getattr(definition, 'prompts', ())):
            when = row.get('When', 'always')
            if when in ('always', 'otherwise') or when in self._state_predicates:
                continue
            try:
                self._state_predicates[when] = parse_predicate(when, fields)
            except ValueError as error:
                raise LayoutError(f'{definition.entry}:{row["line"]}: {error}') from None
        self.schema = schema
    def _compile_session(self, checkpointer):
        """A one-node graph whose only job is to checkpoint ``session`` per thread."""
        graph = StateGraph(_SessionState)
        graph.add_node('commit', lambda state: {})
        graph.add_edge(START, 'commit')
        return graph.compile(checkpointer=checkpointer if checkpointer is not None else InMemorySaver())

    def _turn_payload(self, field):
        found, value = _latest_turn_value(self._turn_state.get()['runs'], field)
        return {field: value} if found else {}

    def _run_services(self):
        return self._services.get()

    def _capture_services(self):
        return self._capture_services_context.get()

    def _event(self, event, **values):
        journal = self._journal.get() or self._run_services().get('journal')
        if journal is not None:
            row = dict(event=event, **values)
            if event in ('scope_start', 'scope_end') and row.get('owner') == 'GRAPH' and row.get('kind') == 'graph':
                row['parent_id'] = None
            journal.observe(row)
        observer = self._run_services().get('observe')
        if observer is not None:
            observer(dict(event=event, **values))


    def _compile(self, parent, *, checkpointer=None):
        children = [item for item in self.layout.steps if item.parent == parent]
        graph = StateGraph(self.schema)
        for step in children:
            if step.kind == 'component':
                node = self._compile(step.id) if self.typed_state else self._component(step)
                if self.typed_state and step.id in self._expiry:
                    node = self._expiring_component(step.id, node)
            elif self.typed_state:
                row = next(row for row in self.definition.nodes if row['ID'] == step.id)
                function = self._binding(row['Source'], row['Registry'])
                if not callable(function):
                    raise LayoutError('node registry must be callable')
                node = self._state_node(row, function)
            else:
                node = self._node(step)
            graph.add_node(step.id, node)
        if self.typed_state and parent is not None:
            entering = [edge for edge in self.definition.edges
                        if edge['From'] == 'START' and self._parents[edge['To']] == parent]
            async def start_component(_state):
                await self._start_component(parent)
                if not entering:
                    self._event('edge', owner=parent, source='START', target=children[0].id, implicit=True)
                return {}
            graph.add_node('__entry', start_component)
            graph.add_edge(START, '__entry')
            if entering:
                graph.add_conditional_edges('__entry', self._conditional_route('START', entering, parent),
                                            {edge['To']: edge['To'] for edge in entering})
            else:
                graph.add_edge('__entry', children[0].id)
        entry = [] if self.typed_state else [row for row in self.layout.tables['Edges']
                                             if row['From'] == 'START' and self._parents[row['To']] == parent]
        def enter(state):
            if entry:
                chosen = self._choose(entry_source(parent), entry, state)
                self._event('edge', source='START', target=chosen['To'], owner=parent or 'GRAPH',
                            condition=chosen['When'], implicit=False, attempt_id=state.get('attempt_id'))
                return chosen['To']
            self._event('edge', source='START', target=children[0].id,
                        owner=parent or 'GRAPH', implicit=True,
                        attempt_id=None if self.typed_state else state.get('attempt_id'))
            return children[0].id
        if not (self.typed_state and parent is not None):
            graph.add_conditional_edges(START, enter, {row['To']: row['To'] for row in entry}
                                        if entry else {children[0].id: children[0].id})
        for index, step in enumerate(children):
            if self.typed_state:
                routes = [edge for edge in self.definition.edges if edge['From'] == step.id]
                if 'When' in routes[0]:
                    route = self._conditional_route(step.id, routes, parent)
                elif routes[0]['Router'] == '-':
                    route = self._static_route(step.id, routes[0]['To'], parent)
                else:
                    router = self._binding(routes[0]['Source'], routes[0]['Router'])
                    if not callable(router):
                        raise LayoutError('router registry must be callable')
                    route = self._route(step.id, router, routes, parent)
                graph.add_conditional_edges(step.id, route,
                    {edge['To']: END if edge['To'] == 'END' else edge['To'] for edge in routes})
                continue
            edges = [row for row in self.layout.tables['Edges'] if row['From'] == step.id]
            if not edges:
                target = children[index + 1].id if index + 1 < len(children) else 'END'
                def implicit_route(state, source=step.id, target=target):
                    if target == 'END':
                        self._exited(parent, source)
                    self._event('edge', source=source, target=target,
                                owner=parent or 'GRAPH', condition='always', implicit=True,
                                visit_id=state.get('visit_id'), attempt_id=state.get('attempt_id'))
                    return target
                graph.add_conditional_edges(step.id, implicit_route,
                                            {target: END if target == 'END' else target})
            else:
                def route(state, edges=edges, step=step):
                    chosen = self._choose(step.id, edges, state)
                    if chosen['To'] == 'END':
                        self._exited(parent, step.id)
                    self._event('edge', source=step.id, target=chosen['To'],
                                owner=parent or 'GRAPH', condition=chosen['When'], implicit=False,
                                visit_id=state.get('visit_id'), attempt_id=state.get('attempt_id'))
                    return chosen['To']
                graph.add_conditional_edges(step.id, route, {row['To']: END if row['To'] == 'END' else row['To'] for row in edges})
        return graph.compile(checkpointer=checkpointer if self.typed_state else False)

    @asynccontextmanager
    async def _scope(self, owner, **metadata):
        declared = self.timeouts.get(owner)
        async with component_timeout('Workflow ' + owner, declared) as budget:
            started = time.time() * 1000
            parent_id = current_structural_scope() if owner == 'GRAPH' and metadata.get('kind') == 'graph' else budget.parent_id
            common = dict(scope_id=budget.id, parent_id=parent_id, owner=owner,
                          started_ms=started, declared_timeout=declared,
                          effective_timeout=budget.timeout, timeout_key=self.timeout_keys.get(owner), **metadata)
            with structural_scope(budget.id):
                outcome = 'failed'
                expired = False
                try:
                    self._event('scope_start', **common)
                    yield budget
                    self._check_deadline(budget)
                    outcome = 'passed'
                except BaseException as error:
                    from api_agent.telemetry.outcomes import report_failure, OutcomeState
                    failed = report_failure(error,
                        observations=self._journal.get() or self._run_services().get('journal'),
                        scope_ids=(budget.id,), terminal=owner == 'GRAPH' and metadata.get('kind') == 'graph' and parent_id is None,
                        deadline_expired=budget.deadline is not None and time.monotonic() >= budget.deadline)
                    outcome = 'cancelled' if failed.state == OutcomeState.CANCELLED else 'failed'
                    expired = (isinstance(error, (asyncio.CancelledError, TimeoutError))
                               and owner in self._expiry and self._own_expiry(owner, budget))
                    raise
                finally:
                    try:
                        self._event('scope_end', **common, stopped_ms=time.time() * 1000,
                                    elapsed=time.monotonic() - budget.started, outcome=outcome,
                                    **({'reason': 'declared_timeout_expired'} if expired else {}))
                    except Exception:
                        if outcome == 'passed':
                            raise


    @staticmethod
    def _check_deadline(budget):
        if budget.remaining == 0:
            raise TimeoutError('Workflow component deadline expired')


    def _choose(self, source, edges, state):
        """The first edge that applies after ``source``; matching none, or several targets, is invalid."""
        choices = self._route_choices(source, edges, state)
        if len({row['To'] for row in choices}) != 1:
            raise WorkflowError('Routing requires exactly one matching condition or fallback')
        return choices[0]

    def _settle(self, component, values):
        """Keep the output of the child that exited ``component`` as its result; no output leaves none."""
        turn = self._turn_state.get()
        child = turn['exits'][component]
        output = turn['results'].get(child) if child in self.components else values.get(child)
        if output is None:
            turn['results'].pop(component, None)
        else:
            turn['results'][component] = deepcopy(output)

    def _exited(self, component, child):
        """Record which child of ``component`` took the edge to its ``END``."""
        if component is not None:
            self._turn_state.get()['exits'][component] = child

    def _route_choices(self, source, edges, state):
        """The edges that apply after ``source``: the matching ones, else the fallback.

        After ``source`` ended at its own declared timeout only the edges that read its expiry
        apply, and there is no fallback, so an expiry never continues as a finished run.
        """
        expired = state.get('expired', {})
        own = bool(expired.get(source))
        # Expired components discard their descendants, but retain a result
        # from an earlier completed visit. A node discards its own prior output.
        abandoned = ({step.id for step in self.layout.steps if self._owned_by(step, source)}
                     if own and source in self.components else {source} if own else set())
        def matches(row):
            try:
                return self._condition(row['When'], state['values'], source=source, ancestry=state['ancestry'],
                                       visits=state.get('starts'), expired=expired, abandoned=abandoned)
            except MissingPredicateField as error:
                if error.owner not in abandoned:
                    raise
                return False
        choices = [row for row in edges if row['When'] not in ('always', 'otherwise')
                   and (not own or row['When'] in self._expiry[source]) and matches(row)]
        if not own:
            choices = choices or [row for row in edges if row['When'] in ('always', 'otherwise')]
        return choices

    def _condition(self, identifier, values, *, source=None, ancestry=(), visits=None, expired=None, abandoned=()):
        if (source, identifier) in self._predicates:
            predicate = self._predicates[source, identifier]
            attempt, maximum = (ancestry[-1][1:] if ancestry else (1, 1))
            results = {key: value for key, value in self._turn_state.get()['results'].items() if key not in abandoned}
            result = predicate.evaluate({**values, **results}, attempt=attempt,
                                        max_passes=maximum, visits=visits, expired=expired,
                                        constants={**self.pass_constants, **self.limit_values})
        else:
            result = self.conditions[identifier](deepcopy(values))
        if type(result) is not bool:
            raise WorkflowError(f'Condition {identifier} must return a boolean')
        return result


    def _component(self, step):
        graph = self._compile(step.id)
        row = self.components[step.id]
        maximum = self.component_passes[step.id]
        controller = component_controller(row, maximum=maximum)
        async def execute(state):
            original_ancestry, original_feedback = state['ancestry'], state['feedback']
            original_previous = state['previous']
            original_attempt_id = state['attempt_id']
            descendants = {item.id for item in self.layout.steps if self._owned_by(item, step.id)}
            current = _entered(state, step.id)
            feedback = {}
            turn = self._turn_state.get()
            run_start, budget = len(turn['runs']), None
            for descendant in descendants:
                turn['contexts'].pop(descendant, None)
            try:
                async with self._scope(step.id, kind='component', component=step.id, attempt=1,
                                       attempt_id=state.get('attempt_id'), max_passes=maximum) as budget:
                    frame = _RepairFrame(self, step.id, maximum, budget)
                    async with _repair_owner(frame if controller['accepted'] else None):
                        return await passes(frame, current, budget, original_ancestry, original_feedback, original_previous, original_attempt_id, descendants)
            except TimeoutError:
                expired = self._expired_state(step.id, budget, tuple(descendants), dict(
                    current, ancestry=self._repair_ancestry(original_ancestry)))
                if expired is None:
                    raise
                del turn['runs'][run_start:]
                for descendant in descendants:
                    turn['results'].pop(descendant, None)
                return expired
        async def passes(frame, current, budget, original_ancestry, original_feedback, original_previous, original_attempt_id, descendants):
            feedback = {}
            turn = self._turn_state.get()
            runs = turn['runs']
            while frame.attempt <= maximum:
                attempt = frame.attempt
                attempt_id = str(uuid4())
                pass_start = len(runs)
                for descendant in descendants:
                    turn['results'].pop(descendant, None)
                previous = {key: value for key, value in current['values'].items() if key in descendants} if frame.guard_attempt > 1 else {}
                current = dict(current, values={key: value for key, value in current['values'].items() if key not in descendants},
                               previous=previous, ancestry=(*original_ancestry, (step.id, attempt, maximum)), feedback=feedback,
                               attempt_id=attempt_id)
                self._event('attempt', scope_id=budget.id, component=step.id, attempt=attempt,
                            attempt_id=attempt_id, max_passes=maximum)
                current = await graph.ainvoke(current, {'recursion_limit': self._recursion_limit})
                attempt = frame.attempt
                if not controller['accepted']:
                    self._settle(step.id, current['values'])
                    return dict(current, ancestry=self._repair_ancestry(original_ancestry), feedback=original_feedback,
                                previous=original_previous, attempt_id=original_attempt_id,
                                visit_id=budget.id)
                if row['Guard'] not in current['values']:
                    raise WorkflowError(f'Component {step.id} did not execute its guard in the current pass')
                guard = GuardResult.from_output(current['values'][row['Guard']])
                self._event('guard', scope_id=budget.id, component=step.id, attempt=attempt, accepted=guard.accepted)
                if component_decision(controller, guard.accepted, attempt) == 'accepted':
                    self._settle(step.id, current['values'])
                    return dict(current, ancestry=self._repair_ancestry(original_ancestry), feedback=original_feedback,
                                previous=original_previous, attempt_id=original_attempt_id,
                                visit_id=budget.id)
                if frame.effect:
                    raise WorkflowError('Component correction cannot replay dispatched effects')
                feedback = await self._safe_feedback(guard.feedback)
                del runs[pass_start:]
                frame.attempt += 1
                frame.guard_attempt += 1
            raise WorkflowError(f'Component {step.id} exhausted {maximum} passes')
        return execute


    def _repair_ancestry(self, ancestry):
        frames = {frame.component: frame for frame in _REPAIRS.get() if frame.workflow is self}
        return tuple((owner, frames[owner].attempt, maximum) if owner in frames else (owner, attempt, maximum)
                     for owner, attempt, maximum in ancestry)


    async def _safe_feedback(self, feedback):
        return await project_correction(feedback, services=self._capture_services())


    def _owned_by(self, step, owner):
        parents = {item.id: item.parent for item in self.layout.steps}
        parent = step.parent
        while parent is not None:
            if parent == owner:
                return True
            parent = parents[parent]
        return False


    def _node(self, step):
        row = self.functions[step.id]
        function = self.registry[row['Registry']]
        bindings = [item for item in self.layout.tables['Bindings'] if item['Node'] == step.id]
        prompts = [item for item in self.layout.tables['Prompts'] if item['Node'] == step.id]
        async def execute(state, budget):
            inputs = {}
            values = state['values']
            services = self._turn_state.get()['llm_history'].services(self._run_services(), step.id)
            if row.get('Display summary', '-') != '-':
                from .node_summary import SummaryServices
                services = SummaryServices(services, row['Display summary'])
            context = NodeContext(step.id, state['ancestry'], tuple(state['prompts'][item['File']] for item in prompts
                if item['When'] == 'always' or self._condition(item['When'], values)), services,
                budget.id, state.get('attempt_id'), row, state.get('_correction'),
                state.get('_rejected'), state.get('_correction_attempt', 0), state.get('_guard_attempt', 1),
                state.get('_repair_limit'))
            state['_node_context'] = context
            self._turn_state.get()['contexts'][step.id] = context
            for binding in bindings:
                source = binding['From']
                sources = {'START': values['START'], 'PREVIOUS': state['previous'].get(step.id, {}),
                           'FEEDBACK': state['feedback'], 'ATTEMPT': {'value': context.attempt},
                           'MAX_PASSES': {'value': context.max_passes},
                           'SESSION': self._turn_state.get().get('session', {}),
                           'TURN': self._turn_payload(binding['Field']) if source == 'TURN' else {}}
                payload = sources.get(source, values.get(source, {}))
                available = (source in ('START', 'ATTEMPT', 'MAX_PASSES')
                             or (source == 'PREVIOUS' and step.id in state['previous'])
                             or (source == 'FEEDBACK' and (bool(payload) or context.guard_attempt > 1))
                             or source in values)
                if binding['Field'] == '*' and available:
                    inputs[binding['Input']] = deepcopy(payload)
                elif binding['Field'] in payload:
                    inputs[binding['Input']] = deepcopy(payload[binding['Field']])
                elif 'default_value' in binding:
                    inputs[binding['Input']] = _thaw(binding['default_value'])
                elif source in ('SESSION', 'TURN'):
                    kind = 'session field' if source == 'SESSION' else 'turn value'
                    raise WorkflowError(f'Missing required {kind} {binding["Field"]} for {step.id}.{binding["Input"]}')
                else:
                    raise WorkflowError(f'Missing required binding for {step.id}.{binding["Input"]}')
            self._validate(row['Input schema'], inputs, step.id, 'input')
            journal = self._journal.get() or self._run_services().get('journal')
            plain_fields = (self._run_services().get('plain_io_fields') or {}).get(step.id, {})
            from .capture_authority import capture_node
            await capture_node(journal, self._capture_services(), visit_id=budget.id,
                direction='input', values=inputs, schema=row['Input schema'],
                plain_fields=plain_fields.get('input', ...))
            started = time.monotonic()
            from .attribution import bind_workflow_visit
            with _node_effect_owner(context), bind_workflow_visit(journal, budget.id, state.get('attempt_id'), capture_services=self._capture_services()):
                try:
                    result = (function.invoke(inputs, context) if inspect.iscoroutinefunction(function.invoke)
                              else await offload(function.invoke, inputs, context))
                    if inspect.isawaitable(result):
                        result = await result
                finally:
                    from .evidence_finalization import finalize
                    try:
                        await finalize()
                    finally:
                        if context._dispatched():
                            context.mark_effect()
            self._check_deadline(budget)
            self._validate_output(row, result, step.id)
            await capture_node(journal, self._capture_services(), visit_id=budget.id,
                direction='output', values=result, schema=row['Output schema'],
                plain_fields=plain_fields.get('output', ...))
            self._event('node', scope_id=budget.id, node=step.id, component=step.parent, attempt=context.attempt,
                        elapsed=time.monotonic() - started, kind=function.kind)
            turn_state = self._turn_state.get()
            output = deepcopy(result)
            turn_state['runs'].append((step.id, output))
            return dict(state, values={**values, step.id: output}, visit_id=budget.id)
        async def timed(state):
            frame = next((item for item in reversed(_REPAIRS.get()) if item.workflow is self), None)
            current = _entered(state, step.id)
            correction_attempt = 0
            while True:
                if frame is not None:
                    self._check_deadline(frame.budget)
                current['ancestry'] = self._repair_ancestry(current['ancestry'])
                context, budget = None, None
                try:
                    async with self._scope(step.id, kind='node', node=step.id, component=step.parent,
                                           attempt=frame.attempt if frame is not None else current['ancestry'][-1][1] if current['ancestry'] else 1,
                                           attempt_id=current.get('attempt_id'),
                                           max_passes=frame.maximum if frame is not None else current['ancestry'][-1][2] if current['ancestry'] else 1) as budget:
                        if frame is not None:
                            current['_guard_attempt'] = frame.guard_attempt
                            current['_repair_limit'] = (frame.attempt, frame.maximum)
                        result = await execute(current, budget)
                        return {key: value for key, value in result.items() if not key.startswith('_')}
                except TimeoutError:
                    expired = self._expired_state(step.id, budget, (step.id,),
                                                  {key: value for key, value in current.items() if not key.startswith('_')})
                    if expired is None:
                        raise
                    return expired
                except CandidateCorrection as error:
                    context = current.get('_node_context')
                    if frame is None:
                        raise
                    self._check_deadline(frame.budget)
                    self._check_deadline(budget)
                    if context is None or context._dispatched():
                        for owner in _REPAIRS.get():
                            owner.effect = True
                        from api_agent.telemetry.outcomes import inherit_failure
                        raise inherit_failure(WorkflowError('Candidate correction cannot replay dispatched effects'), error) from None
                    feedback = await self._safe_feedback(error.feedback)
                    if frame.attempt >= frame.maximum:
                        from api_agent.telemetry.outcomes import inherit_failure
                        raise inherit_failure(WorkflowError(f'Component {frame.component} exhausted {frame.maximum} passes'), error) from None
                    history = self._turn_state.get()['llm_history']
                    history.correct(step.id, feedback)
                    self._check_deadline(frame.budget)
                    frame.attempt += 1
                    correction_attempt += 1
                    ancestry = tuple((owner, frame.attempt, maximum) if owner == frame.component else (owner, attempt, maximum)
                                     for owner, attempt, maximum in current['ancestry'])
                    current = dict(current, ancestry=ancestry, attempt_id=str(uuid4()),
                                   _correction=feedback, _rejected=deepcopy(error.rejected),
                                   _correction_attempt=correction_attempt)
                    self._event('attempt', scope_id=frame.budget.id, component=frame.component,
                                attempt=frame.attempt, attempt_id=current['attempt_id'], max_passes=frame.maximum)
        return timed


    @classmethod
    def _validate_output(cls, row, result, node):
        """Check a node result against its Output schema.

        A violation in a node that declares an Output mode is a correction of the
        current candidate; otherwise it is a plain schema failure.
        """
        try:
            cls._validate(row['Output schema'], result, node, 'output')
        except _SchemaRejection as invalid:
            try:
                encoded = json.dumps(result, allow_nan=False)
                json_value = json.loads(encoded)
            except (ValueError, TypeError):
                raise invalid
            if row.get('Output mode', '-') == '-' or json_value != result:
                raise
            facts = _schema_facts(row['Output schema'], result, invalid.errors)
            from api_agent.telemetry.outcomes import inherit_failure
            raise inherit_failure(CandidateCorrection({'reason': 'output_schema', 'errors': facts}, rejected=result), invalid) from None

    @staticmethod
    def _validate(schema, value, node, direction):
        errors = tuple(Draft202012Validator(schema).iter_errors(value))
        if errors:
            raise _SchemaRejection(node, direction, errors, schema, value)
        # JSON is the state/IPC contract; reject Python-only and nonfinite values.
        try:
            encoded = json.dumps(value, allow_nan=False)
            if json.loads(encoded) != value:
                raise ValueError('Python-only JSON representation')
        except (ValueError, TypeError):
            raise WorkflowError(f'Node {node} returned non-JSON {direction}') from None


    def diagram(self):
        return diagram_layout(self.layout, component_passes=self.component_passes)


    def _binding(self, path, name):
        namespace = self.catalog.namespaces.get(path)
        value = self.catalog.bindings.get((path, name))
        proof = self.catalog.binding_proofs.get((path, name))
        if (value is None or namespace is None or namespace.get(name) is not value
                or callable(value) and getattr(value, '__module__', None) not in self.catalog.admitted_modules
                or proof is not None and (value is not proof[0]
                                          or getattr(value, '__code__', proof[1]) is not proof[1]
                                          or getattr(value, '__globals__', proof[2].__dict__) is not proof[2].__dict__)):
            raise LayoutError(f'{path}:1: missing admitted native binding {name}')
        return value


    def _scope_outcome(self, kind, owner, state, scope_id):
        """Apply an optional trusted business outcome without controlling execution."""
        callback = self._services.get().get('scope_outcome')
        if callback is None:
            return 'passed'
        from api_agent.telemetry.outcomes import ExecutionOutcome
        final = callback(kind, owner, state)
        if final is None:
            return 'passed'
        if type(final) is not ExecutionOutcome:
            raise ValueError('Invalid explicit scope outcome')
        journal = self._journal.get()
        if journal is not None and hasattr(journal, 'report_outcome'):
            journal.report_outcome(scope_id, final)
        return {'success': 'passed', 'failure': 'failed', 'cancelled': 'cancelled',
                'partial': 'partial', 'unknown': 'unknown'}[final.state.value]


    def _state_node(self, row, function):
        async def invoke(state):
            parent = self._parents[row['ID']]
            turn = self._turn_state.get()
            parent_scope = (bind_timeout_budget(turn['open_components'][parent]['budget'])
                            if parent is not None else nullcontext())
            turn['contexts'].pop(row['ID'], None)
            held = []
            try:
                with parent_scope, (structural_scope(turn['open_components'][parent]['budget'].id)
                                    if parent is not None else nullcontext()):
                    async with component_timeout('Workflow ' + row['ID'], self.timeouts.get(row['ID'])) as budget:
                        held.append(budget)
                        with structural_scope(budget.id):
                            return await self._run_node(row, function, state, budget)
            except TimeoutError:
                if not self._routes_expiry(row['ID'], held[0] if held else None, (row['ID'],)):
                    raise
                return self._expiry_update(row['ID'], state)
        return invoke

    def _own_expiry(self, owner, budget):
        """True only when the owner's own declared deadline, not an ancestor's, has passed."""
        declared = self.timeouts.get(owner)
        return (budget is not None and declared is not None and budget.timeout == declared
                and time.monotonic() >= budget.deadline)

    def _routes_expiry(self, owner, budget, nodes):
        """Declared expiry routing applies to an own-deadline expiry without dispatched effects."""
        contexts = self._turn_state.get()['contexts']
        return (owner in self._expiry and self._own_expiry(owner, budget)
                and not any(context._dispatched() for context in map(contexts.get, nodes) if context is not None))

    def _expired_state(self, owner, budget, nodes, state):
        """``state`` after ``owner``'s own declared timeout expired, or ``None`` when its edges cannot route that.

        The abandoned run leaves no output of its own or of the steps it owns, and an expiry is
        routable only when one of the owner's expiry edges matches the resulting state.
        """
        if not self._routes_expiry(owner, budget, nodes):
            return None
        after = dict(state, visit_id=budget.id, expired={**state.get('expired', {}), owner: True},
                     values={key: value for key, value in state['values'].items() if key != owner and key not in nodes})
        edges = [row for row in self.layout.tables['Edges'] if row['From'] == owner]
        return after if self._route_choices(owner, edges, after) else None

    def _expiry_update(self, owner, state):
        expired, expirations = self._expiry[owner]
        previous = state.get(expirations)
        return {expired: owner, expirations: previous + 1 if type(previous) is int else 1}

    def _descendants(self, owner):
        return tuple(item.id for item in self.layout.steps if self._owned_by(item, owner))

    def _expiring_component(self, owner, compiled):
        nodes = self._descendants(owner)
        async def run(state, config):
            try:
                return await compiled.ainvoke(state, config)
            except TimeoutError:
                turn = self._turn_state.get()
                if not self._routes_expiry(owner, turn['budgets'].get(owner), nodes):
                    raise
                steps = {item.id: item for item in self.layout.steps}
                for component in reversed(tuple(turn['open_components'])):
                    if component == owner or self._owned_by(steps[component], owner):
                        try:
                            await self._complete_component(component, outcome='failed')
                        except (Exception, asyncio.CancelledError):
                            pass
                return self._expiry_update(owner, state)
        return run


    async def _run_node(self, row, function, state, budget):
        visit_id = budget.id
        started = time.monotonic()
        wall_started = time.time() * 1000
        self._event('scope_start', scope_id=budget.id, parent_id=budget.parent_id, owner=row['ID'],
                    kind='node', node=row['ID'], visit_id=visit_id, attempt=1, max_passes=1,
                    started_ms=wall_started,
                    declared_timeout=self.timeouts.get(row['ID']), effective_timeout=budget.timeout,
                    timeout_key=self.timeout_keys.get(row['ID']))
        outcome = 'failed'
        expired = False
        try:
            services = self._turn_state.get()['llm_history'].services(self._services.get(), row['ID'])
            function_row = self._typed_functions.get(row['ID'])
            if function_row is not None and function_row.get('Display summary', '-') != '-':
                from .node_summary import SummaryServices
                services = SummaryServices(services, function_row['Display summary'])
            context = NodeContext(row['ID'], (), (), services, request=self._request.get(),
                                  resources=self.definition.declared_resources, visit_id=budget.id,
                                  response_contract=function_row)
            self._turn_state.get()['contexts'][row['ID']] = context
            texts = self._node_prompts(row['ID'], state, context)
            if texts:
                context = replace(context, prompts=tuple(texts.values()), prompt_texts=MappingProxyType(texts))
            from .attribution import bind_workflow_visit
            journal = self._journal.get()
            captures = (self._services.get().get('plain_io_contracts') or {}).get(row['ID'], {})
            async def capture(direction, values):
                from .capture_authority import capture_node
                if journal is None:
                    return
                projection = (self._services.get().get('node_io_projections') or {}).get(row['ID'])
                if projection is not None:
                    values = await _project_capture(journal, projection, direction, visit_id, values)
                    if values is None:
                        return
                contract = captures.get(direction)
                if contract is not None:
                    await capture_node(journal, self._capture_services(), visit_id=visit_id,
                        direction=direction, values=values, schema=contract['schema'],
                        plain_fields=contract['plain_fields'])
                else:
                    await capture_node(journal, self._capture_services(), visit_id=visit_id,
                        direction=direction, values=values, schema={'type': 'object'} if projection is not None else self.capture_schema)
            if function_row is not None:
                # The Input schema validates the projection of State that the node declares it reads.
                properties = function_row['Input schema'].get('properties', {})
                self._validate(function_row['Input schema'], {name: state[name] for name in properties if name in state},
                               row['ID'], 'input')
            await capture('input', state)
            with _node_effect_owner(context), bind_workflow_visit(journal, budget.id, capture_services=self._capture_services()):
                try:
                    result = (await function(state, context) if inspect.iscoroutinefunction(function)
                              else await offload(function, state, context))
                    if inspect.isawaitable(result):
                        result = await result
                finally:
                    from .evidence_finalization import finalize
                    await finalize()
            if type(result) is not dict or any(key not in self.schema.__annotations__ for key in result):
                raise ValueError('node must return a partial state update')
            if function_row is not None:
                self._validate_output(function_row, result, row['ID'])
            await capture('output', result)
            outcome = self._scope_outcome('node', row['ID'], {**state, **result}, budget.id)
            self._event('node', scope_id=budget.id, node=row['ID'], visit_id=visit_id,
                        elapsed=time.monotonic() - started, kind=row['Kind'], outcome=outcome)
            return result
        except BaseException as error:
            from api_agent.telemetry.outcomes import report_failure, OutcomeState
            failed = report_failure(error, observations=self._journal.get(), scope_ids=(budget.id,),
                deadline_expired=budget.deadline is not None and time.monotonic() >= budget.deadline)
            outcome = 'cancelled' if failed.state == OutcomeState.CANCELLED else 'failed'
            expired = self._routes_expiry(row['ID'], budget, (row['ID'],))
            if expired:
                self._event('node', scope_id=budget.id, node=row['ID'], visit_id=visit_id,
                            elapsed=time.monotonic() - started, kind=row['Kind'], outcome='failed')
            raise
        finally:
            self._event('scope_end', scope_id=budget.id, parent_id=budget.parent_id,
                        owner=row['ID'], kind='node', node=row['ID'], visit_id=visit_id,
                        elapsed=time.monotonic() - started, outcome=outcome,
                        started_ms=wall_started, stopped_ms=time.time() * 1000,
                        declared_timeout=self.timeouts.get(row['ID']), effective_timeout=budget.timeout,
                        timeout_key=self.timeout_keys.get(row['ID']),
                        **({'reason': 'declared_timeout_expired'} if expired else {}))
            self._turn_state.get()['visits'][row['ID']] = visit_id


    async def _start_component(self, parent):
        turn = self._turn_state.get()
        owner_parent = self._parents[parent]
        parent_scope = (bind_timeout_budget(turn['open_components'][owner_parent]['budget'])
                        if owner_parent is not None else nullcontext())
        for step in self.layout.steps:
            if self._owned_by(step, parent):
                turn['contexts'].pop(step.id, None)
        ready = asyncio.get_running_loop().create_future()
        finish = asyncio.get_running_loop().create_future()
        async def own():
            try:
                with parent_scope, (structural_scope(turn['open_components'][owner_parent]['budget'].id)
                                    if owner_parent is not None else nullcontext()):
                    async with component_timeout('Workflow ' + parent, self.timeouts.get(parent)) as budget:
                        with structural_scope(budget.id):
                            started = time.monotonic()
                            wall_started = time.time() * 1000
                            self._event('scope_start', scope_id=budget.id, parent_id=budget.parent_id,
                                        owner=parent, kind='component', visit_id=budget.id,
                                        attempt=1, max_passes=1,
                                        started_ms=wall_started,
                                        declared_timeout=self.timeouts.get(parent),
                                        effective_timeout=budget.timeout,
                                        timeout_key=self.timeout_keys.get(parent))
                            turn['budgets'][parent] = budget
                            ready.set_result(budget)
                            outcome = 'failed'
                            expired = False
                            try:
                                outcome = await finish
                                self._event('disposition', scope_id=budget.id, owner=parent,
                                            kind='component', outcome=outcome)
                            except BaseException as error:
                                from api_agent.telemetry.outcomes import report_failure, OutcomeState
                                failed = report_failure(error, observations=self._journal.get(), scope_ids=(budget.id,),
                                    deadline_expired=budget.deadline is not None and time.monotonic() >= budget.deadline)
                                outcome = 'cancelled' if failed.state == OutcomeState.CANCELLED else 'failed'
                                expired = self._routes_expiry(parent, budget, self._descendants(parent))
                                raise
                            finally:
                                self._event('scope_end', scope_id=budget.id, parent_id=budget.parent_id,
                                            owner=parent, kind='component', visit_id=budget.id,
                                            elapsed=time.monotonic() - started, outcome=outcome,
                                            started_ms=wall_started, stopped_ms=time.time() * 1000,
                                            declared_timeout=self.timeouts.get(parent),
                                            effective_timeout=budget.timeout,
                                            timeout_key=self.timeout_keys.get(parent),
                                            **({'reason': 'declared_timeout_expired'} if expired else {}))
            except BaseException as error:
                if not ready.done():
                    ready.set_exception(error)
                raise
        task = asyncio.create_task(own())
        try:
            budget = await ready
        except BaseException:
            task.cancel()
            try:
                await settle(task)
            except BaseException:
                pass
            raise
        turn['open_components'][parent] = dict(budget=budget, task=task, finish=finish)


    async def _complete_component(self, parent, outcome='passed', *, state=None):
        if parent is None:
            return
        turn = self._turn_state.get()
        if parent not in turn['open_components']:
            return
        owned = turn['open_components'][parent]
        if outcome == 'passed' and not owned['finish'].done():
            status = self._scope_outcome('component', parent, state, owned['budget'].id)
            owned['finish'].set_result(status)
        elif not owned['task'].done():
            owned['task'].cancel()
        try:
            await settle(owned['task'])
        finally:
            # settle only exits after the owned task reaches a terminal state.
            turn['open_components'].pop(parent)
            turn['visits'][parent] = owned['budget'].id


    def _static_route(self, source, target, parent):
        async def route(state):
            visit_id = self._turn_state.get()['visits'].get(source)
            self._event('edge', owner=parent or 'GRAPH', source=source, target=target,
                        visit_id=visit_id, implicit=False)
            if target == 'END':
                await self._complete_component(parent, state=state)
            return target
        return route


    def _node_prompts(self, node, state, context):
        """Selected declared prompts for one typed node, with every slot filled."""
        values, texts, filled = {'state': dict(state)}, {}, {}
        slots = {row['Slot']: row for row in getattr(self.definition, 'prompt_slots', ())}
        def fill(match):
            name = match.group(1)
            if name not in filled:
                value = self._binding(slots[name]['Source'], slots[name]['Registry'])(state, context)
                if type(value) is not str:
                    raise WorkflowError(f'Prompt slot {name} must return text')
                filled[name] = value
            return filled[name]
        for row in getattr(self.definition, 'prompts', ()):
            if row['Node'] == node and (row['When'] == 'always' or self._state_predicates[row['When']].evaluate(values, constants=self.limit_values)):
                texts[row['Prompt']] = _PROMPT_SLOT.sub(fill, row['Text'])
        return texts

    def _conditional_route(self, source, edges, parent):
        async def route(state):
            values = {'state': dict(state)}
            matches = [edge for edge in edges if edge['When'] not in ('always', 'otherwise')
                       and self._state_predicates[edge['When']].evaluate(values, constants=self.limit_values)]
            choices = matches or [edge for edge in edges if edge['When'] in ('always', 'otherwise')]
            if len({edge['To'] for edge in choices}) != 1:
                raise WorkflowError('Routing requires exactly one matching condition or fallback')
            target = choices[0]['To']
            visit_id = self._turn_state.get()['visits'].get(source)
            self._event('edge', owner=parent or 'GRAPH', source=source, target=target,
                        visit_id=visit_id, condition=choices[0]['When'], implicit=False)
            if target == 'END':
                await self._complete_component(parent, state=state)
            return target
        return route

    def _route(self, source, router, edges, parent):
        targets = {edge['To'] for edge in edges}
        async def route(state):
            target = router(state, NodeContext(source, (), (), self._services.get(), request=self._request.get(),
                        resources=self.definition.declared_resources))
            if target not in targets:
                raise ValueError(f'router for {source} returned an undeclared target')
            visit_id = self._turn_state.get()['visits'].get(source)
            self._event('edge', owner=parent or 'GRAPH', source=source, target=target,
                        visit_id=visit_id, condition=edges[0]['Router'], implicit=False)
            if target == 'END':
                await self._complete_component(parent, state=state)
            return target
        return route


    @asynccontextmanager
    async def _scope_graph(self, budget, session=None):
        parent_id = current_structural_scope()
        with structural_scope(budget.id):
            started = time.monotonic()
            wall_started = time.time() * 1000
            outcome = 'failed'
            async def capture_graph(direction):
                projection = self._services.get().get('graph_io_projection')
                journal = self._journal.get()
                if projection is not None and journal is not None:
                    values = await _project_capture(journal, projection, direction, budget.id)
                    if values is None:
                        return
                    from .capture_authority import capture_node
                    await capture_node(journal, self._capture_services(), visit_id=budget.id,
                        direction=direction, values=values, schema={'type': 'object'})
            try:
                self._event('scope_start', scope_id=budget.id, parent_id=parent_id,
                            owner='GRAPH', kind='graph', attempt=1, max_passes=1,
                            started_ms=wall_started,
                            declared_timeout=self.timeouts.get('GRAPH'), effective_timeout=budget.timeout,
                            timeout_key=self.timeout_keys.get('GRAPH'))
                await capture_graph('input')
                yield
                await capture_graph('output')
                outcome = 'passed'
                terminal = self._services.get().get('execution_outcome')
                final = None
                if terminal is not None:
                    from api_agent.telemetry.outcomes import ExecutionOutcome, report_outcome
                    final = terminal()
                    if type(final) is not ExecutionOutcome:
                        error_type = ValueError if self.typed_state else WorkflowError
                        raise error_type('Invalid explicit execution outcome')
                    outcome = {'success': 'passed', 'failure': 'failed',
                               'cancelled': 'cancelled', 'partial': 'partial',
                               'unknown': 'unknown'}[final.state.value]
                if session is not None and outcome in self.session_commit:
                    await session.save()
                if final is not None:
                    report_outcome(final, runtime=getattr(self._journal.get(), 'trace', None))
                    current = self._journal.get()
                    if current is not None and hasattr(current, 'report_outcome'):
                        current.report_outcome(budget.id, final)
            except BaseException as error:
                from api_agent.telemetry.outcomes import report_failure, OutcomeState
                scopes = (budget.id, *(owned['budget'].id for owned in self._turn_state.get()['open_components'].values()))
                failed = report_failure(error, observations=self._journal.get(), scope_ids=scopes, terminal=parent_id is None,
                    deadline_expired=budget.deadline is not None and time.monotonic() >= budget.deadline)
                outcome = 'cancelled' if failed.state == OutcomeState.CANCELLED else 'failed'
                raise
            finally:
                try:
                    for component in tuple(self._turn_state.get()['open_components']):
                        try:
                            await self._complete_component(component, outcome=outcome)
                        except BaseException:
                            if outcome == 'passed':
                                raise
                finally:
                    try:
                        self._event('scope_end', scope_id=budget.id, parent_id=parent_id,
                                    owner='GRAPH', kind='graph', attempt=1, max_passes=1, elapsed=time.monotonic() - started,
                                    started_ms=wall_started, stopped_ms=time.time() * 1000,
                                    outcome=outcome, declared_timeout=self.timeouts.get('GRAPH'),
                                    effective_timeout=budget.timeout, timeout_key=self.timeout_keys.get('GRAPH'))
                    except Exception:
                        if outcome == 'passed':
                            raise


    async def astream_events(self, update, *, thread_id, request=None, services=None, journal=None,
                             turn_id=None, turn_kind='initial'):
        if type(thread_id) is not str or not thread_id or type(update) is not dict:
            raise ValueError('thread_id and partial state update are required')
        async with self._turn(thread_id, request, services, journal, turn_id, turn_kind):
            queue = asyncio.Queue(maxsize=32)
            async def produce():
                async for event in self.graph.astream_events(
                        update, {'configurable': {'thread_id': thread_id}}, version='v2'):
                    await queue.put(event)
            producer = asyncio.create_task(produce())
            try:
                while True:
                    if producer.done():
                        while not queue.empty():
                            yield queue.get_nowait()
                        await producer
                        break
                    pending = asyncio.create_task(queue.get())
                    try:
                        done, _ = await asyncio.wait((pending, producer),
                                                     return_when=asyncio.FIRST_COMPLETED)
                        if pending in done:
                            yield pending.result()
                    finally:
                        if not pending.done():
                            pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
            finally:
                if not producer.done():
                    producer.cancel()
                try:
                    await settle(producer)
                except BaseException:
                    pass


    @asynccontextmanager
    async def _turn(self, thread_id, request, services, journal, turn_id=None, turn_kind='initial',
                    session=None):
        from .run_context import canonical_uuid, current_execution_context, current_turn_id, execution_context, turn
        if turn_id is not None:
            canonical_uuid(turn_id)
        turn_id = turn_id if turn_id is not None else current_turn_id()
        self.definition.verify_sources()
        from .capture_authority import resolve_capture_services, requested_log_content
        selected_services = resolve_capture_services(services or {}, inherited=self.services)
        selected_journal = (journal if journal is not None else
                            self.journal if self.journal is not None else
                            selected_services.get('journal'))
        automatic_observations = None
        factory = selected_services.get('observation_factory')
        if selected_journal is None and factory is not None and not recording_enabled():
            from .identity import workflow_identity
            selected_journal = automatic_observations = factory.create(self.definition, turn_id=turn_id,
                semantic_identity=workflow_identity(self.definition, timeouts=self.timeouts, component_passes=self.component_passes, limits=self.limit_values), component_passes=self.component_passes,
                limits=self.limit_values)
        journal_turn = getattr(selected_journal, 'turn_id', None)
        if turn_id is not None and journal_turn is not None and turn_id != journal_turn:
            raise ValueError('explicit turn identity disagrees with the active journal')
        from .identity import workflow_identity
        if selected_journal is not None:
            identity = workflow_identity(self.definition, timeouts=self.timeouts, component_passes=self.component_passes, limits=self.limit_values)
            if (getattr(selected_journal, 'semantic_identity', None) is not None
                    and selected_journal.semantic_identity != identity):
                error_type = ValueError if self.typed_state else WorkflowError
                raise error_type("Workflow journal semantic identity differs from effective budgets")
        if selected_journal is not None and not self.typed_state:
            from .journal import projection
            if selected_journal.projection != projection(self.definition, component_passes=self.component_passes,
                                                         limits=self.limit_values):
                raise WorkflowError('Workflow journal uses a different effective component pass limit or limit value')
        selected_context = current_execution_context()
        if selected_journal is not None:
            selected_context = selected_journal.admit_execution_context(selected_context)
            admit_identity = getattr(selected_journal, 'admit_semantic_identity', None)
            if admit_identity is not None:
                admit_identity(identity)
        recorded = recording_enabled()
        if recorded:
            proof = selected_services.get('packaged_proof')
            source_manifest = _source_manifest(self.definition, proof)
            from .journal import WorkflowJournal
            journal = WorkflowJournal(self.definition,
                semantic_identity=workflow_identity(self.definition, timeouts=self.timeouts,
                                                    component_passes=self.component_passes,
                                                    limits=self.limit_values),
                component_passes=self.component_passes, limits=self.limit_values,
                log_content=requested_log_content(self.config, selected_services),
                source_manifest=source_manifest,
                boundary=selected_services.get('emissions'),
                content_policy=selected_services.get('content_policy'),
                turn_id=turn_id,
                test_id=os.environ.get('PYTEST_CURRENT_TEST', '').rsplit(' ', 1)[0] or None)
            journal.admit_execution_context(selected_context)
            factory = selected_services.get('observation_factory')
            if factory is not None:
                journal = factory.create(self.definition, journal=journal)
        with self._thread_guard:
            if thread_id in self._active_threads:
                raise RuntimeError('conversation already has an active turn')
            self._active_threads.add(thread_id)
        current_journal_token = _CURRENT_JOURNAL.set(journal if recorded else selected_journal)
        token = self._request.set(request)
        node_services = MappingProxyType({key: value for key, value in selected_services.items()
                                          if key != 'tools'})
        services_token = self._services.set(dict(node_services))
        capture_token = self._capture_services_context.set(node_services)
        journal_token = self._journal.set(journal if recorded else selected_journal)
        from .inference_history import InferenceHistory
        history = InferenceHistory()
        state_token = self._turn_state.set({'visits': {}, 'open_components': {}, 'llm_history': history,
                                                  'contexts': {}, 'budgets': {}, 'runs': [], 'exits': {}, 'results': {}})
        completed = False
        try:
            selected_journal = self._journal.get()
            active_turn = turn_id if turn_id is not None else (selected_journal.turn_id if selected_journal is not None else None)
            usage_context = nullcontext()
            if active_turn is not None:
                try:
                    from api_agent.testing.inference_usage import user_turn
                    usage_context = user_turn(active_turn, kind=turn_kind)
                except ModuleNotFoundError:
                    pass
            with execution_context(selected_context), (turn(active_turn) if active_turn is not None else nullcontext()), (
                    usage_context):
                async with component_timeout('Workflow GRAPH', self.timeouts.get('GRAPH')) as budget:
                    from api_agent.tooling.runtime import invocation_tools
                    async with invocation_tools(self.definition, selected_services, timeouts=self.timeouts), self._scope_graph(budget, session):
                        yield
            completed = True
        finally:
            if automatic_observations is not None:
                automatic_observations.close()
            history.close()
            _CURRENT_JOURNAL.reset(current_journal_token)
            self._request.reset(token)
            self._services.reset(services_token)
            self._capture_services_context.reset(capture_token)
            self._journal.reset(journal_token)
            self._turn_state.reset(state_token)
            try:
                if recorded:
                    from .evidence import publish
                    publish(journal.finalize())
            except BaseException:
                completed = False
                raise
            finally:
                # Still holding the thread, so no other turn reads the value being taken back.
                try:
                    if session is not None and not completed:
                        await session.restore()
                finally:
                    with self._thread_guard:
                        self._active_threads.remove(thread_id)


    def _prompt_texts(self):
        """Declared prompt texts with each ``{{slot}}`` replaced by its pack-supplied text."""
        contents = self.definition.contents
        slots = {name: slot_text(contents[path])
                 for name, path in getattr(self.definition, 'slot_files', {}).items()}
        return {path: fill_slots(contents[path].decode('utf-8'), slots)
                for path in dict.fromkeys(row['File'] for row in self.layout.tables['Prompts'])}

    async def arun(self, update, *, thread_id=None, request=None, services=None, journal=None,
                   turn_id=None, turn_kind='initial'):
        from .run_context import canonical_uuid
        if turn_id is not None:
            canonical_uuid(turn_id)
        if self.typed_state and (type(thread_id) is not str or not thread_id or type(update) is not dict):
            raise ValueError('thread_id and partial state update are required')
        if self._session is not None and (type(thread_id) is not str or not thread_id):
            raise ValueError('thread_id is required for a workflow with a Session')
        if not self.typed_state and self._session is None and thread_id is not None:
            raise WorkflowError('Conversation checkpoints require a declared State or Session contract')
        config = {'configurable': {'thread_id': thread_id}}
        commit = self._session_commit(thread_id) if self._session is not None else None
        async with self._turn(thread_id or str(uuid4()), request, services, journal, turn_id, turn_kind,
                              session=commit):
            if self.typed_state:
                return await self.graph.ainvoke(update, config)
            self._validate({'type': 'object'}, update, 'START', 'input')
            session = {}
            if commit is not None:
                session = await commit.load()
                self._turn_state.get()['session'] = session
            prompts = self._prompt_texts()
            result = await self.graph.ainvoke(dict(values={'START': deepcopy(update)}, ancestry=(),
                feedback={}, previous={}, prompts=prompts, visit_id=None, attempt_id=None, starts={}, expired={}),
                {'recursion_limit': self._recursion_limit})
            if commit is not None:
                commit.merged = self._merge_session(session, self._turn_state.get()['runs'])
            return result['values']

    def _session_commit(self, thread_id):
        # Graph IDs never contain ':', so the prefix keeps graphs that share a checkpointer apart.
        return _SessionCommit(
            self._session, {'configurable': {'thread_id': f'{self.definition.graph_id}:{thread_id}'}},
            {row['Field'] for row in self.session_rows})

    async def session(self, thread_id):
        """A deep copy of the session committed for ``thread_id``; empty before any turn commits one."""
        if self._session is None:
            raise WorkflowError('Workflow declares no Session')
        if type(thread_id) is not str or not thread_id:
            raise ValueError('thread_id is required to read a session')
        return await self._session_commit(thread_id).load()

    def _merge_session(self, session, runs):
        """Fold this turn's completed node runs into a copy of the start-of-turn session."""
        merged = deepcopy(session)
        for row, value in session_contract.writes(self.session_rows, runs):
            if row['Merge'] == 'replace':
                merged[row['Field']] = deepcopy(value)
            elif value is None:
                continue
            elif type(value) is list:
                merged[row['Field']] = [*merged.get(row['Field'], ()), *deepcopy(value)]
            else:
                raise WorkflowError(f'Session field {row["Field"]} appends an array but {row["Source"]} is not one')
        return merged



def diagram_layout(layout, *, component_passes=None):
    """Render the same sibling routing used by compilation, grouping ownership."""
    lines = ['```mermaid', 'flowchart TD']
    parents = [None, *(step.id for step in layout.steps if step.kind == 'component')]
    for parent in parents:
        children = [step for step in layout.steps if step.parent == parent]
        scope = parent or 'GRAPH'
        if parent is not None:
            lines.append(f'    subgraph {parent}["component {parent}"]')
        lines.extend([f'    {scope}_START([START])', f'    {scope}_END([END])'])
        for step in children:
            if step.kind == 'node':
                lines.append(f'    {step.id}["node {step.id}"]')
        entry = [row for row in layout.tables['Edges'] if row['From'] == 'START' and next(
            item.parent for item in layout.steps if item.id == row['To']) == parent]
        for row in entry or [dict(When=None, To=children[0].id)]:
            label = '' if row['When'] is None else f'|{row["When"]}|'
            lines.append(f'    {scope}_START -->{label} {row["To"]}')
        for index, step in enumerate(children):
            edges = [row for row in layout.tables['Edges'] if row['From'] == step.id]
            if edges:
                for edge in edges:
                    target = scope + '_END' if edge['To'] == 'END' else edge['To']
                    lines.append(f'    {step.id} -->|{edge["When"]}| {target}')
            else:
                target = children[index + 1].id if index + 1 < len(children) else scope + '_END'
                lines.append(f'    {step.id} --> {target}')
        if parent is not None:
            component = next(row for row in layout.tables['Components'] if row['ID'] == parent)
            maximum = (component_passes or {}).get(parent)
            controller = component_controller(component, maximum=maximum)
            if controller['accepted']:
                lines.append(f'    {scope}_DECISION{{"guard {controller["guard"]} · attempt / {controller["max_passes"]}"}}')
                lines.append(f'    {scope}_ACCEPTED([accepted])')
                lines.append(f'    {scope}_EXHAUSTED([exhausted error])')
                lines.append(f'    {scope}_END --> {scope}_DECISION')
                lines.append(f'    {scope}_DECISION -->|accepted| {scope}_ACCEPTED')
                if controller['retry']:
                    lines.append(f'    {scope}_DECISION -->|rejected, attempt < {controller["max_passes"]}| {scope}_START')
                lines.append(f'    {scope}_DECISION -->|rejected, attempt = {controller["max_passes"]}| {scope}_EXHAUSTED')
            lines.append('    end')
    lines.extend(['```', '', 'Source links:', ''])
    lines.extend(f'- [{step.id}]({step.source.as_uri()})' for step in layout.steps)
    lines.extend(f'- [{row["Node"]} prompt]({row["File"].as_uri()})' for row in layout.tables['Prompts'])
    return '\n'.join(lines) + '\n'
