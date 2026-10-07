"""Resource-pack ownership and paths, resolved without relying on CWD."""

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
import tomllib


class ResourcePackError(ValueError):
    """A pack cannot provide a valid launch context."""


@dataclass(frozen=True)
class LaunchContext:
    base_root: Path
    pack_root: Path
    owner_root: Path
    name: str
    pack_id: str
    config_path: Path
    defaults: Mapping[str, object]
    tests: Mapping[str, tuple[Path, ...]]
    packaged_demo: bool = False
    launcher_root: Path | None = None
    show_pack_bootstrap: bool = False
    credential_role_fields: tuple[str, ...] = ()

    @property
    def dotenv_path(self) -> Path:
        return (self.pack_root if self.external_pack else self.base_root) / ".env"

    @property
    def anchors(self) -> dict[str, Path]:
        return {"base_root": self.base_root, "pack_root": self.pack_root, "owner_root": self.owner_root}

    @property
    def external_pack(self) -> bool:
        return self.pack_root != self.base_root / "demo" and not self.packaged_demo

    def suite_paths(self, mode: str) -> tuple[Path, ...]:
        """Base units always participate; integration/E2E belong to the pack."""
        if mode not in {"unit", "integration", "e2e"}:
            raise ResourcePackError(f"Unknown test mode: {mode}")
        paths = self.tests.get(mode, ())
        if mode == "unit":
            paths = (self.base_root / "tests/unit",) + paths
        return tuple(dict.fromkeys(paths))


def _relative_path(value: object, root: Path, field: str) -> Path:
    if not isinstance(value, str) or not value.strip() or Path(value).is_absolute():
        raise ResourcePackError(f"{field} must be a nonempty relative path")
    return (root / value).resolve()


def load_launch_context(
    base_root: Path,
    resource_pack_dir: Path | None = None,
    launcher_root: Path | None = None,
) -> LaunchContext:
    base = Path(base_root).resolve()
    if not base.is_dir():
        raise ResourcePackError(f"Base api-agent directory does not exist: {base}")
    packaged_demo = False
    if resource_pack_dir is not None:
        pack = Path(resource_pack_dir).resolve()
    elif launcher_root is not None and Path(launcher_root).resolve() != base:
        pack = Path(launcher_root).resolve()
    else:
        pack = base / "demo"
        if not pack.is_dir():
            # Wheels include the same root demo under package data.
            pack = Path(str(files("api_agent").joinpath("demo"))).resolve()
            packaged_demo = True
    manifest = pack / "pack.toml"
    try:
        with manifest.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ResourcePackError(f"Cannot read resource-pack declaration: {manifest}") from exc
    allowed = {"version", "id", "owner", "config", "tests", "defaults", "authentication"}
    if set(data) - allowed:
        raise ResourcePackError(f"Unknown fields in resource-pack declaration: {manifest}")
    if type(data.get("version")) is not int or data["version"] != 1:
        raise ResourcePackError(f"Unsupported resource-pack version in {manifest}; expected 1")
    pack_id = data.get("id")
    if not isinstance(pack_id, str) or not pack_id.strip():
        raise ResourcePackError(f"Resource-pack id must be a nonempty string: {manifest}")
    owner = _relative_path(data.get("owner", "."), pack, "owner")
    if packaged_demo:
        owner = base
    if not owner.is_dir():
        raise ResourcePackError(f"Resource-pack owner directory does not exist: {owner}")
    config = _relative_path(data.get("config"), pack, "config")
    if not config.is_file():
        raise ResourcePackError(f"Resource-pack configuration does not exist: {config}")
    defaults = data.get("defaults", {})
    declared_tests = data.get("tests", {})
    if not isinstance(defaults, dict) or not isinstance(declared_tests, dict):
        raise ResourcePackError("Resource-pack defaults and tests must be TOML tables")
    if set(declared_tests) - {"unit", "integration", "e2e"}:
        raise ResourcePackError("Resource-pack tests contains an unknown test mode")
    authentication = data.get("authentication", {})
    if not isinstance(authentication, dict) or set(authentication) - {"credential_role_fields"}:
        raise ResourcePackError("Resource-pack authentication contains unknown or invalid fields")
    role_fields = authentication.get("credential_role_fields", [])
    if (not isinstance(role_fields, list)
            or any(not isinstance(name, str) or not name.isascii() or not name.isidentifier()
                   for name in role_fields)
            or len(set(role_fields)) != len(role_fields)):
        raise ResourcePackError("authentication.credential_role_fields must list unique configuration field names")
    tests: dict[str, tuple[Path, ...]] = {}
    for mode, paths in declared_tests.items():
        if not isinstance(paths, list):
            raise ResourcePackError(f"tests.{mode} must be an array of relative paths")
        tests[mode] = tuple(_relative_path(path, owner, f"tests.{mode}") for path in paths)
    return LaunchContext(
        base, pack, owner, owner.name, pack_id, config,
        MappingProxyType(dict(defaults)), MappingProxyType(tests), packaged_demo,
        Path(launcher_root).resolve() if launcher_root is not None else base,
        resource_pack_dir is None and (launcher_root is None or Path(launcher_root).resolve() == base),
        tuple(role_fields),
    )
