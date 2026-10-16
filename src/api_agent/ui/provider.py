"""Launch the included static UI through an injected process lifecycle owner."""
import asyncio
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit

from api_agent.providers.contracts import ProviderCapabilities, SetupPrerequisiteError
from api_agent.providers.control import CleanupPendingError
from api_agent.providers.progress import component_timeout


def app_directory():
    """Resolve installed resources first, with a source-checkout fallback."""
    installed = Path(__file__).with_name('app')
    return installed if installed.is_dir() else Path(__file__).resolve().parents[3] / 'apps' / 'api-agent-ui'


def listener_plan(value):
    try:
        url = urlsplit(value)
        port = url.port if url.port is not None else (443 if url.scheme == 'https' else 80)
        valid = (url.scheme in {'http', 'https'} and url.hostname in {'localhost', '127.0.0.1', '::1'}
                 and not url.username and not url.password and url.path in {'', '/'}
                 and not url.query and not url.fragment and 0 < port < 65536)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError('ui_listener_invalid')
    return url, port


class DemoUIProvider:
    version = 1
    capabilities = ProviderCapabilities(kind='ui', operations=('launch',))

    def __init__(self, config, services):
        self.config, self.services = config, services
        self.process = None
        self.pending = None
        self.closed = False
        self._lock = asyncio.Lock()

    async def astart(self):
        async with component_timeout('Studio UI launch', 60):
            async with self._lock:
                if self.closed:
                    raise ValueError('ui_provider_closed')
                if self.process is not None:
                    return
                if self.pending is not None:
                    await self.pending.retry()
                    self.pending = None
                values = self.services.get('runtime_values', self.config)
                listener = values.get('UI_LISTENER_URL', 'https://localhost:9443')
                endpoint, port = listener_plan(listener)
                launcher = self.services.get('ui_processes')
                if launcher is None:
                    raise SetupPrerequisiteError('Studio requires the runtime UI process service')
                executable = shutil.which('node')
                if executable is None:
                    raise SetupPrerequisiteError('Studio requires Node.js')
                app = app_directory()
                if not (app / 'launch.mjs').is_file():
                    raise SetupPrerequisiteError('Studio packaged resources are unavailable')
                env = {'PATH': str(Path(executable).parent) + os.pathsep + os.defpath,
                       'UI_LISTENER_URL': listener,
                       'AGENT_DJANGO_URL': values.get('AGENT_DJANGO_URL', 'https://localhost:8443')}
                if self.services.get('ui_access_token') is not None:
                    env['API_AGENT_UI_ACCESS_TOKEN'] = self.services['ui_access_token']
                if endpoint.scheme == 'https':
                    material = self.services.get('tls_material')
                    if material is None:
                        raise SetupPrerequisiteError('Studio requires runtime TLS material')
                    env.update(API_AGENT_UI_TLS_CERT=str(material.cert_file),
                               API_AGENT_UI_TLS_KEY=str(material.key_file))
                try:
                    self.process = await launcher.start(
                        (executable, str(app / 'launch.mjs')), cwd=app,
                        host=endpoint.hostname, port=port, scheme=endpoint.scheme, env=env)
                except CleanupPendingError as error:
                    self.pending = error
                    raise
                except asyncio.CancelledError as error:
                    self.pending = getattr(error, 'cleanup_pending', None)
                    raise

    async def aclose(self):
        self.closed = True
        async with component_timeout('Studio UI cleanup', 10):
            async with self._lock:
                if self.pending is not None:
                    await self.pending.retry()
                    self.pending = None
                if self.process is not None:
                    await self.process.aclose()
                    self.process = None


def create_provider(config, services):
    return DemoUIProvider(config, services)
