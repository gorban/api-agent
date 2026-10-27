"""Permanent fixture sampling, explicit pilot refresh and reviewed coverage bounds."""
import asyncio
import ast
from dataclasses import replace
from hashlib import sha256
from importlib.metadata import version
from importlib.resources import files
import json
from pathlib import Path
import platform
from uuid import uuid4

from api_agent.providers.selection import ModelSelectionPolicy
from api_agent.providers.loading import provider_location
from api_agent.runtime.tasks import offload
from api_agent.testing.inference_usage import user_turn
from .contrived import build
from .definition import load_definition
from .evidence import publish
from .execution import resolve_component_passes, resolve_timeouts
from .sampling import (DECLARED, TARGETS, COVERAGE_POLICY, cover, coverage_ceiling, validate_calibration,
                       measurement, observed_targets, DETERMINISTIC_CASES, coverage_case, case_targets, structural_targets)
from .run_context import trial as trial_context, turn

USER_INPUT = 'Repeat this demo request verbatim: hello Markdown graph.'
DEPENDENCIES = ('langgraph', 'langchain-core', 'jsonschema', 'markdown-it-py', 'httpx')
# Only code that can change requests, graph execution or observed node hits belongs
# here. Reporting, TUI, pricing and test collection do not change sampling.
SAMPLING_SOURCES = (
    'workflows/fixture_driver.py', 'workflows/contrived.py', 'workflows/execution.py', 'workflows/sampling.py',
    'workflows/layout.py', 'workflows/definition.py', 'workflows/document_format.py',
    'workflows/predicates.py', 'workflows/run_context.py',
    'llm_providers/response_policy.py', 'llm_providers/gpt/response_policy.py',
    'llm_providers/gpt/inference.py', 'llm_providers/gpt/rich.py',
    'llm_providers/gpt/cache_policy.py', 'llm_providers/gpt/reasoning.py', 'llm_providers/gpt/discovery.py',
    'llm_providers/gpt/endpoint.py', 'providers/llm.py', 'providers/contracts.py',
    'providers/control.py', 'providers/progress.py', 'providers/cache_policy.py',
    'providers/inference.py', 'providers/rich_inference.py', 'providers/setup_transport.py',
    'providers/bounded_schema.py', 'providers/schema_validation_worker.py',
    'providers/shared_setup.py', 'providers/setup_registry.py', 'providers/loading.py', 'auth/bindings.py', 'testing/setup_backend.py', 'testing/provider_setup.py', 'testing/setup_pool.py', 'testing/test_profiles.py',
    'llm_providers/gpt/factory.py', 'runtime/tasks.py',
)


def sampling_digest(path):
    body = path.read_bytes()
    if path.name == 'fixture_driver.py':
        # Refresh orchestration and validation do not change independent graph
        # requests. Bind only the fixture input, asset resolution and graph driver.
        tree = ast.parse(body)
        selected = [node for node in tree.body
                    if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name in {'run_graph', 'fixture_root', 'provider_identity', 'calibration_profile'})
                    or (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                        and target.id == 'USER_INPUT' for target in node.targets))]
        # Imports choose the implementations used by these sampling functions.
        # Bind referenced imports too: redirecting build/offload/uuid4 changes the
        # graph even when the function text itself is unchanged.
        referenced = {node.id for statement in selected for node in ast.walk(statement)
                      if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
        imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
                   and any((alias.asname or (alias.name.split('.')[0] if isinstance(node, ast.Import)
                            else alias.name)) in referenced for alias in node.names)]
        body = ast.dump(ast.Module(body=imports + selected, type_ignores=[]), include_attributes=False).encode()
    return sha256(body).hexdigest()


def fixture_root(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[3]
    return root if (root / 'demo/contrived/flow.md.html').is_file() else Path(str(files('api_agent')))


def provider_identity(config, *, owner_root=None):
    """Bind reviewed bounds to the physically selected bundled factory."""
    location = config.get('LLM_PROVIDER')
    if type(location) is not str or not location:
        raise ValueError('No reviewed fixture baseline without a selected bundled provider')
    path_text = location.rpartition(':')[0]
    if not Path(path_text).expanduser().is_absolute() and owner_root is None:
        raise ValueError('Relative fixture provider selection requires its operation owner root')
    path, factory = provider_location(location, owner_root=owner_root)
    package = Path(__file__).resolve().parent.parent
    canonical = package / 'llm_providers/gpt/factory.py'
    profile = calibration_profile(config, owner_root=owner_root)
    if profile is not None:
        return {'kind': profile['kind'], 'entrypoint': profile['entrypoint'],
                'source_sha256': sha256(path.read_bytes()).hexdigest()}
    if factory != 'factory' or not path.samefile(canonical):
        raise ValueError('No reviewed fixture baseline for a replacement provider')
    return {'kind': 'bundled_gpt', 'entrypoint': 'api_agent.llm_providers.gpt.factory:factory',
            'source_sha256': sha256(canonical.read_bytes()).hexdigest()}


def calibration_profile(config, *, owner_root=None):
    """Read inert qualification metadata only for physically bundled factories."""
    path, factory = provider_location(config['LLM_PROVIDER'], owner_root=owner_root)
    package = Path(__file__).resolve().parent.parent
    for declaration in sorted((package / 'llm_providers').glob('*/calibration.json')):
        if factory == 'factory' and path.samefile(declaration.with_name('factory.py')):
            profile = json.loads(declaration.read_text())
            if profile.get('schema') != 1:
                raise ValueError('Unsupported provider calibration declaration')
            supported = profile.get('workflow_selections', [])
            if supported and not any(all(config.get(key) == value for key, value in choice.items()) for choice in supported):
                raise ValueError('Unsupported workflow model/API profile; select a qualified workflow profile before calibration')
            return profile
    return None


def _selection_key(report):
    """Legacy GPT entries remain readable; every new route is provider-bound."""
    identity = report.get('provider_identity', {})
    route = report.get('backend', {}).get('api_mode', report.get('effective_settings', {}).get('api_mode'))
    return (identity.get('entrypoint'), route, report.get('model'))


def fingerprint(selection, config, *, root=None, owner_root=None):
    """Versioned sampling contract; installed assets use the same logical paths."""
    ceiling = coverage_ceiling(config.get('LLM_WORKFLOW_TEST_MAX_GRAPHS', 60))
    base = fixture_root(root)
    package = Path(__file__).resolve().parent.parent
    sources = {'src/api_agent/' + name: sampling_digest(package / name)
               for name in SAMPLING_SOURCES}
    definition = load_definition(base / 'demo/contrived/flow.md.html', base_root=base,
                                 pack_root=base / 'demo')
    timeouts, _ = resolve_timeouts(definition.layout, config, definition)
    component_passes, _ = resolve_component_passes(definition.layout, config)
    sources.update({resource.logical_path: resource.sha256 for resource in definition.resources})
    backend = {'auth_mode': config.get('GPT_AUTH_MODE', 'subscription'),
               'backend_identity': sha256(str(config.get('GPT_BASE_URL', 'https://api.openai.com/v1')).encode()).hexdigest()}
    result = dict(operating_ceiling=ceiling, fingerprint_version=4, sampling_method='simultaneous_exact_binomial_independent_uuid_v2', coverage_policy=COVERAGE_POLICY, model=selection,
        dependency_versions={name: version(name) for name in DEPENDENCIES},
        python_version=platform.python_version(), backend=backend,
        provider_identity=provider_identity(config, owner_root=owner_root),
        user_input_hash=sha256(USER_INPUT.encode()).hexdigest(),
        effective_settings={'temperature': None, 'max_output_tokens': None,
            'setup_timeout': config.get('LLM_SETUP_REQUEST_TIMEOUT_SECONDS', 300),
            'cache_policy': config.get('LLM_CACHE_POLICY') or None,
            'reasoning_effort': config.get('LLM_REASONING_EFFORT') or None,
            'api_mode': config.get('GPT_API_MODE'), 'retries': component_passes,
            'llm_timeout': config.get('WORKFLOW_LLM_TIMEOUT_SECONDS', 120),
            'node_timeout': None,
            'component_timeout': {name: timeouts.get(name) for name in ('first', 'second')},
            'graph_timeout': timeouts.get('GRAPH')}, sources=sources)
    profile = calibration_profile(config, owner_root=owner_root)
    if profile is not None:
        result['fingerprint_version'] = 4
        result['backend'] = {name: config.get(field, default)
            for name, (field, default) in profile['backend'].items()}
        if not result['backend'].get('region'):
            raise ValueError('Fixture calibration requires an explicit inference region')
        identity_values = {name: config.get(name) for name in profile['identity_settings']}
        for name in set(profile['identity_settings']) & set(profile['identity_source_settings']):
            if identity_values[name]:
                path, factory = provider_location(identity_values[name], owner_root=owner_root)
                # Canonicalize only physical bundled modules, never an external
                # copy or a path that escapes the package through a symlink.
                relative = next((path.relative_to(parent) for parent in path.parents
                                 if parent.samefile(package)), None)
                if relative is not None:
                    parts = relative.with_suffix('').parts
                    if all(part.isidentifier() for part in parts):
                        identity_values[name] = 'api_agent.' + '.'.join(parts) + ':' + factory
        result['backend']['configuration_sha256'] = sha256(json.dumps(
            identity_values, sort_keys=True).encode()).hexdigest()
        result['backend']['adapter_sources'] = {name: sha256(provider_location(
            config[name], owner_root=owner_root)[0].read_bytes()).hexdigest()
            for name in profile['identity_source_settings'] if config.get(name)}
        result['effective_settings']['api_mode'] = result['backend']['api_mode']
        result['effective_settings']['max_output_tokens'] = profile['max_output_tokens']
        names = [name for name in SAMPLING_SOURCES if not name.startswith('llm_providers/')]
        names.extend(profile['sources'])
        result['sources'] = {'src/api_agent/' + name: sampling_digest(package / name) for name in names}
        result['sources'].update({resource.logical_path: resource.sha256 for resource in definition.resources})
        result['dependency_versions'].update({name: version(name) for name in profile['dependencies']})
    return result


def _baselines():
    data = json.loads(files('api_agent.workflows').joinpath('fixture_baselines.json').read_text())
    if data.get('schema') != 1 or type(data.get('reports')) is not list:
        raise ValueError('Invalid reviewed fixture baselines')
    return data['reports']


def baseline_limit(selection, config, *, root=None, owner_root=None):
    """Fail stale/unknown sampling before a graph or LLM request can execute."""
    expected = fingerprint(selection, config, root=root, owner_root=owner_root)
    matches = [report for report in _baselines() if _selection_key(report) == _selection_key(expected)]
    if len(matches) != 1:
        raise ValueError('No reviewed matching fixture baseline for selected model/API (missing or stale); refresh calibration explicitly')
    return validate_calibration(matches[0], DECLARED, expected,
                                ceiling=coverage_ceiling(config.get('LLM_WORKFLOW_TEST_MAX_GRAPHS', 60)))


def preflight_baselines(config, *, root=None, owner_root=None):
    """Check configured eligible reviewed models before provider fixture lookup."""
    identity = provider_identity(config, owner_root=owner_root)
    policy = ModelSelectionPolicy(config)
    reports = [report for report in _baselines()
               if report.get('provider_identity', {}).get('entrypoint') == identity['entrypoint']]
    selected = config.get('LLM_WORKFLOW_TEST_MODEL')
    if 'LLM_WORKFLOW_TEST_MODEL' in config and not selected:
        raise ValueError('LLM_WORKFLOW_TEST_MODEL has no qualified workflow selection; select a supported model/API profile before calibration')
    candidates = [report['model'] for report in reports
                  if ((report['model'] == selected) if selected is not None else
                      (policy.default is not None and policy._matches(policy.default, report['model'])))
                  and policy._matches(policy.allowed, report['model'])]
    if not candidates:
        raise ValueError('Configured model selection has no reviewed fixture baseline; refresh calibration explicitly')
    return {selection: baseline_limit(selection, config, root=root, owner_root=owner_root) for selection in candidates}


def coverage_preflight(config, *, root=None):
    """Reject an impossible retry policy and invalid cap before provider setup."""
    base = fixture_root(root)
    definition = load_definition(base / 'demo/contrived/flow.md.html', base_root=base, pack_root=base / 'demo')
    passes, _ = resolve_component_passes(definition.layout, config)
    if any(passes[name] < 2 for name in ('first', 'second')):
        raise ValueError('Workflow coverage requires at least two passes for both components')
    cap = config.get('LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS')
    if cap is not None and (type(cap) is not int or not 1 <= cap <= 1048576):
        raise ValueError('Workflow output token cap must be empty or an integer from 1 to 1048576')


def run_graph(provider, selection, trial, config=None, *, root=None, owner_root=None, observe=None, trial_uuid=None):
    """Execute one real graph with the exact accepted UUID, independent of calibration."""
    events = []
    seed = str(uuid4()) if trial_uuid is None else trial_uuid
    coverage_case(seed)
    cap = (config or {}).get('LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS')

    def observed(event):
        events.append(event)
        if observe is not None:
            observe(event)

    async def complete(request):
        if cap is not None:
            request = replace(request, payload=replace(request.payload, generation=replace(
                request.payload.generation, max_output_tokens=cap)))
        result = await offload(provider.complete, request)
        actual = getattr(result, 'actual_model', None)
        if actual is not None and actual != selection:
            raise ValueError('Fixture inference returned a different model than the selected model')
        return result

    async def execute():
        turn_id = str(uuid4())
        with trial_context(trial), turn(turn_id), user_turn(turn_id):
            workflow = build(fixture_root(root), llm=complete, selection=selection,
                             trial=seed, observe=observed, config=config)
            result = await workflow.arun({'user_input': USER_INPUT}, turn_id=turn_id)
        return result, {event['node'] for event in events if event['event'] == 'node'}

    try:
        return asyncio.run(execute())
    finally:
        publish(dict(trial=trial, trial_uuid=seed, events=events))


def node_coverage(provider, selection, config, *, root=None, owner_root=None):
    """Execute each reachable UUID route/readiness case once, with real proof."""
    from api_agent.providers.progress import component_timeout
    coverage_preflight(config, root=root)

    async def execute():
        missing, seen, scopes, records = set(DETERMINISTIC_CASES), set(), set(), []
        try:
            async with component_timeout('Workflow case coverage', config.get('LLM_WORKFLOW_TEST_TIMEOUT_SECONDS', 600)) as budget:
                while missing:
                    if budget.remaining == 0:
                        raise TimeoutError('Workflow coverage deadline expired during UUID selection')
                    seed = str(uuid4())
                    case = coverage_case(seed)
                    if case not in missing:
                        await asyncio.sleep(0)
                        continue
                    trial, events = len(records) + 1, []
                    values, nodes = await offload(run_graph, provider, selection, trial, config,
                        root=root, owner_root=owner_root, observe=events.append, trial_uuid=seed)
                    row = measurement(trial, values, nodes, events)
                    targets = case_targets(row, case)
                    _, emitted_scopes = structural_targets(row)
                    if scopes & emitted_scopes:
                        raise AssertionError('Coverage graphs reuse executed scope identities')
                    if budget.remaining == 0:
                        raise TimeoutError('Workflow coverage deadline expired after graph proof')
                    scopes.update(emitted_scopes)
                    seen.update(targets)
                    missing.remove(case)
                    records.append(dict(trial=trial, trial_uuid=seed, route=case[0], first=case[1], second=case[2]))
                if not TARGETS <= seen:
                    raise AssertionError('Deterministic cases did not prove all workflow targets')
                if budget.remaining == 0:
                    raise TimeoutError('Workflow coverage deadline expired before coverage result')
        except BaseException as error:
            labels = []
            for route, first, second in sorted(missing, key=repr):
                label = f'{route}/first={str(first).lower()}'
                if second is not None:
                    label += f'/second={str(second).lower()}'
                labels.append(label)
            BaseException.add_note(error, 'Uncovered workflow cases: ' + (', '.join(labels) or 'none'))
            raise
        return dict(runs=len(records), nodes=sorted(seen & DECLARED), targets=sorted(seen),
                    records=records, stopping_reason='all_cases_hit',
                    coverage_policy='deterministic_route_readiness_v1')

    result = asyncio.run(execute())
    publish(dict(coverage=result))
    return result
