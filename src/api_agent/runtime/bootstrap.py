"""CWD-independent launch orchestration for the reviewable foundation CLI."""
from pathlib import Path
import os
import subprocess
import sys


def _extract_option(argv, names):
    """Consume a bootstrap option without changing the remaining argument order."""
    remaining, selected = [], None
    index = 0
    while index < len(argv):
        token = argv[index]
        index += 1
        if token == '--':
            remaining.extend([token, *argv[index:]])
            break
        name, separator, value = token.partition('=')
        if name not in names:
            remaining.append(token)
            continue
        if selected is not None:
            raise ValueError(f'Duplicate option {sorted(names)[0]}')
        if not separator:
            if index == len(argv) or argv[index].startswith('--'):
                raise ValueError(f'{name} requires a directory value')
            value = argv[index]
            index += 1
        if not value.strip() or value.startswith('--'):
            raise ValueError(f'{name} requires a nonempty directory value')
        selected = value
    return remaining, selected


def _extract_view_flow(argv):
    """Select the standalone viewer before unrelated execution configuration."""
    remaining = []
    selected = False
    for token in argv:
        if token == '--view-flow':
            if selected:
                raise ValueError('Duplicate --view-flow')
            selected = True
        elif token.startswith('--view-flow='):
            raise ValueError('--view-flow does not accept a value')
        else:
            remaining.append(token)
    return remaining, selected


def _validate_view_flow_arguments(argv):
    allowed = {'--debug', '--no-std-in', '--help', '-h'}
    entry = None
    for token in argv:
        if token in allowed or token in {'--debug=True', '--debug=False', '--no-std-in=True', '--no-std-in=False'}:
            continue
        if not token.startswith('-'):
            if entry is not None or not token.endswith('.md.html'):
                raise ValueError('--view-flow accepts one optional .md.html flow path')
            entry = Path(token).expanduser().absolute()
            continue
        raise ValueError(f'--view-flow conflicts with {token.partition("=")[0]}')
    return entry


def _bind_runtime_directory(context, argv, environ):
    """Only a pack launcher selects code through API_AGENT_DIR."""
    from dataclasses import replace
    from api_agent.config.dotenv import read_dotenv
    from api_agent.runtime.worktrees import _same_directory
    _, explicit = _extract_option(list(argv), {'--api-agent-dir', '--api_agent_dir'})
    pack_launcher = context.external_pack and not _same_directory(context.launcher_root, context.base_root)
    if pack_launcher:
        saved = read_dotenv(context.pack_root / '.env')
        if explicit is not None:
            raw, source, provided = explicit, 'cli', True
        elif 'API_AGENT_DIR' in environ:
            raw, source, provided = environ['API_AGENT_DIR'], 'environment', True
        elif 'API_AGENT_DIR' in saved:
            raw = saved['API_AGENT_DIR']
            source = '../' + os.path.relpath(context.pack_root / '.env', context.launcher_root.parent)
            provided = True
        else:
            raw, source, provided = '../api-agent', 'shared_default', False
    else:
        raw, source, provided = explicit, 'cli' if explicit is not None else 'source_checkout', explicit is not None
    if raw is not None:
        if not raw.strip():
            raise ValueError('API_AGENT_DIR requires a nonempty directory value')
        selected = Path(raw).expanduser()
        selected = (selected if selected.is_absolute() else context.pack_root / selected).resolve()
        if not _same_directory(selected, context.base_root):
            raise ValueError('API_AGENT_DIR does not match the running checkout. '
                             'Use the resource-pack launcher to select another api-agent checkout.')
    return replace(context, bootstrap_source=source, bootstrap_explicit=provided)


def resource_pack_main(argv=None, *, launcher_root, replace_process=False):
    """Resolve base checkout, then execute its source entry point with this Python."""
    from api_agent.config.dotenv import read_dotenv
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(launcher_root).resolve()
    try:
        remaining, pack_override = _extract_option(argv, {'--resource-pack-dir', '--resource_pack_dir'})
        if pack_override is None:
            pack_override = os.environ.get('RESOURCE_PACK_DIR')
        if pack_override is not None:
            if not pack_override.strip():
                raise ValueError('RESOURCE_PACK_DIR requires a nonempty directory value')
            root = Path(pack_override).expanduser().resolve()
        remaining, explicit = _extract_option(remaining, {'--api-agent-dir', '--api_agent_dir'})
        dotenv = read_dotenv(root / '.env')
        raw = explicit if explicit is not None else os.environ.get(
            'API_AGENT_DIR', dotenv.get('API_AGENT_DIR', '../api-agent'))
        if not raw.strip():
            raise ValueError('API_AGENT_DIR requires a nonempty directory value')
        base = Path(raw).expanduser()
        base = (base if base.is_absolute() else root / base).resolve()
        entry = base / 'launch.sh'
        if not entry.is_file() or not (base / 'src/api_agent/runtime/bootstrap.py').is_file():
            raise ValueError(f'Invalid api-agent checkout {base}: missing shared source entry point. '
                             'Set API_AGENT_DIR or --api-agent-dir to a api-agent checkout.')
        # Preserve the original CLI spelling/value for declarative provenance. The
        # base config parser resolves env and .env itself for non-CLI selections.
        if explicit is not None:
            boundary = remaining.index('--') if '--' in remaining else len(remaining)
            remaining.insert(boundary, f'--api-agent-dir={explicit}')
        command = ['/bin/sh', str(entry), '--resource-pack-dir', str(root), '--_launcher-root', str(Path(launcher_root).resolve()), *remaining]
        if replace_process:
            os.execve(command[0], command, os.environ.copy())
        return subprocess.call(command, env=os.environ.copy())
    except (ValueError, OSError) as exc:
        print(f'Launch error: {exc}', file=sys.stderr)
        return 2


def _retains_cleanup(error):
    from api_agent.providers.control import CleanupPendingError
    if isinstance(error, CleanupPendingError):
        return True
    group = getattr(error, 'inspection_group', None)
    if group is not None and group.active:
        return True
    if getattr(error, 'cleanup_pending', None) is not None:
        return True
    recording = getattr(error, 'recording_prerequisites', None)
    return recording is not None and recording.pending_cleanup is not None


def _prepare_workflow_discovery(context, argv, **handoff_options):
    """Prepare core parser packages before graph-aware configuration on a cold launch."""
    from importlib import import_module
    if (getattr(context, 'tool_catalog_path', None) is None
            and getattr(context, 'workflow_entry', None) is None
            and not (context.base_root / 'flow.md.html').is_file()):
        return None
    try:
        import_module('api_agent.workflows.definition')
    except ModuleNotFoundError:
        from api_agent.runtime.dependencies import ensure_environment
        status = ensure_environment(context, argv, groups=(), **handoff_options)
        if status is not None:
            return status
        import_module('api_agent.workflows.definition')
    return None


def main(argv=None, *, base_root=None, resource_pack_dir=None, launcher_root=None, _worktrees_verified=False, _replace_process=False):
    """Validate foundation contracts; later segments provide actual operations."""
    from api_agent.config import (compose_schema, load_declarations, load_shared_schema,
                                      read_dotenv, resolve_config)
    from api_agent.config.resolve import runtime_directory_overrides, retain_runtime_directory
    from api_agent.runtime.resource_pack import load_launch_context
    from api_agent.runtime.worktrees import verify_worktrees, WorktreeGateError
    argv = list(sys.argv[1:] if argv is None else argv)
    handoff_options = {'replace_process': True} if _replace_process else {}
    try:
        argv, view_flow_requested = _extract_view_flow(argv)
        argv, origin = _extract_option(argv, {'--_launcher-root'})
        if origin is not None:
            launcher_root = Path(origin).expanduser().resolve()
        argv, selected_pack = _extract_option(argv, {'--resource-pack-dir', '--resource_pack_dir'})
        if selected_pack is None and resource_pack_dir is None:
            selected_pack = os.environ.get('RESOURCE_PACK_DIR')
        if selected_pack is not None:
            if not selected_pack.strip():
                raise ValueError('RESOURCE_PACK_DIR requires a nonempty directory value')
            resource_pack_dir = Path(selected_pack).expanduser().resolve()
        if base_root is None:
            origin = Path(__file__).resolve()
            candidate = origin.parents[3]
            if origin != candidate / 'src/api_agent/runtime/bootstrap.py' or not (candidate / 'launch.sh').is_file():
                raise ValueError('Installed api-agent has no verified source checkout. Run your checkout/launch.sh '
                                 'with mise, or use a resource-pack launcher with API_AGENT_DIR.')
            base_root = candidate
        context = load_launch_context(Path(base_root).resolve(), resource_pack_dir, launcher_root)
        context = _bind_runtime_directory(context, argv, os.environ)
        from api_agent.runtime.hook_assurance import assure_hook
        assure_hook(context.base_root)
        if not _worktrees_verified:
            verify_worktrees([context.base_root, context.pack_root, context.owner_root], stdin=sys.stdin, stdout=sys.stdout,
                             resource_pack_name=context.pack_id if context.external_pack else None)
        if view_flow_requested:
            view_arguments, _ = _extract_option(argv, {'--api-agent-dir', '--api_agent_dir'})
            view_entry = _validate_view_flow_arguments(view_arguments)
            if '--help' in view_arguments or '-h' in view_arguments:
                print('Usage: ./launch.sh --view-flow [FLOW.md.html] [--resource-pack-dir PATH] [--debug] [--no-std-in]\n'
                      'Open the selected workflow in a managed headed browser without app or provider startup.\n'
                      'Omit the flow path for the selected application; relative paths use the current directory.\n'
                      'Requires mise; browser prerequisites are prepared only when viewing.')
                return 0
            from api_agent.runtime.dependencies import ensure_environment
            status = ensure_environment(context, ['--view-flow', *argv], groups=('browser',), **handoff_options)
            if status is not None:
                return status
            from api_agent.runtime.view_flow import run_view_flow
            return run_view_flow(context) if view_entry is None else run_view_flow(context, entry=view_entry)
        shared = load_shared_schema(context.base_root)
        schema = compose_schema(shared, load_declarations(context.config_path, base_schema=shared, source='pack_default'),
                                overrides=context.defaults)
        dotenv = {}
        for path in dict.fromkeys((context.base_root / '.env',
                                  *context.shared_dotenv_paths,
                                  (context.launcher_root or context.base_root) / '.env',
                                  context.dotenv_path)):
            dotenv.update(read_dotenv(path))
        foundation_schema = schema
        from api_agent.providers.loading import bootstrap_schema
        schema = bootstrap_schema(schema, argv, os.environ, dotenv,
                                  owner_root=context.owner_root,
                                  configuration_paths=context.provider_config_paths)
        result = resolve_config(schema, argv=argv, environ=os.environ,
                                dotenv=dotenv, anchors=context.anchors,
                                interactive=True, pack_launcher=context.external_pack,
                                overrides=runtime_directory_overrides(context))
        status = _prepare_workflow_discovery(context, argv, **handoff_options)
        if status is not None:
            return status
        from api_agent.config.tool_resolution import compose_operation_tools, bind_operation_tools
        from api_agent.config.options import parse_options
        from api_agent.tooling.models import OperationSelection
        preliminary = result.values
        selected_tools = compose_operation_tools(context, schema,
            selection=OperationSelection('collection') if preliminary.get('TUI_MODE') in {'unit', 'integration', 'e2e'} else None,
            argv=argv, environ=os.environ, dotenv=dotenv,
            overrides=runtime_directory_overrides(context))
        if selected_tools is not None:
            schema = selected_tools.schema
            result = resolve_config(schema, argv=argv, environ=os.environ,
                dotenv=dotenv, anchors=context.anchors, interactive=True,
                pack_launcher=context.external_pack, overrides=runtime_directory_overrides(context))
            result = bind_operation_tools(selected_tools, result, argv=argv)
        result = retain_runtime_directory(result, context)
        errors = list(result.errors)
        from api_agent.runtime.starting import start_configuration_errors, automatically_started
        errors.extend(start_configuration_errors(result))
        if result.values.get('NO_STD_IN') and not result.help_requested:
            errors.extend(f'{name}: required; provide {name} or --{name.lower().replace("_", "-")}'
                          for name in result.missing)
        if not result.help_requested:
            mode = result.values.get('TUI_MODE')
            mode_field = result.fields.get('TUI_MODE')
            if result.values.get('COLLECT_ONLY') and mode_field and mode_field.explicit and not result.values.get('NO_STD_IN'):
                errors.extend(f'{name}: required; provide {name} or --{name.lower().replace("_", "-")}'
                              for name in result.missing)
            if (result.values.get('NO_STD_IN') and mode in {'integration', 'e2e'}
                    and not result.values.get('COLLECT_ONLY')
                    and not automatically_started(result)):
                if mode == 'e2e':
                    errors.append('NO_STD_IN=True with TUI_MODE=e2e requires explicit START=E2E. '
                                  'Use --start=E2E to accept execution, or --no-std-in=False for interactive acceptance; '
                                  'collection-only is still allowed via --collect-only.')
                else:
                    errors.append('NO_STD_IN=True with TUI_MODE=integration requires START=True/E2E or DEBUG=True. '
                                  'Use --start to execute, --debug for browser investigation, or --no-std-in=False for interactive selection; '
                                  'collection-only is still allowed via --collect-only.')
            if result.values.get('NO_STD_IN') and mode in {'config-cleanup', 'cleanup-containers', 'unattended-auth'}:
                errors.append(f'{mode} requires the interactive TUI; NO_STD_IN=True is incompatible.')
            if result.values.get('COLLECT_ONLY') and mode is not None and mode not in {'unit', 'integration', 'e2e'}:
                errors.append('COLLECT_ONLY requires unit, integration, or e2e mode.')
            if result.positionals and (mode not in {'unit', 'integration', 'e2e'} or not mode_field or not mode_field.explicit):
                errors.append('Positional test selectors require an explicit unit, integration, or e2e TUI_MODE from .env, environment, or CLI.')
        if errors:
            for error in errors:
                print(f'Configuration error: {error}', file=sys.stderr)
            return 2
        if result.help_requested:
            print(generated_help(schema, pack_launcher=context.external_pack,
                                 color=_supports_color(sys.stdout),
                                 declaration_paths=[context.base_root / 'cfg/config.toml',
                                                    context.config_path, context.pack_root / 'pack.toml']))
            if context.show_pack_bootstrap:
                print('\n' + _colored('Bootstrap: --resource-pack-dir PATH or RESOURCE_PACK_DIR selects a resource pack.',
                                      '31', _supports_color(sys.stdout)))
            return 0
        mode = result.values.get('TUI_MODE')
        mode_field = result.fields.get('TUI_MODE')
        configured_collection = result.values.get('COLLECT_ONLY') and mode_field and mode_field.explicit
        if configured_collection:
            from api_agent.runtime.dependencies import ensure_environment
            status = ensure_environment(context, argv, collect=True, **handoff_options)
            if status is not None:
                return status
            from api_agent.testing.collection import collect_tests
            for test in collect_tests(context, mode, result.positionals, config=result.values):
                print(test.id)
            return 0
        if result.values.get('NO_STD_IN') and mode == 'unit':
            from api_agent.runtime.dependencies import ensure_environment
            status = ensure_environment(context, argv, collect=True, groups=('dev',), **handoff_options)
            if status is not None:
                return status
            from api_agent.testing.collection import collect_tests
            from api_agent.testing.runner import run_unit_tests, format_unit_summary, unit_parallel_jobs
            tests = collect_tests(context, 'unit', result.positionals)
            selected_tools = compose_operation_tools(context, schema,
                selection=OperationSelection('unit', tuple(test.id for test in tests)),
                argv=argv, environ=os.environ, dotenv=dotenv,
                overrides=runtime_directory_overrides(context))
            if selected_tools is not None:
                schema = selected_tools.schema
                result = resolve_config(schema, argv=argv, environ=os.environ, dotenv=dotenv,
                    anchors=context.anchors, interactive=True, pack_launcher=context.external_pack,
                    overrides=runtime_directory_overrides(context))
                result = retain_runtime_directory(bind_operation_tools(selected_tools, result, argv=argv), context)
                result.raise_for_errors()
                if result.missing:
                    raise ValueError('Missing required configuration: ' + ', '.join(result.missing))
            execution = run_unit_tests(context, tests, output=lambda chunk: print(chunk, end='', flush=True),
                                       parallel_jobs=unit_parallel_jobs(result.values),
                                       coverage_full=result.values.get('UNIT_COVERAGE_FULL', False),
                                       require_full_coverage=not result.positionals)
            print()
            print(format_unit_summary(execution))
            return execution.exit_code
        if result.values.get('NO_STD_IN') and mode == 'integration' and automatically_started(result):
            from api_agent.testing.diagnostics import CredentialScrubber, format_exception, terminal_exception
            from api_agent.auth.terminal_output import TerminalOutput
            scrubber = CredentialScrubber(result.values)
            presentation = TerminalOutput()

            def diagnostic_output(text):
                presentation.write(scrubber(text), error=True)

            pending_output = ''

            def runner_output(chunk):
                nonlocal pending_output
                pending_output += chunk
                lines = pending_output.split('\n')
                pending_output = lines.pop()
                for line in lines:
                    presentation.write(scrubber(line))

            try:
                from api_agent.runtime.dependencies import ensure_environment
                status = ensure_environment(context, argv, collect=True, groups=('dev',), **handoff_options)
                if status is not None:
                    return status
                from api_agent.testing.collection import collect_tests
                from api_agent.testing.launched import run_launched_integration
                from api_agent.testing.terminal_progress import TerminalTestProgress
                from api_agent.telemetry.privacy import ContentPolicy
                tests = collect_tests(context, 'integration', result.positionals, config=result.values)
                from api_agent.testing.requirements import plan_integration_requirements
                tool_intents = ('ui_session',) if plan_integration_requirements(tests).requires_ui_session else ()
                selected_tools = compose_operation_tools(context, schema, selection=tests,
                    argv=argv, environ=os.environ, dotenv=dotenv,
                    overrides=runtime_directory_overrides(context), operation_intents=tool_intents)
                if selected_tools is not None:
                    schema = selected_tools.schema
                    result = resolve_config(schema, argv=argv, environ=os.environ,
                        dotenv=dotenv, anchors=context.anchors, interactive=True,
                        pack_launcher=context.external_pack, overrides=runtime_directory_overrides(context),
                        operation_intents=tool_intents)
                    result = retain_runtime_directory(bind_operation_tools(selected_tools, result, argv=argv), context)
                    result.raise_for_errors()
                    if result.missing:
                        raise ValueError('Missing required configuration: ' + ', '.join(result.missing))
                from api_agent.testing.debug_confirmation import debug_warning, confirm_terminal_debug
                warning = debug_warning('integration', result.values.get('DEBUG'), tests)
                if not confirm_terminal_debug(warning):
                    print('DEBUG multi-test confirmation declined or unavailable. No tests or resources were started.', file=sys.stderr)
                    return 3
                from api_agent.testing.requirements import plan_integration_requirements
                if plan_integration_requirements(tests).requires_ui_session:
                    result = resolve_config(schema, argv=argv, environ=os.environ,
                        dotenv=dotenv, anchors=context.anchors, interactive=True,
                        pack_launcher=context.external_pack, overrides=runtime_directory_overrides(context),
                        operation_intents=('ui_session',))
                    if selected_tools is not None:
                        result = bind_operation_tools(selected_tools, result, argv=argv)
                    result = retain_runtime_directory(result, context).raise_for_errors()
                    if result.missing:
                        raise ValueError('Missing required configuration: ' + ', '.join(result.missing))
                print('Integration debug: shared authentication; provider login may require browser interaction.'
                      if result.values.get('DEBUG') else 'Integration tests: provider login may require browser interaction.', flush=True)
                from api_agent.auth.terminal_interaction import TerminalInteraction
                try:
                    execution = run_launched_integration(context, tests, result.values, accepted=True,
                        config_fields=result.fields,
                        content_policy=ContentPolicy.from_config(result),
                        **({'tool_plan': result.tool_plan} if result.tool_plan is not None else {}),
                        interaction=TerminalInteraction(presentation=presentation),
                        usage_output=lambda line: presentation.write(scrubber(line)) if '\n' not in line else None,
                        unattended_index_source=getattr(result.fields.get('UNATTENDED_USER_INDEX'), 'source', None),
                        on_event=TerminalTestProgress(tests, presentation.write, scrub=scrubber),
                        output=runner_output)
                    if getattr(execution, 'reporting_skipped', False):
                        print('Allure reporting: SKIPPED by configuration', flush=True)
                finally:
                    if pending_output:
                        presentation.write(scrubber(pending_output), end='')
                    presentation.close()
                from api_agent.testing.inspection_controls import finish_terminal_inspection
                if not finish_terminal_inspection(execution, lambda line: print(scrubber(line), flush=True),
                                                  diagnostic_output=diagnostic_output):
                    return 3
                return execution.exit_code
            except BaseException as error:
                diagnostic_output(format_exception(error, boundary='Noninteractive integration execution'))
                if _retains_cleanup(error):
                    raise terminal_exception(error, scrubber,
                        boundary='Noninteractive integration cleanup remains pending') from None
                if isinstance(error, KeyboardInterrupt):
                    return 130
                if isinstance(error, (ValueError, OSError, WorktreeGateError, subprocess.CalledProcessError)):
                    return 2
                if isinstance(error, (SystemExit, GeneratorExit)):
                    raise terminal_exception(error, scrubber) from None
                return 1
            finally:
                presentation.close()
        if result.values.get('NO_STD_IN') and mode == 'run-local':
            from api_agent.auth.extensions import validate_operation
            try:
                validate_operation(context, result.values)
            except ValueError as error:
                print(str(error), file=sys.stderr)
                return 3
            from api_agent.runtime.dependencies import ensure_environment
            status = ensure_environment(context, argv, groups=('browser',), **handoff_options)
            if status is not None:
                return status
            import asyncio
            from api_agent.runtime.local_terminal import run_local_terminal
            return asyncio.run(run_local_terminal(context, lambda: result, argv=argv, schema=schema,
                                                 retain_terminal_cleanup=True))
        if result.values.get('NO_STD_IN'):
            if mode in {'run-local', 'e2e'}:
                from api_agent.auth.extensions import validate_operation
                try:
                    validate_operation(context, result.values)
                except ValueError as error:
                    print(str(error), file=sys.stderr)
                    return 3
            print('Requested operation is not implemented yet. No tests or services were started.', file=sys.stderr)
            return 3
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError('Interactive modes require a terminal. Use --help or a configured test mode with --collect-only for noninteractive output.')
        from api_agent.runtime.dependencies import ensure_environment
        status = ensure_environment(context, argv, collect=True, groups=('dev',), **handoff_options)
        if status is not None:
            return status
        from api_agent.tui.app import run_tui
        return run_tui(context, foundation_schema, argv, os.environ.copy())
    except KeyboardInterrupt as error:
        if _retains_cleanup(error):
            raise
        print('Run interrupted.', file=sys.stderr)
        return 130
    except (ValueError, OSError, WorktreeGateError, subprocess.CalledProcessError) as exc:
        if _retains_cleanup(exc):
            raise
        print(f'Launch error: {exc}', file=sys.stderr)
        return 2


def cli_main(argv=None):
    """Run the executable entry point with one interpreter/process owner."""
    return main(argv, _replace_process=True)


def _supports_color(stream):
    """Use ANSI colors only on an advertised terminal, respecting NO_COLOR."""
    return (stream.isatty() and os.environ.get('TERM', 'dumb') != 'dumb'
            and 'NO_COLOR' not in os.environ)


def _colored(text, code, enabled):
    return f'\033[{code}m{text}\033[0m' if enabled else text


def generated_help(schema, *, pack_launcher=False, declaration_paths=None, color=False):
    """Show CLI behavior and deliberate callouts; declarations own full config docs."""
    lines = ['Usage: ./launch.sh [OPTIONS] [TEST SELECTORS]', '', 'CLI options and selected configuration switches:']
    lines.append('  --view-flow [FLOW.md.html]: open a workflow (default: selected application) without starting the app or providers.')
    callouts = {
        'HELP': '-h, --help: print help without execution configuration validation.',
        'NO_STD_IN': '--no-std-in: use terminal execution without menus; owned authentication may still prompt. Worktree and multi-test DEBUG safety confirmations also remain.',
        'COLLECT_ONLY': '--collect-only: list selected tests without running them.',
        'START': '--start: start the selected TUI operation after normal startup checks; --start=E2E explicitly accepts E2E execution without its human-review screen.',
        'TUI_MODE': '--tui-mode MODE: select an operation; test modes accept positional selectors.',
        'RUN_UI': '--run-ui: enable the optional Run local frontend; test browsers stay headless unless DEBUG (or E2E_DEBUG for E2E) is enabled.',
        'E2E_DEBUG': '--e2e-debug: retain headed test browsers; requires a subset of E2E tests.',
        'DEBUG': '--debug: retain headed browser inspection; does not enable the optional Run local frontend.',
        'SKIP_ALLURE_REPORTING': '--skip-allure-reporting: disable Allure for this test run and permit uncommitted workflow inputs.',
        'UNIT_COVERAGE_FULL': '--unit-coverage-full: show every unit coverage row; JSON/HTML reports are retained either way.',
        'UNATTENDED_USER_INDEX': '--unattended-user-index INDEX: select a zero-based account; --unattended-user-index= uses manual login for this run.',
    }
    for name, description in callouts.items():
        if name in schema.fields:
            lines.append('  ' + description)
    lines.extend(['', 'Start, debug and credential-index settings are hidden from Review configuration.',
                  'All declared configuration values also accept CLI options; see the executable declarations:'])
    from api_agent.config.declarations import shared_declaration_path
    for path in declaration_paths or [shared_declaration_path()]:
        path = Path(path).resolve()
        lines.append('  ' + _colored(os.path.relpath(path, Path.cwd()), '34', color))
    lines.extend(['', 'With space-separated values, --field-name --VALUE=... treats --VALUE as the next option.',
                  'For a boolean field, this means --field-name=True --VALUE=...;',
                  'a nonboolean field instead reports a missing value. Use --field-name=VALUE for a literal value.',
                  '',
                  'Configuration override precedence:',
                  '  shared defaults → resource-pack defaults → .env → environment variables → finally CLI arguments override everything',
                  '',
                  'Modes: run-local starts local services/UI; unit runs base plus pack units;',
                  'integration and e2e select pack suites; allure opens reports; deployment-logs shows deployment logs;',
                  'config-cleanup reviews persisted options; cleanup-containers stops selected running containers;',
                  'unattended-auth configures credential profiles.',
                  ])
    return '\n'.join(lines)
