"""Real staged checker ignores unstaged checker and generated-output edits."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.feature('Workflow viewer')


def dispatch(root):
    environment = {**os.environ, 'API_AGENT_PLANTUML_JAR': str(root / 'absent-renderer.jar')}
    return subprocess.run([sys.executable, 'tools/pre_commit_dispatcher.py'], cwd=root,
                          env=environment, capture_output=True, text=True, timeout=90)


def test_real_index_check_isolated_from_unstaged_runner_and_svg(tmp_path):
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    subprocess.run(['git', 'init', '-q'], cwd=snapshot, check=True, timeout=30)
    ignore = shutil.ignore_patterns('.git', '.venv', '.pytest_cache', '.coverage', '__pycache__', '.api-agent')
    shutil.copytree(ROOT, snapshot, dirs_exist_ok=True, ignore=ignore)
    subprocess.run(['git', 'add', '-A'], cwd=snapshot, check=True, timeout=30)

    assert dispatch(snapshot).returncode == 0
    (snapshot / 'tools/checks.py').write_text('raise SystemExit(0)\n')
    (snapshot / 'src/api_agent/workflows/viewer_build.py').write_text('raise SystemExit(0)\n')
    svg = snapshot / '.private_resources/diagrams/flow.svg'
    svg.write_text('tampered\n')
    assert dispatch(snapshot).returncode == 0

    subprocess.run(['git', 'add', str(svg.relative_to(snapshot))], cwd=snapshot, check=True)
    failure = dispatch(snapshot)
    assert failure.returncode != 0
    assert 'invalid workflow diagram provenance' in failure.stderr
