"""Load an explicitly declared pack identity factory without global path edits."""
import inspect
import importlib.util
import sys
import uuid
from collections.abc import Mapping
from types import MappingProxyType

from .identity import IdentityAdapterV1


class IdentityExtensionError(ValueError):
    """A declared identity extension could not be composed safely."""


def load_identity_adapter(context, config):
    """Instantiate the selected pack's adapter; demo has no identity requirement.

    Resolution of a launch context never imports executable adapter code. The
    application composition root calls this explicitly with resolved values.
    """
    path = context.identity_factory_path
    if path is None:
        return None
    if not isinstance(config, Mapping):
        raise IdentityExtensionError('Identity factory needs resolved configuration values')
    if not path.resolve().is_relative_to(context.pack_root.resolve()) or not path.is_file():
        raise IdentityExtensionError('Identity factory must remain inside its selected pack')
    name = '_api_agent_identity_' + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise IdentityExtensionError('Identity factory could not be loaded')
    module = importlib.util.module_from_spec(spec)
    # dataclasses and other import-time decorators require an isolated module
    # registration. Remove it after composition; do not reuse another runtime's
    # factory or modify global sys.path to make a pack importable.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        factory = getattr(module, context.identity_factory_name, None)
        if not callable(factory):
            raise IdentityExtensionError('Declared identity factory is not callable')
        adapter = factory(MappingProxyType(dict(config)))
        if inspect.iscoroutine(adapter):
            adapter.close()
            raise IdentityExtensionError('Identity factory must be synchronous')
        if (not isinstance(adapter, IdentityAdapterV1) or type(adapter.version) is not int
                or adapter.version != 1 or not all(inspect.iscoroutinefunction(getattr(adapter, method))
                    for method in ('resolve_request', 'login_hint', 'match_llm_identity'))):
            raise IdentityExtensionError('Identity factory must implement contract version 1')
        return adapter
    except Exception:
        # Import/factory exceptions can include credentials or upstream details.
        raise IdentityExtensionError('Identity extension failed to initialize; check its declaration and configuration') from None
    finally:
        sys.modules.pop(name, None)
