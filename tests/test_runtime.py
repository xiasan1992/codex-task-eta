import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('eta', Path(__file__).parents[1] / 'runtime.py')
eta = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eta)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        eta.DATA = Path(self.temp.name) / 'data'

    def tearDown(self):
        self.temp.cleanup()

    def send(self, name, at, turn='t1', **kw):
        eta.record(dict(session_id='s', turn_id=turn, hook_event_name=name, **kw), at=at)

    def task(self):
        with eta.connect() as db:
            return dict(db.execute('SELECT * FROM tasks ORDER BY started DESC LIMIT 1').fetchone())

    def test_unknown_until_enough_comparable_samples(self):
        self.send('UserPromptSubmit', 100, prompt='build plugin', model='m')
        t = self.task()
        self.assertIsNone(eta.forecast(t, [], 110)['remaining'])
        h = [dict(t, id=str(i), ended=100 + n, started=100) for i, n in enumerate([100, 200, 300, 400, 500])]
        result = eta.forecast(t, h, 150)
        # Independent hand calculation: residuals 50,150,250,350,450 -> q20=130,q80=370.
        self.assertEqual(result['remaining'], [130, 370])
        self.assertIsNone(eta.forecast(t, h, 250)['remaining'])
        self.assertIsNone(eta.forecast(dict(t, model='other'), h, 110)['remaining'])

    def test_explicit_estimate_expires(self):
        self.send('UserPromptSubmit', 100)
        t = dict(self.task(), estimate_low=30, estimate_high=90, estimate_at=110)
        self.assertEqual(eta.forecast(t, [], 120)['remaining'], [20, 80])
        self.assertEqual(eta.forecast(t, [], 200)['estimate_source'], 'overdue')
        self.assertIsNone(eta.forecast(t, [], 200)['remaining'])

    def test_wait_interrupt_and_no_delayed_stop_on_new_turn(self):
        self.send('UserPromptSubmit', 100)
        self.send('PermissionRequest', 120, tool_name='Bash')
        self.send('Heartbeat', 125)
        self.assertEqual(eta.forecast(self.task(), [], 130)['status'], 'waiting')
        self.send('PostToolUse', 140)
        self.send('Interrupt', 150)
        self.assertEqual(self.task()['status'], 'interrupted')
        self.send('UserPromptSubmit', 200, turn='t2')
        self.send('Stop', 210, turn='t1')
        self.assertEqual(self.task()['status'], 'running')
        self.assertEqual(eta.forecast(self.task(), [], 1200)['status'], 'stale')

    def test_invalid_estimates_and_duplicate_start(self):
        self.send('UserPromptSubmit', 100)
        self.send('UserPromptSubmit', 101)
        self.assertEqual(self.task()['started'], 100)
        for low, high in [(100, 1), (0, float('nan')), (True, 10)]:
            with self.assertRaises(ValueError):
                eta.set_estimate({'session_id': 's', 'low_seconds': low, 'high_seconds': high})

    def test_scanner_partial_line_and_restart(self):
        sessions = Path(self.temp.name) / 'sessions'
        folder = sessions / '2026/10/22'
        folder.mkdir(parents=True)
        p = folder / 'rollout.jsonl'
        rows = [dict(type='session_meta', payload={'id': 's'}),
                dict(type='event_msg', timestamp='2026-10-22T00:00:00Z', payload={'type': 'task_started', 'turn_id': 't1'})]
        p.write_text('\n'.join(map(json.dumps, rows)) + '\n', 'utf-8')
        scanner = eta.Scanner(sessions)
        scanner.scan()
        self.assertEqual(self.task()['status'], 'running')
        end = json.dumps(dict(type='event_msg', timestamp='2026-10-22T00:00:30Z', payload={'type': 'task_complete', 'turn_id': 't1'}))
        with p.open('a', encoding='utf-8') as f:
            f.write(end[:20])
        scanner.scan()
        self.assertEqual(self.task()['status'], 'running')
        with p.open('a', encoding='utf-8') as f:
            f.write(end[20:] + '\n')
        eta.Scanner(sessions).scan()
        self.assertEqual(self.task()['status'], 'completed')
        self.assertEqual(self.task()['ended'] - self.task()['started'], 30)


if __name__ == '__main__':
    unittest.main()
