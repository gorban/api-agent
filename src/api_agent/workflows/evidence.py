"""Allowlisted structural observations for recorded integration evidence."""
import json
import math
import os
import re
from .contrived import strict_json
from api_agent.telemetry.privacy import EmissionBoundary, ContentPolicy

_KEYS = frozenset('fingerprint_version sampling_method provider_identity entrypoint source_sha256 trial dependency_versions python_version langgraph langchain-core jsonschema markdown-it-py httpx records route backend auth_mode backend_identity user_input_hash pyproject.toml uv.lock events event node component source target attempt max_passes elapsed kind accepted runs nodes seen complete stopping_reason coverage calibration trials hits bounds minimum_runs safety_margin minimum_missing_node_bound max_runs missing_node_bound confidence assumption target model effective_settings sources routes scope_id parent_id owner scope started_ms stopped_ms timeout_key status outcome start stop started stopped start_ms stop_ms timeout declared_timeout effective_timeout timeout_source provenance duration usage input_tokens output_tokens cached_tokens cached_input_tokens cache_write_tokens reasoning_tokens total_tokens llm_timeout node_timeout component_timeout graph_timeout temperature max_output_tokens cache_policy reasoning_effort retries api_mode first second skip next_a next_b graph_id definition_id'.split())
_TEXT = re.compile(r'[A-Za-z0-9_./: -]{1,256}\Z')


def sanitize(value):
    """Never serialize producer inputs, output text, feedback or credentials."""
    if type(value) is dict:
        return {key: sanitize(item) for key, item in value.items()
                if type(key) is str and (key in _KEYS or key.endswith('_draft') or key.endswith('_format') or key.endswith('_guard') or key.startswith(('demo/contrived/', 'demo/cfg/', 'src/api_agent/', 'tests/integration/')))}
    if type(value) in (list, tuple):
        return [sanitize(item) for item in value]
    if value is None or type(value) in (int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is str and _TEXT.fullmatch(value):
        return value
    return None


def publish(data):
    path = os.environ.get('API_AGENT_INTEGRATION_EVENTS')
    node = os.environ.get('PYTEST_CURRENT_TEST')
    if path and node:
        safe = EmissionBoundary(ContentPolicy(True)).export(sanitize(data))
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(kind='workflow_evidence', node=node.rsplit(' ',1)[0], data=safe), allow_nan=False)+'\n')


def attachment(event, boundary):
    row = strict_json(event.text)
    if type(row) is not dict:
        raise ValueError('Workflow evidence must be an object')
    return ('Workflow execution evidence', json.dumps(boundary.content_export(sanitize(row)), indent=2, allow_nan=False).encode(), 'application/json')


# The receipt protects against accidental edits and detached/copied JSON, not an
# adversary controlling the same OS account and its signing key.
from contextlib import contextmanager
from pathlib import Path
import hashlib
import hmac
import secrets
import tempfile


def _canonical(report):
    return json.dumps(report, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


@contextmanager
def reserve_calibration(destination):
    """Hold exclusive pilot ownership before provider setup or graph execution."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or _key_path(destination).exists():
        raise FileExistsError('Calibration destination already exists; choose a new path')
    lock = destination.with_name(destination.name + '.reservation')
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.close(descriptor)
        if destination.exists():
            raise FileExistsError('Calibration destination became occupied')
        yield destination
    finally:
        lock.unlink()


def _key_path(destination):
    return Path(destination).with_name(Path(destination).name + '.receipt-key')


def store_calibration(destination, report):
    """Finalize immutable JSON and its local integrity receipt under reservation."""
    destination = Path(destination)
    if not destination.with_name(destination.name + '.reservation').is_file():
        raise ValueError('Calibration storage requires exclusive pilot ownership')
    from .sampling import validate_calibration, DECLARED
    validate_calibration(report, DECLARED, {})
    key = secrets.token_bytes(32)
    key_path = _key_path(destination)
    descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(key)
        stream.flush()
        os.fsync(stream.fileno())
    signed = dict(report, receipt=hmac.new(key, _canonical(report), hashlib.sha256).hexdigest())
    with tempfile.NamedTemporaryFile(mode='wb', dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(_canonical(signed))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, destination)
    finally:
        temporary.unlink()
    return signed


def read_calibration(destination):
    """Verify local finalized provenance before consuming statistical claims."""
    destination = Path(destination)
    report = strict_json(destination.read_text())
    if type(report) is not dict or type(report.get('receipt')) is not str:
        raise ValueError('Calibration requires a finalized local receipt')
    receipt = report.pop('receipt')
    key = _key_path(destination).read_bytes()
    if len(key) != 32 or not hmac.compare_digest(receipt, hmac.new(key, _canonical(report), hashlib.sha256).hexdigest()):
        raise ValueError('Calibration local receipt does not verify')
    return report
