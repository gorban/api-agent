"""Locate the bundled workflow assets in a checkout or installed package."""
from importlib.resources import files
from pathlib import Path


def _installed_distribution_owns_module(package: Path) -> bool:
    """An adjacent distribution marker denies fallback when its asset is missing."""
    return any(package.parent.glob('api_agent-*.dist-info'))


def bundled_root() -> Path:
    """Prefer the executing package's assets, then its verified source checkout."""
    package = Path(str(files('api_agent'))).resolve()
    if (package / 'flow.md.html').is_file():
        return package
    if _installed_distribution_owns_module(package):
        raise FileNotFoundError(f'Bundled application flow is missing: {package / "flow.md.html"}')
    module = Path(__file__).resolve()
    checkout = module.parents[3]
    source_module = checkout / 'src/api_agent/workflows/resources.py'
    if (source_module.is_file() and source_module.samefile(module)
            and (checkout / 'src/api_agent/__init__.py').is_file()
            and (checkout / 'flow.md.html').is_file()
            and (checkout / 'launch.py').is_file()
            and (checkout / 'pyproject.toml').is_file()):
        return checkout
    raise FileNotFoundError(f'Bundled application flow is missing: {package / "flow.md.html"}')
