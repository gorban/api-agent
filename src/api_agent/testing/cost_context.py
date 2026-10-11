"""Content-free declared configuration fingerprints for historical matching."""
from hashlib import sha256
import json
import os
from pathlib import Path
import stat


def file_fingerprint(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            raw = stream.read(4194305)
        return sha256(raw).hexdigest() if len(raw) <= 4194304 else None
    except OSError:
        return None


def cost_config(context, config):
    """Hash only expressly declared non-secret request-affecting settings.

    Missing declarations/fingerprints prohibit matching, rather than treating
    an unknown context as interchangeable. This function never reads .env or
    starts provider/authentication operations.
    """
    result = dict(config)
    if config.get('TUI_MODE') == 'integration':
        # Integration execution never retries; its inactive E2E option is not
        # missing compatibility evidence for the actual retry policy.
        result['E2E_RERUNS'] = 0
    pack_root = Path(getattr(context, 'pack_root', context.owner_root))
    result['_COST_PACK_IDENTITY'] = file_fingerprint(pack_root / 'pack.toml')
    result['_COST_REQUEST_SETTINGS'] = None
    result['_COST_IGNORED_SETTINGS'] = ()
    location = config.get('LLM_PROVIDER')
    result['_COST_PROVIDER_IDENTITY'] = None
    if isinstance(location, str) and ':' in location:
        filename, _, factory = location.rpartition(':')
        fingerprint = file_fingerprint(filename)
        if fingerprint is not None:
            result['_COST_PROVIDER_IDENTITY'] = sha256((fingerprint + ':' + factory).encode()).hexdigest()
    try:
        from api_agent.config.declarations import load_shared_schema, load_declarations, compose_schema
        from api_agent.providers.loading import bootstrap_schema
        from api_agent.config.resolve import _matches
        from api_agent.telemetry.privacy import _credential_key
        shared = load_shared_schema(context.base_root)
        schema = compose_schema(shared, load_declarations(getattr(context, 'config_path', pack_root / 'cfg/config.toml'), base_schema=shared, source='pack_default'),
                                getattr(context, 'default_overrides', {}))
        choices = {name: value for name, value in config.items() if not name.startswith('_')}
        schema = bootstrap_schema(schema, (), {}, {}, choices, owner_root=context.owner_root,
                                  configuration_paths=getattr(context, 'provider_config_paths', ()))
        selected = {}
        ignored = tuple(name for name, field in schema.fields.items() if not field.cost_affecting)
        for name, field in schema.fields.items():
            if not field.cost_affecting:
                # Non-secret enum selections remain diagnostic evidence only.
                if field.enum and not _credential_key(name) and config.get(name) is not None:
                    selected[name] = sha256(json.dumps(config[name], sort_keys=True, allow_nan=False).encode()).hexdigest()
                continue
            if _credential_key(name) or field.type == 'jsonobject':
                return result
            if not _matches(field.applies_when, config):
                selected[name] = sha256(b'inactive').hexdigest()
                continue
            value = config.get(name)
            if value is None:
                return result
            if isinstance(value, Path):
                value = str(value)
            selected[name] = sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()
        result['_COST_REQUEST_SETTINGS'] = selected
        result['_COST_IGNORED_SETTINGS'] = ignored
    except (OSError, ValueError, TypeError):
        pass
    return result
