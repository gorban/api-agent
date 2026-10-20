"""Early standalone CLI rejection does not prepare providers or a browser."""
from pathlib import Path
import os
import subprocess
import sys
import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.feature('Workflow viewer')


def test_view_flow_rejects_test_mode_before_browser_setup():
    program = ('from pathlib import Path; from api_agent.runtime.bootstrap import main; '
               'raise SystemExit(main(["--view-flow", "--tui-mode=unit"], '
               'base_root=Path.cwd(), _worktrees_verified=True))')
    environment = os.environ.copy()
    environment['PYTHONPATH'] = str(ROOT / 'src')
    result = subprocess.run([sys.executable, '-c', program], cwd=ROOT, env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert '--view-flow conflicts with --tui-mode' in result.stderr
    assert 'Preparing' not in result.stderr
