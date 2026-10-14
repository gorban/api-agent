"""Explicit maintenance pilot; successful candidates require manual review/check-in."""
import argparse
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import sys
import stat
from threading import RLock

from api_agent.providers.progress import progress_sink, report_timeout_sync
from api_agent.testing.diagnostics import CredentialScrubber, format_exception, terminal_exception
from api_agent.testing.suite_lock import suite_locked
from .evidence import reserve_calibration, sanitize, store_calibration
from .fixture_driver import fingerprint, provider_identity, run_graph
from .sampling import DECLARED, measured_calibration


def source_checkout(selected=None):
    """A supplied root must identify the source checkout currently executing."""
    source = Path(__file__).resolve()
    root = Path(selected).expanduser().resolve() if selected is not None else source.parents[3]
    if source != root / 'src/api_agent/workflows/calibrate.py' or not (root / 'launch.py').is_file():
        raise ValueError('Calibration requires the running api-agent source checkout; use its Python/PYTHONPATH and --base-root.')
    return root


def configuration(root, argv):
    """Use normal declarations, provider configuration, .env/env/CLI and source gates."""
    from api_agent.config import compose_schema, load_declarations, load_shared_schema, read_dotenv, resolve_config
    from api_agent.config.declarations import ConfigSchema
    from api_agent.config.resolve import retain_runtime_directory, runtime_directory_overrides
    from api_agent.providers.loading import bootstrap_schema
    from api_agent.runtime.bootstrap import _bind_runtime_directory
    from api_agent.runtime.resource_pack import load_launch_context
    from api_agent.runtime.worktrees import verify_worktrees
    context = _bind_runtime_directory(load_launch_context(root), argv, os.environ)
    shared = load_shared_schema(root)
    schema = compose_schema(shared, load_declarations(context.config_path, base_schema=shared, source='pack_default'),
                            overrides=context.defaults)
    # This command selects its operation without enabling unrelated API/UI/Allure duties.
    fields = dict(schema.fields)
    fields['TUI_MODE'] = replace(fields['TUI_MODE'], enum=(*fields['TUI_MODE'].enum, 'workflow-calibration'))
    authentication = fields['UNATTENDED_AUTH']
    fields['UNATTENDED_AUTH'] = replace(authentication, applies_when=dict(authentication.applies_when,
        TUI_MODE=[*authentication.applies_when['TUI_MODE'], 'workflow-calibration']))
    schema = ConfigSchema(fields)
    dotenv = {}
    for path in dict.fromkeys((context.base_root / '.env',
                              *context.shared_dotenv_paths,
                              (context.launcher_root or context.base_root) / '.env',
                              context.dotenv_path)):
        dotenv.update(read_dotenv(path))
    schema = bootstrap_schema(schema, argv, os.environ, dotenv, owner_root=context.owner_root,
                              configuration_paths=context.provider_config_paths)
    overrides = dict(runtime_directory_overrides(context), TUI_MODE='workflow-calibration')
    result = retain_runtime_directory(resolve_config(schema, argv=argv, environ=os.environ, dotenv=dotenv,
        anchors=context.anchors, pack_launcher=context.external_pack, overrides=overrides), context)
    errors = list(result.errors)
    if result.values.get('COLLECT_ONLY'):
        errors.append('Calibration is an explicit maintenance execution; COLLECT_ONLY is not applicable.')
    if errors or result.positionals:
        error = ValueError('\n'.join(errors) or 'Calibration does not accept test selectors.')
        error.calibration_config = result.values
        raise error
    verify_worktrees([context.base_root, context.pack_root, context.owner_root],
                     stdin=None if result.values.get('NO_STD_IN') else sys.stdin, stdout=sys.stdout)
    return context, result


def _attach_cleanup(primary, receipt):
    """Append a distinct cleanup duty without discarding existing owned receipts."""
    cursor, seen = primary, set()
    while True:
        if cursor is receipt:
            return
        seen.add(id(cursor))
        following = getattr(cursor, 'cleanup_pending', None)
        if following is None or id(following) in seen:
            cursor.cleanup_pending = receipt
            return
        cursor = following


def _provider_cleanup_receipt(provider, error, inherited=()):
    """Keep child disposal and the outer adapter's completion as separate duties."""
    from api_agent.providers.control import CleanupPendingError
    from api_agent.runtime.tasks import offload
    from api_agent.testing.suite_lock import _cleanup_receipts
    children = tuple(dict.fromkeys((*inherited, *_cleanup_receipts(error))))
    async def retry_children():
        for child in children:
            await child.retry()
    def retry_on_owner_loop(loop):
        if loop.is_closed() or loop.is_running():
            raise RuntimeError('Calibration provider cleanup requires its open, idle owning loop; cleanup remains pending.')
        loop.run_until_complete(retry_children())
    async def retry():
        try:
            loop = getattr(provider, 'loop', None)
            if loop is None:
                await retry_children()
            else:
                await offload(retry_on_owner_loop, loop)
            await offload(provider.close)
        except BaseException as following:
            raise _provider_cleanup_receipt(provider, following, children) from following
    receipt = CleanupPendingError((provider,), retry)
    for child in children:
        _attach_cleanup(receipt, child)
    return receipt


def _cleanup(provider, primary):
    """Retain all child duties and retry completion of the standard provider."""
    try:
        provider.close()
    except BaseException as error:
        receipt = _provider_cleanup_receipt(provider, error)
        if primary is not None:
            _attach_cleanup(primary, receipt)
            raise primary from error
        if not isinstance(error, Exception):
            _attach_cleanup(error, receipt)
            raise
        raise receipt from error


def candidate_path(destination):
    """Reject linked destinations and sidecars before canonicalizing any path."""
    path = Path(destination).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    paths = [path, *path.parents, *(path.with_name(path.name + suffix)
        for suffix in ('.receipt-key', '.reservation', '.progress.jsonl'))]
    if any(item.is_symlink() for item in paths):
        raise ValueError('Calibration candidate/parent/sidecar paths must not be symlinks.')
    return path.resolve()


@suite_locked
def pilot(context, result, destination, count, *, output=print):
    """Reserve once, observe every graph/call, clean up, then finalize a candidate."""
    if type(count) is not int or not 1 <= count <= 60:
        raise ValueError('Calibration requires between 1 and 60 graphs.')
    destination = candidate_path(destination)
    progress_path = destination.with_name(destination.name + '.progress.jsonl')
    from api_agent.auth.managers import registry_for_context
    from api_agent.auth.terminal_interaction import TerminalInteraction
    from api_agent.providers.selection import ModelSelectionPolicy
    from api_agent.testing.provider_setup import IntegrationLLM
    from api_agent.providers.setup_transport import _diagnostic
    scrubber = CredentialScrubber(result.values)
    provider = None
    with reserve_calibration(destination):
        descriptor = os.open(progress_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            os.close(descriptor)
            raise ValueError('Calibration progress file must be a regular file owned by this user.')
        with os.fdopen(descriptor, 'w', encoding='utf-8') as progress:
            guard = RLock()
            def emit(row):
                with guard:
                    progress.write(json.dumps(sanitize(row), allow_nan=False) + '\n')
                    progress.flush()
            trial, calls = 0, 0
            def observe(event):
                nonlocal calls
                emit(dict(trial=trial, events=[event]))
                if event['event'] == 'llm':
                    calls += 1
                    output(f'Calibration graph {trial}/{count}: LLM call {calls} completed.')
            def timing(component):
                emit(dict(trial=trial, events=[dict(event='component', component=component.name,
                    scope_id=component.id, parent_id=component.parent_id, timeout=component.timeout,
                    elapsed=component.elapsed, status='running' if component.active else 'finished')]))
            stage = 'Calibration provider setup'
            try:
                with progress_sink(timing), report_timeout_sync('Workflow calibration'):
                    provider_identity(result.values, owner_root=context.owner_root)
                    provider = IntegrationLLM(result.values, context.owner_root, skip_unavailable=False,
                        unattended_index_source=getattr(result.fields.get('UNATTENDED_USER_INDEX'), 'source', None),
                        password_manager_registry=registry_for_context(context),
                        interaction=None if result.values.get('NO_STD_IN') else TerminalInteraction())
                    initial_secrets = set(provider.emissions._secrets)
                    def register_runtime_secrets():
                        for value in provider.emissions._secrets - initial_secrets:
                            scrubber.register_secret(value)
                    try:
                        selection = ModelSelectionPolicy(result.values).select_default(provider.describe())
                        expected = fingerprint(selection, result.values, root=context.base_root, owner_root=context.owner_root)
                        records = []
                        stage = 'Calibration graph execution'
                        for trial in range(1, count + 1):
                            output(f'Calibration graph {trial}/{count} starting.')
                            values, nodes = run_graph(provider, selection, trial, result.values,
                                                     root=context.base_root, owner_root=context.owner_root, observe=observe)
                            records.append(dict(trial=trial, route=values['first_draft']['route'], nodes=sorted(nodes)))
                            emit(dict(records=records))
                    finally:
                        register_runtime_secrets()
                        _cleanup(provider, sys.exc_info()[1])
                    stage = 'Calibration candidate validation'
                    if fingerprint(selection, result.values, root=context.base_root, owner_root=context.owner_root) != expected:
                        raise ValueError('Sampling sources/settings changed during calibration.')
                    report = dict(measured_calibration(records, DECLARED), **expected)
                    candidate_path(destination)
                    store_calibration(destination, report)
                    emit(dict(calibration=report))
                    output(f'Calibration candidate saved: {destination}. Review it before manually updating checked-in model bounds.')
                    return report
            except BaseException as error:
                if provider is not None:
                    for value in provider.emissions._secrets - initial_secrets:
                        scrubber.register_secret(value)
                error.calibration_scrubber = scrubber
                diagnostic = _diagnostic(error, stage).splitlines()
                try:
                    emit(dict(status='failed', events=[dict(event='error', component=stage,
                        kind=diagnostic[0].rsplit(' ', 1)[-1], source=[line.replace('<external code>', 'external code')
                        .replace('<no captured stack>', 'no captured stack') for line in diagnostic[2:]])]))
                except Exception as artifact_error:
                    error.add_note(_diagnostic(artifact_error, 'Calibration progress artifact'))
                raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-root', type=Path)
    parser.add_argument('--candidate', type=Path, required=True, help='New immutable candidate path; never promoted automatically.')
    parser.add_argument('--graphs', type=int, default=20, help='Explicit pilot graph count (1..60); default 20.')
    args, options = parser.parse_known_args(argv)
    scrubber = CredentialScrubber()
    try:
        if not 1 <= args.graphs <= 60:
            raise ValueError('Calibration requires between 1 and 60 graphs.')
        context, result = configuration(source_checkout(args.base_root), options)
        scrubber = CredentialScrubber(result.values)
        pilot(context, result, args.candidate, args.graphs,
              output=lambda text: print(scrubber(text), flush=True))
        return 0
    except BaseException as error:
        if hasattr(error, 'calibration_config'):
            scrubber = CredentialScrubber(error.calibration_config)
        scrubber = getattr(error, 'calibration_scrubber', scrubber)
        print(scrubber(format_exception(error, boundary='Workflow calibration failed')), file=sys.stderr, flush=True)
        from api_agent.runtime.bootstrap import _retains_cleanup
        if _retains_cleanup(error):
            raise terminal_exception(error, scrubber, boundary='Workflow calibration cleanup remains pending') from None
        return 130 if isinstance(error, KeyboardInterrupt) else 1


if __name__ == '__main__':
    raise SystemExit(main())
