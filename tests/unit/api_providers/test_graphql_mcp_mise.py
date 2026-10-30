"""Cold lazy shims are replaced by selected executable paths at use."""
import os
from pathlib import Path
import asyncio
import threading

import pytest

from api_agent.api_providers.graphql_mcp import assets, owner
from api_agent.runtime import mise_tools


@pytest.fixture
def executables(tmp_path, monkeypatch):
    shim_dir, real_dir = tmp_path / 'shims', tmp_path / 'real'
    shim_dir.mkdir()
    real_dir.mkdir()
    mise = tmp_path / 'mise'
    mise.write_text('synthetic')
    mise.chmod(0o755)
    for name in ('node', 'npm'):
        (shim_dir / name).symlink_to(mise)
        executable = real_dir / name
        executable.write_text('synthetic')
        executable.chmod(0o755)
    monkeypatch.setenv('API_AGENT_MISE', '1')
    monkeypatch.setenv('PATH', str(shim_dir))
    requested = []
    def prepare(*names, **kwargs):
        requested.append(names)
        os.environ['PATH'] = str(real_dir) + os.pathsep + str(shim_dir)
    monkeypatch.setattr(mise_tools, 'ensure_tools', prepare)
    return shim_dir, real_dir, mise, requested


@pytest.mark.asyncio
async def test_default_node_shim_is_replaced_before_subprocess(executables, monkeypatch, tmp_path):
    _, real_dir, mise, requested = executables
    transport = owner.GraphQLMcpOwner('http://localhost/graphql')
    assert transport._node == str(mise)
    monkeypatch.setattr(assets, 'installed_script', lambda _: tmp_path / 'script.js')
    command = []
    async def spawn(*args, **kwargs):
        command.extend(args)
        raise OSError('synthetic spawn failure')
    monkeypatch.setattr(owner.asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(owner.GraphQLMcpError, match='process_start'):
        await transport._start()
    assert requested == [('node', 'npm')]
    assert command[0] == str(real_dir / 'node')


@pytest.mark.asyncio
async def test_managed_missing_node_is_prepared_before_subprocess(executables, monkeypatch, tmp_path):
    _, real_dir, _, requested = executables
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    transport = owner.GraphQLMcpOwner('http://localhost/graphql')
    assert transport._node is None
    monkeypatch.setattr(assets, 'installed_script', lambda _: tmp_path / 'script.js')
    command = []
    async def spawn(*args, **kwargs):
        command.extend(args)
        raise OSError('synthetic spawn failure')
    monkeypatch.setattr(owner.asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(owner.GraphQLMcpError, match='process_start'):
        await transport._start()
    assert requested == [('node', 'npm')]
    assert command[0] == str(real_dir / 'node')


def test_unmanaged_or_explicit_missing_node_rejected_at_construction(executables, monkeypatch, tmp_path):
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    with pytest.raises(owner.GraphQLMcpError, match='node_missing'):
        owner.GraphQLMcpOwner('http://localhost/graphql', node='/missing/node')
    monkeypatch.delenv('API_AGENT_MISE')
    with pytest.raises(owner.GraphQLMcpError, match='node_missing'):
        owner.GraphQLMcpOwner('http://localhost/graphql')


@pytest.mark.asyncio
async def test_failed_node_preparation_is_classified(executables, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('private installer output')
    monkeypatch.setattr(mise_tools, 'ensure_tools', fail)
    transport = owner.GraphQLMcpOwner('http://localhost/graphql')
    with pytest.raises(owner.GraphQLMcpError, match='node_preparation'):
        await transport._start()


@pytest.mark.asyncio
async def test_node_still_missing_after_preparation_is_rejected(executables, monkeypatch, tmp_path):
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    monkeypatch.setattr(mise_tools, 'ensure_tools', lambda *args, **kwargs: None)
    transport = owner.GraphQLMcpOwner('http://localhost/graphql')
    with pytest.raises(owner.GraphQLMcpError, match='node_missing'):
        await transport._start()


@pytest.mark.asyncio
async def test_failed_npm_preparation_is_classified(executables, monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise OSError('private installer output')
    monkeypatch.setattr(mise_tools, 'ensure_tools', fail)
    with pytest.raises(owner.GraphQLMcpError, match='npm_preparation'):
        await assets.prepare_assets(tmp_path / 'asset-output')


@pytest.mark.asyncio
async def test_cancelled_npm_preparation_is_drained(executables, monkeypatch, tmp_path):
    started, observed, release, finished = (threading.Event() for _ in range(4))
    def prepare(*args, cancellation, **kwargs):
        started.set()
        assert cancellation.wait(5)
        observed.set()
        assert release.wait(5)
        finished.set()
    monkeypatch.setattr(mise_tools, 'ensure_tools', prepare)
    task = asyncio.create_task(assets.prepare_assets(tmp_path / 'asset-output'))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    assert await asyncio.to_thread(observed.wait, 5)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_cancelled_node_preparation_is_drained(executables, monkeypatch):
    started, observed, release, finished = (threading.Event() for _ in range(4))
    def prepare(*args, cancellation, **kwargs):
        started.set()
        assert cancellation.wait(5)
        observed.set()
        assert release.wait(5)
        finished.set()
    monkeypatch.setattr(mise_tools, 'ensure_tools', prepare)
    transport = owner.GraphQLMcpOwner('http://localhost/graphql')
    task = asyncio.create_task(transport._start())
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    assert await asyncio.to_thread(observed.wait, 5)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_default_npm_shim_is_replaced_before_asset_install(executables, monkeypatch, tmp_path):
    _, real_dir, _, requested = executables
    command = []
    async def spawn(*args, **kwargs):
        command.extend(args)
        raise OSError('synthetic spawn failure')
    monkeypatch.setattr(assets.asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(owner.GraphQLMcpError, match='asset_install'):
        await assets.prepare_assets(tmp_path / 'asset-output')
    assert requested == [('node', 'npm')]
    assert command[0] == str(real_dir / 'npm')


@pytest.mark.asyncio
async def test_explicit_node_path_does_not_acquire_managed_tools(executables, monkeypatch, tmp_path):
    _, real_dir, _, requested = executables
    transport = owner.GraphQLMcpOwner('http://localhost/graphql', node=str(real_dir / 'node'))
    monkeypatch.setattr(assets, 'installed_script', lambda _: tmp_path / 'script.js')
    async def spawn(*args, **kwargs):
        raise OSError('synthetic spawn failure')
    monkeypatch.setattr(owner.asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(owner.GraphQLMcpError, match='process_start'):
        await transport._start()
    assert requested == []


@pytest.mark.asyncio
async def test_explicit_npm_path_does_not_acquire_managed_tools(executables, monkeypatch, tmp_path):
    _, real_dir, _, requested = executables
    command = []
    async def spawn(*args, **kwargs):
        command.extend(args)
        raise OSError('synthetic spawn failure')
    monkeypatch.setattr(assets.asyncio, 'create_subprocess_exec', spawn)
    with pytest.raises(owner.GraphQLMcpError, match='asset_install'):
        await assets.prepare_assets(tmp_path / 'asset-output', npm=str(real_dir / 'npm'))
    assert requested == []
    assert command[0] == str(real_dir / 'npm')
