"""Load explicitly selected, provider-local session acquisition adapters."""
import asyncio
from collections.abc import Mapping
import importlib.util
import inspect
import sys
from types import MappingProxyType, ModuleType
import uuid

from api_agent.providers.contracts import ConfigurationRequiredError, InteractionRequiredError, ProviderAccessError, SessionClosedError, SetupPrerequisiteError, UnattendedCredentialError
from api_agent.providers.control import finish_cleanup
from api_agent.providers.loading import (
    LoadedProvider, ProviderError, _ReturnedAwaitable, provider_location, release_modules,
)
from api_agent.providers.progress import component_timeout
from .ambient import AmbientSession


class SDKProfileAcquirer:
    """Freeze and verify an already authenticated selected SDK profile."""
    version = 1

    def __init__(self):
        self._closed = False
        self._ambient = AmbientSession()

    async def acquire(self, request, *, previous=None):
        async with component_timeout('Bedrock profile acquisition', 60):
            if self._closed:
                raise SessionClosedError('Bedrock session acquirer is closed')
            if request.user_index is not None:
                raise InteractionRequiredError('Indexed Bedrock session identity binding is unavailable.')
            material, _ = await self._ambient.acquire(request)
            if self._closed:
                raise SessionClosedError('Bedrock session acquirer is closed')
            if material is None:
                raise InteractionRequiredError('Selected SDK profile requires authentication; select an attended session adapter to sign in.')
            return material

    async def aclose(self):
        self._closed = True
        await self._ambient.aclose()


class _AcquirerOwner:
    """Keep the adapter's import namespace alive through retriable disposal."""
    version = 1

    def __init__(self, adapter, name):
        self._owner = LoadedProvider(adapter, name)

    def __repr__(self):
        return '<BedrockSessionAcquirer>'

    async def acquire(self, request, *, previous=None):
        if self._owner.closed:
            raise SessionClosedError('Bedrock session acquirer is closed')
        try:
            async with component_timeout('Bedrock session acquisition', 60):
                return await self._owner.implementation.acquire(request, previous=previous)
        except asyncio.CancelledError:
            raise
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
            raise ProviderError('bedrock_session_acquisition_failed') from None

    async def aclose(self):
        try:
            async with component_timeout('Bedrock session adapter cleanup', 60):
                await finish_cleanup(self._owner.aclose())
        except asyncio.CancelledError as cancelled:
            cancelled.retained_owners = (self,)
            raise
        except Exception:
            raise ProviderError('bedrock_session_cleanup_pending', retained_owners=(self,)) from None


class _RejectedAdapter:
    """Dispose even a malformed synchronous close hook without awaiting None."""
    def __init__(self, adapter):
        self.adapter = adapter

    async def aclose(self):
        hook = getattr(self.adapter, 'aclose', None)
        if hook is not None:
            result = hook()
            if inspect.isawaitable(result):
                await result


def _validate(adapter):
    if type(getattr(adapter, 'version', None)) is not int or adapter.version != 1:
        raise ProviderError('bedrock_session_version_invalid')
    for name, args, kwargs in (
        ('acquire', (object(),), {'previous': None}), ('aclose', (), {}),
    ):
        hook = getattr(adapter, name, None)
        if not inspect.iscoroutinefunction(hook):
            raise ProviderError('bedrock_session_protocol_invalid')
        try:
            signature = inspect.signature(hook)
            signature.bind(*args, **kwargs)
            parameters = tuple(signature.parameters.values())
            if name == 'acquire':
                if (len(parameters) != 2
                        or parameters[1].name != 'previous'
                        or parameters[1].kind is not inspect.Parameter.KEYWORD_ONLY
                        or parameters[1].default is not None):
                    raise TypeError
            elif parameters:
                raise TypeError
        except (TypeError, ValueError):
            raise ProviderError('bedrock_session_protocol_invalid') from None


async def load_acquirer(location, config, services):
    """Import only during awaited setup; an explicit selection never falls back."""
    if location == '':
        return SDKProfileAcquirer()
    name = '_api_agent_session_' + uuid.uuid4().hex
    adapter = None
    try:
        async with component_timeout('Bedrock session adapter setup', 60):
            root = services.get('owner_root') if isinstance(services, Mapping) else None
            path, factory_name = provider_location(location, owner_root=root)
            spec = importlib.util.spec_from_file_location(name + '.entry', path)
            module = importlib.util.module_from_spec(spec)
            package = ModuleType(name)
            package.__path__ = [str(path.parent)]
            sys.modules[name] = package
            sys.modules[name + '.entry'] = module
            spec.loader.exec_module(module)
            factory = getattr(module, factory_name, None)
            if not callable(factory):
                raise ProviderError('bedrock_session_factory_invalid')
            adapter = factory(MappingProxyType(dict(config)), services)
            if inspect.isawaitable(adapter):
                if inspect.iscoroutine(adapter):
                    adapter.close()
                    adapter = None
                else:
                    adapter = _ReturnedAwaitable(adapter, name)
                raise ProviderError('bedrock_session_factory_must_be_synchronous')
            _validate(adapter)
            return _AcquirerOwner(adapter, name)
    except BaseException as error:
        if adapter is not None:
            owner = LoadedProvider(_RejectedAdapter(adapter), name)
            try:
                await finish_cleanup(owner.aclose())
            except asyncio.CancelledError as cancelled:
                cancelled.retained_owners = (owner,)
                raise
            except Exception:
                raise ProviderError('bedrock_session_cleanup_pending', retained_owners=(owner,)) from None
        else:
            release_modules(name)
        if isinstance(error, asyncio.CancelledError):
            raise
        raise ProviderError('bedrock_session_initialization_failed') from None
