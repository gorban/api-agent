"""Remove managed containers, volumes and networks that nothing can use any more.

Release is stop-only so the current checkout reuses its stack quickly. Scopes are
per checkout, so a deleted checkout or an abandoned scope would otherwise keep its
stopped containers, volumes and network forever. A scope is reaped only when every
one of its managed containers is stopped, it is not the caller's scope, no live
lease protects it in its own ownership journal, and either its recorded compose
directory is gone or it has been stopped longer than the retention period.

Everything here is best-effort: any uncertainty preserves the resource, and a
failure is reported as a short value-free diagnostic, never raised.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace

from . import prerequisites
from .compose import DEFAULT_RETENTION_DAYS
from .containers import ContainerError, session_container_identity
from .ownership import MANAGED_LABEL, SCOPE_LABEL

BUDGET_SECONDS = 10
_LOG = logging.getLogger(__name__)
_CONTAINER_MARKER = '/.dockerenv'
_WORKING_DIRECTORY = 'com.docker.compose.project.working_dir'
_PROJECT = 'com.docker.compose.project'
_STOPPED = frozenset({'exited', 'created', 'dead'})
_SCOPE = re.compile(r'[0-9a-f]{16}')
_VOLUME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,254}')
_TEMPLATE = ('{"id":"{{.Id}}","status":{{json .State.Status}},"running":{{json .State.Running}},'
             '"finished":{{json .State.FinishedAt}},"created":{{json .Created}},'
             '"labels":{{json .Config.Labels}}}')
_REMOVE_BATCH = 32
_INSPECT_BATCH = 128


@dataclass(frozen=True)
class ReapReport:
    """Counts of removed objects; ``diagnostic`` is empty when the pass completed."""
    scopes: int = 0
    containers: int = 0
    volumes: int = 0
    networks: int = 0
    diagnostic: str = ''


class _Stop(Exception):
    """The caller's deadline or cancellation ended this pass."""


def host_paths_visible(environment):
    """True when a compose directory recorded by the engine can be checked on this host.

    A remote engine, or a session inside a container, records paths this process
    cannot see, so absence there proves nothing.
    """
    for key in ('DOCKER_HOST', 'CONTAINER_HOST'):
        value = environment.get(key)
        if value and not value.startswith('unix://'):
            return False
    try:
        if session_container_identity():
            return False
    except ContainerError:
        return False
    return not os.path.exists(_CONTAINER_MARKER)


def _timestamp(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError('Timestamp has no zone.')
    return moment


def _absent(path):
    """True only when the filesystem positively reports the path does not exist."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


def _chunks(items, size):
    return (items[offset:offset + size] for offset in range(0, len(items), size))


class _Reaper:
    def __init__(self, *, run, prune_network, executable, scope, retention_days, deadline, now, cancelled,
                 identity, wall_clock, host_paths_visible):
        self.runner, self.prune_network, self.executable, self.scope = run, prune_network, executable, scope
        self.retention_days, self.deadline, self.now, self.cancelled = retention_days, deadline, now, cancelled
        self.identity, self.wall_clock, self.paths_visible = identity, wall_clock, host_paths_visible
        self.excluded = {scope.id, *getattr(scope, 'legacy_ids', ())}
        self.scopes = self.containers = self.volumes = self.networks = 0
        self.failure = ''

    def check(self):
        if self.cancelled():
            raise _Stop('cancelled')
        if self.now() >= self.deadline:
            raise _Stop('deadline')

    def run(self, *arguments):
        self.check()
        return self.runner((self.executable, *arguments), self.deadline)

    def listing(self):
        identifiers = self.run('ps', '-a', '--no-trunc', '--filter', f'label={MANAGED_LABEL}=true',
                               '--format', '{{.ID}}').split()
        if (len(set(identifiers)) != len(identifiers)
                or any(not prerequisites._ID.fullmatch(identifier) for identifier in identifiers)):
            raise ValueError('Invalid managed container identifiers.')
        return identifiers

    def inspect(self, identifiers):
        records = []
        for batch in _chunks(identifiers, _INSPECT_BATCH):
            rows = self.run('inspect', '--format', _TEMPLATE, *batch).splitlines()
            if len(rows) != len(batch):
                raise ValueError('Container inspection did not match the listing.')
            for row in rows:
                records.append(self.record(json.loads(row)))
            if {record['id'] for record in records[-len(batch):]} != set(batch):
                raise ValueError('Container identities changed.')
        return records

    @staticmethod
    def record(value):
        try:
            labels = value['labels'] or {}
            record = {'id': value['id'], 'status': value['status'], 'running': value['running'],
                      'labels': labels, 'created': _timestamp(value['created']),
                      'finished': _timestamp(value['finished'])}
        except (KeyError, TypeError) as error:
            raise ValueError('Invalid container inspection.') from error
        if (not isinstance(labels, dict) or not isinstance(record['status'], str)
                or type(record['running']) is not bool):
            raise ValueError('Invalid container inspection.')
        return record

    def candidates(self, records):
        groups, blocked = {}, set()
        for record in records:
            labels = record['labels']
            scope_id = labels.get(SCOPE_LABEL)
            if (labels.get(MANAGED_LABEL) != 'true' or type(scope_id) is not str
                    or not _SCOPE.fullmatch(scope_id)):
                continue
            if labels.get(_PROJECT) != 'sa-' + scope_id:
                blocked.add(scope_id)
                continue
            groups.setdefault(scope_id, []).append(record)
        return {scope_id: group for scope_id, group in sorted(groups.items())
                if scope_id not in blocked and scope_id not in self.excluded
                and all(self.stopped(record) for record in group)}

    @staticmethod
    def stopped(record):
        return record['status'] in _STOPPED and not record['running']

    def ownership_directory(self, scope_id, group):
        """The recorded project directories and the one journal directory their layout proves.

        Each acquisition that creates services records its own retained snapshot directory, so a
        scope may span several, all beside the scope's journal. Anything else is unprovable.
        """
        recorded = {record['labels'].get(_WORKING_DIRECTORY) for record in group}
        pattern = re.escape(scope_id) + r'-assets-[A-Za-z0-9_]+'
        if any(type(working) is not str or not os.path.isabs(working) or not re.fullmatch(pattern, Path(working).name)
               for working in recorded):
            return None, None
        parents = {Path(working).parent for working in recorded}
        if len(parents) != 1:
            return None, None
        return recorded, parents.pop()

    def reason(self, recorded, group):
        if self.paths_visible and all(_absent(working) for working in recorded):
            return 'orphan'
        if self.retention_days > 0:
            stops = [record['finished'] if record['finished'].year > 1 else record['created']
                     for record in group]
            if (self.wall_clock() - max(stops)).total_seconds() >= self.retention_days * 86400:
                return 'aged'
        return None

    def reap(self):
        identifiers = self.listing()
        if not identifiers:
            return
        for scope_id, group in self.candidates(self.inspect(identifiers)).items():
            self.check()
            recorded, journal = self.ownership_directory(scope_id, group)
            if recorded is None or self.reason(recorded, group) is None:
                continue
            try:
                self.reap_scope(scope_id, [record['id'] for record in group], journal)
            except _Stop:
                raise
            except Exception as error:
                self.failure = type(error).__name__

    def reap_scope(self, scope_id, identifiers, journal):
        # A scope's journal lives beside its retained project directory. Holding its
        # lock excludes that scope's own acquisition while we remove its containers.
        guard = (prerequisites.guarded_cleanup(journal, SimpleNamespace(id=scope_id), identity=self.identity,
                                               deadline_seconds=1)
                 if os.path.isdir(journal) else nullcontext(frozenset()))
        with guard as leased:
            if leased:
                return
            fresh = self.inspect(identifiers)
            if (any(record['labels'].get(SCOPE_LABEL) != scope_id for record in fresh)
                    or not all(self.stopped(record) for record in fresh)):
                return
            for batch in _chunks(identifiers, _REMOVE_BATCH):
                self.run('rm', *batch)
                self.containers += len(batch)
            self.scopes += 1
            self.remove_volumes(scope_id)
            self.networks += len(self.prune_network(scope_id, self.deadline))

    def remove_volumes(self, scope_id):
        names = self.run('volume', 'ls', '--quiet', '--filter', f'label={_PROJECT}=sa-{scope_id}',
                         '--filter', f'label={MANAGED_LABEL}=true',
                         '--filter', f'label={SCOPE_LABEL}={scope_id}').split()
        if not names:
            return
        if len(set(names)) != len(names) or any(not _VOLUME.fullmatch(name) for name in names):
            raise ValueError('Invalid volume names.')
        records = json.loads(self.run('volume', 'inspect', *names))
        if (not isinstance(records, list) or len(records) != len(names)
                or any(not isinstance(record, dict) for record in records)
                or {record.get('Name') for record in records} != set(names)):
            raise ValueError('Volume inventory could not be verified.')
        owned = []
        for record in records:
            labels = record.get('Labels')
            if (isinstance(labels, dict) and labels.get(MANAGED_LABEL) == 'true'
                    and labels.get(SCOPE_LABEL) == scope_id and labels.get(_PROJECT) == 'sa-' + scope_id
                    and isinstance(labels.get('api-agent.profile'), str) and labels['api-agent.profile']):
                owned.append(record['Name'])
        for batch in _chunks(owned, _REMOVE_BATCH):
            self.run('volume', 'rm', *batch)
            self.volumes += len(batch)


def reap_unused_managed_resources(*, run, prune_network, executable, scope, deadline,
                                  retention_days=DEFAULT_RETENTION_DAYS, now=time.monotonic,
                                  cancelled=lambda: False, identity=None, wall_clock=None,
                                  host_paths_visible=True):
    """Remove unused managed scopes and report what was removed; never raises.

    ``run(arguments, deadline)`` executes one engine command and returns stdout.
    ``prune_network(scope_id, deadline)`` removes that scope's unreferenced network
    and returns the removed identifiers. ``retention_days`` of zero disables only
    the age rule; the orphan rule always applies when host paths are visible.
    """
    reaper = _Reaper(run=run, prune_network=prune_network, executable=executable, scope=scope,
                     retention_days=retention_days, deadline=deadline, now=now, cancelled=cancelled,
                     identity=identity or prerequisites.process_identity,
                     wall_clock=wall_clock or (lambda: datetime.now(timezone.utc)),
                     host_paths_visible=host_paths_visible)
    diagnostic = ''
    try:
        reaper.reap()
        if reaper.failure:
            diagnostic = 'incomplete: ' + reaper.failure
    except _Stop as stop:
        diagnostic = f'incomplete: {stop}'
    except Exception as error:
        diagnostic = 'incomplete: ' + type(error).__name__
    report = ReapReport(reaper.scopes, reaper.containers, reaper.volumes, reaper.networks, diagnostic)
    if report.scopes:
        _LOG.info('Managed resource reaper removed %d scope(s): %d container(s), %d volume(s), %d network(s).',
                  report.scopes, report.containers, report.volumes, report.networks)
    if diagnostic:
        _LOG.warning('Managed resource reaper failed or was interrupted (%s); remaining resources were preserved.',
                     diagnostic)
    return report
