"""Finite, inert resource-pack infrastructure declarations.

This module captures selected owners' inputs without importing providers or
contacting a container engine. Semantic activation belongs to the planner.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Mapping
import tomllib


class InfrastructureDeclarationError(ValueError):
    """A selected declaration cannot be admitted."""


_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_FIELD = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
MAX_DECLARATIONS = 16
MAX_SERVICES = 32
MAX_ASSETS_PER_SERVICE = 16
MAX_DECLARATION_BYTES = 64 * 1024
MAX_ASSET_BYTES = 1024 * 1024
MAX_CAPTURE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class CapturedAsset:
    relative_path: str
    body: bytes
    sha256: str


@dataclass(frozen=True)
class Activation:
    field: str
    equals: bool | int | str


@dataclass(frozen=True)
class Readiness:
    kind: str
    target: str = ""
    status: int = 200
    identity_field: str = ""
    identity_value: str = ""
    timeout: int = 30


@dataclass(frozen=True)
class EndpointOutput:
    name: str
    host: str
    in_project: str


@dataclass(frozen=True)
class Seed:
    asset: str
    completion: Readiness


@dataclass(frozen=True)
class InfrastructureService:
    id: str
    owner: str
    source: Path
    declaration_source: CapturedAsset
    activation: Activation | None
    dependencies: tuple[str, ...]
    compose: Mapping[str, object]
    compose_source: CapturedAsset
    assets: tuple[CapturedAsset, ...]
    readiness: Readiness
    endpoints: tuple[EndpointOutput, ...]
    seed: Seed | None


def _relative(value: object, label: str) -> tuple[str, ...]:
    if (type(value) is not str or not value or len(value) > 512
            or value.startswith("/") or "\\" in value):
        raise InfrastructureDeclarationError(f"{label} must be a bounded relative path")
    parts = tuple(value.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise InfrastructureDeclarationError(f"{label} is not canonical")
    return parts


def _capture(root: Path, relative: str, limit: int) -> CapturedAsset:
    parts = _relative(relative, "infrastructure asset")
    root = Path(root).resolve(strict=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(root, flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise InfrastructureDeclarationError("infrastructure asset is not a bounded regular file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                body = stream.read(limit + 1)
            after = os.fstat(fd)
            if (len(body) > limit or len(body) != info.st_size or
                    (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) !=
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                raise InfrastructureDeclarationError("infrastructure asset changed during capture")
        finally:
            os.close(fd)
    except (OSError, ValueError) as error:
        if isinstance(error, InfrastructureDeclarationError):
            raise
        raise InfrastructureDeclarationError("cannot capture infrastructure asset") from error
    finally:
        os.close(directory)
    return CapturedAsset(relative, body, sha256(body).hexdigest())


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise InfrastructureDeclarationError(f"{label} must be a bounded ASCII identifier")
    return value


def _readiness(value: object) -> Readiness:
    if (type(value) is not dict or set(value) -
            {"kind", "target", "status", "identity_field", "identity_value", "timeout"}):
        raise InfrastructureDeclarationError("invalid readiness declaration")
    kind = value.get("kind")
    if kind not in ("health", "http") or type(kind) is not str:
        raise InfrastructureDeclarationError("unsupported readiness kind")
    target = value.get("target", "")
    identity_field = value.get("identity_field", "")
    identity_value = value.get("identity_value", "")
    status = value.get("status", 200)
    timeout = value.get("timeout", 30)
    if (any(type(item) is not str or len(item) > 256 for item in
            (target, identity_field, identity_value))
            or type(status) is not int or not 100 <= status <= 599
            or type(timeout) is not int or not 1 <= timeout <= 300):
        raise InfrastructureDeclarationError("invalid readiness value")
    if kind == "health" and {"target", "status", "identity_field", "identity_value"} & set(value):
        raise InfrastructureDeclarationError("health readiness has no HTTP target")
    if kind == "http" and (not target or not identity_field or not identity_value):
        raise InfrastructureDeclarationError("HTTP readiness requires target and identity")
    return Readiness(kind, target, status, identity_field, identity_value, timeout)


def _freeze_json(value: object) -> object:
    if type(value) is dict:
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    return value


def _service(spec: object, *, owner: str, root: Path, source: Path,
             declaration_source: CapturedAsset) -> InfrastructureService:
    allowed = {"id", "when", "dependencies", "compose", "assets", "readiness", "endpoints", "seed"}
    if type(spec) is not dict or set(spec) - allowed or not {"id", "compose", "readiness"} <= set(spec):
        raise InfrastructureDeclarationError("invalid infrastructure service fields")
    local_id = _identifier(spec["id"], "service id")
    when = spec.get("when")
    activation = None
    if when is not None:
        if type(when) is not dict or set(when) != {"field", "equals"}:
            raise InfrastructureDeclarationError("invalid activation predicate")
        field = when["field"]
        equals = when["equals"]
        if (type(field) is not str or _FIELD.fullmatch(field) is None
                or type(equals) not in (str, int, bool) or
                (type(equals) is str and len(equals) > 256)):
            raise InfrastructureDeclarationError("invalid typed activation value")
        activation = Activation(field, equals)
    dependencies = spec.get("dependencies", [])
    if (type(dependencies) is not list or len(dependencies) > MAX_SERVICES
            or any(type(item) is not str or "/" not in item or len(item) > 130
                   for item in dependencies) or len(set(dependencies)) != len(dependencies)):
        raise InfrastructureDeclarationError("invalid service dependencies")
    compose_path = spec["compose"]
    compose_source = _capture(root, compose_path, MAX_DECLARATION_BYTES)
    try:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate Compose JSON key")
                result[key] = value
            return result

        def finite_float(value):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("nonfinite Compose number")
            return number

        parsed = json.loads(compose_source.body, object_pairs_hook=pairs,
                            parse_float=finite_float,
                            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeError, ValueError) as error:
        raise InfrastructureDeclarationError("invalid Compose JSON fragment") from error
    if type(parsed) is not dict:
        raise InfrastructureDeclarationError("Compose fragment must be a service object")
    assets = spec.get("assets", [])
    if (type(assets) is not list or len(assets) > MAX_ASSETS_PER_SERVICE
            or any(type(item) is not str for item in assets)
            or len(set(assets)) != len(assets)):
        raise InfrastructureDeclarationError("invalid infrastructure assets")
    captured = tuple(_capture(root, item, MAX_ASSET_BYTES) for item in assets)
    readiness = _readiness(spec["readiness"])
    endpoints = spec.get("endpoints", [])
    if type(endpoints) is not list or len(endpoints) > 16:
        raise InfrastructureDeclarationError("invalid endpoint outputs")
    outputs = []
    for item in endpoints:
        if type(item) is not dict or set(item) != {"name", "host", "in_project"}:
            raise InfrastructureDeclarationError("invalid endpoint output")
        name = _identifier(item["name"], "endpoint name")
        if (type(item["host"]) is not str or type(item["in_project"]) is not str
                or max(len(item["host"]), len(item["in_project"])) > 512):
            raise InfrastructureDeclarationError("invalid endpoint address")
        outputs.append(EndpointOutput(name, item["host"], item["in_project"]))
    if len({item.name for item in outputs}) != len(outputs):
        raise InfrastructureDeclarationError("duplicate endpoint output")
    seed_spec = spec.get("seed")
    seed = None
    if seed_spec is not None:
        if type(seed_spec) is not dict or set(seed_spec) != {"asset", "completion"}:
            raise InfrastructureDeclarationError("invalid seed declaration")
        asset = seed_spec["asset"]
        if type(asset) is not str or asset not in assets:
            raise InfrastructureDeclarationError("seed asset must be captured")
        seed = Seed(asset, _readiness(seed_spec["completion"]))
    return InfrastructureService(f"{owner}/{local_id}", owner, source, declaration_source, activation,
                                 tuple(dependencies), _freeze_json(parsed), compose_source,
                                 captured, readiness, tuple(outputs), seed)


def load_infrastructure_contributions(context, *, selected_provider_locations=()) -> tuple[InfrastructureService, ...]:
    """Capture selected pack and provider declarations without adapter imports."""
    sources = [(context.pack_root, path.relative_to(context.pack_root).as_posix())
               for path in context.infrastructure_paths]
    from api_agent.providers.loading import provider_location
    for location in selected_provider_locations:
        path, _ = provider_location(location, owner_root=context.owner_root)
        declaration = path.parent / "infrastructure.toml"
        if declaration.is_symlink():
            raise InfrastructureDeclarationError("provider infrastructure declaration is symlinked")
        if declaration.exists():
            sources.append((path.parent, "infrastructure.toml"))
    if len(sources) > MAX_DECLARATIONS:
        raise InfrastructureDeclarationError("too many infrastructure declarations")
    services = []
    total = 0
    seen = set()
    for root, relative in sources:
        declaration = _capture(root, relative, MAX_DECLARATION_BYTES)
        total += len(declaration.body)
        try:
            data = tomllib.loads(declaration.body.decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError) as error:
            raise InfrastructureDeclarationError("invalid infrastructure TOML") from error
        if (set(data) != {"version", "owner", "services"}
                or type(data["version"]) is not int or data["version"] != 1
                or type(data["services"]) is not list):
            raise InfrastructureDeclarationError("unsupported infrastructure declaration")
        owner = _identifier(data["owner"], "infrastructure owner")
        if Path(root).resolve() == context.pack_root.resolve() and owner != context.pack_id:
            raise InfrastructureDeclarationError("pack infrastructure owner differs from pack id")
        source = Path(root) / relative
        for spec in data["services"]:
            item = _service(spec, owner=owner, root=root, source=source,
                            declaration_source=declaration)
            if item.id in seen:
                raise InfrastructureDeclarationError("duplicate infrastructure service id")
            seen.add(item.id)
            services.append(item)
            total += len(item.compose_source.body) + sum(len(asset.body) for asset in item.assets)
            if len(services) > MAX_SERVICES or total > MAX_CAPTURE_BYTES:
                raise InfrastructureDeclarationError("infrastructure capture exceeds budget")
    return tuple(services)
