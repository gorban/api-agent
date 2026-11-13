"""Prepare selected launcher-managed executables and files on demand."""
from api_agent.runtime.processes import wait_owned
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib

from api_agent.providers.progress import report_timeout_sync


ROOT = Path(__file__).resolve().parents[3]
TOOLS = {
    'node': 'core:node',
    'npm': 'aqua:npm/cli',
    'java': 'core:java',
    'allure': 'github:allure-framework/allure2',
    'mkcert': 'aqua:FiloSottile/mkcert',
    'openssl': 'conda:openssl',
    'plantuml': 'github:plantuml/plantuml',
}
# These install one release file, named by the tool's literal mise asset_pattern.
FILE_TOOLS = frozenset({'plantuml'})
# Background JVMs must not open a desktop app window or take focus (macOS Dock).
HEADLESS_JAVA_OPTION = '-Djava.awt.headless=true'
# The isolated tool state ./launch.sh exports, relative to <root>/.api-agent/bootstrap.
LAUNCHER_STATE = {
    'MISE_CONFIG_DIR': 'mise/config', 'MISE_GLOBAL_CONFIG_FILE': 'mise/config/config.toml',
    'MISE_SYSTEM_CONFIG_DIR': 'mise/system', 'MISE_SYSTEM_CONFIG_FILE': 'mise/system/config.toml',
    'MISE_DATA_DIR': 'mise/data', 'MISE_CACHE_DIR': 'mise/cache', 'MISE_STATE_DIR': 'mise/state',
}


def adopt_launcher_state():
    """Give a direct source-checkout command the tool state ./launch.sh selects."""
    if os.environ.get('API_AGENT_MISE') == '1' or not (ROOT / 'mise.toml').is_file():
        return
    mise = shutil.which('mise')
    if mise is None:
        raise OSError('mise is required to prepare project tools; install it or run through ./launch.sh')
    # Like ./launch.sh, record trust in the caller's mise state before isolating it.
    result = subprocess.run([mise, '--no-env', '--no-hooks', '--quiet', 'trust', str(ROOT / 'mise.toml')],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise OSError('Could not trust the project mise.toml; check mise state-directory permissions')
    state = ROOT / '.api-agent/bootstrap'
    os.environ.update({name: str(state / suffix) for name, suffix in LAUNCHER_STATE.items()})
    os.environ.update(API_AGENT_MISE='1', MISE_CEILING_PATHS=str(ROOT.parent),
                      MISE_OVERRIDE_CONFIG_FILENAMES='mise.toml', MISE_ENV='', MISE_NO_ENV='1', MISE_NO_HOOKS='1')


def ensure_tools(*names, timeout=300, cancellation=None):
    """Install only named tools when the source launcher opted into mise.

    Returns the installed path of each named file tool; without the launcher's
    opt-in nothing is installed and no file is returned.
    """
    if os.environ.get('API_AGENT_MISE') != '1':
        return {}
    names = tuple(dict.fromkeys(names))
    if not names or any(name not in TOOLS for name in names):
        raise ValueError('Unknown managed executable')
    mise = shutil.which('mise')
    if mise is None or not (ROOT / 'mise.toml').is_file():
        raise OSError('Managed executable preparation requires the project mise configuration')
    try:
        config = tomllib.loads((ROOT / 'mise.toml').read_text(encoding='utf-8'))['tools']
        versions = {}
        assets = {}
        for name in names:
            value = config[TOOLS[name]]
            version = value['version'] if isinstance(value, dict) else value
            if not isinstance(version, str) or not version or '@' in version or any(c.isspace() for c in version):
                raise ValueError('invalid version')
            versions[name] = f'{TOOLS[name]}@{version}'
            if name in FILE_TOOLS:
                asset = value['asset_pattern']
                if (not isinstance(asset, str) or asset in ('', '.', '..')
                        or any(c.isspace() or c in '/\\*?[]{}' for c in asset)):
                    raise ValueError('invalid asset')
                assets[name] = asset
    except (OSError, KeyError, TypeError, ValueError):
        raise OSError('Managed executable version is missing or invalid in project mise configuration') from None
    env = os.environ.copy()
    env.update(MISE_CEILING_PATHS=str(ROOT.parent), MISE_OVERRIDE_CONFIG_FILENAMES='mise.toml',
               MISE_ENV='', MISE_NO_ENV='1', MISE_NO_HOOKS='1')

    def run(arguments, budget):
        if cancellation is not None and cancellation.is_set():
            raise InterruptedError('Managed executable preparation cancelled')
        if budget.remaining is not None and budget.remaining <= 0:
            raise TimeoutError('Managed executable preparation timed out')
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.Popen([mise, '--cd', str(ROOT), '--no-env', '--no-hooks', *arguments],
                    cwd=ROOT, env=env, stdout=output, stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError:
                raise OSError('Managed executable preparation could not start') from None
            try:
                while True:
                    if cancellation is not None and cancellation.is_set():
                        raise InterruptedError('Managed executable preparation cancelled')
                    if budget.remaining is not None and budget.remaining <= 0:
                        raise TimeoutError('Managed executable preparation timed out')
                    try:
                        code = wait_owned(process, timeout=min(.1, budget.remaining or .1))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if code:
                    raise OSError('Managed executable preparation failed; check mise and retry')
                output.seek(0)
                return output.read(4097)
            finally:
                from api_agent.runtime.processes import finalize
                finalize(process)

    def resolved(arguments, budget, asset=None):
        raw = run(arguments, budget)
        try:
            value = raw.decode('utf-8').strip()
            path = Path(value)
            if len(raw) > 4096 or '\n' in value or not path.is_absolute():
                raise ValueError
            if asset is not None:
                path = path / asset
            if not path.is_file():
                raise ValueError
        except (UnicodeError, ValueError, OSError):
            raise OSError('Managed executable path is invalid; check mise and retry') from None
        return path

    with report_timeout_sync('Managed executable preparation', timeout) as budget:
        # Select the declarations so mise retains their lockfile source, including
        # transitive packages. An explicit tool@version can lose that source.
        run(['install', *(TOOLS[name] for name in names)], budget)
        paths = []
        files = {}
        java_home = None
        for name in names:
            if name in FILE_TOOLS:
                files[name] = resolved(['where', versions[name]], budget, assets[name])
                continue
            executable = resolved(['which', '--tool', versions[name], name], budget)
            paths.append(str(executable.parent))
            if name == 'java':
                java_home = str(executable.parent.parent)
        if cancellation is not None and cancellation.is_set():
            raise InterruptedError('Managed executable preparation cancelled')
        # Later requested tools take precedence when installations bundle the
        # same executable, such as the npm shipped with Node.
        os.environ['PATH'] = os.pathsep.join((*dict.fromkeys(reversed(paths)), os.environ.get('PATH', '')))
        if java_home is not None:
            os.environ['JAVA_HOME'] = java_home
    return files
