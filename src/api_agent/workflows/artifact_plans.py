"""Generate and statically verify checked-in compiled workflow plans."""
import argparse
from pathlib import Path

from .artifact_compile import ArtifactCompileError, compile_plan

PLAN_DIRECTORY = '.private_resources/plans'
IGNORED_PARTS = frozenset({'.git', '.venv', 'node_modules', '__pycache__', 'dist', 'build'})
QUALIFICATION_PLAN = 'qualification/simple_chat.json'
QUALIFICATION_WHEEL = 'demo/.private_resources/wheels/api_agent_demo_bindings-0.1.0-py3-none-any.whl'


def plan_path(pack_root: Path, graph_id: str) -> Path:
    """The generated-plan path mirrors workflow-view generated resources."""
    return pack_root / PLAN_DIRECTORY / f'{graph_id}.json'


def compile_selected_plan(context) -> bytes:
    """Compile the source declaration selected by one artifact-enabled pack."""
    entry = context.workflow_entry
    if entry is None:
        raise ValueError('compiled workflow plan requires a selected workflow entry')
    wheels = getattr(context, 'workflow_artifact_wheels', ())
    if not wheels:
        raise ValueError('compiled workflow plan requires declared source wheels')
    missing = next((wheel for wheel in wheels if not wheel.is_file()), None)
    if missing is not None:
        raise ValueError(f'compiled workflow plan declared source wheel is missing: {missing}')
    base = getattr(context, 'workflow_base_root', None) or context.base_root
    pack = getattr(context, 'workflow_pack_root', None) or context.pack_root
    return compile_plan(entry, base_root=base, pack_root=pack, wheels=wheels,
                        entry_root=context.workflow_root, replacements=context.workflow_replacements)


def _qualification_plan(root: Path) -> bytes:
    """The production image qualifier's plan: a fresh compile of the root workflow."""
    return compile_plan(root / 'flow.md.html', base_root=root, pack_root=root, entry_root='base',
                        wheels=[root / QUALIFICATION_WHEEL])


def _contexts(root: Path):
    from api_agent.runtime.resource_pack import ResourcePackError, load_launch_context

    manifests = sorted(path for path in root.rglob('pack.toml')
                       if not any(part in IGNORED_PARTS for part in path.relative_to(root).parts))
    for manifest in manifests:
        try:
            context = load_launch_context(root, manifest.parent)
        except ResourcePackError as error:
            raise ValueError(f'{manifest.relative_to(root)}: invalid resource pack: {error}') from error
        if context.workflow_artifact_plan is not None:
            yield context


def _plan_files(root: Path):
    """List files at canonical plan paths (``<pack>/PLAN_DIRECTORY/<graph_id>.json``) only.

    Other files in a plan directory, including nested ones, are not generated plans and are
    neither checked nor removed.
    """
    directories = sorted(path for path in root.rglob(PLAN_DIRECTORY)
                         if path.is_dir()
                         and not any(part in IGNORED_PARTS for part in path.relative_to(root).parts))
    for directory in directories:
        yield from sorted(path for path in directory.glob('*.json') if path.is_file())


def check_plans(root: Path) -> None:
    """Require every selected checked-in plan to equal a fresh deterministic compile."""
    root = Path(root).resolve()
    contexts = tuple(_contexts(root))
    selected = {context.workflow_artifact_plan for context in contexts}
    for context in contexts:
        plan = context.workflow_artifact_plan
        expected = plan_path(context.pack_root, context.workflow_artifact_graph_id)
        if plan != expected:
            raise ValueError(f'{plan.relative_to(root)}: compiled plans belong at '
                             f'{expected.relative_to(root)}')
        if not plan.is_file():
            raise ValueError(f'{plan.relative_to(root)}: compiled plan is missing for selected graph '
                             f'{context.workflow_artifact_graph_id}')
        try:
            compiled = compile_selected_plan(context)
        except (ArtifactCompileError, OSError, ValueError) as error:
            raise ValueError(f'{plan.relative_to(root)}: cannot compile selected graph: {error}') from error
        if plan.read_bytes() != compiled:
            raise ValueError(f'{plan.relative_to(root)}: compiled plan is stale; regenerate with '
                             'python -m api_agent.workflows.artifact_plans regenerate --root .')
    for plan in _plan_files(root):
        if plan not in selected:
            raise ValueError(f'{plan.relative_to(root)}: orphaned compiled plan is not selected by any '
                             'resource pack; delete it or restore its declaration')
    qualification = root / QUALIFICATION_PLAN
    if qualification.is_file() and qualification.read_bytes() != _qualification_plan(root):
        raise ValueError(f'{QUALIFICATION_PLAN}: compiled plan is stale; regenerate with '
                         'python -m api_agent.workflows.artifact_plans regenerate --root .')


def regenerate_plans(root: Path) -> tuple[Path, ...]:
    """Write all selected plans; this is the canonical explicit regeneration path."""
    root = Path(root).resolve()
    contexts = tuple(_contexts(root))
    selected = {context.workflow_artifact_plan for context in contexts}
    written = []
    for context in contexts:
        plan = context.workflow_artifact_plan
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_bytes(compile_selected_plan(context))
        written.append(plan)
    for plan in _plan_files(root):
        if plan not in selected:
            plan.unlink()
    qualification = root / QUALIFICATION_PLAN
    if qualification.is_file():
        qualification.write_bytes(_qualification_plan(root))
        written.append(qualification)
    return tuple(written)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('check', 'regenerate'))
    parser.add_argument('--root', type=Path, default=Path('.'))
    args = parser.parse_args(argv)
    try:
        if args.command == 'check':
            check_plans(args.root)
        else:
            regenerate_plans(args.root)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Compiled workflow plan check failed: {error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
