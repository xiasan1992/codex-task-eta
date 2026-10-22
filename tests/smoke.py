"""Exercise real stdio and HTTP transports, isolated from the user's data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

root = Path(__file__).parents[1]
with tempfile.TemporaryDirectory() as tmp:
    env = dict(os.environ, CODEX_ETA_DATA=tmp, CODEX_HOME=tmp)
    proc = subprocess.Popen([sys.executable, str(root / 'runtime.py'), 'mcp'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', env=env)
    service = None
    try:
        def rpc(method, params=None):
            proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}) + '\n')
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())['result']
        assert rpc('initialize')['capabilities'] == {'tools': {}}
        assert len(rpc('tools/list')['tools']) == 3
        result = rpc('tools/call', {'name': 'eta_panel'})
        url = json.loads(result['content'][0]['text'])['url']
        service = json.loads((Path(tmp) / 'service.json').read_text())
        base, token = url.split('#')
        assert '任务还需要多久' in urllib.request.urlopen(base).read().decode()
        try:
            urllib.request.urlopen(base + 'api/status')
            raise AssertionError('Missing authorization accepted')
        except urllib.error.HTTPError as e:
            assert e.code == 401
        event = dict(session_id='smoke', turn_id='t', hook_event_name='UserPromptSubmit', prompt='测试任务')
        run = subprocess.run([sys.executable, str(root / 'runtime.py'), 'hook'], input=json.dumps(event), text=True, encoding='utf-8', capture_output=True, env=env, check=True)
        assert json.loads(run.stdout)['hookSpecificOutput']['hookEventName'] == 'UserPromptSubmit'
        result = rpc('tools/call', {'name': 'eta_set_estimate', 'arguments': dict(session_id='smoke', low_seconds=60, high_seconds=120, basis='transport test')})
        assert not result['isError']
        req = urllib.request.Request(base + 'api/status', headers={'Authorization': 'Bearer ' + token})
        data = json.load(urllib.request.urlopen(req))
        assert data['tasks'][0]['remaining'][1] <= 120
        assert data['tasks'][0]['estimate_source'] == 'agent'
        event['hook_event_name'] = 'Stop'
        subprocess.run([sys.executable, str(root / 'runtime.py'), 'hook'], input=json.dumps(event), text=True, encoding='utf-8', capture_output=True, env=env, check=True)
        assert json.load(urllib.request.urlopen(req))['tasks'][0]['status'] == 'completed'
        print('PASS: MCP handshake/tools, panel HTML, unauthorized rejection, hooks, live ETA and completion')
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        if service:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(service['pid']), '/F'], capture_output=True)
            else:
                os.kill(service['pid'], 15)
