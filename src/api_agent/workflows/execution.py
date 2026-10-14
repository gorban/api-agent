"""LangGraph compilation, owned nested pass budgets and strict node contracts."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
import inspect
import json
from pathlib import Path
import time
from typing import TypedDict

from jsonschema import Draft202012Validator
from langgraph.graph import END, START, StateGraph

from api_agent.providers.progress import component_timeout
from api_agent.runtime.tasks import offload


class WorkflowError(RuntimeError):
    pass


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


class Workflow:
    def __init__(self, layout, registry, conditions=None, *, config=None, services=None):
        self.layout, self.registry = layout, registry
        self.conditions = conditions or {}
        self.config, self.services = config or {}, services or {}
        self.functions = {row['ID']: row for row in layout.tables['Functions']}
        self.components = {row['ID']: row for row in layout.tables['Components']}
        self.timeouts = {}
        self.timeout_keys = {}
        for row in layout.tables['Timeouts']:
            from api_agent.config.declarations import schema_from_dict, UNSET
            import tomllib
            try:
                schema = schema_from_dict(tomllib.loads(row['Source'].read_text(encoding='utf-8')))
            except (OSError, UnicodeError, ValueError) as error:
                layout.error(str(error), row['line'])
            field = schema.fields.get(row['Key'])
            if field is None or field.type not in ('int', 'float'):
                layout.error('timeout must reference a numeric configuration declaration', row['line'])
            value = self.config.get(row['Key'], None if field.default is UNSET else field.default)
            if value is not None and (type(value) not in (int, float) or not 0 < value < float('inf')):
                layout.error('timeout must be finite positive seconds or omitted', row['line'])
            self.timeouts[row['Owner']] = value
            self.timeout_keys[row['Owner']] = row['Key']
        steps = {step.id: step for step in layout.steps}
        for row in self.functions.values():
            function = registry.get(row['Registry'])
            if not isinstance(function, NodeFunction) or function.kind not in ('llm', 'deterministic') or not callable(function.invoke):
                layout.error('unknown or invalid registered node function', row['line'])
            try:
                source = inspect.getsourcefile(function.invoke)
            except TypeError:
                source = None
            if source is None or Path(source).resolve() != steps[row['ID']].source:
                layout.error('registered function does not match its declared source link', row['line'])
        for row in (*layout.tables['Edges'], *layout.tables['Prompts']):
            if row['When'] != 'always' and not callable(self.conditions.get(row['When'])):
                layout.error('unknown registered condition', row['line'])
        self.graph = self._compile(None)

    def _event(self, event, **values):
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

    def _condition(self, identifier, values):
        result = self.conditions[identifier](deepcopy(values))
        if type(result) is not bool:
            raise WorkflowError(f'Condition {identifier} must return a boolean')
        return result

    def _compile(self, parent):
        children = [item for item in self.layout.steps if item.parent == parent]
        graph = StateGraph(_State)
        for step in children:
            graph.add_node(step.id, self._component(step) if step.kind == 'component' else self._node(step))
        graph.add_edge(START, children[0].id)
        for index, step in enumerate(children):
            edges = [row for row in self.layout.tables['Edges'] if row['From'] == step.id]
            if not edges:
                graph.add_edge(step.id, children[index + 1].id if index + 1 < len(children) else END)
            else:
                def route(state, edges=edges, step=step):
                    matches = [row['To'] for row in edges if row['When'] != 'always' and self._condition(row['When'], state['values'])]
                    fallback = [row['To'] for row in edges if row['When'] == 'always']
                    targets = matches or fallback
                    if len(targets) != 1:
                        raise WorkflowError('Routing requires exactly one matching condition or fallback')
                    self._event('edge', source=step.id, target=targets[0])
                    return targets[0]
                graph.add_conditional_edges(step.id, route, {row['To']: END if row['To'] == 'END' else row['To'] for row in edges})
        return graph.compile()

    def _component(self, step):
        graph = self._compile(step.id)
        row = self.components[step.id]
        maximum = int(row['Max passes'])
        async def execute(state):
            original_ancestry, original_feedback = state['ancestry'], state['feedback']
            original_previous = state['previous']
            descendants = {item.id for item in self.layout.steps if self._owned_by(item, step.id)}
            current = dict(state)
            feedback = {}
            async with self._scope(step.id, kind='component', component=step.id, attempt=1, max_passes=maximum) as budget:
                for attempt in range(1, maximum + 1):
                    previous = {key: value for key, value in current['values'].items() if key in descendants} if attempt > 1 else {}
                    current = dict(current, values={key: value for key, value in current['values'].items() if key not in descendants},
                                   previous=previous, ancestry=(*original_ancestry, (step.id, attempt, maximum)), feedback=feedback)
                    self._event('attempt', scope_id=budget.id, component=step.id, attempt=attempt, max_passes=maximum)
                    current = await graph.ainvoke(current, {'recursion_limit': len(self.layout.steps) + 1})
                    if row['Guard'] not in current['values']:
                        raise WorkflowError(f'Component {step.id} did not execute its guard in the current pass')
                    guard = GuardResult.from_output(current['values'][row['Guard']])
                    self._event('guard', scope_id=budget.id, component=step.id, attempt=attempt, accepted=guard.accepted)
                    if guard.accepted:
                        return dict(current, ancestry=original_ancestry, feedback=original_feedback, previous=original_previous)
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
                if item['When'] == 'always' or self._condition(item['When'], values)), self.services)
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
            started = time.monotonic()
            result = (function.invoke(inputs, context) if inspect.iscoroutinefunction(function.invoke)
                      else await offload(function.invoke, inputs, context))
            if inspect.isawaitable(result):
                result = await result
            self._check_deadline(budget)
            self._validate(row['Output schema'], result, step.id, 'output')
            self._event('node', scope_id=budget.id, node=step.id, component=step.parent, attempt=context.attempt,
                        elapsed=time.monotonic() - started, kind=function.kind)
            return dict(state, values={**values, step.id: deepcopy(result)})
        async def timed(state):
            async with self._scope(step.id, kind='node', node=step.id, component=step.parent,
                                   attempt=state['ancestry'][-1][1] if state['ancestry'] else 1,
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

    async def arun(self, inputs):
        async with self._scope('GRAPH', kind='graph', attempt=1, max_passes=1):
            self._validate({'type': 'object'}, inputs, 'START', 'input')
            prompts = {path: path.read_text(encoding='utf-8') for path in dict.fromkeys(row['File'] for row in self.layout.tables['Prompts'])}
            result = await self.graph.ainvoke(dict(values={'START': deepcopy(inputs)}, ancestry=(), feedback={}, previous={}, prompts=prompts),
                                             {'recursion_limit': len(self.layout.steps) + 1})
        return result['values']

    def diagram(self):
        return diagram_layout(self.layout)


def diagram_layout(layout):
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
            lines.append(f'    {scope}_END -. reject, at most {component["Max passes"]} passes .-> {scope}_START')
            lines.append('    end')
    lines.extend(['```', '', 'Source links:', ''])
    lines.extend(f'- [{step.id}]({step.source.as_uri()})' for step in layout.steps)
    lines.extend(f'- [{row["Node"]} prompt]({row["File"].as_uri()})' for row in layout.tables['Prompts'])
    return '\n'.join(lines) + '\n'
