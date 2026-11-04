"""Bounded tracing resolution inputs carried only by protected launch channels."""
from copy import deepcopy
import json
import re


def restore_trace_context(snapshot, *, required=False, config=None):
    """Validate the protected resolution context without reading the worker environment."""
    from api_agent.telemetry.tracing_selection import OTEL_KEYS
    context = snapshot.get('trace_context')
    if context is None:
        if required:
            raise ValueError('Active protected tracing requires its captured resolution context')
        return {}
    if (type(context) is not dict or set(context) != {'version', 'environment', 'explicit_fields'}
            or type(context['version']) is not int or context['version'] != 1
            or type(context['environment']) is not dict or set(context['environment']) - OTEL_KEYS
            or type(context['explicit_fields']) is not list or len(context['explicit_fields']) > 1024
            or any(type(name) is not str or not re.fullmatch('[A-Z][A-Z0-9_]{0,63}', name)
                   for name in context['explicit_fields'])
            or len(set(context['explicit_fields'])) != len(context['explicit_fields'])
            or any(type(value) is not str or len(value) > 16384 or '\x00' in value
                   for value in context['environment'].values())):
        raise ValueError('Invalid protected tracing resolution context')
    if len(json.dumps(context).encode()) > 131072:
        raise ValueError('Protected tracing resolution context exceeds its budget')
    if config is not None:
        values = config.values if hasattr(config, 'fields') else config
        if set(context['explicit_fields']) - values.keys():
            raise ValueError('Protected tracing explicit fields are absent from matching configuration')
    return {'environ': deepcopy(context['environment']), 'explicit_fields': frozenset(context['explicit_fields'])}


def capture_trace_context(selection):
    context = {'version': 1, 'environment': dict(selection.environment),
               'explicit_fields': sorted(selection.explicit_fields)}
    restore_trace_context({'trace_context': context}, config=selection.values)
    return context
