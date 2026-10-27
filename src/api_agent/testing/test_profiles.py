"""Inert provider-declared defaults for exact, admitted integration cases."""
from copy import deepcopy
from pathlib import Path
import re
import tomllib

from api_agent.config import compose_schema, load_declarations, load_shared_schema
from api_agent.config.resolve import convert_value
from api_agent.providers.loading import provider_location
from api_agent.providers.selection import ModelSelectionPolicy

# Providers may additionally declare their own API/generation controls. Identity,
# credentials, policies and security configuration can never be profile inputs.
SHARED_KEYS = frozenset({
    'LLM_DEFAULT_MODEL', 'LLM_REASONING_EFFORT', 'LLM_SETUP_REQUEST_TIMEOUT_SECONDS',
    'WORKFLOW_MAX_PASSES', 'WORKFLOW_COMPONENT_TIMEOUT_SECONDS',
    'WORKFLOW_GRAPH_TIMEOUT_SECONDS', 'WORKFLOW_LLM_TIMEOUT_SECONDS',
    'LLM_WORKFLOW_TEST_MODEL', 'LLM_WORKFLOW_TEST_MAX_GRAPHS', 'LLM_REASONING_TEST_MODEL',
    'LLM_STUDIO_TEST_DEFAULT_MODEL', 'LLM_STUDIO_TEST_SELECTED_MODEL',
    'LLM_STUDIO_TEST_DEFAULT_EFFORT', 'LLM_STUDIO_TEST_SELECTED_EFFORT',
    'LLM_REASONING_TEST_LOW_EFFORT', 'LLM_REASONING_TEST_HIGH_EFFORT',
    'LLM_REASONING_TEST_TOKEN_REPORTING', 'LLM_REASONING_TEST_MAX_OUTPUT_TOKENS',
    'LLM_REASONING_TEST_TIMEOUT_SECONDS',
})
TEST_KEYS = frozenset(key for key in SHARED_KEYS if '_TEST_' in key) | {'LLM_SETUP_REQUEST_TIMEOUT_SECONDS'}


def bundled_suite_root():
    package = Path(__file__).resolve().parents[1]
    source = package.parent.parent
    candidate = source / 'src/api_agent'
    return source if candidate.is_dir() and candidate.samefile(package) else package


def declaration(config, *, owner_root):
    """Read beside the selected factory; importing an adapter is unnecessary."""
    location = config.get('LLM_PROVIDER')
    if not location:
        return None
    factory, symbol = provider_location(location, owner_root=owner_root)
    path = factory.parent / 'test-profiles.toml'
    if not path.is_file():
        return None
    data = tomllib.loads(path.read_text())
    if set(data) != {'version', 'factory', 'provider_fields', 'profiles'} or data['version'] != 1 or data['factory'] != symbol:
        raise ValueError('Invalid provider test profile declaration')
    shared = load_shared_schema()
    schema = compose_schema(shared, load_declarations(factory.parent / 'cfg/config.toml', base_schema=shared))
    provider_fields = data['provider_fields']
    def safe_provider_control(key):
        if type(key) is not str or key not in schema.fields or key in shared.fields:
            return False
        field = schema.fields[key]
        if field.sensitive:
            return False
        if key.endswith('API_MODE'):
            return field.type == 'str' and bool(field.enum)
        if key.endswith('REASONING_EFFORT'):
            return field.type == 'str' and bool(field.enum) and set(field.enum) <= {'', 'off', 'low', 'medium', 'high'}
        if key.endswith('MAX_OUTPUT_TOKENS'):
            return field.type == 'int' and field.validation == 'positive'
        if key.endswith('INFERENCE_TIMEOUT_SECONDS'):
            return field.type in ('int', 'float') and field.validation == 'positive'
        return False
    if (type(provider_fields) is not list or len(set(provider_fields)) != len(provider_fields)
            or any(not safe_provider_control(key) or not re.fullmatch(r'[A-Z][A-Z0-9_]*(?:API_MODE|INFERENCE_TIMEOUT_SECONDS|MAX_OUTPUT_TOKENS|REASONING_EFFORT)', key) for key in provider_fields)):
        raise ValueError('Provider test profile fields must be declared API or generation controls')
    profiles = data['profiles']
    if type(profiles) is not dict:
        raise ValueError('Invalid provider test profiles')
    admitted = set()
    for name, profile in profiles.items():
        if (type(profile) is not dict or set(profile) != {'cases', 'settings'}
                or type(profile['cases']) is not list or not profile['cases']
                or type(profile['settings']) is not dict):
            raise ValueError('Invalid provider test profile: ' + name)
        for case in profile['cases']:
            if (type(case) is not str or not re.fullmatch(r'tests/integration/[^/]+\.py::test_[^/]+', case)
                    or case in admitted):
                raise ValueError('Profile cases must be unique exact integration node IDs')
            admitted.add(case)
        for key, value in profile['settings'].items():
            if key not in SHARED_KEYS | set(provider_fields) or schema.fields[key].sensitive:
                raise ValueError('Forbidden test profile setting: ' + key)
            convert_value(schema.fields[key], value, external=False, anchors=None)
            if key == 'LLM_WORKFLOW_TEST_MAX_GRAPHS':
                from api_agent.workflows.sampling import coverage_ceiling
                coverage_ceiling(value)
    return path, schema, profiles


def resolve_test_profiles(tests, config, *, owner_root, fields=None):
    """Application < test defaults < explicitly supplied test-specific controls.

    Dict-only callers have no provenance, so their existing test-specific keys
    conservatively count as explicit. No application/environment mapping changes.
    """
    loaded = declaration(config, owner_root=owner_root)
    output = {}
    for test in tests:
        if test.id in output:
            raise ValueError('Duplicate admitted integration case')
        values, provenance = deepcopy(dict(config)), {}
        if loaded is not None:
            path, schema, profiles = loaded
            for name, profile in profiles.items():
                if test.node_id not in profile['cases']:
                    continue
                # Only bundled suite cases belong to a provider's bundled profile.
                if not Path(test.root or owner_root).samefile(bundled_suite_root()):
                    continue
                for key, value in profile['settings'].items():
                    explicit = key in TEST_KEYS and (key in config if fields is None else bool(getattr(fields.get(key), 'explicit', False)))
                    if explicit:
                        provenance[key] = getattr(fields.get(key), 'source', 'explicit_mapping') if fields is not None else 'explicit_mapping'
                        continue
                    values[key] = convert_value(schema.fields[key], value, external=False, anchors=None)
                    provenance[key] = f'provider_test_profile:{path.name}:{name}'
        if 'LLM_WORKFLOW_TEST_MAX_GRAPHS' in values:
            from api_agent.workflows.sampling import coverage_ceiling
            coverage_ceiling(values['LLM_WORKFLOW_TEST_MAX_GRAPHS'])
        policy = ModelSelectionPolicy(values)
        for key in ('LLM_WORKFLOW_TEST_MODEL', 'LLM_REASONING_TEST_MODEL', 'LLM_STUDIO_TEST_DEFAULT_MODEL', 'LLM_STUDIO_TEST_SELECTED_MODEL'):
            if key in provenance and values.get(key):
                policy.require_allowed(values[key])
        output[test.id] = {'node_id': test.node_id, 'repo': test.repo, 'config': values, 'provenance': provenance}
    return output


def validate_snapshot_profiles(profiles):
    """Reject malformed IPC before any worker acquires a provider/session."""
    from api_agent.providers.setup_transport import validate_descriptor
    if type(profiles) is not dict:
        raise ValueError('Invalid integration profile map')
    for node, row in profiles.items():
        if (type(node) is not str or type(row) is not dict
                or set(row) - {'node_id', 'repo', 'config', 'provenance', 'setup_backend'}
                or not {'config', 'provenance'} <= set(row)
                or type(row['config']) is not dict or type(row['provenance']) is not dict
                or any(type(key) is not str or type(value) is not str for key, value in row['provenance'].items())):
            raise ValueError('Invalid integration profile entry')
        if 'setup_backend' in row:
            validate_descriptor(row['setup_backend'])
