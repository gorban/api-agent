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


def resource_pack_main(argv=None, *, launcher_root):
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
        entry = base / 'launch.py'
        if not entry.is_file() or not (base / 'src/api_agent/runtime/bootstrap.py').is_file():
            raise ValueError(f'Invalid api-agent checkout {base}: missing shared source entry point. '
                             'Set API_AGENT_DIR or --api-agent-dir to a api-agent checkout.')
        # Preserve the original CLI spelling/value for declarative provenance. The
        # base config parser resolves env and .env itself for non-CLI selections.
        if explicit is not None:
            boundary = remaining.index('--') if '--' in remaining else len(remaining)
            remaining.insert(boundary, f'--api-agent-dir={explicit}')
        return subprocess.call([sys.executable, str(entry), '--resource-pack-dir', str(root), '--_launcher-root', str(Path(launcher_root).resolve()), *remaining],
                               env=os.environ.copy())
    except (ValueError, OSError) as exc:
        print(f'Launch error: {exc}', file=sys.stderr)
        return 2


def _retains_cleanup(error):
    group = getattr(error, 'inspection_group', None)
    if group is not None and group.active:
        return True
    if getattr(error, 'cleanup_pending', None) is not None:
        return True
    recording = getattr(error, 'recording_prerequisites', None)
    return recording is not None and recording.pending_cleanup is not None


def main(argv=None, *, base_root=None, resource_pack_dir=None, launcher_root=None, _worktrees_verified=False):
    """Validate foundation contracts; later segments provide actual operations."""
    from api_agent.config import (compose_schema, load_declarations, load_shared_schema,
                                      read_dotenv, resolve_config)
    from api_agent.config.resolve import runtime_directory_overrides, retain_runtime_directory
    from api_agent.runtime.resource_pack import load_launch_context
    from api_agent.runtime.worktrees import verify_worktrees, WorktreeGateError
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
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
            if origin != candidate / 'src/api_agent/runtime/bootstrap.py' or not (candidate / 'launch.py').is_file():
                raise ValueError('Installed api-agent has no verified source checkout. Run your checkout/launch.py '
                                 'with the current Python, or use a resource-pack launcher with API_AGENT_DIR.')
            base_root = candidate
        context = load_launch_context(Path(base_root).resolve(), resource_pack_dir, launcher_root)
        context = _bind_runtime_directory(context, argv, os.environ)
        if not _worktrees_verified:
            verify_worktrees([context.base_root, context.pack_root, context.owner_root], stdin=sys.stdin, stdout=sys.stdout,
                             resource_pack_name=context.pack_id if context.external_pack else None)
        shared = load_shared_schema(context.base_root)
        schema = compose_schema(shared, load_declarations(context.config_path, base_schema=shared, source='pack_default'),
                                overrides=context.defaults)
        dotenv = {}
        for path in dict.fromkeys((context.base_root / '.env',
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
                    errors.append('NO_STD_IN=True with TUI_MODE=integration requires START=True/E2E. '
                                  'Use --start to execute, or --no-std-in=False for interactive selection; '
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
            status = ensure_environment(context, argv, collect=True)
            if status is not None:
                return status
            from api_agent.testing.collection import collect_tests
            for test in collect_tests(context, mode, result.positionals, config=result.values):
                print(test.id)
            return 0
        if result.values.get('NO_STD_IN') and mode == 'unit':
            from api_agent.runtime.dependencies import ensure_environment
            status = ensure_environment(context, argv, collect=True, groups=('dev',))
            if status is not None:
                return status
            from api_agent.testing.collection import collect_tests
            from api_agent.testing.runner import run_unit_tests, format_unit_summary
            tests = collect_tests(context, 'unit', result.positionals)
            execution = run_unit_tests(context, tests, output=lambda chunk: print(chunk, end='', flush=True),
                                       parallel_jobs=result.values.get('TEST_PARALLEL_JOBS', 8),
                                       require_full_coverage=not result.positionals)
            print()
            print(format_unit_summary(execution))
            return execution.exit_code
        if result.values.get('NO_STD_IN') and mode == 'integration' and automatically_started(result):
            from api_agent.runtime.dependencies import ensure_environment
            status = ensure_environment(context, argv, collect=True, groups=('dev',))
            if status is not None:
                return status
            from api_agent.testing.collection import collect_tests
            from api_agent.testing.launched import run_launched_integration
            tests = collect_tests(context, 'integration', result.positionals, config=result.values)
            print('Integration debug: serial execution; provider login may require browser interaction.'
                  if result.values.get('DEBUG') else 'Integration tests: provider login may require browser interaction.', flush=True)
            from api_agent.auth.terminal_interaction import TerminalInteraction
            execution = run_launched_integration(context, tests, result.values, accepted=True,
                interaction=TerminalInteraction(),
                unattended_index_source=getattr(result.fields.get('UNATTENDED_USER_INDEX'), 'source', None),
                output=lambda chunk: print(chunk, end='', flush=True))
            from api_agent.testing.inspection_controls import finish_terminal_inspection
            if not finish_terminal_inspection(execution, lambda line: print(line, flush=True)):
                return 3
            return execution.exit_code
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
        status = ensure_environment(context, argv, collect=True, groups=('dev',))
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


def _supports_color(stream):
    """Use ANSI colors only on an advertised terminal, respecting NO_COLOR."""
    return (stream.isatty() and os.environ.get('TERM', 'dumb') != 'dumb'
            and 'NO_COLOR' not in os.environ)


def _colored(text, code, enabled):
    return f'\033[{code}m{text}\033[0m' if enabled else text


def generated_help(schema, *, pack_launcher=False, declaration_paths=None, color=False):
    """Show CLI behavior and deliberate callouts; declarations own full config docs."""
    lines = ['Usage: ./launch.py [OPTIONS] [TEST SELECTORS]', '', 'CLI options and selected configuration switches:']
    callouts = {
        'HELP': '-h, --help: print help without execution configuration validation.',
        'NO_STD_IN': '--no-std-in: disable interactive input after the worktree gate.',
        'COLLECT_ONLY': '--collect-only: list selected tests without running them.',
        'START': '--start: start the selected TUI operation after normal startup checks; --start=E2E explicitly accepts E2E execution without its human-review screen.',
        'TUI_MODE': '--tui-mode MODE: select an operation; test modes accept positional selectors.',
        'E2E_DEBUG': '--e2e-debug: retain headed test browsers; requires a subset of E2E tests.',
        'DEBUG': '--debug: retain integration login browsers; allows serial --no-std-in integration debugging.',
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
