"""LangGraph compilation, owned nested pass budgets and strict node contracts."""
from .run_context import recording_enabled
from .run_context import current_structural_scope, structural_scope
import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextlib import nullcontext
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
import inspect
import json
import os
from pathlib import Path
import time
from typing import TypedDict
from uuid import uuid4

from jsonschema import Draft202012Validator
from langgraph.graph import END, START, StateGraph

from api_agent.providers.progress import component_timeout
from api_agent.runtime.tasks import offload
from .predicates import component_controller, component_decision, native_condition_id, parse_predicate, predicate_fields


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
            async with component_timeout('Workflow correction authority', 30):
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
_CURRENT_SERVICES = ContextVar('workflow_invocation_services', default=None)


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
        # Compatibility for callers using the earlier explicit resolver API.
        resolver = self.services.get('tools')
        if resolver is None:
            raise WorkflowError('Workflow has no admitted tool bindings')
        borrowed = resolver.for_port(scope_kind='node', scope=self.node_id, port=port)
        context = self
        class EffectTool:
            def __getattr__(self, name):
                return getattr(borrowed, name)
            async def invoke(self, prepared):
                context.mark_effect()
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


class _State(TypedDict):
    values: dict
    ancestry: tuple
    feedback: dict
    previous: dict
    prompts: dict
    visit_id: str | None
    attempt_id: str | None


def resolve_timeouts(layout, config, definition=None):
    """Resolve only declared scope budgets from captured configuration defaults."""
    from api_agent.config.declarations import schema_from_dict, UNSET
    import tomllib

    values, keys = {}, {}
    for row in layout.tables['Timeouts']:
        try:
            source_bytes = (definition.contents[row['Source']] if definition is not None
                            else row['Source'].read_bytes())
            schema = schema_from_dict(tomllib.loads(source_bytes.decode('utf-8')))
        except (OSError, UnicodeError, ValueError) as error:
            layout.error(str(error), row['line'])
        field = schema.fields.get(row['Key'])
        if field is None or field.type not in ('int', 'float'):
            layout.error('timeout must reference a numeric configuration declaration', row['line'])
        value = config.get(row['Key'], None if field.default is UNSET else field.default)
        if value is not None and (type(value) not in (int, float) or not 0 < value < float('inf')):
            layout.error('timeout must be finite positive seconds or omitted', row['line'])
        values[row['Owner']] = value
        keys[row['Owner']] = row['Key']
    return values, keys


def resolve_component_passes(layout, config):
    """Resolve captured component limits against explicit effective configuration."""
    from .layout import MAX_COMPONENT_PASSES

    passes, constants = {}, {}
    for row in layout.tables.get('Components', ()):
        key = row.get('max_passes_key')
        default = row.get('max_passes_default')
        if default is None:
            default = int(row['Max passes'])
        value = config.get(key, default) if key is not None else default
        if type(value) is not int or not 1 <= value <= MAX_COMPONENT_PASSES:
            layout.error(f'Max passes must be an integer from 1 to {MAX_COMPONENT_PASSES}', row.get('line'))
        if row['Guard'] == '-' and value != 1:
            layout.error('unguarded component requires one pass', row.get('line'))
        passes[row['ID']] = value
        if key is not None:
            constants[key] = value
    return passes, constants


class Workflow:
    def __init__(self, layout, registry, conditions=None, *, config=None, services=None):
        self.definition = layout if hasattr(layout, 'definition_id') else None
        if self.definition is not None:
            layout = self.definition.layout
        self.layout, self.registry = layout, registry
        self.conditions = conditions or {}
        self.config, self.services = config or {}, services or {}
        self._predicates = {}
        self.functions = {row['ID']: row for row in layout.tables['Functions']}
        self.components = {row['ID']: row for row in layout.tables['Components']}
        self.timeouts, self.timeout_keys = resolve_timeouts(layout, self.config, self.definition)
        self.component_passes, self.pass_constants = resolve_component_passes(layout, self.config)
        steps = {step.id: step for step in layout.steps}
        if self.definition is not None:
            self.definition.verify_native_sources()
        for row in self.functions.values():
            function = registry.get(row['Registry'])
            if (not isinstance(function, NodeFunction) or function.kind not in ('llm', 'deterministic')
                    or not callable(function.invoke)
                    or (self.definition is not None and function.kind != row['Kind'])):
                layout.error('unknown or invalid registered node function', row['line'])
            if 'Output mode' in row and ((function.kind == 'deterministic') != (row['Output mode'] == '-')):
                layout.error('response declaration must match registered node kind', row['line'])
            try:
                source = inspect.getsourcefile(function.invoke)
                matches_source = source is not None and Path(source).samefile(steps[row['ID']].source)
            except (TypeError, OSError):
                matches_source = False
            if not matches_source:
                layout.error('registered function does not match its declared source link', row['line'])
            if self.definition is not None:
                from .definition import native_origin
                path = steps[row['ID']].source
                if native_origin(function.invoke) != (self.definition.definition_id, path):
                    layout.error('native callable does not belong to the admitted definition', row['line'])
        for row in (*layout.tables['Edges'], *layout.tables['Prompts']):
            when = row['When']
            if when == 'always' or (when == 'otherwise' and row in layout.tables['Edges']):
                continue
            if native_condition_id(when) and callable(self.conditions.get(when)):
                continue
            if row in layout.tables['Prompts']:
                layout.error('unknown registered condition', row['line'])
            try:
                self._predicates[row['From'], when] = parse_predicate(when, predicate_fields(layout, row['From']))
            except ValueError as error:
                layout.error(str(error), row['line'])
        self.graph = self._compile(None)

    def _run_services(self):
        current = _CURRENT_SERVICES.get()
        return current[1] if current is not None and current[0] is self else self.services

    def _capture_services(self):
        current = _CURRENT_SERVICES.get()
        return current[2] if current is not None and current[0] is self else self.services

    def _event(self, event, **values):
        journal = _CURRENT_JOURNAL.get() or self._run_services().get('journal')
        if journal is not None:
            row = dict(event=event, **values)
            if event in ('scope_start', 'scope_end') and row.get('owner') == 'GRAPH' and row.get('kind') == 'graph':
                row['parent_id'] = None
            journal.observe(row)
        observer = self._run_services().get('observe')
        if observer is not None:
            observer(dict(event=event, **values))

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
                try:
                    self._event('scope_start', **common)
                    yield budget
                    self._check_deadline(budget)
                    outcome = 'passed'
                except BaseException as error:
                    from api_agent.telemetry.outcomes import report_failure, OutcomeState
                    failed = report_failure(error,
                        observations=_CURRENT_JOURNAL.get() or self._run_services().get('journal'),
                        scope_ids=(budget.id,), terminal=owner == 'GRAPH' and metadata.get('kind') == 'graph' and parent_id is None,
                        deadline_expired=budget.deadline is not None and time.monotonic() >= budget.deadline)
                    outcome = 'cancelled' if failed.state == OutcomeState.CANCELLED else 'failed'
                    raise
                finally:
                    try:
                        self._event('scope_end', **common, stopped_ms=time.time() * 1000,
                                    elapsed=time.monotonic() - budget.started, outcome=outcome)
                    except Exception:
                        if outcome == 'passed':
                            raise

    @staticmethod
    def _check_deadline(budget):
        if budget.remaining == 0:
            raise TimeoutError('Workflow component deadline expired')

    def _condition(self, identifier, values, *, source=None, ancestry=()):
        if (source, identifier) in self._predicates:
            predicate = self._predicates[source, identifier]
            attempt, maximum = (ancestry[-1][1:] if ancestry else (1, 1))
            result = predicate.evaluate(values, attempt=attempt, max_passes=maximum,
                                        constants=self.pass_constants)
        else:
            result = self.conditions[identifier](deepcopy(values))
        if type(result) is not bool:
            raise WorkflowError(f'Condition {identifier} must return a boolean')
        return result

    def _compile(self, parent):
        children = [item for item in self.layout.steps if item.parent == parent]
        graph = StateGraph(_State)
        for step in children:
            graph.add_node(step.id, self._component(step) if step.kind == 'component' else self._node(step))
        def enter(state):
            self._event('edge', source='START', target=children[0].id,
                        owner=parent or 'GRAPH', implicit=True,
                        attempt_id=state.get('attempt_id'))
            return children[0].id
        graph.add_conditional_edges(START, enter, {children[0].id: children[0].id})
        for index, step in enumerate(children):
            edges = [row for row in self.layout.tables['Edges'] if row['From'] == step.id]
            if not edges:
                target = children[index + 1].id if index + 1 < len(children) else 'END'
                def implicit_route(state, source=step.id, target=target):
                    self._event('edge', source=source, target=target,
                                owner=parent or 'GRAPH', condition='always', implicit=True,
                                visit_id=state.get('visit_id'), attempt_id=state.get('attempt_id'))
                    return target
                graph.add_conditional_edges(step.id, implicit_route,
                                            {target: END if target == 'END' else target})
            else:
                def route(state, edges=edges, step=step):
                    matches = [row for row in edges if row['When'] not in ('always', 'otherwise') and self._condition(row['When'], state['values'], source=step.id, ancestry=state['ancestry'])]
                    fallback = [row for row in edges if row['When'] in ('always', 'otherwise')]
                    choices = matches or fallback
                    if len(choices) != 1:
                        raise WorkflowError('Routing requires exactly one matching condition or fallback')
                    chosen = choices[0]
                    self._event('edge', source=step.id, target=chosen['To'],
                                owner=parent or 'GRAPH', condition=chosen['When'], implicit=False,
                                visit_id=state.get('visit_id'), attempt_id=state.get('attempt_id'))
                    return chosen['To']
                graph.add_conditional_edges(step.id, route, {row['To']: END if row['To'] == 'END' else row['To'] for row in edges})
        # This executor owns invocation-local state, never a checkpoint store.
        # Do not inherit a stateful caller's saver for an independently run graph.
        return graph.compile(checkpointer=False)

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
            current = dict(state)
            feedback = {}
            async with self._scope(step.id, kind='component', component=step.id, attempt=1,
                                   attempt_id=state.get('attempt_id'), max_passes=maximum) as budget:
                frame = _RepairFrame(self, step.id, maximum, budget)
                async with _repair_owner(frame if controller['accepted'] else None):
                    return await passes(frame, current, budget, original_ancestry, original_feedback, original_previous, original_attempt_id, descendants)
        async def passes(frame, current, budget, original_ancestry, original_feedback, original_previous, original_attempt_id, descendants):
            feedback = {}
            while frame.attempt <= maximum:
                attempt = frame.attempt
                attempt_id = str(uuid4())
                previous = {key: value for key, value in current['values'].items() if key in descendants} if frame.guard_attempt > 1 else {}
                current = dict(current, values={key: value for key, value in current['values'].items() if key not in descendants},
                               previous=previous, ancestry=(*original_ancestry, (step.id, attempt, maximum)), feedback=feedback,
                               attempt_id=attempt_id)
                self._event('attempt', scope_id=budget.id, component=step.id, attempt=attempt,
                            attempt_id=attempt_id, max_passes=maximum)
                current = await graph.ainvoke(current, {'recursion_limit': len(self.layout.steps) + 1})
                attempt = frame.attempt
                if not controller['accepted']:
                    return dict(current, ancestry=self._repair_ancestry(original_ancestry), feedback=original_feedback,
                                previous=original_previous, attempt_id=original_attempt_id,
                                visit_id=budget.id)
                if row['Guard'] not in current['values']:
                    raise WorkflowError(f'Component {step.id} did not execute its guard in the current pass')
                guard = GuardResult.from_output(current['values'][row['Guard']])
                self._event('guard', scope_id=budget.id, component=step.id, attempt=attempt, accepted=guard.accepted)
                if component_decision(controller, guard.accepted, attempt) == 'accepted':
                    return dict(current, ancestry=self._repair_ancestry(original_ancestry), feedback=original_feedback,
                                previous=original_previous, attempt_id=original_attempt_id,
                                visit_id=budget.id)
                if frame.effect:
                    raise WorkflowError('Component correction cannot replay dispatched effects')
                feedback = await self._safe_feedback(guard.feedback)
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
            invocation = _CURRENT_SERVICES.get()
            services = invocation[3].services(self._run_services(), step.id)
            if row.get('Display summary', '-') != '-':
                from .node_summary import SummaryServices
                services = SummaryServices(services, row['Display summary'])
            context = NodeContext(step.id, state['ancestry'], tuple(state['prompts'][item['File']] for item in prompts
                if item['When'] == 'always' or self._condition(item['When'], values)), services,
                budget.id, state.get('attempt_id'), row, state.get('_correction'),
                state.get('_rejected'), state.get('_correction_attempt', 0), state.get('_guard_attempt', 1),
                state.get('_repair_limit'))
            state['_node_context'] = context
            for binding in bindings:
                source = binding['From']
                sources = {'START': values['START'], 'PREVIOUS': state['previous'].get(step.id, {}),
                           'FEEDBACK': state['feedback'], 'ATTEMPT': {'value': context.attempt},
                           'MAX_PASSES': {'value': context.max_passes}}
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
                    inputs[binding['Input']] = deepcopy(binding['default_value'])
                else:
                    raise WorkflowError(f'Missing required binding for {step.id}.{binding["Input"]}')
            self._validate(row['Input schema'], inputs, step.id, 'input')
            journal = _CURRENT_JOURNAL.get() or self._run_services().get('journal')
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
            try:
                self._validate(row['Output schema'], result, step.id, 'output')
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
            await capture_node(journal, self._capture_services(), visit_id=budget.id,
                direction='output', values=result, schema=row['Output schema'],
                plain_fields=plain_fields.get('output', ...))
            self._event('node', scope_id=budget.id, node=step.id, component=step.parent, attempt=context.attempt,
                        elapsed=time.monotonic() - started, kind=function.kind)
            return dict(state, values={**values, step.id: deepcopy(result)}, visit_id=budget.id)
        async def timed(state):
            frame = next((item for item in reversed(_REPAIRS.get()) if item.workflow is self), None)
            current = dict(state)
            correction_attempt = 0
            while True:
                if frame is not None:
                    self._check_deadline(frame.budget)
                current['ancestry'] = self._repair_ancestry(current['ancestry'])
                context = None
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
                    history = _CURRENT_SERVICES.get()[3]
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

    async def arun(self, inputs, *, turn_id=None, turn_kind='initial'):
        from .capture_authority import resolve_capture_services
        from .run_context import canonical_uuid
        if turn_id is not None:
            canonical_uuid(turn_id)
        selected = resolve_capture_services(self.services)
        from .inference_history import InferenceHistory
        history = InferenceHistory()
        token = _CURRENT_SERVICES.set((self, dict(selected), selected, history))
        try:
            from api_agent.tooling.runtime import invocation_tools
            async with invocation_tools(self.definition, selected):
                return await self._arun(inputs, turn_id=turn_id, turn_kind=turn_kind)
        finally:
            history.close()
            _CURRENT_SERVICES.reset(token)

    async def _arun(self, inputs, *, turn_id=None, turn_kind='initial'):
        from .capture_authority import requested_log_content
        from .run_context import canonical_uuid, current_execution_context, current_turn_id, execution_context, turn
        if turn_id is not None:
            canonical_uuid(turn_id)
        turn_id = turn_id if turn_id is not None else current_turn_id()
        journal = self._run_services().get('journal')
        automatic_observations = None
        factory = self._run_services().get('observation_factory')
        if journal is None and factory is not None and self.definition is not None and not recording_enabled():
            from .identity import workflow_identity
            journal = automatic_observations = factory.create(self.definition, turn_id=turn_id,
                component_passes=self.component_passes,
                semantic_identity=workflow_identity(self.definition, timeouts=self.timeouts,
                    component_passes=self.component_passes))
        if turn_id is not None and journal is not None and turn_id != journal.turn_id:
            raise ValueError('explicit turn identity disagrees with the active journal')
        if journal is not None and self.definition is not None:
            from .identity import workflow_identity
            identity = workflow_identity(self.definition, timeouts=self.timeouts,
                                         component_passes=self.component_passes)
            if (getattr(journal, 'semantic_identity', None) is not None
                    and journal.semantic_identity != identity):
                raise WorkflowError("Workflow journal semantic identity differs from effective budgets")
            from .journal import projection
            if journal.projection != projection(self.definition, component_passes=self.component_passes):
                raise WorkflowError('Workflow journal uses a different effective component pass limit')
        selected_context = current_execution_context()
        if journal is not None:
            selected_context = journal.admit_execution_context(selected_context)
            admit_identity = getattr(journal, 'admit_semantic_identity', None)
            if self.definition is not None and admit_identity is not None:
                admit_identity(identity)
        if recording_enabled():
            if self.definition is None:
                raise WorkflowError('Reporting requires an admitted workflow definition')
            from .provenance import verified_documents
            proof = self._run_services().get('packaged_proof')
            source_manifest = verified_documents(self.definition, packaged_proof=proof)
            from .journal import WorkflowJournal
            from .identity import workflow_identity
            journal = WorkflowJournal(self.definition,
                semantic_identity=workflow_identity(self.definition, timeouts=self.timeouts,
                                                    component_passes=self.component_passes),
                component_passes=self.component_passes,
                log_content=requested_log_content(self.config, self._run_services()),
                source_manifest=source_manifest,
                boundary=self._run_services().get('emissions'),
                content_policy=self._run_services().get('content_policy'),
                turn_id=turn_id,
                test_id=os.environ.get('PYTEST_CURRENT_TEST', '').rsplit(' ', 1)[0] or None)
            journal.admit_execution_context(selected_context)
            factory = self._run_services().get('observation_factory')
            if factory is not None:
                journal = factory.create(self.definition, journal=journal)
        token = _CURRENT_JOURNAL.set(journal)
        try:
            active_turn = turn_id if turn_id is not None else (journal.turn_id if journal is not None else None)
            if active_turn is not None:
                from api_agent.testing.inference_usage import user_turn
            with execution_context(selected_context), (turn(active_turn) if active_turn is not None else nullcontext()), (
                    user_turn(active_turn, kind=turn_kind) if active_turn is not None else nullcontext()):
                async with self._scope('GRAPH', kind='graph', attempt=1, max_passes=1):
                    self._validate({'type': 'object'}, inputs, 'START', 'input')
                    prompts = {path: (self.definition.contents[path].decode('utf-8') if self.definition is not None
                                      else path.read_text(encoding='utf-8'))
                               for path in dict.fromkeys(row['File'] for row in self.layout.tables['Prompts'])}
                    result = await self.graph.ainvoke(dict(values={'START': deepcopy(inputs)}, ancestry=(), feedback={}, previous={}, prompts=prompts,
                                                         visit_id=None, attempt_id=None),
                                                     {'recursion_limit': len(self.layout.steps) + 1})
                    terminal = self._run_services().get('execution_outcome')
                    if terminal is not None:
                        from api_agent.telemetry.outcomes import ExecutionOutcome, report_outcome
                        final = terminal()
                        if type(final) is not ExecutionOutcome:
                            raise WorkflowError('Invalid explicit execution outcome')
                        report_outcome(final, runtime=getattr(journal, 'trace', None))
                        if journal is not None and hasattr(journal, 'report_outcome'):
                            journal.report_outcome(current_structural_scope(), final)
            return result['values']
        finally:
            _CURRENT_JOURNAL.reset(token)
            if automatic_observations is not None:
                automatic_observations.close()
            if recording_enabled() and journal is not None:
                from .evidence import publish
                publish(journal.finalize())

    def diagram(self):
        return diagram_layout(self.layout, component_passes=self.component_passes)


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
        lines.append(f'    {scope}_START --> {children[0].id}')
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
