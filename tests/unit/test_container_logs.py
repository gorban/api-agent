"""Read-only managed logs reuse the bounded shared process owner."""
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

import pytest

from api_agent.runtime.containers import ContainerError, ContainerRuntime
from api_agent.runtime.prerequisites import PrerequisiteError
from api_agent.testing.diagnostics import CredentialScrubber
from test_containers import SCOPE, record


@pytest.mark.parametrize('identifier,scope', [('a', SCOPE), ('-x', SCOPE), (None, SCOPE), ('a'*64, None)])
def test_logs_refuse_invalid_or_unscoped_selection_without_engine(identifier, scope):
    with patch('api_agent.runtime.prerequisites.run_command') as run:
        with pytest.raises(ContainerError, match='managed container'):
            ContainerRuntime('docker', scope).logs(identifier)
        run.assert_not_called()


def test_logs_missing_engine():
    with patch('api_agent.runtime.containers.shutil.which', return_value=None):
        with pytest.raises(ContainerError, match='absent'):
            ContainerRuntime(scope=SCOPE).logs('a'*64)


@pytest.mark.parametrize('items', [[], [record('b'*64)], [record(), record()],
    [record(Config={'Labels':{}})], [record(Name='/session')]])
def test_logs_reinspect_exact_identity_and_scope_before_any_log_read(items, monkeypatch):
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: {'session'})
    with patch('api_agent.runtime.prerequisites.run_command', return_value=json.dumps(items)) as run:
        with pytest.raises(ContainerError, match='no longer owned'):
            ContainerRuntime('docker', SCOPE).logs('a'*64)
        assert run.call_count == 1 and run.call_args.args[0] == ('docker', 'inspect', 'a'*64)


def test_logs_preserve_shared_collector_cleanup_capability(monkeypatch):
    pending = object()
    failure = PrerequisiteError('a private diagnostic')
    failure.cleanup_pending = pending
    with patch('api_agent.runtime.prerequisites.run_command', side_effect=failure):
        with pytest.raises(ContainerError) as caught:
            ContainerRuntime('docker', SCOPE).logs('a'*64)
    assert caught.value.cleanup_pending is pending
    assert 'private' not in str(caught.value)


def test_logs_pass_shared_budget_scrubber_and_total_output_limit(monkeypatch):
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: set())
    secret = 'secret-value-spans-the-final-display-boundary'
    scrubber = CredentialScrubber(secret_values=(secret,))
    calls = []
    def command(args, **kwargs):
        calls.append((args, kwargs))
        assert kwargs['deadline'] - time.monotonic() <= 5
        assert kwargs['output_scrubber'] is scrubber
        if args[1] == 'inspect': return json.dumps([record(State={'Running':False, 'Status':'exited'})])
        assert kwargs['output_limit']==65536
        return scrubber.capture(('prefix'+secret).encode())
    monkeypatch.setattr('api_agent.runtime.prerequisites.run_command', command)
    text = ContainerRuntime('docker', SCOPE).logs('a'*64, scrubber=scrubber)
    assert text=='prefix[REDACTED]' and secret not in text
    assert calls[1][0] == ('docker','logs','--tail','200','a'*64)
    assert calls[1][1]['combine_output'] is True and calls[0][1]['combine_output'] is False
    assert calls[1][1]['deadline'] <= calls[0][1]['deadline'] + .01


def test_logs_default_scrubber_and_safe_failure(monkeypatch):
    monkeypatch.setattr('api_agent.runtime.containers.shutil.which', lambda name: '/engine' if name == 'podman' else None)
    with patch('api_agent.runtime.prerequisites.run_command', side_effect=PrerequisiteError('private')) as run:
        with pytest.raises(ContainerError, match='unavailable') as caught:
            ContainerRuntime(scope=SCOPE).logs('a'*64)
    assert not hasattr(caught.value, 'cleanup_pending') and 'private' not in str(caught.value)
    assert run.call_args.args[0][0] == '/engine'


@pytest.mark.parametrize('ids,records', [('bad', []), ('a'*64+' '+'a'*64, []), ('a'*64, []), ('a'*64, [record('b'*64)])])
def test_inventory_rejects_invalid_duplicate_or_changed_full_ids(ids, records):
    runtime = ContainerRuntime('docker', SCOPE)
    with patch.object(runtime, '_run', side_effect=[ids,json.dumps(records)]):
        with pytest.raises(ContainerError): runtime.inventory()


def engine(tmp_path, body):
    path = tmp_path/'engine'
    path.write_text('#!'+sys.executable+'\nimport json,os,sys,time,subprocess\n'
                    +'if sys.argv[1]=="inspect":\n print('+repr(json.dumps([record()]))+')\nelse:\n'
                    +'\n'.join(' '+line for line in body.splitlines())+'\n')
    path.chmod(0o700)
    return path


def test_real_process_both_streams_split_and_escaped_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: set())
    secret = 'synthetic-value-"with-escape'
    code = "print('x'*70000,flush=True)\nprint('stdout-visible',flush=True)\nprint('stderr-visible',file=sys.stderr,flush=True)\n"
    code += 'sys.stdout.write('+repr(secret[:12])+');sys.stdout.flush();time.sleep(.03)\n'
    code += 'sys.stdout.write('+repr(secret[12:]+'\n')+');sys.stdout.flush()\n'
    code += 'print('+repr(json.dumps(secret))+',file=sys.stderr,flush=True)'
    path = engine(tmp_path, code)
    text = ContainerRuntime(str(path), SCOPE).logs('a'*64, scrubber=CredentialScrubber(secret_values=(secret,)))
    assert 'stdout-visible' in text and 'stderr-visible' in text
    assert secret not in text and json.dumps(secret)[1:-1] not in text and '[REDACTED]' in text
    assert 'stdout' in text and 'stderr' in text and len(text.encode())<=65536


def test_real_process_cancel_reaps_reader_and_its_child(tmp_path, monkeypatch):
    if not sys.platform.startswith('linux'):
        pytest.skip('Controlled /proc process-group proof is Linux-specific')
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity', lambda: set())
    ready = tmp_path/'pids.json'
    path = engine(tmp_path, 'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\n'
        +'open('+repr(str(ready)+'.tmp')+',"w").write(json.dumps([os.getpid(),child.pid]))\n'
        +'os.replace('+repr(str(ready)+'.tmp')+','+repr(str(ready))+')\n'
        +'print("ready",flush=True)\nwhile True: time.sleep(.01)')
    cancelled, failures = threading.Event(), []
    def read():
        try: ContainerRuntime(str(path), SCOPE).logs('a'*64, cancelled=cancelled.is_set)
        except ContainerError as error: failures.append(error)
    reader = threading.Thread(target=read)
    reader.start()
    try:
        deadline = time.monotonic()+10
        while not ready.exists() and reader.is_alive() and time.monotonic()<deadline: time.sleep(.01)
        assert ready.exists()
        pids = json.loads(ready.read_text())
        cancelled.set();reader.join(10)
        assert not reader.is_alive() and len(failures)==1
        for pid in pids:
            stat = Path(f'/proc/{pid}/stat')
            assert not stat.exists() or stat.read_text().rpartition(')')[2].split()[0]=='Z'
    finally:
        cancelled.set();reader.join(10)


def test_running_and_stopped_inventory_follow_engine_all_contract(monkeypatch):
    monkeypatch.setattr('api_agent.runtime.containers.session_container_identity',lambda:set())
    records={item['Id']:item for item in [record(),record('b'*64,State={'Running':False,'Status':'exited'})]}
    runtime=ContainerRuntime('docker',SCOPE)
    def command(arguments):
        if arguments[0]=='ps':return ' '.join(records if '--all' in arguments else ['a'*64])
        return json.dumps([records[identifier] for identifier in arguments[1:]])
    monkeypatch.setattr(runtime,'_run',command)
    items=runtime.inventory()
    assert [(item.id,item.running) for item in items]==[('a'*64,True),('b'*64,False)]
