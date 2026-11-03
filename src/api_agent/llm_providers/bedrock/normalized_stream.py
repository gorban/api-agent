"""Validated stream frames and bounded credential-aware text release."""
from api_agent.providers.contracts import ContractError
from api_agent.providers.credential_guard import guard_json
from api_agent.providers.inference import result_from_wire, result_to_wire
from api_agent.providers.llm import StreamEvent, TextContent
from api_agent.providers.rejected_evidence import guard_rejected_evidence as guard_response_evidence
from api_agent.telemetry.redaction import registered_text_ranges, registered_values
from .mantle import StreamDecoder

_LIMIT = 524288


def guard_semantic(value, secrets):
    """Reject embedded serialized credentials as well as complete JSON leaves."""
    value = guard_json(value, secrets)
    def check(item):
        if type(item) is str:
            if registered_text_ranges(item, secrets):
                raise ContractError('Content cannot be safely released.')
        elif type(item) is dict:
            for key, child in item.items():
                check(key)
                check(child)
        elif type(item) is list:
            if item and all(type(child) is str for child in item):
                check(''.join(item))
            for child in item:
                check(child)
    check(value)
    return value


def guard_result(decoded, secrets):
    """Guard every caller-visible carrier before publishing a normalized result."""
    result = result_to_wire(decoded)
    try:
        guard_semantic(result, secrets)
        import json
        from api_agent.providers.response_carriers import observable_response_carriers
        text = ''.join(part.text for part in decoded.payload.content)
        arguments = (json.loads(call.arguments) for call in decoded.payload.calls)
        # A Responses item can carry both a display summary and separate native
        # plaintext. That undisplayed replay carrier still crosses private IPC.
        state = json.loads(decoded.payload.continuation)['state'] if decoded.payload.continuation else []
        native = ''.join(part['text'] for item in state if item.get('type') == 'reasoning' and item.get('summary')
                         for part in item.get('content') or [])
        for carrier in observable_response_carriers(text, arguments, summary=decoded.reasoning_summary,
                additional_text=(native,) if native else ()):
            guard_semantic(carrier, secrets)
    except ContractError as error:
        error.usage = result_from_wire(result).usage
        raise
    return result


def ambiguous_carrier(text):
    """Hold incomplete quoted, escaped or bracketed content for inspection."""
    stack = []
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == '\\':
            # An unquoted escape may itself be nested. Its endpoint cannot
            # be inferred reliably without a containing string.
            return True
        elif char in '{[':
            stack.append(char)
        elif char in '}]' and stack:
            if stack[-1] != ('{' if char == '}' else '['):
                return True
            stack.pop()
    return quoted or bool(stack)


def safe_boundary(text, maximum):
    """Do not leave a suffix starting inside a previously parsed carrier."""
    stack = []
    quoted = escaped = False
    safe = 0
    for index, char in enumerate(text[:maximum], 1):
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '{[':
            stack.append(char)
        elif char in '}]' and stack:
            stack.pop()
        if not quoted and not stack:
            safe = index
    return safe


class WorkerStream:
    def __init__(self, mode, model, secrets, *, endpoint="mantle", reasoning_format='auto'):
        self.decoder = StreamDecoder(mode, selected_llm=model, endpoint=endpoint, reasoning_format=reasoning_format)
        self.secrets = registered_values(secrets)
        self.lookbehind = max((len(s) for s in self.secrets), default=1) - 1
        if self.lookbehind > _LIMIT:
            raise ContractError('Credential protection limit exceeded')
        self.pending = ''
        self.quarantined = False
        self.has_tools = False
        self.summary_released = ''

    def _summary_frames(self):
        """Release bounded plaintext prefixes; retain a credential-sized tail."""
        summary = ''.join(self.decoder.reasoning.values()) if self.decoder.native_reasoning else ''.join(
            part['text'] for item in self.decoder.items if item.get('type') == 'reasoning'
            for part in item.get('summary', []))
        if len(summary.encode('utf-8')) > 65536:
            raise ContractError('Invalid normalized Bedrock summary')
        if self.quarantined or ambiguous_carrier(summary):
            return []
        guard_semantic(summary, self.secrets)
        guard_semantic(summary + self.decoder.text, self.secrets)
        guard_semantic(self.decoder.text + summary, self.secrets)
        count = len(summary)
        visible = 0
        while count and visible < self.lookbehind:
            count -= 1
            visible += summary[count].isprintable()
        count = safe_boundary(summary, count)
        prefix = summary[:count]
        if not prefix.startswith(self.summary_released):
            raise ContractError('Invalid normalized Bedrock summary')
        fragment = prefix[len(self.summary_released):]
        self.summary_released = prefix
        return [{'kind': 'reasoning_summary', 'text': fragment}] if fragment else []

    def feed(self, event):
        frames = []
        events = self.decoder.feed(event)
        self.has_tools = bool(self.decoder.tools) or any(item.get('type') == 'function_call' for item in self.decoder.items)
        if self.has_tools:
            self.quarantined = True
        frames.extend(self._summary_frames())
        for item in events:
            self.pending += item.payload.text
            if len(self.pending.encode('utf-8')) > _LIMIT:
                raise ContractError('Credential protection limit exceeded')
            if self.quarantined or ambiguous_carrier(self.pending):
                continue
            guard_semantic(self.pending, self.secrets)
            # Retain enough printable characters to cover a credential whose
            # characters are separated by arbitrary controls. Keep intervening
            # controls too, so exact credentials containing them remain safe.
            count = len(self.pending)
            visible = 0
            while count and visible < self.lookbehind:
                count -= 1
                visible += self.pending[count].isprintable()
            count = safe_boundary(self.pending, count)
            if count:
                frames.append({'kind': 'text', 'text': self.pending[:count]})
                self.pending = self.pending[count:]
        return frames

    def finish(self):
        decoded = self.decoder.finish()
        result = guard_result(decoded, self.secrets)
        frames = []
        if self.decoder.held_envelope:
            self.pending = ''.join(part.text for part in decoded.payload.content)
        if self.pending:
            frames.append({'kind': 'text', 'text': self.pending})
        if decoded.reasoning_summary:
            if not decoded.reasoning_summary.startswith(self.summary_released):
                raise ContractError('Invalid normalized Bedrock summary')
            remainder = decoded.reasoning_summary[len(self.summary_released):]
            if remainder:
                frames.append({'kind': 'reasoning_summary', 'text': remainder})
        frames.append({'kind': 'completed', 'result': result})
        return frames


class ParentStream:
    def __init__(self, mode, *, selected_llm):
        self.finished = False
        self.text = ''
        self.summary = None
        self.result = None

    def feed(self, frame):
        if self.finished or type(frame) is not dict:
            raise ContractError('Invalid normalized Bedrock stream')
        if set(frame) == {'kind', 'text'} and frame['kind'] == 'reasoning_summary' and type(frame['text']) is str:
            summary = (self.summary or '') + frame['text']
            if len(summary.encode('utf-8')) > 65536:
                raise ContractError('Invalid normalized Bedrock summary')
            self.summary = summary
            return (StreamEvent(TextContent(frame['text']), kind='reasoning_summary'),)
        if set(frame) == {'kind', 'text'} and frame['kind'] == 'text' and type(frame['text']) is str:
            self.text += frame['text']
            if len(self.text.encode('utf-8')) > _LIMIT:
                raise ContractError('Invalid normalized Bedrock stream')
            return (StreamEvent(TextContent(frame['text'])),)
        if set(frame) == {'kind', 'result'} and frame['kind'] == 'completed':
            result = result_from_wire(frame['result'])
            if ''.join(part.text for part in result.payload.content) != self.text:
                raise ContractError('Invalid normalized Bedrock stream')
            if result.reasoning_summary != self.summary:
                raise ContractError('Invalid normalized Bedrock summary')
            self.result = result
            self.finished = True
            return ()
        raise ContractError('Invalid normalized Bedrock stream')

    def finish(self):
        if not self.finished:
            raise ContractError('Invalid normalized Bedrock stream')
        return self.result
