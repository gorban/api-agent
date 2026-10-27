"""Own a coalesced session snapshot and its acquisition lifetime."""
import asyncio
import time

from api_agent.providers.contracts import ConfigurationRequiredError, ContractError, InteractionRequiredError, ProviderAccessError, SessionClosedError, SetupPrerequisiteError, UnattendedCredentialError
from api_agent.providers.control import finish_cleanup
from api_agent.providers.progress import component_timeout, independent_timeout_scope
from .acquisition import SessionMaterial


class AcquiredSession:
    def __init__(self, acquirer, request, *, clock=time.time, refresh_margin=30, credential_owner=None):
        self._credential_owner = credential_owner
        self._acquirer = acquirer
        self._request = request
        self._clock = clock
        self._refresh_margin = refresh_margin
        self._material = None
        self._pending = None
        self._closed = False
        self._adapter_closed = False
        self._closing = None

    def __repr__(self):
        return '<AcquiredSession>'

    def _live(self):
        if self._closed:
            raise SessionClosedError('Bedrock session is closed')

    async def _acquire(self):
        async with component_timeout('Bedrock session acquisition', 60):
            try:
                material = await self._acquirer.acquire(self._request, previous=self._material)
                if type(material) is not SessionMaterial:
                    raise ContractError('Invalid Bedrock session material')
                material.worker_config(self._request, self._clock())
            except ProviderAccessError:
                raise
            except ConfigurationRequiredError:
                raise
            except SetupPrerequisiteError:
                raise SetupPrerequisiteError('Selected Bedrock session adapter prerequisites are unavailable; check requirements.') from None
            except UnattendedCredentialError:
                raise
            except InteractionRequiredError:
                raise InteractionRequiredError('Bedrock session acquisition requires interaction.') from None
            except Exception:
                raise ContractError('Bedrock session acquisition failed') from None
            self._live()
            self._material = material

    def _finished(self, task):
        if not task.cancelled():
            task.exception()
        self._pending = None

    async def __call__(self):
        async with component_timeout('Bedrock session refresh', 60):
            self._live()
            material = self._material
            if material is not None and (material.refresh_after is None or material.refresh_after > self._clock()) and (material.expires_at is None or material.expires_at > self._clock() + self._refresh_margin):
                self.worker_config()
                return self
            if self._pending is None:
                with independent_timeout_scope():
                    self._pending = asyncio.create_task(self._acquire())
                self._pending.add_done_callback(self._finished)
            task = self._pending
            await asyncio.shield(task)
            self.worker_config()
            return self

    def worker_config(self):
        self._live()
        if self._material is None:
            raise ContractError('Bedrock session has not been acquired')
        return self._material.worker_config(self._request, self._clock())

    async def _dispose(self):
        async with component_timeout('Bedrock session cleanup', 60):
            if self._pending is not None:
                task = self._pending
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                self._pending = None
            failed = False
            if self._credential_owner is not None:
                try:
                    await self._credential_owner.aclose()
                except Exception:
                    failed = True
            if not self._adapter_closed:
                try:
                    await self._acquirer.aclose()
                except Exception:
                    failed = True
                else:
                    self._adapter_closed = True
            if failed:
                raise ContractError('Bedrock session cleanup failed') from None

    async def aclose(self):
        self._closed = True
        if self._credential_owner is not None:
            self._credential_owner.revoke()
        self._material = None
        if self._closing is None or self._closing.done():
            with independent_timeout_scope():
                self._closing = asyncio.create_task(self._dispose())
        task = self._closing

        async def drain():
            await asyncio.shield(task)

        await finish_cleanup(drain())
