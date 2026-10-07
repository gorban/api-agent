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
        if not separator:
            if index == len(argv) or argv[index].startswith('--'):
                raise ValueError(f'{name} requires a directory value')
            value = argv[index]
            index += 1
        if not value.strip() or value.startswith('--'):
            raise ValueError(f'{name} requires a nonempty directory value')
        selected = value
    return remaining, selected


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
        return subprocess.call([sys.executable, str(entry), '--resource-pack-dir', str(root), *remaining],
                               env=os.environ.copy())
    except (ValueError, OSError) as exc:
        print(f'Launch error: {exc}', file=sys.stderr)
        return 2


def main(argv=None, *, base_root=None, resource_pack_dir=None, launcher_root=None):
    """Validate foundation contracts; later segments provide actual operations."""
    from api_agent.config import (compose_schema, load_declarations, load_shared_schema,
                                      read_dotenv, resolve_config)
    from api_agent.runtime.resource_pack import load_launch_context
    from api_agent.runtime.worktrees import verify_worktrees, WorktreeGateError
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
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
        verify_worktrees([context.base_root, context.pack_root, context.owner_root], stdin=sys.stdin, stdout=sys.stdout)
        shared = load_shared_schema(context.base_root)
        schema = compose_schema(shared, load_declarations(context.config_path, base_schema=shared, source='pack_default'),
                                overrides=context.defaults)
        result = resolve_config(schema, argv=argv, environ=os.environ,
                                dotenv={**read_dotenv(context.base_root / '.env'),
                                        **read_dotenv(context.dotenv_path)}, anchors=context.anchors,
                                interactive=False, pack_launcher=context.external_pack)
        errors = list(result.errors)
        if not result.help_requested:
            mode = result.values.get('TUI_MODE')
            mode_field = result.fields.get('TUI_MODE')
            if result.values.get('NO_STD_IN') and mode in {'integration', 'e2e'} and not result.values.get('COLLECT_ONLY'):
                errors.append('NO_STD_IN=True is incompatible with integration/E2E execution; collection-only is allowed.')
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
            if resource_pack_dir is None and launcher_root is None:
                print('\n' + _colored('Bootstrap: --resource-pack-dir PATH or RESOURCE_PACK_DIR selects a resource pack.',
                                      '31', _supports_color(sys.stdout)))
            return 0
        print(f'Validated foundation configuration for {context.name} ({context.pack_root}).')
        operation = 'Test collection' if result.values.get('COLLECT_ONLY') else 'Requested operation'
        print(f'{operation} is not implemented yet. No tests or services were started.',
              file=sys.stderr)
        return 3
    except (ValueError, OSError, WorktreeGateError) as exc:
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
        'TUI_MODE': '--tui-mode MODE: select an operation; test modes accept positional selectors.',
        'E2E_DEBUG': '--e2e-debug: retain headed test browsers; requires a subset of E2E tests.',
        'UNATTENDED_USER_INDEX': '--unattended-user-index INDEX: select the same zero-based entry in needed credential arrays.',
    }
    for name, description in callouts.items():
        if name in schema.fields:
            lines.append('  ' + description)
    lines.extend(['', 'Debug and credential-index switches skip startup default acknowledgement/persistence.',
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
                  'config-cleanup reviews persisted options; unattended-auth configures credential profiles.',
                  ])
    return '\n'.join(lines)
