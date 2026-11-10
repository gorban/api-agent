"""Build the minimal source wheel used to compile the bundled demo plans."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = '''\
[build-system]
requires = ["hatchling>=1.26,<2"]
build-backend = "hatchling.build"

[project]
name = "api-agent-demo-bindings"
version = "0.1.0"
requires-python = ">=3.13"

[tool.hatch.build.targets.wheel.force-include]
"functions.py" = "api_agent/demo/simple/functions.py"
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'demo/.private_resources/wheels')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='demo-bindings-') as temporary:
        source = Path(temporary)
        shutil.copyfile(ROOT / 'demo/simple/functions.py', source / 'functions.py')
        (source / 'pyproject.toml').write_text(PYPROJECT)
        subprocess.run([sys.executable, '-m', 'build', '--wheel', '--no-isolation',
                        '--outdir', str(args.output.resolve()), str(source)], check=True)


if __name__ == '__main__':
    main()
