"""Permanent fixture sampling, explicit pilot refresh and reviewed coverage bounds."""
import asyncio
import ast
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
from .sampling import DECLARED, cover, validate_calibration

USER_INPUT = 'Repeat this demo request verbatim: hello Markdown graph.'
DEPENDENCIES = ('langgraph', 'langchain-core', 'jsonschema', 'markdown-it-py', 'httpx')
# Only code that can change requests, graph execution or observed node hits belongs
# here. Reporting, TUI, pricing and test collection do not change sampling.
SAMPLING_SOURCES = (
    'workflows/fixture_driver.py', 'workflows/contrived.py', 'workflows/execution.py',
    'workflows/layout.py', 'workflows/definition.py', 'llm_providers/gpt/inference.py', 'llm_providers/gpt/rich.py',
    'llm_providers/gpt/cache_policy.py', 'llm_providers/gpt/reasoning.py', 'llm_providers/gpt/discovery.py',
    'llm_providers/gpt/endpoint.py', 'providers/llm.py', 'providers/contracts.py',
    'providers/control.py', 'providers/progress.py', 'providers/cache_policy.py',
    'providers/inference.py', 'providers/rich_inference.py', 'providers/setup_transport.py',
    'providers/shared_setup.py', 'providers/loading.py', 'auth/bindings.py', 'testing/setup_backend.py', 'testing/provider_setup.py',
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
                        and node.name in {'run_graph', 'fixture_root', 'provider_identity'})
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
    """Only the physically selected bundled factory has reviewed fixture bounds."""
    location = config.get('LLM_PROVIDER')
    if type(location) is not str or not location:
        raise ValueError('No reviewed fixture baseline without a selected bundled GPT provider')
    path_text = location.rpartition(':')[0]
    if not Path(path_text).expanduser().is_absolute() and owner_root is None:
        raise ValueError('Relative fixture provider selection requires its operation owner root')
    path, factory = provider_location(location, owner_root=owner_root)
    canonical = Path(__file__).resolve().parent.parent / 'llm_providers/gpt/factory.py'
    if factory != 'factory' or not path.samefile(canonical):
        raise ValueError('No reviewed fixture baseline for a replacement provider')
    return {'kind': 'bundled_gpt', 'entrypoint': 'api_agent.llm_providers.gpt.factory:factory',
            'source_sha256': sha256(canonical.read_bytes()).hexdigest()}


def fingerprint(selection, config, *, root=None, owner_root=None):
    """Versioned sampling contract; installed assets use the same logical paths."""
    base = fixture_root(root)
    package = Path(__file__).resolve().parent.parent
    sources = {'src/api_agent/' + name: sampling_digest(package / name)
               for name in SAMPLING_SOURCES}
    definition = load_definition(base / 'demo/contrived/flow.md.html', base_root=base,
                                 pack_root=base / 'demo')
    sources.update({resource.logical_path: resource.sha256 for resource in definition.resources})
    backend = {'auth_mode': config.get('GPT_AUTH_MODE', 'subscription'),
               'backend_identity': sha256(str(config.get('GPT_BASE_URL', 'https://api.openai.com/v1')).encode()).hexdigest()}
    return dict(fingerprint_version=3, sampling_method='simultaneous_exact_binomial_independent_uuid_v1', model=selection,
        dependency_versions={name: version(name) for name in DEPENDENCIES},
        python_version=platform.python_version(), backend=backend,
        provider_identity=provider_identity(config, owner_root=owner_root),
        user_input_hash=sha256(USER_INPUT.encode()).hexdigest(),
        effective_settings={'temperature': None, 'max_output_tokens': None,
            'cache_policy': config.get('LLM_CACHE_POLICY') or None,
            'reasoning_effort': config.get('LLM_REASONING_EFFORT') or None,
            'api_mode': config.get('GPT_API_MODE'), 'retries': {'first': 2, 'second': 2},
            'llm_timeout': config.get('WORKFLOW_LLM_TIMEOUT_SECONDS', 120),
            'node_timeout': config.get('WORKFLOW_NODE_TIMEOUT_SECONDS'),
            'component_timeout': config.get('WORKFLOW_COMPONENT_TIMEOUT_SECONDS'),
            'graph_timeout': config.get('WORKFLOW_GRAPH_TIMEOUT_SECONDS')}, sources=sources)


def _baselines():
    data = json.loads(files('api_agent.workflows').joinpath('fixture_baselines.json').read_text())
    if data.get('schema') != 1 or type(data.get('reports')) is not list:
        raise ValueError('Invalid reviewed fixture baselines')
    return data['reports']


def baseline_limit(selection, config, *, root=None, owner_root=None):
    """Fail stale/unknown sampling before a graph or LLM request can execute."""
    matches = [report for report in _baselines() if report.get('model') == selection]
    if len(matches) != 1:
        raise ValueError('No reviewed fixture baseline for selected model; refresh calibration explicitly')
    return validate_calibration(matches[0], DECLARED, fingerprint(selection, config, root=root, owner_root=owner_root))


def preflight_baselines(config, *, root=None, owner_root=None):
    """Check configured eligible reviewed models before provider fixture lookup."""
    provider_identity(config, owner_root=owner_root)
    policy = ModelSelectionPolicy(config)
    reports = _baselines()
    candidates = [report['model'] for report in reports
                  if policy.default is not None and policy._matches(policy.default, report['model'])
                  and policy._matches(policy.allowed, report['model'])]
    if not candidates:
        raise ValueError('Configured model selection has no reviewed fixture baseline; refresh calibration explicitly')
    return {selection: baseline_limit(selection, config, root=root, owner_root=owner_root) for selection in candidates}


def run_graph(provider, selection, trial, config=None, *, root=None, owner_root=None, observe=None):
    """One independent graph with unchanged request/seed and structural evidence."""
    provider_identity(config or {}, owner_root=owner_root)
    events = []

    def observed(event):
        events.append(event)
        if observe is not None:
            observe(event)

    async def complete(request):
        result = await offload(provider.complete, request)
        actual = getattr(result, 'actual_model', None)
        if actual is not None and actual != selection:
            raise ValueError('Fixture inference returned a different model than the reviewed selection')
        return result

    async def execute():
        with user_turn(str(uuid4())):
            workflow = build(fixture_root(root), llm=complete, selection=selection,
                             trial=str(uuid4()), observe=observed, config=config)
            result = await workflow.arun({'user_input': USER_INPUT})
        return result, {event['node'] for event in events if event['event'] == 'node'}

    try:
        return asyncio.run(execute())
    finally:
        publish(dict(trial=trial, events=events))


def node_coverage(provider, selection, config, *, root=None, owner_root=None):
    """Mandatory early stopping using checked-in, recomputed per-model bounds."""
    maximum = baseline_limit(selection, config, root=root, owner_root=owner_root)

    async def execute(trial):
        return await offload(run_graph, provider, selection, trial, config, root=root, owner_root=owner_root)

    result, _ = asyncio.run(cover(execute, DECLARED, maximum, publish))
    publish(dict(coverage=result))
    return result
