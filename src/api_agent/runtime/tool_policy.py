"""Pure sidecar policy and finite container isolation admission."""
from collections.abc import Mapping
from dataclasses import dataclass, field
import math
import re
from urllib.parse import urlsplit

from .compose import ComposeError


@dataclass(frozen=True)
class SidecarPolicy:
    mode: str
    configured: str = field(repr=False)
    source: str


def sidecar_policy(value, *, source, profile='local', selected=True):
    """Preserve user intent; deployment never authorizes container ownership."""
    if profile not in ('local', 'deployment') or type(source) is not str:
        raise ComposeError('Unsupported sidecar policy.')
    if not selected:
        return SidecarPolicy('unused', '', source)
    if type(value) is not str or value != value.strip():
        raise ComposeError('Sidecar address must be a canonical string.')
    if profile == 'deployment' and (not value or source != 'environment'):
        raise ComposeError('Deployment sidecar requires an explicit nonempty environment address.')
    if not value:
        return SidecarPolicy('managed-local', value, source)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ComposeError('Invalid sidecar address.') from None
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or '?' in value or '#' in value or '\\' in value
            or any(ord(char) < 33 or ord(char) > 126 for char in value)
            or port == 0):
        raise ComposeError('Invalid sidecar address.')
    authority = '[' + parsed.hostname + ']' if ':' in parsed.hostname else parsed.hostname
    if port is not None:
        authority += ':' + str(port)
    if parsed.netloc.lower() != authority:
        raise ComposeError('Invalid sidecar authority.')
    return SidecarPolicy('deployment-sidecar' if profile == 'deployment' else 'explicit-external',
                         value, source)


def isolation_fragment(policy):
    """Admit a versioned runsc policy; never forward arbitrary Compose fields."""
    keys = {'version', 'runtime', 'user', 'memory_mb', 'cpus', 'pids_limit'}
    if (not isinstance(policy, Mapping) or set(policy) != keys
            or type(policy['version']) is not int or policy['version'] != 1
            or policy['runtime'] != 'runsc'):
        raise ComposeError('Unsupported isolated runtime policy.')
    user = policy['user']
    if type(user) is not str or not re.fullmatch(r'[1-9][0-9]{0,5}:[1-9][0-9]{0,5}', user):
        raise ComposeError('Isolated runtime requires a non-root numeric user and group.')
    memory, cpus, pids = policy['memory_mb'], policy['cpus'], policy['pids_limit']
    if (type(memory) is not int or not 16 <= memory <= 8192
            or type(cpus) not in (int, float) or not math.isfinite(cpus) or not .1 <= cpus <= 8
            or type(pids) is not int or not 8 <= pids <= 512):
        raise ComposeError('Isolated runtime requires finite resource limits.')
    return {'runtime': 'runsc', 'user': user, 'read_only': True,
            'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
            'mem_limit': memory * 1024 * 1024, 'cpus': cpus, 'pids_limit': pids,
            'tmpfs': ['/tmp:rw,noexec,nosuid,nodev,size=16777216']}


def verify_isolation(spec, record, *, network):
    """Check engine inspect data, never a service's self-reported health string."""
    if 'runtime' not in spec:
        return
    try:
        host = record['HostConfig']
        valid = (host['Runtime'] == spec['runtime'] and host['Privileged'] is False
                 and host['ReadonlyRootfs'] is True and host['CapDrop'] == ['ALL']
                 and host['CapAdd'] in (None, [])
                 and host['SecurityOpt'] == ['no-new-privileges:true']
                 and host['Memory'] == spec['mem_limit']
                 and host['NanoCpus'] == int(spec['cpus'] * 1_000_000_000)
                 and host['PidsLimit'] == spec['pids_limit']
                 and type(network) is str and bool(network)
                 and host['NetworkMode'] == network
                 and host['Tmpfs'] == {'/tmp': 'rw,noexec,nosuid,nodev,size=16777216'}
                 and record['Config']['User'] == spec['user'])
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise ComposeError('Engine isolation controls differ from the admitted policy.')
