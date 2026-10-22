"""Local Codex task monitor. Standard library only; no model/API calls."""
import argparse
from contextlib import contextmanager
import datetime as dt
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent
HOME = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
DATA = Path(os.environ.get('CODEX_ETA_DATA', HOME / 'task-eta'))
LOCK = threading.RLock()


@contextmanager
def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / 'tasks.sqlite', timeout=2)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS tasks (
      id TEXT PRIMARY KEY, session TEXT, title TEXT, category TEXT, model TEXT,
      started REAL, updated REAL, ended REAL, status TEXT, tool TEXT,
      estimate_low REAL, estimate_high REAL, estimate_at REAL, basis TEXT,
      source TEXT);
    CREATE TABLE IF NOT EXISTS cursors (path TEXT PRIMARY KEY, offset INTEGER);
    ''')
    try:
        with db:
            yield db
    finally:
        db.close()


def category(title):
    for key, words in [('research', ('文献', '论文', 'research', 'literature')),
                       ('build', ('插件', '开发', '实现', 'build', 'implement', 'create')),
                       ('fix', ('修复', '报错', 'bug', 'fix', 'debug')),
                       ('review', ('审查', 'review', '检查'))]:
        if any(w in title.lower() for w in words):
            return key
    return 'general'


def stamp(value):
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return time.time()


def record(event, *, at=None, source='hook'):
    """Store only metadata, never commands, tool output, or full transcripts."""
    sid = str(event.get('session_id') or '')
    if not sid:
        return
    at = time.time() if at is None else at
    name = event.get('hook_event_name')
    turn = event.get('turn_id')
    with LOCK, connect() as db:
        row = db.execute('SELECT * FROM tasks WHERE session=? ORDER BY started DESC LIMIT 1', (sid,)).fetchone()
        if name == 'UserPromptSubmit':
            tid = sid + ':' + str(turn or int(at * 1000))
            if row and row['started'] <= at and row['id'] != tid and row['status'] in ('running', 'waiting'):
                db.execute("UPDATE tasks SET status='unknown' WHERE id=?", (row['id'],))
            # Turn IDs may be absent in older hooks: metadata updates still use latest row.
            title = ' '.join(str(event.get('prompt') or 'Codex 任务').split())[:100]
            db.execute('INSERT OR IGNORE INTO tasks (id,session,title,category,model,started,updated,status,tool,source) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (tid, sid, title, category(title), event.get('model', ''), at, at, 'running', '', source))
            if title != 'Codex 任务':
                db.execute('UPDATE tasks SET title=?,category=?,model=CASE WHEN ? != \'\' THEN ? ELSE model END WHERE id=?',
                           (title, category(title), event.get('model', ''), event.get('model', ''), tid))
            return
        if not row:
            return
        if at < row['updated']:
            return
        # Delayed events from a previous turn must never finish a newer turn.
        if turn and row['id'] != sid + ':' + str(turn):
            return
        if name in ('Stop', 'Interrupt', 'SessionEnd'):
            status = {'Stop': 'completed', 'Interrupt': 'interrupted', 'SessionEnd': 'unknown'}[name]
            if row['status'] in ('completed', 'interrupted'):
                return
            db.execute('UPDATE tasks SET status=?,ended=?,updated=?,tool=\'\' WHERE id=?', (status, at, at, row['id']))
        elif row['status'] in ('running', 'waiting'):
            if name == 'Heartbeat':
                db.execute('UPDATE tasks SET updated=? WHERE id=?', (at, row['id']))
                return
            status = 'waiting' if name == 'PermissionRequest' else 'running'
            tool = str(event.get('tool_name') or '')[:100] if name in ('PreToolUse', 'PermissionRequest') else ''
            db.execute('UPDATE tasks SET status=?,updated=?,tool=? WHERE id=?', (status, at, tool, row['id']))


def quantile(values, q):
    s = sorted(values)
    i = (len(s) - 1) * q
    lo = int(i)
    return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (i - lo)


def forecast(task, history, now):
    elapsed = max(0, (task['ended'] or now) - task['started'])
    result = dict(task)
    result.update(elapsed=round(elapsed), remaining=None, finish=None, sample_count=0,
                  estimate_source='insufficient', estimate_note='样本不足，暂无法估算')
    if task['status'] not in ('running', 'waiting'):
        result['estimate_note'] = '任务已结束' if task['status'] in ('completed', 'interrupted') else '状态未确认'
        return result
    if now - task['updated'] > 900:
        result.update(status='stale', estimate_note='超过 15 分钟没有新事件，运行状态待确认')
        return result
    if task['status'] == 'waiting':
        result['estimate_note'] = '等待批准，完成时间暂不可预测'
        return result
    if task['estimate_high'] is not None:
        age = max(0, now - task['estimate_at'])
        if age >= task['estimate_high']:
            result.update(estimate_source='overdue', estimate_note='已超出上次估算，等待更新')
            return result
        low, high = max(0, task['estimate_low'] - age), task['estimate_high'] - age
        result.update(estimate_source='agent', estimate_note=task['basis'] or '代理估算，可能随任务范围调整')
    else:
        # Conditional remaining lifetime: use only matching completed turns that
        # lasted longer than this turn already has. Never subtract to zero forever.
        samples = [h['ended'] - h['started'] for h in history
                   if h['category'] == task['category'] and h['model'] == task['model']
                   and h['id'] != task['id'] and h['ended'] and h['ended'] > h['started']]
        survivors = [v - elapsed for v in samples if v > elapsed]
        result['sample_count'] = len(survivors)
        if len(survivors) < 5:
            result['estimate_note'] = '同类且耗时更长的完成样本不足 5 个，暂无法估算'
            return result
        low, high = quantile(survivors, .2), quantile(survivors, .8)
        result.update(estimate_source='history', estimate_note='同类、同模型历史经验区间；不保证覆盖实际耗时')
    result['remaining'] = [round(low), round(high)]
    result['finish'] = [now + low, now + high]
    return result


def snapshot():
    now = time.time()
    with LOCK, connect() as db:
        history = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE status='completed' ORDER BY ended DESC LIMIT 1000")]
        rows = [dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY started DESC LIMIT 60')]
    return {'now': now, 'tasks': [forecast(t, history, now) for t in rows],
            'history_count': len(history), 'privacy': '本机运行 · 无外发 · 每 2 秒刷新'}


def set_estimate(args):
    sid = args.get('session_id')
    low, high = args.get('low_seconds'), args.get('high_seconds')
    if not isinstance(sid, str) or not sid or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (low, high)) or not 0 <= low <= high <= 604800 or high == 0:
        raise ValueError('需要 session_id 和有效秒数：0 <= low_seconds <= high_seconds <= 604800')
    with LOCK, connect() as db:
        row = db.execute("SELECT id FROM tasks WHERE session=? AND status='running' ORDER BY started DESC LIMIT 1", (sid,)).fetchone()
        if not row:
            raise ValueError('找不到这个会话正在运行的任务')
        db.execute('UPDATE tasks SET estimate_low=?,estimate_high=?,estimate_at=?,basis=? WHERE id=?',
                   (low, high, time.time(), str(args.get('basis', '代理估算'))[:200], row['id']))
    return {'updated': row['id'], 'remaining_seconds': [low, high]}


class Scanner:
    """Best-effort JSONL adapter. Hooks are the stable integration.

    Scans only recent local session files, at most 2 MB/file per pass. Unknown
    formats remain unknown. Cursors live in our own DB, never in Codex state.
    """
    def __init__(self, sessions=None):
        self.sessions = sessions or HOME / 'sessions'
        self.meta = {}

    def scan(self):
        folders = sorted(self.sessions.glob('*/*/*'), reverse=True)[:3]
        paths = sorted((p for d in folders for p in d.glob('*.jsonl')), key=lambda p: p.stat().st_mtime, reverse=True)[:40]
        for path in paths:
            try:
                self.read(path)
            except (OSError, ValueError, sqlite3.Error):
                continue

    def read(self, path):
        key = str(path)
        with connect() as db:
            row = db.execute('SELECT offset FROM cursors WHERE path=?', (key,)).fetchone()
        size = path.stat().st_size
        offset = row['offset'] if row and row['offset'] <= size else 0
        with path.open('rb') as f:
            first = f.readline(262144)
            try:
                meta = json.loads(first).get('payload', {})
            except ValueError:
                return
            sid = meta.get('id')
            if not sid:
                return
            if offset == 0 and size > 2_000_000:
                offset = size - 2_000_000
                f.seek(offset)
                f.readline()  # Discard the partial line at tail boundary.
            else:
                f.seek(offset)
            budget = f.tell() + 2_000_000
            while f.tell() < budget:
                start = f.tell()
                line = f.readline(2_000_001)
                if not line or not line.endswith(b'\n'):
                    f.seek(start)
                    break
                try:
                    self.consume(sid, json.loads(line))
                except (ValueError, TypeError, AttributeError):
                    pass
            offset = f.tell()
        with connect() as db:
            db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)', (key, offset))

    def consume(self, sid, row):
        pl = row.get('payload', {})
        kind = row.get('type')
        typ = pl.get('type')
        at = stamp(row.get('timestamp'))
        if kind == 'event_msg':
            event = {'session_id': sid, 'turn_id': pl.get('turn_id')}
            if typ == 'task_started':
                event.update(hook_event_name='UserPromptSubmit', prompt='Codex 任务')
            elif typ == 'task_complete':
                event['hook_event_name'] = 'Stop'
            elif typ in ('turn_aborted', 'task_interrupted'):
                event['hook_event_name'] = 'Interrupt'
            else:
                # Any timestamped event is a heartbeat, but never revives an ended turn.
                event['hook_event_name'] = 'Heartbeat'
            record(event, at=at, source='transcript')
        elif kind == 'turn_context':
            with connect() as db:
                db.execute("UPDATE tasks SET model=? WHERE id=(SELECT id FROM tasks WHERE session=? ORDER BY started DESC LIMIT 1)", (pl.get('model', ''), sid))
        elif kind == 'response_item' and typ == 'message' and pl.get('role') == 'user':
            title = ' '.join(c.get('text', '') for c in pl.get('content', []) if c.get('type') in ('input_text', 'text'))
            if title and not title.lstrip().startswith(('<environment_context>', '<external_', '<permissions')):
                title = ' '.join(title.split())[:100]
                with connect() as db:
                    db.execute("UPDATE tasks SET title=?,category=? WHERE session=? AND title='Codex 任务'", (title, category(title), sid))


def load_service():
    try:
        return json.loads((DATA / 'service.json').read_text('utf-8'))
    except (OSError, ValueError):
        return None


def request_service(service, route='/health'):
    req = urllib.request.Request('http://127.0.0.1:' + str(service['port']) + route,
                                 headers={'Authorization': 'Bearer ' + service['token']})
    with urllib.request.urlopen(req, timeout=2) as res:
        return json.load(res)


def ensure_service():
    service = load_service()
    try:
        if service and request_service(service).get('app') == 'codex-task-eta':
            return service
    except (OSError, ValueError):
        pass
    # Atomic startup lock prevents simultaneous hook/MCP invocations spawning daemons.
    DATA.mkdir(parents=True, exist_ok=True)
    lockfile = DATA / 'starting.lock'
    try:
        fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        if time.time() - lockfile.stat().st_mtime > 20:
            lockfile.unlink(missing_ok=True)
        else:
            for _ in range(30):
                time.sleep(.1)
                service = load_service()
                try:
                    if service and request_service(service).get('app') == 'codex-task-eta':
                        return service
                except OSError:
                    pass
            raise RuntimeError('服务启动中，请稍后重试')
    else:
        try:
            flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == 'nt' else 0
            subprocess.Popen([sys.executable, str(ROOT / 'runtime.py'), 'serve'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags, start_new_session=os.name != 'nt')
            for _ in range(40):
                time.sleep(.1)
                service = load_service()
                try:
                    if service and request_service(service).get('app') == 'codex-task-eta':
                        return service
                except OSError:
                    pass
            raise RuntimeError('本地面板启动失败')
        finally:
            lockfile.unlink(missing_ok=True)
    return ensure_service()


def panel_url():
    service = ensure_service()
    return 'http://127.0.0.1:' + str(service['port']) + '/#' + service['token']


def serve():
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            # Reject DNS rebinding and cross-origin data access.
            if self.headers.get('Host') != '127.0.0.1:' + str(self.server.server_port):
                self.send_error(403)
                return
            route = self.path.split('?', 1)[0]
            if route == '/':
                body = (ROOT / 'dashboard.html').read_bytes()
                mime = 'text/html; charset=utf-8'
            elif route in ('/api/status', '/health'):
                if not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                    self.send_error(401)
                    return
                body = json.dumps(snapshot() if route == '/api/status' else {'app': 'codex-task-eta'}).encode()
                mime = 'application/json; charset=utf-8'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / 'service.json').write_text(json.dumps({'port': httpd.server_port, 'token': token, 'pid': os.getpid()}), 'utf-8')
    scanner = Scanner()

    def scan_loop():
        while True:
            try:
                scanner.scan()
            except Exception as exc:
                (DATA / 'scanner-error.txt').write_text(type(exc).__name__ + ': ' + str(exc), 'utf-8')
            time.sleep(5)

    threading.Thread(target=scan_loop, daemon=True).start()
    httpd.serve_forever()


TOOLS = [
    {'name': 'eta_status', 'description': 'Read local Codex turn status, elapsed time and estimated remaining range. Unknown is explicit.', 'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}, 'annotations': {'readOnlyHint': True}},
    {'name': 'eta_panel', 'description': 'Start the local live task monitor and return its URL. Open it in Codex using open_in_codex browser.', 'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'eta_set_estimate', 'description': 'Set an explicit estimate of REMAINING seconds for one running session. This is an agent judgment, not measured runtime.', 'inputSchema': {'type': 'object', 'properties': {'session_id': {'type': 'string'}, 'low_seconds': {'type': 'number', 'minimum': 0}, 'high_seconds': {'type': 'number', 'minimum': 1}, 'basis': {'type': 'string'}}, 'required': ['session_id', 'low_seconds', 'high_seconds'], 'additionalProperties': False}}
]


def mcp():
    for line in sys.stdin:
        req = None
        try:
            req = json.loads(line)
            if 'id' not in req:
                continue
            method = req.get('method')
            if method == 'initialize':
                result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'codex-task-eta', 'version': '0.1.3'}}
            elif method == 'tools/list':
                result = {'tools': TOOLS}
            elif method == 'tools/call':
                params = req.get('params', {})
                name = params.get('name')
                try:
                    if name == 'eta_panel':
                        value = {'url': panel_url()}
                    elif name == 'eta_status':
                        value = snapshot()
                    elif name == 'eta_set_estimate':
                        value = set_estimate(params.get('arguments', {}))
                    else:
                        raise ValueError('Unknown tool')
                    result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], 'isError': False}
                except Exception as exc:
                    result = {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}
            elif method == 'ping':
                result = {}
            else:
                print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)
                continue
            print(json.dumps({'jsonrpc': '2.0', 'id': req['id'], 'result': result}, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({'jsonrpc': '2.0', 'id': req.get('id') if isinstance(req, dict) else None, 'error': {'code': -32603, 'message': str(exc)}}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['hook', 'serve', 'panel', 'status', 'mcp'])
    command = parser.parse_args().command
    if command == 'hook':
        output = {}
        try:
            event = json.load(sys.stdin)
            record(event)
            if event.get('hook_event_name') == 'UserPromptSubmit':
                ensure_service()
                output = {'hookSpecificOutput': {
                    'hookEventName': 'UserPromptSubmit',
                    'additionalContext': 'Task Clock is observing this turn locally. Keep one Task Clock panel open for this chat: if none is open and eta_panel/open_in_codex are available, get eta_panel URL and open it as browser on the right. Reuse the existing tab on later turns. If a defensible remaining-time range is available, use eta_set_estimate with session_id=' + json.dumps(str(event.get('session_id', ''))) + ', low_seconds, high_seconds, and a brief basis. This is an agent estimate. Update it when scope changes; leave unknown when there is no reasonable basis. Continue the user task normally.'}}
        except Exception as exc:
            print('Task ETA: ' + str(exc), file=sys.stderr)
        print(json.dumps(output))  # Never block, approve, or continue the agent.
    elif command == 'serve':
        serve()
    elif command == 'panel':
        print(panel_url())
    elif command == 'status':
        print(json.dumps(snapshot(), ensure_ascii=False))
    else:
        mcp()


if __name__ == '__main__':
    main()
