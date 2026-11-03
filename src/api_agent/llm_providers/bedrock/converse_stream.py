"""Bounded collection of native ConverseStream into the validated native union.

The SDK stream remains owned by the worker. No unvalidated partial content is
published: native state, tool input and plaintext are admitted together.
"""
import json
import time

from .mantle import _require

_LIMIT = 524288
_MAX_EVENTS = 100000


def collect(events, deadline, observe=None):
    blocks = []
    active = None
    started = stopped = False
    result = {}
    used = 0

    def charge(value):
        nonlocal used
        try:
            amount = len(value) if type(value) in (bytes, bytearray) else len(value.encode('utf-8'))
        except UnicodeEncodeError:
            _require(False, rule='text_value', path='$.output.message.content[].text')
        used += amount
        _require(used <= _LIMIT, rule='response_object', path='$')

    for count, event in enumerate(events):
        if time.monotonic() >= deadline:
            raise TimeoutError('Bedrock Converse stream deadline expired')
        _require(count < _MAX_EVENTS and type(event) is dict and len(event) == 1)
        name, value = next(iter(event.items()))
        _require(type(value) is dict)
        if name in ('internalServerException', 'modelStreamErrorException', 'validationException', 'throttlingException', 'serviceUnavailableException'):
            from botocore.exceptions import ClientError
            status = {'validationException': 400, 'throttlingException': 429,
                      'serviceUnavailableException': 503, 'modelStreamErrorException': 424}.get(name, 500)
            raise ClientError({'Error': {'Code': name[0].upper() + name[1:], 'Message': value.get('message', name)},
                               'ResponseMetadata': {'HTTPStatusCode': status}}, 'ConverseStream')
        if observe is not None:
            observe()
        if name == 'messageStart':
            _require(not started and value == {'role': 'assistant'})
            started = True
        elif name in ('contentBlockStart', 'contentBlockDelta'):
            _require(started and not stopped)
            index = value.get('contentBlockIndex')
            _require(type(index) is int and 0 <= index < 64)
            if active is None:
                _require(index == len(blocks))
                active = {'index': index, 'type': None}
            _require(index == active['index'])
            if name == 'contentBlockStart':
                _require(active['type'] is None and not active.get('explicit', False) and set(value) == {'contentBlockIndex', 'start'})
                active['explicit'] = True
                start = value['start']
                _require(type(start) is dict and set(start) in (set(), {'toolUse'}))
                if start:
                    tool = start['toolUse']
                    _require(type(tool) is dict and set(tool) in ({'toolUseId', 'name'}, {'toolUseId', 'name', 'type'})
                             and all(type(child) is str and child for child in tool.values())
                             and tool.get('type') in (None, 'function', 'tool_use'))
                    charge(tool['toolUseId'])
                    charge(tool['name'])
                    active.update(type='toolUse', tool=tool, input=[])
                continue
            _require(set(value) == {'contentBlockIndex', 'delta'})
            delta = value['delta']
            _require(type(delta) is dict and len(delta) == 1)
            kind, child = next(iter(delta.items()))
            _require(kind in ('text', 'reasoningContent', 'toolUse')
                     and active['type'] in (None, kind))
            active['type'] = kind
            if kind == 'text':
                _require(type(child) is str)
                charge(child)
                active.setdefault('text', []).append(child)
            elif kind == 'toolUse':
                _require('tool' in active and type(child) is dict and set(child) == {'input'}
                         and type(child['input']) is str)
                charge(child['input'])
                active['input'].append(child['input'])
            else:
                _require(type(child) is dict and len(child) == 1)
                field, fragment = next(iter(child.items()))
                _require(field in ('text', 'signature', 'redactedContent'))
                opaque = field == 'redactedContent'
                _require((type(fragment) in (bytes, bytearray) if opaque else type(fragment) is str)
                         and active.get('opaque', opaque) == opaque)
                active['opaque'] = opaque
                charge(fragment)
                active.setdefault(field, []).append(bytes(fragment) if opaque else fragment)
        elif name == 'contentBlockStop':
            _require(active is not None and value == {'contentBlockIndex': active['index']})
            kind = active['type']
            _require(kind is not None)
            if kind == 'text':
                block = {'text': ''.join(active['text'])}
            elif kind == 'toolUse':
                from api_agent.providers.rich_inference import object_json
                block = {'toolUse': {**active['tool'], 'input': object_json(''.join(active['input']))}}
            else:
                reasoning = ({'redactedContent': b''.join(active['redactedContent'])} if active['opaque'] else
                             {'reasoningText': {key: ''.join(active[key]) for key in ('text', 'signature') if key in active}})
                block = {'reasoningContent': reasoning}
            blocks.append(block)
            active = None
        elif name == 'messageStop':
            _require(started and not stopped and active is None
                     and set(value) <= {'stopReason', 'additionalModelResponseFields'}
                     and type(value.get('stopReason')) is str)
            result['stopReason'] = value['stopReason']
            stopped = True
        else:
            _require(name == 'metadata' and stopped and 'usage' not in result
                     and type(value.get('usage')) is dict)
            charge(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')))
            result['usage'] = value['usage']
            for key in ('metrics', 'serviceTier', 'performanceConfig'):
                if key in value:
                    result[key] = value[key]
    _require(started and stopped and active is None and 'usage' in result)
    result['output'] = {'message': {'role': 'assistant', 'content': blocks}}
    return result
