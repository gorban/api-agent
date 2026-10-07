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
        if pack_override is not None:
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
        if selected_pack is not None:
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
        shared = load_shared_schema()
        schema = compose_schema(shared, load_declarations(context.config_path, base_schema=shared, source='pack_default'),
                                overrides=context.defaults)
        result = resolve_config(schema, argv=argv, environ=os.environ,
                                dotenv=read_dotenv(context.dotenv_path), anchors=context.anchors,
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
            print(generated_help(schema, pack_launcher=context.external_pack))
            print('\nBootstrap: --resource-pack-dir PATH selects a resource pack independently of CWD.\n'
                  'Fresh-worktree verification precedes help and every operation.\n'
                  'Foundation only: TUI, test collection/execution, servers, and reports are pending Segment B.')
            return 0
        print(f'Validated foundation configuration for {context.name} ({context.pack_root}).')
        operation = 'Test collection' if result.values.get('COLLECT_ONLY') else 'Requested operation'
        print(f'{operation} is unavailable: pending Segment B. No tests or services were started.',
              file=sys.stderr)
        return 3
    except (ValueError, OSError, WorktreeGateError) as exc:
        print(f'Launch error: {exc}', file=sys.stderr)
        return 2


def generated_help(schema, *, pack_launcher=False):
    """Generate options from exactly the schema used by the resolver."""
    from api_agent.config.declarations import UNSET
    lines = ['Usage: python launch.py [OPTIONS] [TEST SELECTORS]', '', 'Configuration options:']
    for name, field in schema.fields.items():
        if field.pack_only and not pack_launcher:
            continue
        option = '--' + name.lower().replace('_', '-')
        details = [field.type]
        if field.default is not UNSET:
            details.append(f'default={field.default!r} ({field.default_source})')
        if field.enum:
            details.append('choices=' + ','.join(map(str, field.enum)))
        if field.anchor:
            details.append('relative to ' + field.anchor)
        if field.required or field.required_when:
            details.append('required' if field.required else f'required when {dict(field.required_when)}')
        if field.applies_when:
            details.append(f'applies when {dict(field.applies_when)}')
        lines.append(f'  {option} ({name}; ' + '; '.join(details) + ')')
        if field.help:
            lines.append('      ' + field.help)
    lines.extend(['', 'Option forms: --field-name VALUE, --field_name VALUE, and --field-name=VALUE.',
                  'Boolean switches alone mean True; explicit boolean values must be exactly True or False.',
                  '-h aliases --help. Values starting with -- are treated as option keys, even after =.',
                  '-- ends option parsing; relative directory values can use ./ to avoid that prefix.',
                  'Shared defaults < pack defaults < active pack/base .env < environment < CLI.',
                  'Help skips execution requirements; unknown options still fail.',
                  'Modes: run-local starts local services/UI; unit runs base plus pack units;',
                  'integration and e2e select pack suites; allure opens reports; deployment-logs shows deployment logs;',
                  'config-cleanup reviews persisted options; unattended-auth configures credential profiles.',
                  'Positional test selectors require an explicit test mode from .env, environment, or CLI.',
                  'Explicit unit mode can bypass its picker; integration/E2E execution retains an interactive picker.',
                  '--collect-only validates collection requirements and skips execution acceptance.',
                  'NO_STD_IN=True requires TUI_MODE and rejects integration/E2E execution, except collection-only.',
                  'E2E execution requires human acceptance; E2E_RERUNS applies only to E2E; unit/integration never retry.',
                  'Fresh-worktree acceptance uses stdin even with NO_STD_IN=True or --help.',
                  'These operation descriptions are contracts: all execution, collection, and UI operations remain pending Segment B.'])
    return '\n'.join(lines)
