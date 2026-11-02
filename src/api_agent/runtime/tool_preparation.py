"""Trusted-owner bounded image preparation, separate from service admission.

Build receipts never equate a local image content ID with a registry manifest.
Only managed development callers may select this seam. Declarations cannot
supply host hooks, additional build flags or executable commands.
"""
from dataclasses import dataclass, field
import hashlib
import math
from pathlib import PurePosixPath, Path
import re
import tempfile
import time
from types import MappingProxyType

from api_agent.providers.progress import report_timeout_sync
from api_agent.runtime.prerequisites import run_command
from api_agent.testing.diagnostics import CredentialScrubber


class PreparationError(ValueError):
    """Neutral failure without build output or private source paths."""


def _require(condition):
    if not condition:
        raise PreparationError('Invalid image preparation request.')


@dataclass(frozen=True)
class SecretInput:
    id: str
    content: bytes = field(repr=False)

    def __post_init__(self):
        _require(type(self.id) is str and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', self.id))
        _require(type(self.content) is bytes and 0 < len(self.content) <= 65536)


@dataclass(frozen=True)
class BuildRequest:
    """Captured source bytes with fixed limits; never traverses a live checkout."""
    local_tag: str
    expected_image_id: str
    context: object = field(repr=False)
    dockerfile: bytes = field(repr=False)
    secrets: tuple[SecretInput, ...] = field(default=(), repr=False)
    timeout: float = 120

    def __post_init__(self):
        _require(type(self.local_tag) is str and re.fullmatch(r'[a-z0-9][a-z0-9./_-]{0,126}:[a-z0-9][a-z0-9_.-]{0,63}', self.local_tag))
        _require(type(self.expected_image_id) is str and re.fullmatch(r'sha256:[0-9a-f]{64}', self.expected_image_id))
        _require(type(self.dockerfile) is bytes and 0 < len(self.dockerfile) <= 262144)
        _require(type(self.timeout) in (int, float) and math.isfinite(self.timeout) and 0 < self.timeout <= 600)
        _require(type(self.secrets) is tuple and len(self.secrets) <= 8)
        _require(all(type(secret) is SecretInput for secret in self.secrets))
        _require(len({secret.id for secret in self.secrets}) == len(self.secrets))
        _require(type(self.context) in (dict, MappingProxyType) and 0 < len(self.context) <= 512)
        captured = dict(self.context)
        for name, content in captured.items():
            _require(type(name) is str and len(name) <= 240)
            parts = PurePosixPath(name).parts
            _require(bool(parts) and not name.startswith('/') and '\\' not in name
                     and all(part not in ('', '.', '..') for part in name.split('/'))
                     and all(ord(char) >= 32 for char in name))
            _require(type(content) is bytes and len(content) <= 8 * 1024 * 1024)
        _require(sum(len(content) for content in captured.values()) <= 32 * 1024 * 1024)
        names = sorted(captured)
        _require(not any(right.startswith(left + '/') for left in names for right in names))
        object.__setattr__(self, 'context', MappingProxyType(captured))

    @property
    def context_digest(self):
        digest = hashlib.sha256(self.dockerfile)
        for name, content in sorted(self.context.items()):
            digest.update(name.encode() + b'\0' + hashlib.sha256(content).digest())
        return digest.hexdigest()


@dataclass(frozen=True)
class BuildReceipt:
    local_tag: str
    image_id: str
    context_digest: str


def prepare_image(request, *, target, cancelled=lambda: False, runner=run_command):
    """Build on a frozen engine and verify its local image ID.

    Secret files live outside the build context with owner-only permissions.
    The caller owns admission and must not pass this receipt as a registry pin.
    A trusted Dockerfile can access explicitly supplied secrets; it must not
    copy them into layers. This seam cannot certify Dockerfile behavior.
    """
    _require(type(request) is BuildRequest)
    scrubber = CredentialScrubber(secret_values=tuple(secret.content.decode('utf-8', errors='replace') for secret in request.secrets))
    try:
        with report_timeout_sync('Tool image preparation', request.timeout) as budget:
            deadline = time.monotonic() + budget.remaining
            target.verify(cancelled=cancelled)
            with tempfile.TemporaryDirectory(prefix='tool-build-') as directory:
                root = Path(directory)
                context = root / 'context'
                context.mkdir(mode=0o700)
                for name, content in request.context.items():
                    path = context / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                dockerfile = root / 'Dockerfile'
                dockerfile.write_bytes(request.dockerfile)
                arguments = ['build', '--file', str(dockerfile), '--tag', request.local_tag]
                for index, secret in enumerate(request.secrets):
                    path = root / ('secret-' + str(index))
                    path.touch(mode=0o600)
                    path.write_bytes(secret.content)
                    arguments.extend(('--secret', 'id=' + secret.id + ',src=' + str(path)))
                arguments.append(str(context))
                options = dict(environment=target.environment, directory=root,
                               deadline=deadline, cancelled=cancelled,
                               output_scrubber=scrubber, output_limit=65536)
                runner((target.executable, *arguments), **options)
                target.verify(cancelled=cancelled)
                image_id = runner((target.executable, 'image', 'inspect', '--format', '{{.Id}}', request.local_tag), **options).strip()
                _require(image_id == request.expected_image_id)
                return BuildReceipt(request.local_tag, image_id, request.context_digest)
    except Exception:
        pass
    # Raise outside the handler so private exceptions cannot survive as context.
    raise PreparationError('Tool image preparation failed.')


def prepare_selected_sidecar(policy, *, request_factory, target_factory, runner=run_command,
                             cancelled=lambda: False):
    """Resolve source, secrets and engine only for selected managed-local policy."""
    from .tool_policy import SidecarPolicy
    _require(type(policy) is SidecarPolicy)
    if policy.mode != 'managed-local':
        _require(policy.mode in ('unused', 'explicit-external', 'deployment-sidecar'))
        return None
    try:
        request = request_factory()
        target = target_factory()
        return prepare_image(request, target=target, runner=runner, cancelled=cancelled)
    except Exception:
        pass
    raise PreparationError('Tool image preparation failed.')
