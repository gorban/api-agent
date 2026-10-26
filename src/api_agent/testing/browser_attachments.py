"""Bounded immutable UI evidence across protected worker/recording IPC."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4

from .browser_evidence import MAX_IMAGE_BYTES, PNG_SIGNATURE

_TITLE = re.compile(r'UI (?:success|failure) context (?:[0-9]|1[0-6]) page (?:[0-9]|[12][0-9]|3[0-2])')
_NAME = re.compile(r'[0-9a-f]{32}\.evidence')


def validate_owner(owner, boundary=None):
    """Accept only explicit canonical turn identity, optionally at a live boundary."""
    if (type(owner) is not dict or set(owner) != {'kind', 'conversation_id', 'turn_id'}
            or owner['kind'] != 'turn'):
        raise ValueError('Invalid browser attachment owner')
    for key in ('conversation_id', 'turn_id'):
        value = owner[key]
        if type(value) is not str:
            raise ValueError('Invalid browser attachment identity')
        try:
            canonical = str(UUID(value))
        except ValueError as error:
            raise ValueError('Invalid browser attachment identity') from error
        if canonical != value:
            raise ValueError('Invalid browser attachment identity')
    if boundary is not None:
        from .evidence import _safe_owner
        _safe_owner(owner, boundary)
    return dict(owner)


def publish_attachment(attachment, *, owner=None):
    """Freeze safe provider bytes before temporary browser/test teardown."""
    event_path = os.environ.get('API_AGENT_INTEGRATION_EVENTS')
    node = os.environ.get('PYTEST_CURRENT_TEST')
    if not event_path or not node:
        return
    title, content, mime = attachment
    if not _valid(title, content, mime):
        raise ValueError('Invalid browser attachment')
    if owner is not None:
        owner = validate_owner(owner)
    name = uuid4().hex + '.evidence'
    path = Path(event_path).parent / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(content)
    row = {'kind':'browser_attachment', 'node':node.rsplit(' ',1)[0], 'data':{
        'name':name, 'title':title, 'mime':mime, 'sha256':hashlib.sha256(content).hexdigest()}}
    if owner is not None:
        row['data']['owner'] = owner
    with open(event_path, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(row, allow_nan=False) + '\n')


def _valid(title, content, mime):
    if type(title) is not str or not _TITLE.fullmatch(title) or type(content) is not bytes:
        return False
    if mime == 'image/png':
        return content.startswith(PNG_SIGNATURE) and len(content) <= MAX_IMAGE_BYTES
    if mime != 'application/json' or len(content) > 1024:
        return False
    try:
        row = json.loads(content)
    except (ValueError, UnicodeError):
        return False
    return (type(row) is dict and set(row) in ({'status','phase','context','page'}, {'status','phase','context','page','reason'})
            and ('reason' not in row or row['reason'] == 'budget_exceeded')
            and row['status'] == 'unavailable' and row['phase'] in ('success','failure')
            and type(row['context']) is int and 0 <= row['context'] <= 16
            and type(row['page']) is int and 0 <= row['page'] <= 32)


def validate_descriptor(row):
    """Validate only the fixed transport descriptor, before touching files."""
    if (type(row) is not dict or set(row) not in ({'name','title','mime','sha256'}, {'name','title','mime','sha256','owner'})
            or type(row['name']) is not str or not _NAME.fullmatch(row['name'])
            or type(row['sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',row['sha256'])):
        raise ValueError('Invalid browser attachment descriptor')
    if 'owner' in row:
        validate_owner(row['owner'])


def read_attachment(row, directory):
    """Validate immutable attachment identity without following links or FIFOs."""
    validate_descriptor(row)
    path = Path(directory) / row['name']
    flags = os.O_RDONLY | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_NONBLOCK',0)
    if path.is_symlink():
        raise ValueError('Invalid browser attachment file')
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_IMAGE_BYTES
                or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise ValueError('Invalid browser attachment file')
        content = stream.read(MAX_IMAGE_BYTES + 1)
    if hashlib.sha256(content).hexdigest() != row['sha256'] or not _valid(row['title'],content,row['mime']):
        raise ValueError('Invalid browser attachment content')
    return row['title'], content, row['mime']
