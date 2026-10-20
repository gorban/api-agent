"""Optional real headed-browser proof for the standalone workflow viewer."""
import os
import signal
from hashlib import sha256
from pathlib import Path
import select
import shutil
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest


ROOT = Path(__file__).resolve().parents[2]


pytestmark = pytest.mark.feature('Workflow viewer')


@pytest.mark.parametrize('stop', ['page-close', 'ctrl-c'])
def test_view_flow_opens_original_managed_browser_and_cleans_up(stop):
    from playwright.sync_api import sync_playwright

    jar = Path(os.environ.get('API_AGENT_PLANTUML_JAR') or
               Path.home() / '.cache/api-agent/plantuml/plantuml-mit-1.2026.9.jar')
    if not jar.is_file() or not shutil.which('java'):
        pytest.fail('Viewer QA requires the pinned PlantUML JAR and Java; run python -m api_agent.workflows.viewer_build prepare.')
    if sys.platform.startswith('linux') and not os.environ.get('DISPLAY'):
        if not shutil.which('xvfb-run'):
            pytest.fail('Viewer QA on headless Linux requires xvfb-run; install Xvfb or supply DISPLAY.')
        display = ['xvfb-run', '-a']
    else:
        display = []

    script = ('from pathlib import Path; from api_agent.runtime.bootstrap import main; '
              'raise SystemExit(main(["--view-flow"], base_root=Path.cwd(), _worktrees_verified=True))')
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(ROOT / 'src')
    owned = (ROOT / 'flow.md.html', ROOT / '.private_resources/diagrams/flow.svg')
    before = tuple(sha256(path.read_bytes()).hexdigest() for path in owned)
    process = subprocess.Popen([*display, sys.executable, '-c', script], cwd=ROOT,
                               env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    endpoint = url = None
    try:
        deadline = time.monotonic() + 50
        pending = b''
        observed = []
        while time.monotonic() < deadline and (endpoint is None or url is None):
            readable, _, _ = select.select([process.stdout], [], [], 1)
            if readable:
                pending += os.read(process.stdout.fileno(), 4096)
                while b'\n' in pending:
                    raw, pending = pending.split(b'\n', 1)
                    line = raw.decode().strip()
                    observed.append(line)
                    if line.startswith('Workflow browser CDP: '):
                        endpoint = line.partition(': ')[2]
                    if line.startswith('Workflow view: '):
                        url = line.partition(': ')[2]
            if process.poll() is not None:
                break
        assert endpoint and url, f'rc={process.poll()} lines={observed!r} pending={pending!r}'
        with urlopen(url, timeout=5) as response:
            assert response.status == 200
            assert b'Workflow' in response.read()
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(endpoint)
            assert any(page.url == url for context in browser.contexts for page in context.pages)
            if stop == 'page-close':
                # Closing the original page releases the owned browser and host.
                for context in browser.contexts:
                    for page in context.pages:
                        if page.url == url:
                            page.close()
            browser.close()
        if stop == 'ctrl-c':
            os.killpg(process.pid, signal.SIGINT)
        assert process.wait(timeout=15) == (0 if stop == 'page-close' else 130)
        assert tuple(sha256(path.read_bytes()).hexdigest() for path in owned) == before
        with pytest.raises(OSError):
            urlopen(url, timeout=1)
        with pytest.raises(OSError):
            urlopen(endpoint + '/json/version', timeout=1)
        for _ in range(30):
            listing = subprocess.run(['ps', '-e', '-o', 'pid=,pgid=,stat='], capture_output=True,
                                     text=True, timeout=2, check=True).stdout
            descendants = [row for row in listing.splitlines()
                           if len(row.split()) >= 3 and row.split()[1] == str(process.pid)
                           and not row.split()[2].startswith('Z')]
            if not descendants:
                break
            time.sleep(.1)
        assert not descendants
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
