"""Adversarial review regressions, using only synthetic configuration and owners."""
import asyncio
import json
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from textual.widgets import DataTable, Static

from api_agent.config import schema_from_dict
from api_agent.config.dotenv import read_dotenv, decode_value
from api_agent.config.editor import DotenvEditor
from api_agent.config.session import ConfigSession
from api_agent.runtime.bootstrap import _bind_runtime_directory
from api_agent.runtime.resource_pack import load_launch_context
from api_agent.runtime.worktrees import WorktreeGateError, _git, verify_worktrees
from api_agent.testing.collection import TestIdentity
from api_agent.testing.selection import SelectionModel
from test_tui import fixture


def make_context(tmp_path, launcher_pack=True):
    base, pack = tmp_path / 'api-agent', tmp_path / 'pack'
    base.mkdir(); pack.mkdir()
    (pack / 'cfg.toml').write_text('version=1\n')
    (pack / 'pack.toml').write_text('version=1\nid="pack"\nconfig="cfg.toml"\n')
    return load_launch_context(base, pack, launcher_root=pack if launcher_pack else base)


@pytest.mark.parametrize('source', ['default', 'dotenv', 'environment', 'cli'])
def test_bootstrap_binding_ignores_base_dotenv_and_retains_actual_source(tmp_path, source):
    context = make_context(tmp_path)
    (context.base_root / '.env').write_text('API_AGENT_DIR=""\n')
    argv, env = [], {}
    if source == 'dotenv':
        (context.pack_root / '.env').write_text('API_AGENT_DIR=../api-agent\n')
    elif source == 'environment':
        env['API_AGENT_DIR'] = '../api-agent'
    elif source == 'cli':
        argv = ['--api-agent-dir=../api-agent']
    bound = _bind_runtime_directory(context, argv, env)
    schema = schema_from_dict({'version':1, 'fields': {
        'API_AGENT_DIR': {'type':'path','anchor':'pack_root','default':'../api-agent','pack_only':True}}})
    config = ConfigSession(bound, schema, argv=argv, environ=env)
    result = config.result()
    assert not result.errors
    assert result.values['API_AGENT_DIR'] == context.base_root
    field = result.fields['API_AGENT_DIR']
    assert field.source == {'default':'shared_default','dotenv':'../pack/.env','environment':'environment','cli':'cli'}[source]
    assert field.explicit == (source != 'default')
    # Changes after bootstrap cannot misrepresent which checkout is executing.
    (context.pack_root / '.env').write_text('API_AGENT_DIR=/another-runtime\n')
    assert config.result().values['API_AGENT_DIR'] == context.base_root


def test_direct_source_launch_does_not_advertise_ignored_checkout_choice(tmp_path):
    context = make_context(tmp_path, launcher_pack=False)
    bound = _bind_runtime_directory(context, [], {'API_AGENT_DIR':'/ignored'})
    assert bound.bootstrap_source == 'source_checkout'
    with pytest.raises(ValueError, match='resource-pack launcher'):
        _bind_runtime_directory(context, ['--api-agent-dir=/different'], {})
    with pytest.raises(ValueError, match='nonempty'):
        _bind_runtime_directory(context, ['--api-agent-dir='], {})
    assert _bind_runtime_directory(context, ['--api-agent-dir', str(context.base_root)], {}).bootstrap_source == 'cli'


def test_bootstrap_rejects_selection_changed_since_source_loading(tmp_path):
    context = make_context(tmp_path)
    with pytest.raises(ValueError, match='nonempty'):
        _bind_runtime_directory(context, [], {'API_AGENT_DIR':' '})
    with pytest.raises(ValueError, match='running checkout'):
        _bind_runtime_directory(context, [], {'API_AGENT_DIR':'../another-runtime'})


def test_default_acceptance_round_trips_declared_path_enum_token(tmp_path):
    context = SimpleNamespace(base_root=tmp_path,pack_root=tmp_path,dotenv_path=tmp_path/'.env',
                              anchors={'base_root':tmp_path},external_pack=False)
    schema = schema_from_dict({'version':1,'fields':{'LOCATION': {
        'type':'path','anchor':'base_root','enum':['one','two'],'default':'one'}}})
    config = ConfigSession(context,schema,environ={})
    result = config.save_defaults(['LOCATION'])
    assert not result.errors and result.values['LOCATION'] == tmp_path/'one'
    assert read_dotenv(context.dotenv_path)['LOCATION'] == 'one'
    assert config.external_value('LOCATION', tmp_path/'two') == 'two'
    assert config.external_value('LOCATION', tmp_path/'other') == str(tmp_path/'other')


def test_tui_accept_default_and_editor_show_declared_default(tmp_path):
    async def scenario():
        app = fixture(tmp_path, fields={'LOCATION': {
            'type':'path','anchor':'base_root','enum':['one','two'],'default':'one'}})
        app.context.anchors={'base_root':tmp_path}
        async with app.run_test(size=(100,35)) as pilot:
            await pilot.pause()
            assert app.screen.kind == 'defaults'
            await app.accept_default()
            await pilot.pause()
            assert read_dotenv(tmp_path/'.env')['LOCATION'] == 'one'
            app.edit_setting()
            await pilot.pause()
            assert 'Default: one.' in str(app.screen.query_one('#body',Static).content)
            assert app.screen.query_one('#config-value').value == 'one'
    asyncio.run(scenario())


@pytest.mark.parametrize('error', [False, True])
@pytest.mark.parametrize('cancelled', [False, True])
def test_stale_back_to_selection_cannot_replace_new_operation(tmp_path, error, cancelled):
    async def scenario():
        app = fixture(tmp_path)
        old = TestIdentity('base::old.py::test_old','base','old.py','old.py::test_old')
        new = TestIdentity('base::new.py::test_new','base','new.py','new.py::test_new')
        app.selection = SelectionModel([old])
        app.show = Mock(); app.show_selection = Mock()
        entered, release = asyncio.Event(), asyncio.Event()
        async def discover(cancel_event):
            entered.set(); await release.wait()
            if error: raise ValueError('obsolete error')
            return [old]
        app.discover_tests = discover
        refresh = asyncio.create_task(app.back_to_selection())
        await entered.wait()
        if cancelled: app.collection_cancel.set()
        import threading
        app.collection_cancel = threading.Event()
        app.selection = SelectionModel([new])
        release.set(); await refresh
        assert [test.id for test in app.selection.tests] == [new.id]
        app.show_selection.assert_not_called()
        assert app.show.call_count == 1  # Only the initial refreshing screen.
    asyncio.run(scenario())


def test_git_timeout_is_best_effort_and_never_prompts(tmp_path):
    with patch('api_agent.runtime.worktrees.subprocess.run',side_effect=subprocess.TimeoutExpired('git',5)) as run:
        with pytest.raises(WorktreeGateError): _git(tmp_path,'status')
        assert run.call_args.kwargs['timeout'] == 5
        stdin, stdout = Mock(), Mock()
        verify_worktrees([tmp_path],stdin=stdin,stdout=stdout)
        stdin.readline.assert_not_called(); stdout.write.assert_not_called()


def test_escaped_dotenv_is_portable_and_legacy_backslashes_do_not_change(tmp_path):
    path=tmp_path/'.env'
    path.write_text('LEGACY="C:\\new\\thing"\n')
    value=json.dumps({'user':'O\'Connor "quoted"','path':r'C:\new\thing','literal':'${HOME}'},ensure_ascii=False)
    DotenvEditor(path).update({'NEW':value})
    assert read_dotenv(path) == {'LEGACY':r'C:\new\thing','NEW':value}
    line=path.read_text().splitlines()[1]
    # Independent shell lexical parsing agrees with the emitted dotenv quoting;
    # no shell evaluation or expansion is performed.
    assert shlex.split(line,comments=True) == ['NEW='+value]
    for bad in ('42 # api-agent-escaped','"unclosed # api-agent-escaped'):
        with pytest.raises(ValueError,match='escaped'): decode_value(bad)


def test_editor_description_tracks_conditional_default_and_no_default(tmp_path):
    app = fixture(tmp_path, fields={
        'MODE': {'type':'str'},
        'TARGET': {'type':'str','conditional_defaults':[
            {'when':{'MODE':'first'},'value':'one'},
            {'when':{'MODE':'second'},'value':'two'}]}})
    app.prompt_name = 'TARGET'
    assert 'Default:' not in app.setting_description()
    app.session.set_value('MODE','second')
    assert 'Default: two.' in app.setting_description()
    app.session.set_value('MODE','first')
    assert 'Default: one.' in app.setting_description()
