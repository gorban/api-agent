"""LangGraph compilation, owned nested pass budgets and strict node contracts."""
import asyncio
from contextlib import asynccontextmanager
from contextlib import nullcontext
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
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
        return self.ancestry[-1][1] if self.ancestry else 1

    @property
    def max_passes(self):
        return self.ancestry[-1][2] if self.ancestry else 1


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

    def _event(self, event, **values):
        journal = _CURRENT_JOURNAL.get() or self.services.get('journal')
        if journal is not None:
            journal.observe(dict(event=event, **values))
        observer = self.services.get('observe')
        if observer is not None:
            observer(dict(event=event, **values))

    @asynccontextmanager
    async def _scope(self, owner, **metadata):
        declared = self.timeouts.get(owner)
        async with component_timeout('Workflow ' + owner, declared) as budget:
            started = time.time() * 1000
            common = dict(scope_id=budget.id, parent_id=budget.parent_id, owner=owner,
                          started_ms=started, declared_timeout=declared,
                          effective_timeout=budget.timeout, timeout_key=self.timeout_keys.get(owner), **metadata)
            outcome = 'failed'
            try:
                self._event('scope_start', **common)
                yield budget
                self._check_deadline(budget)
                outcome = 'passed'
            except asyncio.CancelledError:
                outcome = 'failed' if budget.deadline is not None and time.monotonic() >= budget.deadline else 'cancelled'
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
        return graph.compile()

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
                for attempt in range(1, maximum + 1):
                    attempt_id = str(uuid4())
                    previous = {key: value for key, value in current['values'].items() if key in descendants} if attempt > 1 else {}
                    current = dict(current, values={key: value for key, value in current['values'].items() if key not in descendants},
                                   previous=previous, ancestry=(*original_ancestry, (step.id, attempt, maximum)), feedback=feedback,
                                   attempt_id=attempt_id)
                    self._event('attempt', scope_id=budget.id, component=step.id, attempt=attempt,
                                attempt_id=attempt_id, max_passes=maximum)
                    current = await graph.ainvoke(current, {'recursion_limit': len(self.layout.steps) + 1})
                    if not controller['accepted']:
                        return dict(current, ancestry=original_ancestry, feedback=original_feedback,
                                    previous=original_previous, attempt_id=original_attempt_id,
                                    visit_id=budget.id)
                    if row['Guard'] not in current['values']:
                        raise WorkflowError(f'Component {step.id} did not execute its guard in the current pass')
                    guard = GuardResult.from_output(current['values'][row['Guard']])
                    self._event('guard', scope_id=budget.id, component=step.id, attempt=attempt, accepted=guard.accepted)
                    if component_decision(controller, guard.accepted, attempt) == 'accepted':
                        return dict(current, ancestry=original_ancestry, feedback=original_feedback,
                                    previous=original_previous, attempt_id=original_attempt_id,
                                    visit_id=budget.id)
                    feedback = guard.feedback
                raise WorkflowError(f'Component {step.id} exhausted {maximum} passes')
        return execute

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
            context = NodeContext(step.id, state['ancestry'], tuple(state['prompts'][item['File']] for item in prompts
                if item['When'] == 'always' or self._condition(item['When'], values)), self.services,
                budget.id, state.get('attempt_id'))
            for binding in bindings:
                source = binding['From']
                sources = {'START': values['START'], 'PREVIOUS': state['previous'].get(step.id, {}),
                           'FEEDBACK': state['feedback'], 'ATTEMPT': {'value': context.attempt},
                           'MAX_PASSES': {'value': context.max_passes}}
                payload = sources.get(source, values.get(source, {}))
                available = (source in ('START', 'ATTEMPT', 'MAX_PASSES')
                             or (source == 'PREVIOUS' and step.id in state['previous'])
                             or (source == 'FEEDBACK' and (bool(payload) or context.attempt > 1))
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
            journal = _CURRENT_JOURNAL.get() or self.services.get('journal')
            plain_fields = (self.services.get('plain_io_fields') or {}).get(step.id, {})
            if journal is not None:
                journal.capture(visit_id=budget.id, direction='input', values=inputs,
                                schema=row['Input schema'], plain_fields=plain_fields.get('input'))
            started = time.monotonic()
            from .attribution import bind_workflow_visit
            with bind_workflow_visit(journal, budget.id, state.get('attempt_id')):
                result = (function.invoke(inputs, context) if inspect.iscoroutinefunction(function.invoke)
                          else await offload(function.invoke, inputs, context))
                if inspect.isawaitable(result):
                    result = await result
            self._check_deadline(budget)
            self._validate(row['Output schema'], result, step.id, 'output')
            if journal is not None:
                journal.capture(visit_id=budget.id, direction='output', values=result,
                                schema=row['Output schema'], plain_fields=plain_fields.get('output'))
            self._event('node', scope_id=budget.id, node=step.id, component=step.parent, attempt=context.attempt,
                        elapsed=time.monotonic() - started, kind=function.kind)
            return dict(state, values={**values, step.id: deepcopy(result)}, visit_id=budget.id)
        async def timed(state):
            async with self._scope(step.id, kind='node', node=step.id, component=step.parent,
                                   attempt=state['ancestry'][-1][1] if state['ancestry'] else 1,
                                   attempt_id=state.get('attempt_id'),
                                   max_passes=state['ancestry'][-1][2] if state['ancestry'] else 1) as budget:
                return await execute(state, budget)
        return timed

    @staticmethod
    def _validate(schema, value, node, direction):
        errors = tuple(Draft202012Validator(schema).iter_errors(value))
        if errors:
            raise WorkflowError(f'Node {node} returned invalid {direction} structure at {list(errors[0].path)}')
        # JSON is the state/IPC contract; reject Python-only and nonfinite values.
        try:
            encoded = json.dumps(value, allow_nan=False)
            if json.loads(encoded) != value:
                raise ValueError('Python-only JSON representation')
        except (ValueError, TypeError):
            raise WorkflowError(f'Node {node} returned non-JSON {direction}') from None

    async def arun(self, inputs, *, turn_id=None, turn_kind='initial'):
        journal = self.services.get('journal')
        if journal is not None and self.definition is not None:
            from .journal import projection
            if journal.projection != projection(self.definition, component_passes=self.component_passes):
                raise WorkflowError('Workflow journal uses a different effective component pass limit')
        if os.environ.get('API_AGENT_ALLURE_REPORTING') == '1':
            if self.definition is None:
                raise WorkflowError('Reporting requires an admitted workflow definition')
            from .provenance import verified_documents
            proof = self.services.get('packaged_proof')
            source_manifest = verified_documents(self.definition, packaged_proof=proof)
            from .journal import WorkflowJournal
            journal = WorkflowJournal(self.definition,
                component_passes=self.component_passes,
                log_content=self.config.get('LOG_CONTENT') is True,
                source_manifest=source_manifest,
                boundary=self.services.get('emissions'),
                turn_id=turn_id,
                test_id=os.environ.get('PYTEST_CURRENT_TEST', '').rsplit(' ', 1)[0] or None)
        token = _CURRENT_JOURNAL.set(journal)
        try:
            active_turn = turn_id or (journal.turn_id if journal is not None else None)
            if active_turn is not None:
                from api_agent.testing.inference_usage import user_turn
            with user_turn(active_turn, kind=turn_kind) if active_turn is not None else nullcontext():
                async with self._scope('GRAPH', kind='graph', attempt=1, max_passes=1):
                    self._validate({'type': 'object'}, inputs, 'START', 'input')
                    prompts = {path: (self.definition.contents[path].decode('utf-8') if self.definition is not None
                                      else path.read_text(encoding='utf-8'))
                               for path in dict.fromkeys(row['File'] for row in self.layout.tables['Prompts'])}
                    result = await self.graph.ainvoke(dict(values={'START': deepcopy(inputs)}, ancestry=(), feedback={}, previous={}, prompts=prompts,
                                                         visit_id=None, attempt_id=None),
                                                     {'recursion_limit': len(self.layout.steps) + 1})
            return result['values']
        finally:
            _CURRENT_JOURNAL.reset(token)
            if os.environ.get('API_AGENT_ALLURE_REPORTING') == '1' and journal is not None:
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
