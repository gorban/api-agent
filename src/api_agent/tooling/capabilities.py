"""Explicit borrowed service capabilities with one operation-owned lifetime."""
import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import inspect
import re
from types import MappingProxyType

from api_agent.providers.progress import component_timeout


_CAPABILITY = re.compile(r'[a-z][a-z0-9_-]*@[1-9][0-9]*\Z')
_IDENTITY = re.compile(r'[a-z][a-z0-9_-]*/[a-z][a-z0-9_-]*\Z')


@dataclass(frozen=True)
class ServiceBinding:
    handle: object = field(repr=False)
    capabilities: Mapping = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.capabilities, Mapping) or not self.capabilities:
            raise ValueError('Service binding requires explicit capabilities')
        for name, view in self.capabilities.items():
            if type(name) is not str or not _CAPABILITY.fullmatch(name):
                raise ValueError('Service capability requires a versioned name')
            if view is self.handle or any(callable(getattr(view, method, None)) for method in
                                          ('close', 'aclose', 'dispose', 'adispose', 'release')):
                raise ValueError('Service capability must be a separate non-owning view')
        object.__setattr__(self, 'capabilities', MappingProxyType(dict(self.capabilities)))


class ServiceRegistry:
    """Bind explicit views, then close each parent once after its consumers settle."""
    def __init__(self, bindings):
        if not isinstance(bindings, Mapping) or any(
                type(identity) is not str or not _IDENTITY.fullmatch(identity)
                or not isinstance(binding, ServiceBinding) for identity, binding in bindings.items()):
            raise ValueError('Service registry requires qualified typed bindings')
        self._bindings = MappingProxyType(dict(bindings))
        self._disposed = set()
        self._closing = False
        self._loop = None
        self._task = None

    def bind(self, bindings, capabilities):
        if self._closing:
            raise ValueError('Service registry is closing')
        if (not isinstance(bindings, Mapping) or not isinstance(capabilities, Mapping)
                or set(bindings) != set(capabilities)
                or any(type(slot) is not str or not slot or type(identity) is not str
                       for slot, identity in bindings.items())):
            raise ValueError('Service capability slots must match exactly')
        result = {}
        for slot, identity in bindings.items():
            binding = self._bindings.get(identity)
            capability = capabilities[slot]
            if type(capability) is not str or not _CAPABILITY.fullmatch(capability):
                raise ValueError('Service requirement needs a versioned capability')
            if binding is None or capability not in binding.capabilities:
                raise ValueError('Required service capability is unavailable')
            result[slot] = binding.capabilities[capability]
        return MappingProxyType(result)

    async def aclose(self):
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise ValueError('Service registry belongs to another event loop')
        self._loop, self._closing = loop, True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._dispose())
        await asyncio.shield(self._task)

    async def _dispose(self):
        failures, attempted = [], set()
        async with component_timeout('Shared service cleanup', 30):
            for binding in reversed(tuple(self._bindings.values())):
                identity = id(binding.handle)
                if identity in self._disposed or identity in attempted:
                    continue
                attempted.add(identity)
                try:
                    close = getattr(binding.handle, 'aclose', None)
                    if close is not None:
                        result = close()
                        if inspect.isawaitable(result):
                            await result
                except Exception as error:
                    failures.append(error)
                else:
                    self._disposed.add(identity)
        if failures:
            raise ExceptionGroup('Shared service cleanup remains pending', failures)
