"""Noninteractive ASGI owner for one checked-in compiled workflow plan.

This module intentionally bypasses the development bootstrap: it does not
resolve a resource pack, provision prerequisites, or import terminal/viewer
owners.  Deployments configure a loopback listener and explicit provider
locations through the environment.
"""
import argparse
import asyncio
from dataclasses import dataclass
import ipaddress
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from api_agent.api.application import _validated_hosts, create_application
from api_agent.config.declarations import load_shared_schema
from api_agent.config.resolve import ConfigResult, ResolvedValue, resolve_config
from api_agent.runtime.chat_composition import compose_chat_runtime
from api_agent.runtime.local_llm import LocalLLM
from api_agent.runtime.local_tools import selected_application
from api_agent.workflows.artifact import load_artifact


class ProductionConfigurationError(ValueError):
    """The explicit production process configuration is unsafe or incomplete."""


@dataclass(frozen=True)
class ProductionSettings:
    plan: Path
    pack_id: str
    owner_root: Path
    listen_host: str
    listen_port: int
    callback_origin: str
    chat_origin: str
    session_nonce: str
    values: dict
    shutdown_timeout: float
    config: ConfigResult | None = None
    allowed_hosts: tuple[str, ...] = ('localhost', '127.0.0.1')
    permitted_browser_origins: tuple[str, ...] = ()
    client_address_policy: str = 'loopback'


def _required(environ, name):
    value = environ.get(name, '').strip()
    if not value:
        raise ProductionConfigurationError(f'{name} is required')
    return value


def _listen_address(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        raise ProductionConfigurationError('API_AGENT_LISTEN_HOST must be an IP address') from None


def _csv(value, name):
    if type(value) is not str or not value or value != value.strip():
        raise ProductionConfigurationError(f'{name} must be a nonempty comma-separated value')
    values = tuple(value.split(','))
    if any(not item or item != item.strip() for item in values) or len(set(values)) != len(values):
        raise ProductionConfigurationError(f'{name} must contain distinct nonempty values')
    return values


def _allowed_hosts(value):
    try:
        return _validated_hosts(_csv(value, 'API_AGENT_ALLOWED_HOSTS'))
    except ValueError:
        raise ProductionConfigurationError(
            'API_AGENT_ALLOWED_HOSTS must contain exact host names without wildcards, schemes, or ports') from None


def _browser_origins(value):
    origins = []
    for origin in _csv(value, 'API_AGENT_PERMITTED_BROWSER_ORIGINS'):
        try:
            parts = urlsplit(origin)
            port = parts.port
        except ValueError:
            raise ProductionConfigurationError('API_AGENT_PERMITTED_BROWSER_ORIGINS contains an invalid origin') from None
        if (parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password
                or parts.path or parts.query or parts.fragment or '*' in parts.hostname):
            raise ProductionConfigurationError('API_AGENT_PERMITTED_BROWSER_ORIGINS contains an invalid origin')
        hostname = parts.hostname.lower()
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            try:
                _validated_hosts((hostname,))
            except ValueError:
                raise ProductionConfigurationError('API_AGENT_PERMITTED_BROWSER_ORIGINS contains an invalid origin')
        default_port = 80 if parts.scheme == 'http' else 443
        authority = hostname if ':' not in hostname else f'[{hostname}]'
        if port is not None and port != default_port:
            authority += f':{port}'
        origins.append(f'{parts.scheme}://{authority}')
    if len(set(origins)) != len(origins):
        raise ProductionConfigurationError('API_AGENT_PERMITTED_BROWSER_ORIGINS contains duplicate origins')
    return tuple(origins)


def _network_settings(config, chat_origin):
    values, fields = config.values, config.fields
    host = _listen_address(values['API_AGENT_LISTEN_HOST'])
    allowed_hosts = _allowed_hosts(values['API_AGENT_ALLOWED_HOSTS'])
    origins_value = values['API_AGENT_PERMITTED_BROWSER_ORIGINS']
    origins = _browser_origins(origins_value) if origins_value else _browser_origins(chat_origin)
    client_policy = values['API_AGENT_CLIENT_ADDRESS_POLICY']
    if not ipaddress.ip_address(host).is_loopback:
        required = ('API_AGENT_ALLOWED_HOSTS', 'API_AGENT_PERMITTED_BROWSER_ORIGINS',
                    'API_AGENT_CLIENT_ADDRESS_POLICY')
        missing = tuple(name for name in required if not fields[name].explicit)
        if missing or client_policy != 'any':
            raise ProductionConfigurationError(
                'non-loopback API_AGENT_LISTEN_HOST requires explicit allowed hosts, browser origins, '
                'and API_AGENT_CLIENT_ADDRESS_POLICY=any')
    return host, allowed_hosts, origins, client_policy


def _integer(value, name, *, low, high):
    try:
        parsed = int(value)
    except ValueError:
        raise ProductionConfigurationError(f'{name} must be an integer') from None
    if not low <= parsed <= high:
        raise ProductionConfigurationError(f'{name} is outside its supported range')
    return parsed


def _duration(value, name):
    try:
        parsed = float(value)
    except ValueError:
        raise ProductionConfigurationError(f'{name} must be a number') from None
    if not 0 < parsed <= 120:
        raise ProductionConfigurationError(f'{name} is outside its supported range')
    return parsed


def _production_environment(environ):
    """Translate explicit deployment names while retaining provider-owned fields."""
    values = dict(environ)
    values['LLM_PROVIDER'] = _required(environ, 'API_AGENT_LLM_PROVIDER')
    values['UI_PROVIDER'] = ''
    for name in ('LLM_ALLOWED_MODELS', 'LLM_DEFAULT_MODEL', 'LLM_REASONING_EFFORT',
                 'LLM_MAX_OUTPUT_TOKENS'):
        production_name = 'API_AGENT_' + name
        if production_name in environ:
            values[name] = environ[production_name]
    if environ.get('API_AGENT_API_PROVIDER'):
        values['API_PROVIDER'] = environ['API_AGENT_API_PROVIDER']
    if values.get('TRACE_EXPORT_ENDPOINT') and not values.get('TRACE_PROVIDER'):
        values['TRACE_PROVIDER'] = (
            str(Path(__file__).parents[1] / 'telemetry/trace_adapters/otel/provider.py')
            + ':create_tracing_provider')
    elif not values.get('TRACE_PROVIDER'):
        values['TRACE_PROVIDER'] = ''
    if values.get('TRACE_PROVIDER') and not values.get('TRACE_EXPORT_ENDPOINT'):
        raise ProductionConfigurationError('TRACE_EXPORT_ENDPOINT is required when tracing is enabled')
    if values.get('TRACE_EXPORT_ENDPOINT') and not values.get('TRACE_MODE'):
        values['TRACE_MODE'] = 'OTEL'
    return values


def _resolve_production_config(environ, owner_root):
    """Resolve deployment configuration through the ordinary schema pipeline."""
    from api_agent.providers.loading import bootstrap_schema
    values = _production_environment(environ)
    schema = bootstrap_schema(load_shared_schema(), (), values, {}, owner_root=owner_root)
    resolved = resolve_config(schema, environ=values, dotenv={},
                              anchors={'owner_root': owner_root, 'base_root': owner_root,
                                       'pack_root': owner_root},
                              interactive=False)
    if resolved.errors or resolved.missing:
        raise ProductionConfigurationError('; '.join(
            ('invalid production configuration', *resolved.errors,
             *(f'{name}: required' for name in resolved.missing))))
    fixed = {'NO_STD_IN': True, 'DEBUG': False, 'RUN_UI': False,
             'API_AGENT_STATE_DIR': environ.get('API_AGENT_STATE_DIR', '/var/lib/api-agent')}
    fields = dict(resolved.fields)
    fields.update({name: ResolvedValue(value, 'production', True)
                   for name, value in fixed.items()})
    return ConfigResult({**resolved.values, **fixed}, fields, resolved.positionals,
                        resolved.missing, resolved.errors, resolved.help_requested,
                        active_fields=resolved.active_fields,
                        deferred_required_fields=resolved.deferred_required_fields)


def settings_from_environment(environ=None):
    """Read only explicit deployment configuration; no dotenv or bootstrap."""
    environ = os.environ if environ is None else environ
    plan = Path(_required(environ, 'API_AGENT_PRODUCTION_PLAN')).resolve()
    if not plan.is_file():
        raise ProductionConfigurationError('API_AGENT_PRODUCTION_PLAN must name a readable compiled plan')
    owner_root = Path(environ.get('API_AGENT_OWNER_ROOT', plan.parent)).resolve()
    config = _resolve_production_config(environ, owner_root)
    chat_origin = _required(environ, 'API_AGENT_CHAT_ORIGIN')
    host, allowed_hosts, origins, client_policy = _network_settings(config, chat_origin)
    port = _integer(str(config.values['API_AGENT_LISTEN_PORT']), 'API_AGENT_LISTEN_PORT',
                    low=1, high=65535)
    return ProductionSettings(
        plan=plan, pack_id=environ.get('API_AGENT_PACK_ID', 'compiled-agent'),
        owner_root=owner_root, listen_host=host, listen_port=port,
        callback_origin=environ.get('API_AGENT_CALLBACK_ORIGIN', 'https://localhost:9443'),
        chat_origin=chat_origin,
        session_nonce=_required(environ, 'API_AGENT_SESSION_NONCE'),
        values=dict(config.values),
        shutdown_timeout=_duration(environ.get('API_AGENT_SHUTDOWN_TIMEOUT_SECONDS', '10'),
                                   'API_AGENT_SHUTDOWN_TIMEOUT_SECONDS'),
        config=config,
        allowed_hosts=allowed_hosts,
        permitted_browser_origins=origins,
        client_address_policy=client_policy,
    )


class ProductionApplication:
    """Lazily compose one immutable plan at ASGI startup."""
    def __init__(self, settings, *, llm_factory=LocalLLM):
        self.settings, self.llm_factory = settings, llm_factory
        self._application = self._runtime = None
        self._started = False

    async def _start(self):
        if self._started:
            return
        definition = load_artifact(self.settings.plan.read_bytes())
        graph = selected_application(definition, config=self.settings.values)
        context = SimpleNamespace(pack_id=self.settings.pack_id, owner_root=self.settings.owner_root)
        config = self.settings.config or ConfigResult(dict(self.settings.values), {}, (), (), ())
        composed = compose_chat_runtime(
            context, config, callback_origin=self.settings.callback_origin, chat_origin=self.settings.chat_origin,
            tls_material=None, graph=graph, llm_factory=self.llm_factory, on_error=None,
            required_kinds=(),
            nonce=self.settings.session_nonce, extra_services=None,
            permitted_browser_origins=self.settings.permitted_browser_origins,
            client_address_policy=self.settings.client_address_policy)
        self._runtime = composed.runtime
        self._application = create_application(
            self._runtime, allowed_hosts=self.settings.allowed_hosts)
        await self._runtime.astart()
        self._started = True

    async def _shutdown(self):
        if self._runtime is None:
            return
        async with asyncio.timeout(self.settings.shutdown_timeout):
            await self._runtime.aclose()
        self._started = False

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'lifespan':
            if self._application is None or not self._started:
                await send({'type': 'http.response.start', 'status': 503,
                            'headers': [(b'content-type', b'application/json')]})
                await send({'type': 'http.response.body', 'body': b'{"code":"unavailable"}'})
                return
            return await self._application(scope, receive, send)
        while True:
            event = await receive()
            if event['type'] == 'lifespan.startup':
                try:
                    await self._start()
                except Exception as error:
                    from api_agent.providers.setup_transport import _diagnostic
                    logging.getLogger(__name__).error(_diagnostic(error, 'Runtime startup'))
                    await send({'type': 'lifespan.startup.failed', 'message': 'runtime_start_failed'})
                    return
                await send({'type': 'lifespan.startup.complete'})
            elif event['type'] == 'lifespan.shutdown':
                try:
                    await self._shutdown()
                except TimeoutError:
                    await send({'type': 'lifespan.shutdown.failed',
                                'message': 'runtime_shutdown_timed_out'})
                    return
                except Exception:
                    await send({'type': 'lifespan.shutdown.failed', 'message': 'runtime_shutdown_failed'})
                    return
                await send({'type': 'lifespan.shutdown.complete'})
                return


def application():
    """ASGI factory for ``uvicorn --factory``."""
    return ProductionApplication(settings_from_environment())


def main(argv=None):
    parser = argparse.ArgumentParser(description='Serve one compiled api-agent workflow.')
    parser.parse_args(argv)
    settings = settings_from_environment()
    import uvicorn
    uvicorn.run(ProductionApplication(settings), host=settings.listen_host, port=settings.listen_port,
                workers=1, reload=False, lifespan='on',
                timeout_graceful_shutdown=settings.shutdown_timeout)
    return 0


if __name__ == '__main__':  # pragma: no cover - exercised by Python's module runner.
    raise SystemExit(main())
