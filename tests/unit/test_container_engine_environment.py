"""Actual engine children receive transport configuration, never provider secrets."""
import json
import os
from pathlib import Path
import sys
import threading

import pytest

from api_agent.runtime.containers import ContainerError, ContainerRuntime
from api_agent.testing.diagnostics import CredentialScrubber
from test_containers import SCOPE, record


@pytest.fixture
def engine_context(tmp_path, monkeypatch):
    required = {
        'PATH': str(tmp_path)+os.pathsep+os.environ.get('PATH',''),
        'HOME': str(tmp_path/'home'), 'XDG_RUNTIME_DIR': str(tmp_path/'runtime'),
        'DOCKER_HOST': 'unix:///synthetic/docker.sock',
        'CONTAINER_HOST': 'unix:///synthetic/podman.sock',
        'DOCKER_CONFIG': str(tmp_path/'docker'), 'API_AGENT_HOST_UID': str(os.getuid()),
    }
    forbidden = {'API_KEY':'synthetic-exported-api-key', 'GPT_API_KEY':'synthetic-exported-provider-key',
                 'UNRELATED_SETTING':'synthetic-unrelated-setting'}
    for key,value in {**required,**forbidden}.items(): monkeypatch.setenv(key,value)
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity',lambda:set())
    helper=tmp_path/'engine-transport-helper'
    helper.write_text('#!'+sys.executable+'\nprint("wrapper-reachable")\n');helper.chmod(0o700)
    report=tmp_path/'observations.jsonl'
    secret='synthetic-runtime-config-"credential'
    def create(mode='success'):
        executable=tmp_path/('engine-'+mode)
        executable.write_text('#!'+sys.executable+'\nimport os,sys,json,subprocess,time\n'
            +'keys='+repr(tuple(required)+tuple(forbidden))+'\n'
            +'observed={"command":sys.argv[1],"pid":os.getpid(),"environment":{key:os.environ.get(key) for key in keys},\n'
            +' "wrapper":subprocess.check_output(["engine-transport-helper"],text=True).strip()}\n'
            +'with open('+repr(str(report))+',"a") as stream: stream.write(json.dumps(observed)+"\\n")\n'
            +'if sys.argv[1]=="ps": print('+repr('a'*64)+')\n'
            +'elif sys.argv[1]=="inspect": print('+repr(json.dumps([record()]))+')\n'
            +'elif sys.argv[1]=="logs":\n'
            +' print('+repr(secret)+',flush=True)\n'
            +' print('+repr(json.dumps(secret))+',file=sys.stderr,flush=True)\n'
            +(' raise SystemExit(7)\n' if mode=='failure' else ' while True: time.sleep(.01)\n' if mode=='cancel' else '')
            +'else: print("stopped")\n')
        executable.chmod(0o700)
        return ContainerRuntime(str(executable),SCOPE)
    def observations():return [json.loads(line) for line in report.read_text().splitlines()]
    return create,observations,required,forbidden,secret


def assert_isolated(observations, required, forbidden, commands):
    assert [row['command'] for row in observations]==commands
    for row in observations:
        assert row['wrapper']=='wrapper-reachable'
        assert all(row['environment'][key]==value for key,value in required.items())
        assert all(row['environment'][key] is None for key in forbidden)


def test_inventory_and_explicit_stop_children_keep_transport_but_not_exported_credentials(engine_context):
    create,observations,required,forbidden,_=engine_context
    runtime=create()
    assert runtime.inventory()[0].id=='a'*64
    runtime.stop(('a'*64,))
    assert_isolated(observations(),required,forbidden,['ps','inspect','inspect','stop'])


@pytest.mark.parametrize('mode',['success','failure'])
def test_logs_inspection_and_both_streams_keep_isolation_and_configured_redaction(engine_context,mode):
    create,observations,required,forbidden,secret=engine_context
    runtime=create(mode)
    scrubber=CredentialScrubber(secret_values=(secret,))
    if mode=='success':
        text=runtime.logs('a'*64,scrubber=scrubber)
    else:
        with pytest.raises(ContainerError) as caught:runtime.logs('a'*64,scrubber=scrubber)
        text=str(caught.value)+'\n'+caught.value.__context__.local_details
    assert secret not in text and json.dumps(secret)[1:-1] not in text and '[REDACTED]' in text
    assert_isolated(observations(),required,forbidden,['inspect','logs'])


def test_cancelled_log_child_keeps_isolation_redaction_and_disposal(engine_context,monkeypatch):
    from api_agent.runtime import prerequisites
    create,observations,required,forbidden,secret=engine_context
    runtime=create('cancel');received,cancelled=threading.Event(),threading.Event()
    read=prerequisites.os.read
    def observed_read(fd,size):
        data=read(fd,size)
        if secret.encode() in data:received.set()
        return data
    monkeypatch.setattr(prerequisites.os,'read',observed_read)
    failures=[]
    def run():
        try:runtime.logs('a'*64,scrubber=CredentialScrubber(secret_values=(secret,)),cancelled=cancelled.is_set)
        except ContainerError as error:failures.append(error)
    thread=threading.Thread(target=run);thread.start()
    try:
        assert received.wait(10)
        cancelled.set();thread.join(10)
        assert not thread.is_alive() and len(failures)==1
        text=str(failures[0])+'\n'+failures[0].__context__.local_details
        assert secret not in text and json.dumps(secret)[1:-1] not in text and '[REDACTED]' in text
        assert_isolated(observations(),required,forbidden,['inspect','logs'])
        if Path('/proc').is_dir():
            assert not Path('/proc/'+str(observations()[-1]['pid'])).exists()
    finally:
        cancelled.set();thread.join(10)
