from types import SimpleNamespace

import pytest


def test_read_bounded_reads_a_regular_nested_file(tmp_path):
    from api_agent.workflows.safe_read import read_bounded

    source = tmp_path / 'nested' / 'source.py'
    source.parent.mkdir()
    source.write_bytes(b'content')

    assert read_bounded(source, tmp_path) == b'content'


@pytest.mark.parametrize('kind', ['outside', 'root', 'symlink', 'directory', 'oversize'])
def test_read_bounded_rejects_unsafe_or_invalid_resources(tmp_path, monkeypatch, kind):
    from api_agent.workflows import safe_read
    from api_agent.workflows.model import LayoutError

    source = tmp_path / 'source.py'
    source.write_bytes(b'content')
    if kind == 'outside':
        source = tmp_path.parent / 'outside.py'
        source.write_bytes(b'content')
    elif kind == 'root':
        source = tmp_path
    elif kind == 'symlink':
        link = tmp_path / 'link.py'
        link.symlink_to(source)
        source = link
    elif kind == 'directory':
        source = tmp_path / 'directory'
        source.mkdir()
    else:
        monkeypatch.setattr(safe_read, 'MAX_RESOURCE_BYTES', 1)

    with pytest.raises(LayoutError, match='unsafe or unavailable resource'):
        safe_read.read_bounded(source, tmp_path)


def test_read_bounded_rejects_data_that_grows_past_its_initial_bound(tmp_path, monkeypatch):
    from api_agent.workflows import safe_read
    from api_agent.workflows.model import LayoutError

    source = tmp_path / 'source.py'
    source.write_bytes(b'content')
    original, calls = safe_read.os.fstat, []
    monkeypatch.setattr(safe_read, 'MAX_RESOURCE_BYTES', 1)

    def initially_small(descriptor):
        result = original(descriptor)
        calls.append(result)
        if len(calls) == 1:
            return SimpleNamespace(
                st_dev=result.st_dev, st_ino=result.st_ino, st_size=0,
                st_mtime_ns=result.st_mtime_ns, st_mode=result.st_mode)
        return result

    monkeypatch.setattr(safe_read.os, 'fstat', initially_small)

    with pytest.raises(LayoutError, match='resource exceeds 8 MiB'):
        safe_read.read_bounded(source, tmp_path)


def test_read_bounded_rejects_changes_during_read(tmp_path, monkeypatch):
    from api_agent.workflows import safe_read
    from api_agent.workflows.model import LayoutError

    source = tmp_path / 'source.py'
    source.write_bytes(b'content')
    original, calls = safe_read.os.fstat, []

    def changed(descriptor):
        result = original(descriptor)
        calls.append(result)
        if len(calls) == 2:
            return SimpleNamespace(
                st_dev=result.st_dev, st_ino=result.st_ino, st_size=result.st_size,
                st_mtime_ns=result.st_mtime_ns + 1, st_mode=result.st_mode)
        return result

    monkeypatch.setattr(safe_read.os, 'fstat', changed)

    with pytest.raises(LayoutError, match='resource changed during read'):
        safe_read.read_bounded(source, tmp_path)
