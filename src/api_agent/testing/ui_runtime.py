"""Launch-owned UI test fixture services without application or LLM activation."""
import asyncio
from pathlib import Path
import socket
from urllib.parse import urlunsplit

from api_agent.auth.bindings import bind_role_credentials
from api_agent.providers.control import _cancel_once, CleanupPendingError
from api_agent.providers.contracts import SessionClosedError
from api_agent.providers.progress import component_timeout, report_timeout, independent_timeout_scope
from api_agent.runtime.managed_browser import managed_browser
from api_agent.providers.sessions import InspectionOwner
from api_agent.runtime.https import prepare_local_https
from api_agent.runtime.client_tls import verified_client_context
from api_agent.ui.process import StudioProcesses
from api_agent.ui.session import PreparedUISession
from api_agent.ui.provider import listener_plan
from .inference_content import content_boundary


class UIIntegrationOwner:
    """Retain provider, credential helpers and TLS work until explicit disposal."""
    def __init__(self, data, registry, *, services=None, backend=None):
        from api_agent.auth.interaction_transport import OwnedInteraction
        self.config = dict(data['config'])
        self.owner_root = Path(data['owner_root'])
        self.registry = registry
        self.index_source = data.get('unattended_index_source')
        self.interaction = OwnedInteraction(data['interaction']) if data.get('interaction') else None
        self.emissions = content_boundary(self.config)
        self.pending_tasks = set()
        self._tls_task = None
        self._starting = None
        self._cancel_requested = set()
        self._started = self.closed = False
        self.prepared = None
        self.extra_services = dict(services or {})
        self.backend_factory, self.backend = backend, None
        self._backend_cleanup = None
        self.inspection = InspectionOwner(on_session=self._show_browser) if self.config.get('DEBUG') else None
        self.browser_owner = managed_browser(inspection=self.inspection,
            headless=not (self.config.get('DEBUG') or self.config.get('RUN_UI')))

    @staticmethod
    def _show_browser(session):
        if session.inspection_endpoint:
            print('UI provider browser inspection: ' + session.inspection_endpoint, flush=True)

    def _credentials_for(self, roles, index):
        return bind_role_credentials(self.config, roles, index, registry=self.registry,
            emissions=self.emissions, pending_tasks=self.pending_tasks,
            interaction_owner=self.interaction, index_source=self.index_source)

    async def start(self):
        if self._started or self.closed:
            raise SessionClosedError('UI test fixture is one-shot')
        self._started = True
        self._starting = asyncio.current_task()
        try:
            prepared = await self._start()
            if self.closed:
                raise SessionClosedError('UI test fixture closed during preparation')
            return prepared
        finally:
            self._starting = None

    async def _start(self):
        async with component_timeout('UI fixture readiness', 90):
            origin = self.config.get('UI_LISTENER_URL')
            endpoint, _ = listener_plan(origin)
            # Each test owns a listener; separate workers must not compete for
            # the local application's configured port. Preserve scheme/host,
            # and fail closed if the released ephemeral port is taken at launch.
            family = socket.AF_INET6 if endpoint.hostname == '::1' else socket.AF_INET
            with socket.socket(family, socket.SOCK_STREAM) as reservation:
                reservation.bind((endpoint.hostname, 0))
                port = reservation.getsockname()[1]
            address = f'[{endpoint.hostname}]' if family == socket.AF_INET6 else endpoint.hostname
            values = dict(self.config, UI_LISTENER_URL=urlunsplit((endpoint.scheme, f'{address}:{port}', '', '', '')))
            material = None
            if endpoint.scheme == 'https':
                self._tls_task = asyncio.create_task(asyncio.to_thread(prepare_local_https,
                    self.owner_root / '.certs', hostname=endpoint.hostname,
                    interactive=not self.config.get('NO_STD_IN', False)))
                material = (await asyncio.shield(self._tls_task)).material
            if self.closed:
                raise SessionClosedError('UI test fixture closed during preparation')
            ssl_context = verified_client_context(material.ca_file if material else self.config.get('CLIENT_CA_FILE'))
            services = {'owner_root':self.owner_root, 'emissions':self.emissions,
                'runtime_values':values, 'tls_material':material, 'ssl_context':ssl_context,
                'ui_processes':StudioProcesses(ssl_context=ssl_context), **self.extra_services}
            if self.backend_factory is not None:
                self.backend = self.backend_factory(values)
                binding = await self.backend.__aenter__()
                _validate_backend_binding(binding)
                if self.closed:
                    raise SessionClosedError('UI test fixture closed during preparation')
                values['AGENT_DJANGO_URL'] = binding['agent_url']
                self.emissions.register_secret(binding['access_token'])
                services['ui_access_token'] = binding['access_token']
            self.prepared = PreparedUISession(values, services, browser_owner=self.browser_owner,
                user_index=self.config.get('UNATTENDED_USER_INDEX'), credentials_for=self._credentials_for,
                retain_on_failure=True)
            await self.prepared.start()
            return self.prepared

    async def capture(self, phase):
        from .browser_evidence import capture_ui_evidence
        from .browser_attachments import publish_attachment
        # Do not even inspect browser/provider state when content is disabled.
        if self.emissions.policy.allow_content is not True:
            return
        session = self.browser_owner.session
        provider = None if self.prepared is None else self.prepared.provider
        policy = None
        if provider is not None and 'capture_evidence' in provider.implementation.capabilities.operations:
            async def policy(page, boundary):
                return await provider.require('ui','capture_evidence').acapture_evidence(page=page, boundary=boundary)
        with independent_timeout_scope():
            await capture_ui_evidence(() if session is None else (session.context,), self.emissions,
                publish_attachment, capture_policy=policy, phase=phase)

    async def aclose(self):
        self.closed = True
        with independent_timeout_scope():
            async with report_timeout('UI fixture disposal', 20) as budget:
                if self.interaction is not None:
                    self.interaction.cancel()
                starting = self._starting
                if starting is not None and starting is not asyncio.current_task() and not starting.done():
                    _cancel_once(starting, self._cancel_requested)
                    done, _ = await asyncio.wait({starting}, timeout=budget.remaining)
                    if not done:
                        raise CleanupPendingError((self,), self.aclose)
                if self.prepared is not None:
                    await self.prepared.aclose()
                else:
                    await self.browser_owner.aclose()
                if self.backend is not None:
                    try:
                        if self._backend_cleanup is not None:
                            await self._backend_cleanup.retry()
                        else:
                            await self.backend.__aexit__(None, None, None)
                    except CleanupPendingError as error:
                        self._backend_cleanup = error
                        raise
                    self.backend = self._backend_cleanup = None
                tasks = set(self.pending_tasks)
                if self._tls_task is not None:
                    tasks.add(self._tls_task)
                if tasks:
                    _, pending = await asyncio.wait(tasks, timeout=budget.remaining)
                    if pending:
                        raise CleanupPendingError((self,), self.aclose)
                    for task in tasks:
                        if not task.cancelled():
                            task.exception()
                if self.interaction is not None:
                    self.interaction.close()


def _validate_backend_binding(binding):
    """Permit only a loopback Agent URL and the instance's ephemeral nonce."""
    if type(binding) is not dict or set(binding) != {'agent_url', 'access_token'}:
        raise ValueError('Invalid UI backend binding')
    listener_plan(binding['agent_url'])
    token = binding['access_token']
    if (type(token) is not str or not 32 <= len(token) <= 256
            or not token.isascii() or not token.isprintable()):
        raise ValueError('Invalid UI backend binding')
