"""Remove managed containers that no live lease protects, then storage nothing will use again.

Containers are disposable and storage is not: a graceful release removes the
caller's containers and leaves its named volumes, so nothing stays behind. A
crashed or killed process never reaches release, so every managed start and
release also cleans leaderless containers of other scopes: those that hold no
live lease in their own scope's ownership journal. Running ones are stopped and
then removed; stopped ones are removed. Containers never take a volume with them.

Volumes are removed by a separate rule. An owned volume that no container
references, whose scope holds no live lease, is removed once its scope's
recorded journal directories are gone (orphan) or its journal was last written
more than the retention period ago. Docker volumes carry no last-use time and
their creation date is not their last use, so the modification time of the
scope's journal, rewritten at every acquire and release, stands in for it.

Everything is best-effort: any uncertainty preserves the resource (an
unreadable journal, a held lock, an unrecognised layout, an unknown journal
location, a lease whose owner is alive), and a failure is reported as a short
value-free diagnostic, never raised.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace

from . import prerequisites, scope_index
from .compose import DEFAULT_RETENTION_DAYS
from .containers import ContainerError, session_container_identity
from .ownership import (CHECKOUT_LABEL, COMPOSE_PROJECT, MANAGED_LABEL, PROFILE_LABEL, SCOPE_LABEL, accepted_projects,
                        volume_project_names_scope)

BUDGET_SECONDS = 30
STOP_GRACE_SECONDS = '5'
_LOG = logging.getLogger(__name__)
_WORKING_DIRECTORY = 'com.docker.compose.project.working_dir'
_PROJECT = COMPOSE_PROJECT
_CONTAINER_MARKER = '/.dockerenv'
_BUSY = frozenset({'removing'})
_SCOPE = re.compile(r'[0-9a-f]{16}')
_VOLUME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,254}')
_TEMPLATE = ('{"id":"{{.Id}}","status":{{json .State.Status}},"running":{{json .State.Running}},'
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
    """True when a directory recorded for a scope can be checked on this host.

    A remote engine, or a session inside a container, may record paths this process
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


def _absent(path):
    """True only when the filesystem positively reports the path does not exist."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


def volume_owner(record):
    """The scope id of a fully labelled managed volume record, else ``None``.

    Needs the managed marker, a scope label, a project label that names that same scope,
    a volume name that is ``<project>_<volume>`` and a profile label.
    """
    labels, name = record.get('Labels'), record.get('Name')
    if not isinstance(labels, dict) or not isinstance(name, str):
        return None
    scope_id, project, profile = labels.get(SCOPE_LABEL), labels.get(_PROJECT), labels.get(PROFILE_LABEL)
    if (labels.get(MANAGED_LABEL) != 'true' or type(scope_id) is not str or not _SCOPE.fullmatch(scope_id)
            or type(project) is not str or not volume_project_names_scope(scope_id, project)
            or not name.startswith(project + '_') or name == project + '_'
            or type(profile) is not str or not profile):
        return None
    return scope_id


def _chunks(items, size):
    return (items[offset:offset + size] for offset in range(0, len(items), size))


class _Reaper:
    def __init__(self, *, run, prune_network, executable, scope, deadline, now, cancelled, identity, own_lock,
                 retention_days, wall_clock, paths_visible, extra_directories=None):
        self.runner, self.prune_network, self.executable, self.scope = run, prune_network, executable, scope
        self.deadline, self.now, self.cancelled, self.identity = deadline, now, cancelled, identity
        self.own_lock = own_lock
        self.retention_days, self.wall_clock, self.paths_visible = retention_days, wall_clock, paths_visible
        self.extra_directories = extra_directories or {}
        self.volumes = 0
        # Previous path-based scopes are never touched. The caller's own scope keeps its current project
        # stack (ordinary acquire logic); only its containers under another, legacy project name are
        # candidates, and only when the caller says how that scope's lease state is protected.
        self.excluded = set(getattr(scope, 'legacy_ids', ()))
        if own_lock is None:
            self.excluded.add(scope.id)
        self.scopes = self.containers = self.networks = 0
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
                      'labels': labels}
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
            project = labels.get(_PROJECT)
            if project not in accepted_projects(scope_id, labels.get(CHECKOUT_LABEL)):
                blocked.add(scope_id)
                continue
            if scope_id == self.scope.id and project == self.scope.project:
                continue
            groups.setdefault(scope_id, []).append(record)
        return {scope_id: group for scope_id, group in sorted(groups.items())
                if scope_id not in blocked and scope_id not in self.excluded}

    @staticmethod
    def ownership_directory(scope_id, group):
        """The journal directory that the recorded project directories prove.

        Each acquisition that creates services records its own retained snapshot
        directory, so a scope may span several, all beside the scope's journal.
        Anything else is unprovable.
        """
        recorded = {record['labels'].get(_WORKING_DIRECTORY) for record in group}
        pattern = re.escape(scope_id) + r'-assets-[A-Za-z0-9_]+'
        if any(type(working) is not str or not os.path.isabs(working) or not re.fullmatch(pattern, Path(working).name)
               for working in recorded):
            return None
        parents = {Path(working).parent for working in recorded}
        return parents.pop() if len(parents) == 1 else None

    def reap(self):
        self.reap_containers()
        self.reap_volumes()

    def reap_containers(self):
        identifiers = self.listing()
        if not identifiers:
            return
        from . import reporting_registry
        try:
            pinned = reporting_registry.enrolled_container_ids()
        except Exception as error:
            # Which containers an enrolled binding pins is unknown, so none may go.
            self.failure = type(error).__name__
            return
        for scope_id, group in self.candidates(self.inspect(identifiers)).items():
            self.check()
            journal = self.ownership_directory(scope_id, group)
            if (journal is None or any(record['status'] in _BUSY for record in group)
                    or any(record['id'] in pinned for record in group)):
                continue
            try:
                self.reap_scope(scope_id, [record['id'] for record in group], journal)
            except _Stop:
                raise
            except Exception as error:
                self.failure = type(error).__name__

    def reap_scope(self, scope_id, identifiers, journal):
        # Containers are the one place that still names this scope's journal directory, and
        # their removal leaves its volumes without any other pointer to it.
        scope_index.record(scope_id, journal, used=False)
        # A scope's journal lives beside its retained project directory. Holding its
        # lock excludes that scope's own acquisition while we remove its containers.
        # A checkout whose directory is gone has no journal, hence no lease.
        if isinstance(self.own_lock, tuple) and scope_id == self.scope.id and journal == self.own_lock[0]:
            # The caller already holds this very lock and has read the journal under it.
            guard = nullcontext(self.own_lock[1])
        else:
            if os.path.isdir(journal):
                guard = prerequisites.guarded_cleanup(journal, SimpleNamespace(id=scope_id), identity=self.identity,
                                                      deadline_seconds=1)
            elif self.paths_visible and _absent(journal):
                guard = nullcontext(frozenset())
            else:
                # Not provably gone (unreadable, or a host path this process cannot see): its lease is unknown.
                return
        with guard as leased:
            if leased:
                return
            fresh = self.inspect(identifiers)
            if (any(record['labels'].get(SCOPE_LABEL) != scope_id or record['status'] in _BUSY
                    for record in fresh)):
                return
            running = [record['id'] for record in fresh if record['running']]
            for batch in _chunks(running, _REMOVE_BATCH):
                self.run('stop', '--time', STOP_GRACE_SECONDS, *batch)
            for batch in _chunks(identifiers, _REMOVE_BATCH):
                self.run('rm', *batch)
                self.containers += len(batch)
            self.scopes += 1
            self.networks += len(self.prune_network(scope_id, self.deadline))


    def volume_inventory(self):
        """Fully labelled managed volume records by scope, as ``{scope id: [record, ...]}``."""
        names = self.run('volume', 'ls', '--quiet', '--filter', f'label={MANAGED_LABEL}=true').split()
        if len(set(names)) != len(names) or any(not _VOLUME.fullmatch(name) for name in names):
            raise ValueError('Invalid volume names.')
        owned = {}
        for batch in _chunks(names, _INSPECT_BATCH):
            records = json.loads(self.run('volume', 'inspect', *batch))
            if (not isinstance(records, list) or len(records) != len(batch)
                    or any(not isinstance(record, dict) for record in records)
                    or {record.get('Name') for record in records} != set(batch)):
                raise ValueError('Volume inventory could not be verified.')
            for record in records:
                scope_id = volume_owner(record)
                if scope_id is not None:
                    owned.setdefault(scope_id, []).append(record)
        return owned

    def referenced(self, name):
        """True when any container, running or not, mounts the volume."""
        identifiers = self.run('ps', '-a', '--no-trunc', '--filter', f'volume={name}', '--format', '{{.ID}}').split()
        if any(not prerequisites._ID.fullmatch(identifier) for identifier in identifiers):
            raise ValueError('Invalid container identifiers.')
        return bool(identifiers)

    def directories(self, scope_id):
        """The journal directories known for a scope, or ``None`` when its location is unknown."""
        recorded = scope_index.directories(scope_id)
        extra = self.extra_directories.get(scope_id, ())
        return None if recorded is None and not extra else tuple(dict.fromkeys((*(recorded or ()), *extra)))

    @contextmanager
    def lease_guard(self, scope_id, directories):
        """Hold every recorded journal lock and yield the container ids any live lease protects."""
        with ExitStack() as stack:
            leased = set()
            for directory in directories:
                if os.path.isdir(directory):
                    leased |= stack.enter_context(prerequisites.guarded_cleanup(
                        directory, SimpleNamespace(id=scope_id), identity=self.identity, deadline_seconds=1))
            yield frozenset(leased)

    def last_use(self, scope_id, directories):
        """The newest journal modification time (epoch seconds), or ``None`` when every directory is gone.

        Raises ``ValueError`` when any journal location cannot be judged: a directory that still exists
        without its journal, an unreadable file, or an absence this process cannot trust.
        """
        newest = None
        for directory in directories:
            try:
                moment = os.stat(directory / f'{scope_id}.json').st_mtime
            except FileNotFoundError:
                if self.paths_visible and _absent(directory):
                    continue
                raise ValueError('Scope journal is missing.') from None
            except OSError as error:
                raise ValueError('Scope journal cannot be read.') from error
            newest = moment if newest is None else max(newest, moment)
        return newest

    def unused_reason(self, scope_id, directories):
        """``'orphan'`` or ``'aged'`` when the scope's storage may go, else ``None``."""
        try:
            newest = self.last_use(scope_id, directories)
        except ValueError:
            return None
        if newest is None:
            # Journals are under artifacts/, which a clean or an unmounted drive removes without the
            # volumes being abandoned, so an orphan also waits out the retention period from its
            # last acquire (recorded in the scope index); no recorded time means unknown.
            if self.retention_days <= 0:
                return 'orphan'
            acquired = scope_index.modified(scope_id)
            if acquired is None:
                return None
            newest = acquired
            orphan = True
        else:
            orphan = False
        idle = (self.wall_clock() - datetime.fromtimestamp(newest, timezone.utc)).total_seconds()
        if self.retention_days > 0 and idle >= self.retention_days * 86400:
            return 'orphan' if orphan else 'aged'
        return None

    def reap_volumes(self, selected=None):
        """Remove unreferenced owned volumes of scopes without a live lease.

        With no ``selected`` set the orphan and age rule decides and the caller's own scope is left
        alone; with one, only those names are considered, on the operator's say-so, so only the
        references, the labels and the lease check apply.
        """
        for scope_id, records in sorted(self.volume_inventory().items()):
            if selected is None and (scope_id == self.scope.id or scope_id in self.excluded):
                continue
            names = [record['Name'] for record in records if selected is None or record['Name'] in selected]
            directories = self.directories(scope_id) if names else None
            if directories is None:
                continue
            self.check()
            try:
                self.reap_scope_volumes(scope_id, directories, names, rule=selected is None)
            except _Stop:
                raise
            except Exception as error:
                self.failure = type(error).__name__

    def reap_scope_volumes(self, scope_id, directories, names, *, rule):
        if all(self.referenced(name) for name in names):
            return
        with self.lease_guard(scope_id, directories) as leased:
            if leased:
                return
            reason = self.unused_reason(scope_id, directories) if rule else 'selected'
            if reason is None:
                return
            unused = [name for name in names if not self.referenced(name)]
            for batch in _chunks(unused, _REMOVE_BATCH):
                self.run('volume', 'rm', *batch)
                self.volumes += len(batch)
            if reason == 'orphan' and len(unused) == len(names):
                scope_index.forget(scope_id)


def _reaper(*, run, prune_network, executable, scope, deadline, now, cancelled, identity, own_lock,
            retention_days, wall_clock, host_paths_visible, extra_directories=None):
    return _Reaper(run=run, prune_network=prune_network, executable=executable, scope=scope,
                   deadline=deadline, now=now, cancelled=cancelled,
                   identity=identity or prerequisites.process_identity, own_lock=own_lock,
                   retention_days=retention_days,
                   wall_clock=wall_clock or (lambda: datetime.now(timezone.utc)),
                   paths_visible=host_paths_visible, extra_directories=extra_directories)


def _report(reaper, outcome):
    """Run one pass and turn whatever happened into a report and a value-free log; never raises."""
    diagnostic = ''
    try:
        outcome()
        if reaper.failure:
            diagnostic = 'incomplete: ' + reaper.failure
    except _Stop as stop:
        diagnostic = f'incomplete: {stop}'
    except Exception as error:
        diagnostic = 'incomplete: ' + type(error).__name__
    report = ReapReport(reaper.scopes, reaper.containers, reaper.volumes, reaper.networks, diagnostic)
    if report.scopes or report.volumes:
        _LOG.info('Managed resource reaper removed %d container(s) of %d scope(s), %d volume(s), %d network(s).',
                  report.containers, report.scopes, report.volumes, report.networks)
    if diagnostic:
        _LOG.warning('Managed resource reaper failed or was interrupted (%s); remaining resources were preserved.',
                     diagnostic)
    return report


def reap_unused_managed_resources(*, run, prune_network, executable, scope, deadline,
                                  retention_days=DEFAULT_RETENTION_DAYS, now=time.monotonic,
                                  cancelled=lambda: False, identity=None, own_lock=None, wall_clock=None,
                                  host_paths_visible=True):
    """Remove leaderless managed containers and unused volumes; report what was removed; never raises.

    ``run(arguments, deadline)`` executes one engine command and returns stdout.
    ``prune_network(scope_id, deadline)`` removes that scope's unreferenced network
    and returns the removed identifiers. The caller's own scope is never touched
    unless ``own_lock`` says how it is protected: ``'free'`` when the caller holds no
    lock on its journal (its lease state is read under the usual guard), or
    ``(directory, leased_ids)`` when the caller already holds that journal's lock.
    Even then only containers under a legacy project name are considered, never the
    caller's current stack, and its volumes are never removed by the age rule.
    ``retention_days`` of zero disables only the age rule for volumes; the orphan rule
    applies whenever ``host_paths_visible``.
    """
    reaper = _reaper(run=run, prune_network=prune_network, executable=executable, scope=scope, deadline=deadline,
                     now=now, cancelled=cancelled, identity=identity, own_lock=own_lock,
                     retention_days=retention_days, wall_clock=wall_clock, host_paths_visible=host_paths_visible)
    return _report(reaper, reaper.reap)


def list_managed_volumes(*, run, executable, scope, deadline, now=time.monotonic, cancelled=lambda: False):
    """Every fully labelled managed volume with whether a container references it; raises on failure."""
    reaper = _reaper(run=run, prune_network=None, executable=executable, scope=scope, deadline=deadline,
                     now=now, cancelled=cancelled, identity=None, own_lock=None,
                     retention_days=0, wall_clock=None, host_paths_visible=False)
    volumes = []
    for scope_id, records in sorted(reaper.volume_inventory().items()):
        for record in records:
            labels = record['Labels']
            volumes.append(SimpleNamespace(name=record['Name'], scope=scope_id, project=labels[_PROJECT],
                                           profile=labels[PROFILE_LABEL], in_use=reaper.referenced(record['Name'])))
    return tuple(volumes)


def remove_managed_volumes(*, run, executable, scope, names, deadline, ownership_directory, identity=None,
                           now=time.monotonic, cancelled=lambda: False):
    """Remove the operator's selected volumes after the same verification the age rule needs.

    Each must be fully labelled, referenced by no container, and its scope must hold no live lease;
    a scope whose journal location is unknown is left alone. The caller's own journal directory is
    always known.
    """
    reaper = _reaper(run=run, prune_network=None, executable=executable, scope=scope, deadline=deadline,
                     now=now, cancelled=cancelled, identity=identity, own_lock=None, retention_days=0,
                     wall_clock=None, host_paths_visible=False,
                     extra_directories={scope.id: (Path(ownership_directory),)})
    return _report(reaper, lambda: reaper.reap_volumes(frozenset(names)))
