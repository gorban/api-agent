"""Explicitly enabled, credential-scrubbed inference evidence for test history."""
import json
import os
from uuid import UUID

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary, _credential_key


def content_boundary(config):
    boundary = EmissionBoundary(ContentPolicy(config.get('LOG_CONTENT') is True), preserve_json_text=True)
    for name, value in config.items():
        if _credential_key(name) and type(value) is str and value:
            boundary.register_secret(value)
    return boundary


def export_content(boundary, request, result=None):
    if not boundary.policy.allow_content:
        return ()
    from api_agent.providers.inference import request_to_wire, result_to_wire
    rows = [('request', request_to_wire(request))]
    if result is not None:
        rows.append(('response', result_to_wire(result)))
    for _, content in rows:
        messages = content.get('messages', [content['message']] if 'message' in content else [])
        for message in messages:
            message.pop('continuation', None)
    return tuple({'phase': phase, 'content': boundary.export(content)} for phase, content in rows)


def publish_content(rows, call_id):
    path = os.environ.get('API_AGENT_INTEGRATION_EVENTS')
    node = os.environ.get('PYTEST_CURRENT_TEST')
    if not path or not node or call_id is None:
        return
    for row in rows:
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps({'kind': 'inference_content', 'node': node.rsplit(' ', 1)[0],
                                    'data': dict(call_id=call_id, **row)}, allow_nan=False) + '\n')


def content_attachment(event, boundary):
    row = json.loads(event.text)
    if (type(row) is not dict or set(row) != {'call_id', 'phase', 'content'}
            or type(row['call_id']) is not str or str(UUID(row['call_id'])) != row['call_id']
            or row['phase'] not in ('request', 'response') or type(row['content']) is not dict):
        raise ValueError('Invalid inference content event')
    return (f"LLM {row['phase']} ({row['call_id']})",
            json.dumps(boundary.content_export(row['content']), ensure_ascii=False, indent=2, allow_nan=False).encode(),
            'application/json')
