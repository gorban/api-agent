"""Direct worker startup must not depend on the launcher's import environment."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from api_agent.llm_providers.bedrock import login_worker


@pytest.mark.parametrize('layout', ['source', 'installed'])
def test_direct_worker_starts_without_inherited_package_path(tmp_path, layout):
    worker = Path(login_worker.__file__)
    if layout == 'installed':
        package = tmp_path / 'site-packages' / 'api_agent'
        worker_directory = package / 'llm_providers' / 'bedrock'
        worker_directory.mkdir(parents=True)
        for directory in (package, package / 'llm_providers', worker_directory):
            (directory / '__init__.py').write_text('')
        for name in ('login_worker.py', 'persistence.py'):
            shutil.copyfile(worker.with_name(name), worker_directory / name)
        worker = worker_directory / worker.name
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)

    # A malformed frame exits through the worker protocol before SDK imports,
    # credential resolution, native commands or any external operation.
    result = subprocess.run([sys.executable, str(worker)], input=b'[]\n',
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
        cwd=tmp_path, timeout=5, check=False)

    assert result.returncode == 0
    assert result.stderr == b''
    assert json.loads(result.stdout) == dict(error='acquisition',
        stage='worker_protocol', kind='unknown', reason='unknown')
