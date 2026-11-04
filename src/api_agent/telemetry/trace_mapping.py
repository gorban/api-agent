"""Versioned observation mapping seam, independent of capture/export ownership."""
from typing import Protocol, runtime_checkable
from dataclasses import dataclass


@runtime_checkable
class NativeTraceMapping(Protocol):
    version: int

    def io_attribute(self, direction: str) -> str:
        """Return an attribute name for already admitted JSON observation data."""


class TraceMapping:
    """Compatibility mapping supported by both existing OTLP routes.

    Executable resource-pack factory discovery remains a separate admission
    capability. A mapping never grants content authority or owns an exporter.
    """
    version = 1

    def io_attribute(self, direction):
        if direction not in ('input', 'output'):
            raise ValueError('Invalid observation direction')
        return 'langfuse.observation.' + direction


@dataclass(frozen=True)
class AdmittedMapping:
    input_key: str
    output_key: str
    version: int = 1

    def io_attribute(self, direction):
        if direction not in ('input', 'output'):
            raise ValueError('Invalid observation direction')
        return self.input_key if direction == 'input' else self.output_key


def admit_mapping(mapping):
    if (not isinstance(mapping, NativeTraceMapping) or type(mapping.version) is not int
            or mapping.version != 1):
        raise ValueError('Unsupported trace mapping contract')
    import re
    keys = []
    for direction in ('input', 'output'):
        key = mapping.io_attribute(direction)
        if (type(key) is not str or re.fullmatch(r'[a-z][a-z0-9_.]{0,127}', key) is None
                or key.startswith(('gen_ai.', 'api_agent.', 'error.', 'otel.', 'service.', 'http.'))
                or key not in ('langfuse.observation.' + direction, 'input.value' if direction == 'input' else 'output.value')):
            raise ValueError('Invalid native trace attribute')
        keys.append(key)
    return AdmittedMapping(*keys)


def _text_parts(value):
    if type(value) is str:
        return [{'type': 'text', 'content': value}]
    if type(value) is not list or len(value) > 64:
        return []
    parts = []
    for part in value:
        if type(part) is str:
            parts.append({'type': 'text', 'content': part})
        elif type(part) is dict:
            text = part.get('text')
            if type(text) is str and part.get('type') in (None, 'text', 'input_text', 'output_text'):
                parts.append({'type': 'text', 'content': text})
            call = part.get('toolUse')
            if type(call) is dict and type(call.get('name')) is str:
                parts.append({'type': 'tool_call', 'name': call['name'],
                    **({'id':call['toolUseId']} if type(call.get('toolUseId')) is str else {}),
                    **({'arguments':call['input']} if 'input' in call else {})})
            result = part.get('toolResult')
            if type(result) is dict and type(result.get('toolUseId')) is str:
                visible = _text_parts(result.get('content'))
                parts.append({'type':'tool_call_response', 'id':result['toolUseId'],
                    'response':'\n'.join(item['content'] for item in visible if item['type']=='text')})
    return parts


def _message(row):
    if type(row) is not dict or row.get('role') not in ('system', 'user', 'assistant', 'tool', 'developer'):
        return None
    parts = _text_parts(row.get('text', row.get('content')))
    for call in row.get('tool_calls', row.get('calls', [])) if type(row.get('tool_calls', row.get('calls', []))) is list else []:
        if type(call) is not dict:
            continue
        function = call.get('function', call)
        if type(function) is dict and type(function.get('name')) is str:
            parts.append({'type':'tool_call', 'name':function['name'],
                **({'id':call['id']} if type(call.get('id')) is str else {}),
                **({'arguments':function['arguments']} if 'arguments' in function else {})})
    for result in row.get('results', []) if type(row.get('results')) is list else []:
        if type(result) is dict and type(result.get('id')) is str:
            parts.append({'type':'tool_call_response','id':result['id'],
                          'response':result.get('content', [])})
    if row['role']=='tool' and type(row.get('tool_call_id')) is str:
        parts=[{'type':'tool_call_response','id':row['tool_call_id'],
                'response':row.get('content','')}]
    return {'role':row['role'], 'parts':parts} if parts else None


def genai_messages(direction, value):
    """Normalize admitted visible canonical, Completions, Responses and Converse I/O.

    Only authored text/tool fields are selected. Unknown content, continuation,
    encrypted state and reasoning carriers are never copied into this schema.
    Native backend panels retain their independently projected source shape.
    """
    if direction not in ('input', 'output') or type(value) is not dict:
        return None
    value = value.get('llm_request' if direction == 'input' else 'llm_response', value)
    if type(value) is not dict:
        return None
    value = value.get('provider_payload' if direction=='input' else 'provider_response', value)
    if type(value) is not dict:
        return None
    if direction=='output' and type(value.get('text')) is str:
        return [{'role':'assistant','parts':[{'type':'text','content':value['text']}]}]
    rows = value.get('messages')
    if direction=='output':
        if type(value.get('message')) is dict:
            rows=[value['message']]
        elif type(value.get('output')) is dict and type(value['output'].get('message')) is dict:
            rows=[value['output']['message']]
        elif type(value.get('choices')) is list:
            rows=[item.get('message') for item in value['choices'] if type(item) is dict]
        else:
            rows=value.get('output')
    elif rows is None:
        rows=value.get('input')
        if type(rows) is str:
            rows=[{'role':'user','content':rows}]
    if type(rows) is not list or len(rows)>256:
        return None
    result=[]
    if direction=='input':
        system=value.get('system', value.get('instructions'))
        parts=_text_parts(system)
        if parts:
            result.append({'role':'system','parts':parts})
    for row in rows:
        message=_message(row)
        if message is not None:
            result.append(message)
        elif type(row) is dict and row.get('type')=='function_call' and type(row.get('name')) is str:
            result.append({'role':'assistant','parts':[{'type':'tool_call','name':row['name'],
                **({'id':row['call_id']} if type(row.get('call_id')) is str else {}),
                **({'arguments':row['arguments']} if 'arguments' in row else {})}]})
        elif type(row) is dict and row.get('type')=='function_call_output' and type(row.get('call_id')) is str:
            result.append({'role':'tool','parts':[{'type':'tool_call_response','id':row['call_id'],
                'response':row.get('output','')}]})
    return result or None
