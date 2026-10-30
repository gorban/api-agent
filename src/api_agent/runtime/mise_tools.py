"""Prepare selected launcher-managed executables on demand."""
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
}


def ensure_tools(*names, timeout=300, cancellation=None):
    """Install only named tools when the source launcher opted into mise."""
    if os.environ.get('API_AGENT_MISE') != '1':
        return
    names = tuple(dict.fromkeys(names))
    if not names or any(name not in TOOLS for name in names):
        raise ValueError('Unknown managed executable')
    mise = shutil.which('mise')
    if mise is None or not (ROOT / 'mise.toml').is_file():
        raise OSError('Managed executable preparation requires the project mise configuration')
    try:
        config = tomllib.loads((ROOT / 'mise.toml').read_text(encoding='utf-8'))['tools']
        versions = {}
        for name in names:
            value = config[TOOLS[name]]
            version = value['version'] if isinstance(value, dict) else value
            if not isinstance(version, str) or not version or '@' in version or any(c.isspace() for c in version):
                raise ValueError('invalid version')
            versions[name] = f'{TOOLS[name]}@{version}'
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
                        process.wait(timeout=min(.1, budget.remaining or .1))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if process.returncode:
                    raise OSError('Managed executable preparation failed; check mise and retry')
                output.seek(0)
                return output.read(4097)
            finally:
                from api_agent.testing.processes import finalize
                finalize(process)

    with report_timeout_sync('Managed executable preparation', timeout) as budget:
        run(['install', *(versions[name] for name in names)], budget)
        paths = []
        java_home = None
        for name in names:
            raw = run(['which', '--tool', versions[name], name], budget)
            try:
                value = raw.decode('utf-8').strip()
                executable = Path(value)
                if len(raw) > 4096 or '\n' in value or not executable.is_absolute() or not executable.is_file():
                    raise ValueError
            except (UnicodeError, ValueError, OSError):
                raise OSError('Managed executable path is invalid; check mise and retry') from None
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
