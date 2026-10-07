"""Stable ownership identity for stacks installed under a pair of roots."""
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

MANAGED_LABEL = "api-agent.managed"
SCOPE_LABEL = "api-agent.scope"
PROFILE_LABEL = "api-agent.profile"
PROFILES = frozenset({"langfuse", "otel", "localstack"})


@dataclass(frozen=True)
class StackScope:
    id: str

    @classmethod
    def from_roots(cls, base_root, pack_root):
        identity = f"{Path(base_root).resolve()}\n{Path(pack_root).resolve()}"
        return cls(sha256(identity.encode()).hexdigest()[:16])

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
