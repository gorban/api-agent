"""Stable ownership identity for stacks installed under a pair of roots."""
from dataclasses import dataclass, field
from hashlib import sha256
from itertools import product
from pathlib import Path

MANAGED_LABEL = "api-agent.managed"
SCOPE_LABEL = "api-agent.scope"
PROFILE_LABEL = "api-agent.profile"
PROFILES = frozenset({"langfuse", "otel", "localstack"})


@dataclass(frozen=True)
class StackScope:
    id: str
    legacy_ids: tuple[str, ...] = field(default=(), compare=False)

    @classmethod
    def from_roots(cls, base_root, pack_root):
        roots = tuple(Path(root).resolve() for root in (base_root, pack_root))
        identities = []
        for root in roots:
            ancestor, suffix = root, []
            while not ancestor.exists():
                suffix.insert(0, ancestor.name)
                ancestor = ancestor.parent
            stat = ancestor.stat()
            identities.append(f'{stat.st_dev}:{stat.st_ino}/' + '/'.join(suffix))
        identifier = sha256(('physical-v1\n' + '\n'.join(identities)).encode()).hexdigest()[:16]
        # Remember previous path-based identities, including this host's physical
        # /home alias. Never adopt old labels without checking their leases.
        aliases = []
        for root in roots:
            value = str(root)
            candidate = Path('/var' + value) if value.startswith('/home/') else Path(value[4:]) if value.startswith('/var/home/') else root
            aliases.append({root, candidate} if candidate.exists() and candidate.samefile(root) else {root})
        legacy = tuple(sorted(sha256(f'{base}\n{pack}'.encode()).hexdigest()[:16]
                              for base, pack in product(*aliases)))
        return cls(identifier, legacy)

    def is_legacy(self, labels):
        return (isinstance(labels, dict) and labels.get(MANAGED_LABEL) == 'true'
                and labels.get(SCOPE_LABEL) in self.legacy_ids
                and labels.get('com.docker.compose.project') == 'sa-' + labels[SCOPE_LABEL])

    @property
    def project(self):
        return f"sa-{self.id}"

    def labels(self, profile):
        if profile not in PROFILES:
            raise ValueError("Unsupported stack profile.")
        return {MANAGED_LABEL: "true", SCOPE_LABEL: self.id, PROFILE_LABEL: profile}

    def owns(self, labels):
        return (isinstance(labels, dict)
                and labels.get(MANAGED_LABEL) == "true"
                and labels.get(SCOPE_LABEL) == self.id
                and labels.get(PROFILE_LABEL) in PROFILES
                and labels.get("com.docker.compose.project") == self.project
                and isinstance(labels.get("com.docker.compose.service"), str)
                and bool(labels["com.docker.compose.service"].strip()))
