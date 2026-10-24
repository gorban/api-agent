"""Operation-scoped selection of the bundled demonstration API."""
from dataclasses import replace

from api_agent.config.resolve import ConfigResult, ResolvedValue


SERVICE_ID = 'demo/api'
OUTPUT_NAME = 'graphql'
ACTIVATION_FIELD = 'DEMO_API_LOCAL'


def _bundled(context):
    return context.pack_id == 'demo' and not context.external_pack


def prepare_demo_api(config: ConfigResult, context, *, requires_api: bool):
    """Select a local API only for an API-capable bundled demo operation.

    The original ConfigResult is retained by the caller; this copy carries the
    operation's internal activation value without changing endpoint provenance.
    """
    if not isinstance(config, ConfigResult) or config.errors or config.missing:
        raise ValueError('invalid_demo_api_configuration')
    if type(requires_api) is not bool:
        raise ValueError('invalid_demo_api_operation')
    if not _bundled(context):
        return config, False
    activation = config.fields.get(ACTIVATION_FIELD)
    if activation is not None and activation.explicit:
        raise ValueError('demo_api_activation_is_operation_owned')
    endpoint = config.values.get('API_ENDPOINT')
    if endpoint is not None and type(endpoint) is not str:
        raise ValueError('invalid_demo_api_endpoint')
    local = requires_api and (endpoint is None or endpoint == '')
    values = dict(config.values)
    values[ACTIVATION_FIELD] = local
    fields = dict(config.fields)
    fields[ACTIVATION_FIELD] = ResolvedValue(local, 'operation:demo-api', False)
    return replace(config, values=values, fields=fields), local


def bind_demo_api_endpoint(config: ConfigResult, infrastructure_session):
    """Publish the acquired local endpoint into a copied operation config."""
    if config.values.get(ACTIVATION_FIELD) is not True:
        return config
    services = infrastructure_session.endpoints
    outputs = services.get(SERVICE_ID)
    if outputs is None:
        raise ValueError('demo_api_endpoint_not_ready')
    matches = [item.host for item in outputs if item.name == OUTPUT_NAME]
    if len(matches) != 1:
        raise ValueError('demo_api_endpoint_not_ready')
    values = dict(config.values)
    values['API_ENDPOINT'] = matches[0].rstrip('/') + '/graphql'
    fields = dict(config.fields)
    fields['API_ENDPOINT'] = ResolvedValue(values['API_ENDPOINT'], 'infrastructure:demo/api/graphql', False)
    return replace(config, values=values, fields=fields)
