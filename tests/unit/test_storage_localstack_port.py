import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from api_agent.testing.storage import RunStorage


@pytest.mark.parametrize('endpoint,port,expected', [
    ('http://localhost:14566',14566,{'aws_access_key_id':'test','aws_secret_access_key':'test'}),
    ('http://127.0.0.1:14566/',14566,{'aws_access_key_id':'test','aws_secret_access_key':'test'}),
    ('http://localhost:14566',4566,{}),
    ('http://localhost:4566',14566,{}),
    ('http://localhost:14566/path',14566,{}),
    ('http://user:password@localhost:14566',14566,{}),
    ('http://remote.example:14566',14566,{}),
    ('https://localhost:14566',14566,{}),
    ('http://localhost:14566?query=yes',14566,{}),
    ('http://localhost:14566#fragment',14566,{}),
])
def test_only_configured_localstack_endpoint_gets_synthetic_credentials(monkeypatch,endpoint,port,expected):
    client=Mock(return_value='client')
    monkeypatch.setitem(sys.modules,'boto3',SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules,'botocore.config',SimpleNamespace(Config=lambda **kwargs:kwargs))
    result=RunStorage('bucket',endpoint_url=endpoint,localstack_port=port)._client()
    assert result=='client'
    options=client.call_args.kwargs
    assert {key:value for key,value in options.items() if key.startswith('aws_')}==expected
    assert options['endpoint_url']==endpoint
    assert options['config']['retries']['total_max_attempts']==3


@pytest.mark.parametrize('value',[True,False,'14566',None,14566.0,0,65536,-1])
def test_localstack_port_rejects_invalid_types_and_range(value):
    with pytest.raises(ValueError,match='LocalStack port'):
        RunStorage('bucket',localstack_port=value)


@pytest.mark.parametrize('value',[1,65535])
def test_localstack_port_accepts_range_boundaries(value):
    assert RunStorage('bucket',localstack_port=value).localstack_port==value


def test_default_port_remains_compatible(tmp_path):
    from api_agent.config import load_shared_schema,resolve_config
    from api_agent.config.declarations import ConfigSchema
    from api_agent.config.session import ConfigSession
    shared=load_shared_schema()
    schema=ConfigSchema({name:shared.fields[name] for name in ('NO_STD_IN','TUI_MODE','COLLECT_ONLY','ALLURE_LOCALSTACK_PORT')})
    invalid=resolve_config(schema,argv=['--tui-mode=integration'],environ={},overrides={'ALLURE_LOCALSTACK_PORT':None})
    assert invalid.errors==('ALLURE_LOCALSTACK_PORT: external values must be strings',)
    assert 'ALLURE_LOCALSTACK_PORT' not in invalid.values
    context=SimpleNamespace(base_root=tmp_path,pack_root=tmp_path,external_pack=False,dotenv_path=tmp_path/'.env',anchors={})
    session=ConfigSession(context,schema,argv=['--tui-mode=integration'],environ={})
    assert session.set_value('ALLURE_LOCALSTACK_PORT','14566',persist=True).values['ALLURE_LOCALSTACK_PORT']==14566
    cleared=session.unset_saved('ALLURE_LOCALSTACK_PORT')
    assert not cleared.errors
    assert cleared.values['ALLURE_LOCALSTACK_PORT']==4566
    assert RunStorage('bucket').localstack_port==4566
    assert RunStorage('bucket',localstack_port=cleared.values['ALLURE_LOCALSTACK_PORT']).localstack_port==4566


@pytest.mark.parametrize('settings,port',[({},4566),({'ALLURE_LOCALSTACK_PORT':14566},14566)])
def test_recording_threads_configured_port(tmp_path,monkeypatch,settings,port):
    from api_agent.testing import recorded
    from test_recorded_integration import fixture,Storage,fake_runner
    context,test,config=fixture(tmp_path)
    config.update(settings)
    factory=Mock(return_value=Storage())
    monkeypatch.setattr(recorded,'RunStorage',factory)
    result=recorded.run_recorded_integration(context,[test],config,accepted=True,runner=fake_runner())
    assert result.persisted
    assert factory.call_args.kwargs['localstack_port']==port


@pytest.mark.parametrize('settings,port',[({},4566),({'ALLURE_LOCALSTACK_PORT':14566},14566)])
def test_history_threads_configured_port(tmp_path,monkeypatch,settings,port):
    from api_agent.testing import cost_history
    from test_recorded_integration import fixture
    context,test,config=fixture(tmp_path)
    config.update(settings)
    factory=Mock()
    monkeypatch.setattr(cost_history,'RunStorage',factory)
    monkeypatch.setattr(cost_history,'cost_config',lambda context,config:config)
    monkeypatch.setattr(cost_history,'HistoryCache',Mock())
    monkeypatch.setattr(cost_history,'HistoricalEstimator',Mock())
    cost_history.estimator_for(context,[test],config,rate_card={'synthetic':1})
    assert factory.call_args.kwargs['localstack_port']==port
