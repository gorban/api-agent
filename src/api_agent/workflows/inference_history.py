"""Invocation-local ordered requests for the runtime's borrowed LLM services."""
import asyncio
from collections.abc import MutableMapping
from concurrent.futures import Future
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
import inspect
import json
from threading import Lock

from api_agent.providers.contracts import ContractError, ProviderAccessError
from api_agent.providers.inference import request_to_wire
from api_agent.providers.llm import LLMRequest, LLMResult, Message, ResponseEvidence, TextContent


_DIALOGUE = ContextVar('workflow_inference_dialogue', default=())
MAX_HISTORY_BYTES = 32 * 1024 * 1024
MAX_DIALOGUES = 128


@contextmanager
def inference_dialogue(name):
    """Separate independent inference roles within one declared node."""
    if type(name) is not str or not name or len(name) > 200 or not name.isprintable():
        raise ContractError('Invalid workflow inference dialogue')
    token = _DIALOGUE.set((*_DIALOGUE.get(), name))
    try:
        yield
    finally:
        _DIALOGUE.reset(token)


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode())


@dataclass
class _Dialogue:
    callback: object = field(repr=False)
    authorities: tuple = field(default=(), repr=False)
    messages: tuple = field(default=(), repr=False)
    previous_input: tuple = field(default=(), repr=False)
    size: int = 0
    configuration_size: int = 0
    blocked: bool = False
    tail: Future = field(default_factory=Future)
    guard: Lock = field(default_factory=Lock)

    def __post_init__(self):
        self.tail.set_result(None)

    @asynccontextmanager
    async def own(self):
        # Concurrent futures allow a synchronous native node's separate event
        # loop to join the same queue without blocking either event loop.
        ticket = Future()
        with self.guard:
            previous, self.tail = self.tail, ticket
        try:
            await asyncio.shield(asyncio.wrap_future(previous))
        except BaseException:
            previous.add_done_callback(lambda _: ticket.set_result(None))
            raise
        try:
            yield
        finally:
            ticket.set_result(None)


class InferenceHistory:
    """Borrow callbacks, preserve message prefixes, and own no provider resources."""
    def __init__(self, *, max_bytes=MAX_HISTORY_BYTES, max_dialogues=MAX_DIALOGUES):
        if type(max_bytes) is not int or max_bytes < 1 or type(max_dialogues) is not int or max_dialogues < 1:
            raise ValueError('Invalid workflow inference history bounds')
        self.max_bytes, self.max_dialogues = max_bytes, max_dialogues
        self._entries, self._bytes, self._closed = {}, 0, False
        self._guard = Lock()

    def close(self):
        with self._guard:
            self._closed = True
            self._entries.clear()
            self._bytes = 0

    def services(self, values, node):
        return _Services(values, self, node)

    def _entry(self, node, service, callback, request, args, kwargs):
        payload = request.payload
        row = request_to_wire(request, _validate_instances=False)
        # Service extensions can carry authority under provider-owned parameter
        # names or positionally. Never infer that an unfamiliar argument is safe
        # to share between identities; retain every argument against ID reuse.
        identity = (tuple(map(id, args)), tuple(sorted((key, id(value)) for key, value in kwargs.items())))
        key = (node, service, id(callback), _DIALOGUE.get(), identity, request.selected_llm,
               payload.tools, payload.generation, payload.structured, payload.response,
               tuple(message for message in payload.messages if message.role == 'system'))
        with self._guard:
            if self._closed:
                raise ContractError('Workflow inference history is closed')
            if key in self._entries:
                return self._entries[key]
            # Keep owner/configuration identity in the same bounded retention
            # budget as messages. The entry retains its callback against ID reuse.
            row = dict(row, messages=[], request_id=None)
            size = _bytes(row)
            if len(self._entries) >= self.max_dialogues or self._bytes + size > self.max_bytes:
                raise ContractError('Workflow inference history budget exceeded before dispatch')
            authorities = (*args, *(kwargs[key] for key, _ in identity[1]))
            entry = _Dialogue(callback, authorities=authorities, size=size, configuration_size=size)
            self._entries[key] = entry
            self._bytes += size
            return entry

    @staticmethod
    def _extend(entry, incoming):
        history = entry.messages
        if not history or incoming[:len(history)] == history:
            return incoming
        previous = entry.previous_input
        if incoming[:len(previous)] == previous and len(incoming) > len(previous):
            extension = incoming[len(previous):]
            if extension[0].role == 'assistant' and extension[0] == history[-1]:
                return (*history, *extension[1:])
        common = 0
        for before, after in zip(previous, incoming):
            if before != after:
                break
            common += 1
        suffix = incoming[common:]
        if not suffix:
            suffix = tuple(message for message in incoming if message.role == 'user')[-1:]
        if not suffix or any(message.role == 'system' for message in suffix):
            raise ContractError('Workflow retry requires an unchanged system prefix and a user update')
        if suffix[0].role in ('assistant', 'tool'):
            raise ContractError('Workflow retry rewrites its accepted inference continuation')
        return (*history, *suffix)

    def _save(self, entry, request, incoming, response, evidence=None):
        messages = request.payload.messages
        if (request.payload.response is not None and request.payload.response.schema is not None
                and isinstance(evidence, ResponseEvidence)
                and evidence.actual_mode in ('json', 'prompted_json')):
            from api_agent.providers.response_format import json_instruction
            messages = (*messages, json_instruction(request.payload.response.schema.schema))
        messages = (*messages, response)
        size = (entry.configuration_size + _bytes([asdict(message) for message in messages])
                + _bytes([asdict(message) for message in incoming]))
        # A completed result is never discarded or converted into a new
        # inference failure merely because no further history can fit.
        with self._guard:
            if self._closed:
                return
            if self._bytes - entry.size + size > self.max_bytes:
                entry.blocked = True
                return
            self._bytes += size - entry.size
            entry.size = size
            entry.messages, entry.previous_input = messages, incoming

    async def call(self, node, service, callback, request, *args, **kwargs):
        # A nested graph borrows the underlying provider, not its parent's
        # transcript. Only unwrap our own service view; provider decorators and
        # evidence/credential boundaries remain intact.
        while isinstance(callback, _BorrowedInference):
            callback = callback.callback
        if not isinstance(request, LLMRequest):
            value = callback(request, *args, **kwargs)
            return await value if inspect.isawaitable(value) else value
        entry = self._entry(node, service, callback, request, args, kwargs)
        async with entry.own():
            if self._closed:
                raise ContractError('Workflow inference history is closed')
            if entry.blocked:
                raise ContractError('Workflow inference history budget exceeded before dispatch')
            incoming = request.payload.messages
            messages = self._extend(entry, incoming)
            if len(messages) > 64:
                raise ContractError('Workflow inference history exceeds the 64-message request bound')
            prepared = replace(request, payload=replace(request.payload, messages=messages))
            # Admit the complete request, including old continuations and tool
            # result identities, before entering the existing provider boundary.
            request_to_wire(prepared, _validate_instances=False)
            try:
                value = callback(prepared, *args, **kwargs)
                result = await value if inspect.isawaitable(value) else value
                if not isinstance(result, LLMResult):
                    raise ContractError('Workflow LLM service returned an invalid result')
                previous_calls = {call.call_id for message in messages for call in message.calls}
                identifiers = [call.call_id for call in result.payload.calls]
                if len(set(identifiers)) != len(identifiers) or previous_calls.intersection(identifiers):
                    error = ProviderAccessError('unknown', 'Workflow response repeats a tool call identity.',
                        code='tool_call_replay', type='tool_call_validation', usage=result.usage,
                        response=result.response)
                    error.origin = 'local_output_validation'
                    error.execution = 'completed'
                    raise error
            except BaseException as error:
                category = ('cancelled' if isinstance(error, asyncio.CancelledError) else
                            'timeout' if isinstance(error, TimeoutError) else 'failed')
                # Do not invent an assistant/tool response or relay raw provider
                # exception text, which may contain credentials or server internals.
                failure = Message('user', (TextContent(json.dumps({
                    'previous_inference': category, 'accepted_response': False,
                    'execution': 'unknown'})),))
                self._save(entry, prepared, incoming, failure, getattr(error, 'response', None))
                raise
            self._save(entry, prepared, incoming, result.payload, result.response)
            return result


@dataclass(repr=False)
class _BorrowedInference:
    history: InferenceHistory
    node: str
    service: str
    callback: object

    async def __call__(self, request, *args, **kwargs):
        return await self.history.call(self.node, self.service, self.callback, request, *args, **kwargs)


class _Services(MutableMapping):
    def __init__(self, values, history, node):
        self._values, self._history, self._node = values, history, node

    def __getitem__(self, key):
        value = self._values[key]
        if key not in ('llm', 'complete') or not callable(value):
            return value
        return _BorrowedInference(self._history, self._node, key, value)

    def __setitem__(self, key, value):
        self._values[key] = value

    def __delitem__(self, key):
        del self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)
