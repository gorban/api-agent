"""Bounded file reads shared by runtime admission paths."""
import os
from pathlib import Path
import stat

MAX_RESOURCE_BYTES = 8 * 1024 * 1024


def read_bounded(path, root):
    """Read one root-relative regular file without following links."""
    path, root = Path(path), Path(root)
    try:
        relative = path.relative_to(root)
        if not relative.parts or any(part in ('', '.', '..') for part in relative.parts):
            raise ValueError('invalid root-relative path')
        directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in root.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            for part in relative.parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError('resource is not a regular file')
                if before.st_size > MAX_RESOURCE_BYTES:
                    raise ValueError('resource exceeds 8 MiB')
                chunks, remaining = [], MAX_RESOURCE_BYTES + 1
                while remaining:
                    chunk = os.read(descriptor, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b''.join(chunks)
                after = os.fstat(descriptor)
                if len(data) > MAX_RESOURCE_BYTES:
                    raise ValueError('resource exceeds 8 MiB')
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                    raise ValueError('resource changed during read')
                from api_agent.config.source_reads import record_file
                record_file(path, root, data)
                return data
            finally:
                os.close(descriptor)
        finally:
            os.close(directory)
    except (OSError, ValueError) as error:
        from api_agent.config.source_reads import record_unreadable
        from api_agent.workflows.model import LayoutError
        record_unreadable(path, root)
        raise LayoutError(f'{path}:1: unsafe or unavailable resource: {error}') from error
