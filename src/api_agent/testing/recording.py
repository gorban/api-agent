"""Deterministic Allure result files for interactive integration and E2E runs."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

from api_agent.telemetry.privacy import ContentPolicy, EmissionBoundary
from .contracts import TestResult
from .collection import TestIdentity


def _timestamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('Run timestamps must be timezone-aware')
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _safe_component(value):
    if not isinstance(value, str) or not value or value in {'.', '..'} or any(c in value for c in '/\\\0'):
        raise ValueError('Invalid identity component')
    return value


@dataclass(frozen=True)
class RunIdentity:
    run_id: str
    repo: str
    mode: str
    started_at: datetime

    def __post_init__(self):
        if str(UUID(self.run_id)) != self.run_id:
            raise ValueError('run_id must be a canonical UUID')
        _safe_component(self.repo)
        if self.mode not in {'integration', 'e2e'}:
            raise ValueError('Only integration and E2E runs may be recorded')
        _timestamp(self.started_at)

    @classmethod
    def create(cls, repo, mode):
        return cls(str(uuid4()), repo, mode, datetime.now(timezone.utc))


class RunRecorder:
    def __init__(self, directory, identity: RunIdentity, *, clock=None, evidence_boundary=None):
        if not isinstance(identity, RunIdentity):
            raise TypeError('A valid RunIdentity is required')
        self.identity = identity
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._files = set()
        self._results = []
        self._finalized = False
        self._write_failed = False
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._include_trace = evidence_boundary is not None
        self._evidence = evidence_boundary or EmissionBoundary(ContentPolicy(True))

    def _safe_text(self, value):
        return self._evidence.export({'value': value}).get('value', '[REDACTED]')

    def _write(self, name, data):
        path = self.directory / name
        try:
            with path.open('xb') as stream:
                stream.write(data)
        except OSError:
            self._write_failed = True
            raise
        self._files.add(name)
        return name

    def record_artifact(self, name, content):
        """Add an explicit suite artifact, such as usage CSV, before finalization."""
        if self._finalized:
            raise RuntimeError('Run already finalized')
        _safe_component(name)
        if name == 'manifest.json' or type(content) is not bytes:
            raise ValueError('Artifact requires a distinct filename and bytes')
        return self._write(name, content)

    def record_test(self, result: TestResult, attempt=1, attachments=()):
        if self._finalized:
            raise RuntimeError('Run already finalized')
        if not isinstance(result, TestResult) or result.identity.repo != self.identity.repo:
            raise ValueError('Test result belongs to another repository')
        if type(attempt) is not int or attempt < 1:
            raise ValueError('Attempt must be a positive integer')
        status = {'passed': 'passed', 'failed': 'failed', 'broken': 'broken',
                  'skipped': 'skipped', 'cancelled': 'broken', 'interrupted': 'broken',
                  'error': 'broken'}.get(result.status)
        if status is None:
            raise ValueError('Unsupported result status')
        test = result.identity
        if not isinstance(test.file, str) or not test.file or not isinstance(test.node_id, str) or not test.node_id:
            raise ValueError('Incomplete test identity')
        attachment_rows = []
        attachments = tuple(attachments)
        for title, content, mime in attachments:
            if not isinstance(title, str) or not title or type(content) is not bytes or not isinstance(mime, str) or not mime:
                raise ValueError('Attachments require a title, bytes and MIME type')
        for title, content, mime in attachments:
            name = self._write(f'{uuid4()}-attachment', content)
            attachment_rows.append({'name': self._safe_text(title), 'source': name, 'type': mime})
        name = self._safe_text(test.node_id.split('::')[-1])
        ended = self._clock()
        _timestamp(ended)
        stopped_ms = int(ended.timestamp() * 1000)
        started_ms = stopped_ms - int(max(0, result.duration) * 1000)
        row = {
            'uuid': str(uuid4()), 'historyId': hashlib.sha256(f'{self.identity.repo}:{self.identity.mode}:{test.node_id}'.encode()).hexdigest(),
            'name': name, 'fullName': self._safe_text(test.node_id), 'status': status,
            'labels': [{'name': 'parentSuite', 'value': self._safe_text(self.identity.repo)},
                       {'name': 'suite', 'value': self.identity.mode},
                       {'name': 'subSuite', 'value': self._safe_text(Path(test.file).name)}],
            'parameters': [{'name': 'attempt', 'value': str(attempt)}],
            'attachments': attachment_rows,
            'start': started_ms, 'stop': stopped_ms,
        }
        if result.status in {'cancelled', 'interrupted'}:
            row['statusDetails'] = {'message': 'Test interrupted'}
        elif status in {'failed', 'broken'}:
            row['statusDetails'] = {'message': 'Test failed' if status == 'failed' else 'Test setup or execution failed'}
        if result.traceback and self._include_trace:
            row.setdefault('statusDetails', {})['trace'] = self._safe_text(result.traceback)
        filename = self._write(f'{row["uuid"]}-result.json', json.dumps(row, sort_keys=True).encode())
        self._results.append({'file': filename, 'status': result.status})
        return filename

    def finalize(self, metadata=None, *, status=None):
        if self._finalized:
            raise RuntimeError('Run already finalized')
        if self._write_failed:
            raise RuntimeError('Run has an incomplete artifact write')
        statuses = {row['status'] for row in self._results}
        derived = ('interrupted' if statuses & {'cancelled', 'interrupted'} else
                   'failed' if statuses & {'failed', 'broken', 'error'} else 'completed')
        if status is None:
            status = derived
        if status == 'completed' and derived != 'completed':
            raise ValueError('Failed or interrupted tests cannot finalize as completed')
        if not self._results and status == 'completed':
            raise ValueError('A completed run requires at least one test result')
        if status not in {'completed', 'interrupted', 'failed'}:
            raise ValueError('Invalid run status')
        if ((status == 'failed' and derived == 'completed') or
                (status == 'interrupted' and derived != 'interrupted')):
            setup = TestIdentity(f'{self.identity.repo}::__suite__', self.identity.repo,
                                 '__suite__', '__suite__::setup')
            self.record_test(TestResult(setup, 'interrupted' if status == 'interrupted' else 'broken'))
        boundary = EmissionBoundary(assigned_ids=(self.identity.run_id,))
        safe_metadata = boundary.export(metadata or {})
        files = {name: hashlib.sha256((self.directory / name).read_bytes()).hexdigest()
                 for name in sorted(self._files)}
        manifest = {'schema': 1, 'run_id': self.identity.run_id, 'repo': self.identity.repo,
                    'mode': self.identity.mode, 'started_at': _timestamp(self.identity.started_at),
                    'finished_at': _timestamp(datetime.now(timezone.utc)), 'status': status,
                    'files': files, 'metadata': safe_metadata}
        self._write('manifest.json', json.dumps(manifest, sort_keys=True).encode())
        self._finalized = True
        return manifest
