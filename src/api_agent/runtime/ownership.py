"""Stable ownership identity for stacks installed under a pair of roots."""
from dataclasses import dataclass, field
from hashlib import sha256
from itertools import product
from pathlib import Path
import re

MANAGED_LABEL = "api-agent.managed"
SCOPE_LABEL = "api-agent.scope"
PROFILE_LABEL = "api-agent.profile"
PROFILES = frozenset({"langfuse", "otel", "localstack"})
CHECKOUT_LABEL = "api-agent.checkout"


def checkout_label(name):
    """A readable, label-safe form of a checkout directory name."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:63]


SLUG_LIMIT = 40
COMPOSE_PROJECT = "com.docker.compose.project"


def checkout_slug(label):
    """The lowercase Compose-project-safe form of a checkout label; never empty."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", label.lower()).strip("-_")[:SLUG_LIMIT].strip("-_")
    return slug or "checkout"


def derived_project(scope_id, checkout):
    """The readable Compose project name for a scope: ``sa-<checkout slug>-<first 8 hex of the scope id>``.

    The 16-hex scope id stays the identity. Without a checkout name the legacy ``sa-<scope id>`` form applies.
    """
    return f"sa-{checkout_slug(checkout)}-{scope_id[:8]}" if checkout else f"sa-{scope_id}"


def accepted_projects(scope_id, checkout):
    """Every project name an object labelled with this scope and checkout may carry.

    That is the legacy ``sa-<scope id>`` name or the name derived from the same two labels, so a name is
    never trusted on its own: it must be reproducible from the scope and checkout labels beside it.
    """
    names = {f"sa-{scope_id}"}
    if isinstance(checkout, str) and checkout:
        names.add(derived_project(scope_id, checkout))
    return frozenset(names)


def volume_project_names_scope(scope_id, project):
    """True when a volume's project label is the legacy name or a derived name embedding this scope.

    Compose volumes carry no checkout label, so the slug cannot be recomputed; the name must still end
    with this scope's own 8-hex prefix and be a valid derived name, beside the scope label it is checked with.
    """
    return (project == f"sa-{scope_id}"
            or re.fullmatch(r"sa-[a-z0-9][a-z0-9_-]*-" + re.escape(scope_id[:8]), project) is not None)


@dataclass(frozen=True)
class StackScope:
    id: str
    legacy_ids: tuple[str, ...] = field(default=(), compare=False)
    # Never part of identity. It names the Compose project (see derived_project), and every object's
    # project must be reproducible from its own scope and checkout labels before it is trusted.
    checkout: str = field(default="", compare=False)

    @classmethod
    def from_roots(cls, base_root, pack_root, *, label=None):
        """The scope of two roots. ``label`` only names the stack; identity always comes from the roots."""
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
        return cls(identifier, legacy, checkout_label(label or roots[1].name))

    def is_legacy(self, labels):
        return (isinstance(labels, dict) and labels.get(MANAGED_LABEL) == 'true'
                and labels.get(SCOPE_LABEL) in self.legacy_ids
                and labels.get('com.docker.compose.project') == 'sa-' + labels[SCOPE_LABEL])

    @property
    def project(self):
        return derived_project(self.id, self.checkout)

    def labels(self, profile, *, profiles=PROFILES):
        if profile not in profiles:
            raise ValueError("Unsupported stack profile.")
        return {MANAGED_LABEL: "true", SCOPE_LABEL: self.id, PROFILE_LABEL: profile}

    def owns(self, labels, *, profiles=PROFILES):
        return (isinstance(labels, dict)
                and labels.get(MANAGED_LABEL) == "true"
                and labels.get(SCOPE_LABEL) == self.id
                and labels.get(PROFILE_LABEL) in profiles
                and labels.get(COMPOSE_PROJECT) in accepted_projects(self.id, labels.get(CHECKOUT_LABEL))
                and isinstance(labels.get("com.docker.compose.service"), str)
                and bool(labels["com.docker.compose.service"].strip()))
