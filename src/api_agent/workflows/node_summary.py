"""Declarative, request-stable display explanations over borrowed inference."""
from collections.abc import MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import replace
import json

from referencing.jsonschema import DRAFT202012

from api_agent.providers.contracts import ContractError
from api_agent.providers.llm import Message, ResponsePolicy, StructuredOutput, TextContent
from api_agent.providers.presentation import call_progress_bound, present_summary, publish_call_progress
from api_agent.providers.rich_inference import async_valid_schema, response_json

_selection = ContextVar('workflow_display_selection', default=None)


def _embedded_schema(schema):
    """Rebase root-relative references only at actual JSON Schema positions."""
    root = deepcopy(schema)
    pending = [root]
    while pending:
        node = pending.pop()
        if type(node) is dict:
            if '$ref' in node:
                node['$ref'] = '#/properties/result/' + node['$ref'][2:]
            pending.extend(DRAFT202012.subresources_of(node))
    return root


@contextmanager
def display_selection(selection):
    """Bind the exact catalog selection without inferring capability from names."""
    token = _selection.set(selection)
    try:
        yield
    finally:
        _selection.reset(token)


class SummaryServices(MutableMapping):
    """Project after history has saved the exact provider request and response."""
    def __init__(self, services, policy):
        self.services, self.policy = services, policy

    def __getitem__(self, key):
        value = self.services[key]
        if key not in ('llm', 'complete') or not callable(value):
            return value

        async def complete(request, *args, **kwargs):
            selection = _selection.get()
            native = (selection is not None and selection.selected_llm == request.selected_llm
                      and 'display_reasoning' in selection.features
                      and request.payload.generation.reasoning_effort != 'off')
            if self.policy == 'native' or self.policy == 'auto' and native:
                prepared = replace(request, payload=replace(request.payload,
                    generation=replace(request.payload.generation, display_summary=True)))
                result = await value(prepared, *args, **kwargs)
                await present_summary(result, request_id=request.request_id)
                return result
            response = request.payload.response
            if response is None or response.schema is None:
                raise ContractError('Generated node summaries require a declared JSON response schema')
            original = json.loads(response.schema.schema)
            envelope = {'type': 'object', 'properties': {
                'progress_summary': {'type': 'string', 'minLength': 1, 'maxLength': 4096,
                    'description': 'A brief user-facing explanation of this result; do not expose private deliberation.'},
                'result': _embedded_schema(original)}, 'required': ['progress_summary', 'result'], 'additionalProperties': False}
            structured = StructuredOutput(response.schema.name, json.dumps(envelope), response.schema.strict)
            prepared = replace(request, payload=replace(request.payload,
                response=ResponsePolicy(response.mode, response.fallback, structured)))
            result = await value(prepared, *args, **kwargs)
            if result.outcome != 'completed':
                await present_summary(result, request_id=request.request_id)
                return result
            decoded = response_json(result)
            if not await async_valid_schema(envelope, decoded) or not decoded['progress_summary'].strip():
                raise ContractError('Generated node summary does not match its envelope schema')
            summary = result.reasoning_summary or 'Explanation: ' + decoded['progress_summary']
            # This is an application projection, not a second provider result.
            # The original continuation/evidence remains in the history owner.
            projected = replace(result, payload=Message('assistant', (TextContent(json.dumps(decoded['result'], ensure_ascii=False)),)),
                                response=None, reasoning_summary=summary)
            await present_summary(projected, request_id=request.request_id)
            return projected
        return complete

    def __setitem__(self, key, value):
        self.services[key] = value

    def __delitem__(self, key):
        del self.services[key]

    def __iter__(self):
        return iter(self.services)

    def __len__(self):
        return len(self.services)


class ProgressServices(MutableMapping):
    """Report each completed model call to the request's progress sink, outermost."""
    def __init__(self, services, node, attempt):
        self.services, self.node, self.attempt = services, node, attempt

    @classmethod
    def bind(cls, services, node, attempt):
        return cls(services, node, attempt) if call_progress_bound() else services

    def __getitem__(self, key):
        value = self.services[key]
        if key not in ('llm', 'complete') or not callable(value):
            return value

        async def complete(request, *args, **kwargs):
            result = await value(request, *args, **kwargs)
            await publish_call_progress(self.node, self.attempt, request.request_id, result)
            return result
        return complete

    def __setitem__(self, key, value):
        self.services[key] = value

    def __delitem__(self, key):
        del self.services[key]

    def __iter__(self):
        return iter(self.services)

    def __len__(self):
        return len(self.services)
