#!/usr/bin/env python3
"""Snippy helper tests. A fake image service on 127.0.0.1, a temporary data folder, a throwaway key.

Run:  SNIPPY_IMAGE_URL=http://127.0.0.1:18811/v1/images/edits python3 test_helper.py
Refuses to start unless SNIPPY_IMAGE_URL points to 127.0.0.1.
"""
import base64
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
import zlib

BLOCKED_PORTS = {8765, 8791, 8792, 18791, 18792, 18793, 18801, 18810}
_url = os.environ.get('SNIPPY_IMAGE_URL', '')
_parts = urlsplit(_url)
if _parts.scheme != 'http' or _parts.hostname != '127.0.0.1' or not _parts.port or _parts.port in BLOCKED_PORTS:
    sys.exit('REFUSED: set SNIPPY_IMAGE_URL to a free http://127.0.0.1:<port>/ address. No test ran.')

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

HERE = Path(__file__).resolve().parent
DAEMON = HERE / 'daemon.py'
MCP = HERE / 'mcp_server.py'
IMAGE_URL = _url
FAKE_KEY = 'sk-' + 'throwaway' * 3
REPORTED_COST = 0.0535  # 1000 image, 100 text, 1500 output tokens
SOURCE = 'data:image/png;base64,' + base64.b64encode(b'fake source').decode()


def png(mode='cutout'):
    width = height = 4
    colour = 2 if mode == 'rgb' else 6
    rows = []
    for y in range(height):
        row = b'\x00'
        for x in range(width):
            if mode == 'rgb':
                row += b'\xff\x66\x00'
            elif mode == 'opaque':
                row += b'\xff\x66\x00\xff'
            else:
                row += b'\xff\x66\x00' + (b'\xff' if x < 2 else b'\x00')
        rows.append(row)

    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, colour, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b''))


USAGE = {'input_tokens_details': {'image_tokens': 1000, 'text_tokens': 100}, 'output_tokens': 1500}
BIG_USAGE = {'input_tokens_details': {'image_tokens': 1000, 'text_tokens': 100}, 'output_tokens': 10000}


def success(mode='cutout', usage=USAGE):
    return {'data': [{'b64_json': base64.b64encode(png(mode)).decode()}], 'usage': usage}


ERRORS = {
    '400': (400, {'error': {'type': 'invalid_request_error', 'code': 'moderation_blocked'}}),
    '401': (401, {'error': {'type': 'invalid_request_error', 'code': 'invalid_api_key'}}),
    '403': (403, {'error': {'type': 'permission_error', 'code': 'forbidden'}}),
    '429': (429, {'error': {'type': 'rate_limit_error', 'code': 'rate_limit_exceeded'}}),
    'quota': (429, {'error': {'type': 'insufficient_quota', 'code': 'insufficient_quota'}}),
    'credit': (429, {'error': {'type': 'invalid_request_error', 'code': 'credit_balance_exhausted'}}),
    'credit400': (400, {'error': {'type': 'invalid_request_error', 'code': 'credit_balance_exhausted'}}),
    '500': (500, {'error': {'type': 'server_error', 'code': None}}),
    '503': (503, {'error': {'type': 'server_error', 'code': 'overloaded'}}),
}


class Fake:
    scenario = 'ok'
    calls = []
    lock = threading.Lock()
    release = threading.Event()
    entered = threading.Event()
    redirect_hits = 0
    script = []
    in_flight = 0
    max_in_flight = 0

    @classmethod
    def reset(cls, scenario='ok'):
        cls.release.set()
        with cls.lock:
            cls.calls = []
            cls.scenario = scenario
            cls.redirect_hits = 0
            cls.script = []
            cls.max_in_flight = cls.in_flight
        cls.release = threading.Event()
        cls.entered = threading.Event()

    @classmethod
    def count(cls):
        with cls.lock:
            return len(cls.calls)


class FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, status, body, kind='application/json', extra=()):
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('x-request-id', 'req_fake')
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        with Fake.lock:
            Fake.redirect_hits += 1
        self.send(200, success())

    def do_POST(self):
        with Fake.lock:
            Fake.in_flight += 1
            Fake.max_in_flight = max(Fake.max_in_flight, Fake.in_flight)
        try:
            self.respond()
        finally:
            with Fake.lock:
                Fake.in_flight -= 1

    def respond(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.path == '/redirected':
            with Fake.lock:
                Fake.redirect_hits += 1
            self.send(200, success())
            return
        with Fake.lock:
            Fake.calls.append({'auth': self.headers.get('Authorization'), 'body': json.loads(body)})
            scenario = Fake.scenario
            release = Fake.release
            entered = Fake.entered
        entered.set()
        if scenario == 'script':
            # Waits at the gate, then answers with the next scripted reply ('ok' once it runs out).
            release.wait(30)
            with Fake.lock:
                scenario = Fake.script.pop(0) if Fake.script else 'ok'
        if scenario == 'ok':
            self.send(200, success())
        elif scenario == 'costly':
            self.send(200, success(usage=BIG_USAGE))
        elif scenario in ERRORS:
            self.send(*ERRORS[scenario])
        elif scenario == '502':
            self.send(502, b'<html><body>Bad Gateway</body></html>', 'text/html')
        elif scenario == 'garbage':
            self.send(200, b'not json at all', 'text/plain')
        elif scenario == 'not_png':
            self.send(200, {'data': [{'b64_json': base64.b64encode(b'hello').decode()}], 'usage': USAGE})
        elif scenario == 'no_usage':
            self.send(200, {'data': [{'b64_json': base64.b64encode(png()).decode()}]})
        elif scenario == 'noalpha':
            self.send(200, success('rgb'))
        elif scenario == 'opaque':
            self.send(200, success('opaque'))
        elif scenario == 'empty':
            self.send(200, b'')
        elif scenario == 'nodata':
            self.send(200, {'data': [], 'usage': {}})
        elif scenario == 'hang':
            release.wait(30)
            try:
                self.send(200, success())
            except OSError:
                pass
        elif scenario == 'drop':
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
        elif scenario == 'truncated':
            self.send_response(200)
            self.send_header('Content-Length', '100000')
            self.end_headers()
            self.wfile.write(b'{"data"')
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
        elif scenario == 'redirect':
            location = 'http://127.0.0.1:%d/redirected' % _parts.port
            self.send(307, b'', extra=[('Location', location)])
        else:
            self.send(500, {'error': {'type': 'unknown_scenario'}})


class FakeServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def free_port():
    while True:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        if port not in BLOCKED_PORTS and port != _parts.port:
            return port


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FakeServer(('127.0.0.1', _parts.port), FakeHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        Fake.release.set()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Fake.reset()
        self.tmp = Path(tempfile.mkdtemp(prefix='snippy-helper-test-'))
        self.data = self.tmp / 'data'
        self.key_file = self.tmp / 'fake-key'
        self.key_file.write_text(FAKE_KEY + '\n')
        self.key_file.chmod(0o600)
        self.port = free_port()
        self.process = None
        # Per-user config folder for this test, with the artist's settings. No real folder or key is ever read.
        self.config = self.tmp / 'config'
        self.config.mkdir()
        self.drive = self.tmp / 'drive'
        self.pieces = self.tmp / 'pieces'
        self.write_settings()
        for name, value in (('SNIPPY_CONFIG_DIR', str(self.config)),):
            old = os.environ.get(name)
            os.environ[name] = value
            self.addCleanup(lambda n=name, o=old: os.environ.pop(n, None) if o is None else os.environ.__setitem__(n, o))
        saved_key = os.environ.pop('SNIPPY_OPENAI_KEY', None)
        if saved_key is not None:
            self.addCleanup(lambda: os.environ.__setitem__('SNIPPY_OPENAI_KEY', saved_key))
        self.log_path = self.tmp / 'daemon.log'
        self.approvals = 0
        self.page_has = set()
        self.extra_env = {}

    def write_settings(self, **changes):
        settings = {'drive_folder': str(self.drive), 'output_folder': str(self.pieces), 'budget_usd': 1000,
                    'artist_name': 'Test Artist'}
        settings.update(changes)
        settings = {k: v for k, v in settings.items() if v is not None}
        (self.config / 'settings.json').write_text(json.dumps(settings))

    def tearDown(self):
        Fake.release.set()
        self.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self):
        # Built from nothing, with HOME moved, so no default path can reach a real file.
        if sys.platform == 'win32':
            # Windows needs SYSTEMROOT to start Python and USERPROFILE/APPDATA (moved into the temp folder) instead of HOME.
            base = {'PATH': os.environ.get('SystemRoot', 'C:\\Windows') + '\\System32', 'SYSTEMROOT': os.environ.get('SystemRoot', 'C:\\Windows'),
                    'USERPROFILE': str(self.tmp), 'APPDATA': str(self.tmp / 'AppData' / 'Roaming')}
        else:
            base = {'PATH': '/usr/bin:/bin', 'HOME': str(self.tmp)}
        return {**base, 'SNIPPY_HELPER_DATA': str(self.data),
                'SNIPPY_CONFIG_DIR': str(self.config), 'SNIPPY_HELPER_PORT': str(self.port), 'SNIPPY_KEY_FILE': str(self.key_file),
                'SNIPPY_IMAGE_URL': IMAGE_URL, 'SNIPPY_IMAGE_TIMEOUT': '2',
                'SNIPPY_ORIGINALS': str(self.tmp / 'originals'), **self.extra_env}

    def start(self):
        log = open(self.log_path, 'a')
        self.process = subprocess.Popen([sys.executable, '-I', str(DAEMON)], env=self.env(), stdout=log, stderr=log)
        log.close()
        deadline = time.time() + 8
        while time.time() < deadline:
            if self.process.poll() is not None:
                self.fail('daemon exited: ' + self.log_path.read_text())
            try:
                if self.tool('status').get('ok'):
                    return
            except (OSError, http.client.HTTPException, ValueError):
                pass
            time.sleep(0.03)
        self.fail('daemon did not start')

    def kill(self):
        if self.process and self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        self.process = None

    def restart(self):
        self.kill()
        self.start()

    def raw(self, path, body, headers):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            connection.request('POST', path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def tool(self, tool_name, /, **args):
        # Acts like the page: it lists the charges its balance already includes.
        if tool_name == 'start_job':
            args.setdefault('counted_job_ids', sorted(self.page_has))
        token = (self.data / 'access-token').read_text().strip()
        reply = self.raw('/tool/' + tool_name, json.dumps(args).encode(), {'Authorization': 'Bearer ' + token})[1]
        if tool_name == 'get_result' and reply.get('done'):
            self.page_has.add(args['job_id'])
        if tool_name == 'resolve_hold' and reply.get('ok') and args.get('outcome') == 'charged':
            self.page_has.add(args['job_id'])
        return reply

    def approval(self, pieces=5, usd=1.0, approval_id=None):
        self.approvals += 1
        approval = {'approval_id': approval_id or 'approval-%d' % self.approvals, 'piece_count': pieces,
                    'approved_usd': usd}
        # An approval made while stopped is a Resume, so it names the stop it has seen.
        if self.process and self.process.poll() is None:
            pause = self.tool('status').get('pause')
            if pause:
                approval['resume_pause_id'] = pause['pause_id']
        return approval

    def job(self, job_id, approval, balance=50.0, **extra):
        args = {'job_id': job_id, 'approval': approval, 'balance_usd': balance, 'source_id': job_id.split(':')[0],
                'region_id': job_id.split(':')[-1], 'name': 'Piece ' + job_id, 'reconstruct': False, 'comment': '',
                'overall_comment': '', 'source_image': 'blob-id', 'source_image_data': SOURCE,
                'box': [1, 2, 3, 4], 'reconstruction_reason': '', 'source_note': ''}
        args.update(extra)
        return self.tool('start_job', **args)

    def wait(self, job_id, seconds=8):
        deadline = time.time() + seconds
        while time.time() < deadline:
            reply = self.tool('job_status', job_id=job_id)
            if reply['status'] in ('done', 'failed', 'cancelled', 'returned'):
                return reply
            time.sleep(0.02)
        self.fail('job did not finish: ' + str(reply))

    def run_ok(self, job_id, approval, **extra):
        reply = self.job(job_id, approval, **extra)
        self.assertTrue(reply.get('accepted'), reply)
        return self.wait(job_id)

    def state(self):
        return json.loads((self.data / 'state.json').read_text())

    def entry(self, job_id):
        return self.state()['reservations'][job_id]

    def status(self):
        return self.tool('status')

    def assertExplains(self, reply):
        self.assertTrue(reply.get('kind'), reply)
        self.assertTrue(reply.get('message'), reply)
        self.assertTrue(reply.get('next_step'), reply)


PIECE_FAILURES = {  # scenario: plain code
    '502': 'bad_gateway', 'garbage': 'garbage', 'noalpha': 'no_transparency', 'empty': 'empty',
    'hang': 'timeout', 'drop': 'dropped',
}
PAUSE_FAILURES = {'401': '401', '403': '403', '429': '429', 'quota': 'insufficient_quota',
                  '500': '500', '503': '503'}


class MoneyRules(Base):
    def test_money_no_approval_no_request(self):
        self.start()
        for bad in (None, {}, {'approval_id': 'a'}, {'approval_id': 'a', 'piece_count': 1},
                    {'approval_id': 'a', 'piece_count': 0, 'approved_usd': 1},
                    {'approval_id': 'a', 'piece_count': 1, 'approved_usd': 0},
                    {'approval_id': 'a', 'piece_count': True, 'approved_usd': 1},
                    {'approval_id': '', 'piece_count': 1, 'approved_usd': 1},
                    {'approval_id': 'a', 'piece_count': 1, 'approved_usd': 'NaN'}):
            reply = self.job('s:1:1', bad)
            self.assertFalse(reply['accepted'], bad)
            self.assertEqual(reply['kind'], 'price_approval_required')
        time.sleep(0.15)
        self.assertEqual(Fake.count(), 0)

    def test_money_page_call_as_it_is_today_sends_nothing(self):
        self.start()
        reply = self.tool('start_job', job_id='s:1:1', source_id='s', region_id='1', name='n', reconstruct=False,
                          comment='', overall_comment='', source_image='abc')
        self.assertEqual((reply['accepted'], reply['kind']), (False, 'price_approval_required'))
        self.assertEqual(Fake.count(), 0)

    def test_money_no_balance_is_no_longer_a_stop_in_the_kit(self):
        # The artist kit's hard stop is the Snippy spending limit; a job with no balance gets past the balance step.
        self.start()
        reply = self.tool('start_job', job_id='s:1:1', approval=self.approval())
        self.assertNotEqual(reply['kind'], 'balance_unknown')
        self.assertEqual(Fake.count(), 0)

    def test_money_missing_source_sends_nothing(self):
        self.start()
        reply = self.job('s:1:1', self.approval(), source_image_data=None)
        self.assertEqual(reply['kind'], 'missing_source')
        self.assertEqual(Fake.count(), 0)

    def test_money_reserve_written_before_call(self):
        Fake.scenario = 'hang'
        self.start()
        self.job('s:1:1', self.approval())
        self.assertTrue(Fake.entered.wait(4))
        on_disk = self.entry('s:1:1')
        self.assertEqual((on_disk['status'], on_disk['reserve_usd']), ('reserved', '0.07'))
        self.assertEqual(self.state()['jobs']['s:1:1']['state'], 'sending')

    def test_money_settles_on_reported_cost(self):
        self.start()
        reply = self.run_ok('s:1:1', self.approval())
        self.assertEqual(reply['status'], 'done')
        self.assertAlmostEqual(reply['cost_usd'], REPORTED_COST)
        entry = self.entry('s:1:1')
        self.assertEqual(entry['status'], 'settled')
        self.assertAlmostEqual(float(entry['actual_usd']), REPORTED_COST)
        status = self.status()
        self.assertAlmostEqual(status['spent_usd'], REPORTED_COST)
        self.assertEqual(status['reserved_usd'], 0)

    def test_money_ambiguous_failure_holds_reservation(self):
        Fake.scenario = 'drop'
        self.start()
        reply = self.run_ok('s:1:1', self.approval())
        self.assertEqual((reply['kind'], reply['charged']), ('dropped', 'maybe'))
        self.assertEqual(self.entry('s:1:1')['status'], 'held')
        self.assertAlmostEqual(self.status()['reserved_usd'], 0.07)

    def test_money_never_auto_retries(self):
        for scenario in list(PIECE_FAILURES) + list(PAUSE_FAILURES):
            if scenario == 'hang':
                continue
            with self.subTest(scenario):
                Fake.reset(scenario)
                if not self.process:
                    self.start()
                job_id = 'never-%s:1:1' % scenario
                self.run_ok(job_id, self.approval())
                time.sleep(0.25)
                self.assertEqual(Fake.count(), 1)

    def test_money_crash_mid_call_needs_check_never_resent(self):
        Fake.scenario = 'hang'
        self.start()
        self.job('s:1:1', self.approval())
        self.assertTrue(Fake.entered.wait(4))
        self.restart()
        reply = self.tool('job_status', job_id='s:1:1')
        self.assertEqual((reply['status'], reply['kind'], reply['state']), ('failed', 'needs_check', 'needs_check'))
        self.assertEqual(self.entry('s:1:1')['status'], 'held')
        Fake.scenario = 'ok'
        time.sleep(1.5)
        self.assertEqual(Fake.count(), 1)
        self.assertEqual(self.job('s:1:1', self.approval())['kind'], 'duplicate_job')
        self.assertEqual(Fake.count(), 1)

    def test_money_hard_stop_on_approved_dollars(self):
        self.start()
        approval = self.approval(pieces=5, usd=0.14)
        self.assertEqual(self.run_ok('s:1:1', approval)['status'], 'done')  # 0.0535 spent
        self.assertEqual(self.run_ok('s:1:2', approval)['status'], 'done')  # 0.107 spent
        reply = self.job('s:1:3', approval)  # 0.107 + 0.0802 passes 0.14
        self.assertEqual((reply['accepted'], reply['kind']), (False, 'over_approved'))
        self.assertExplains(reply)
        self.assertEqual(Fake.count(), 2)

    def test_money_hard_stop_on_approved_exact_edge_allowed(self):
        self.start()
        self.assertEqual(self.run_ok('s:1:1', self.approval(pieces=1, usd=0.07))['status'], 'done')

    def test_money_hard_stop_on_piece_count(self):
        self.start()
        approval = self.approval(pieces=1, usd=5)
        self.run_ok('s:1:1', approval)
        reply = self.job('s:1:2', approval)
        self.assertEqual(reply['kind'], 'approval_used_up')
        self.assertEqual(Fake.count(), 1)

    def test_money_hard_stop_on_balance(self):
        self.start()
        reply = self.job('s:1:1', self.approval(), balance=0.06)
        self.assertEqual((reply['accepted'], reply['kind']), (False, 'over_balance'))
        self.assertEqual(Fake.count(), 0)

    def test_money_balance_counts_spent_and_held(self):
        self.start()
        approval = self.approval(pieces=5, usd=5)
        self.run_ok('s:1:1', approval, balance=0.15)  # 0.15 - 0.0535 = 0.0965 left
        Fake.scenario = 'drop'
        self.run_ok('s:1:2', approval, balance=0.15)  # 0.07 held, 0.0265 free
        Fake.scenario = 'ok'
        reply = self.job('s:1:3', approval, balance=0.15)
        self.assertEqual(reply['kind'], 'over_balance')
        self.assertEqual(Fake.count(), 2)

    def test_money_page_cannot_raise_balance_inside_an_approval(self):
        self.start()
        approval = self.approval(pieces=5, usd=5)
        self.run_ok('s:1:1', approval, balance=0.10)
        reply = self.job('s:1:2', approval, balance=99)  # 0.10 - 0.0535 = 0.0465 is the real room
        self.assertEqual(reply['kind'], 'over_balance')
        self.assertEqual(Fake.count(), 1)

    def test_money_same_job_id_never_charged_twice(self):
        self.start()
        approval = self.approval()
        self.run_ok('s:1:1', approval)
        self.assertEqual(self.job('s:1:1', approval)['kind'], 'already_done')
        self.assertEqual(self.job('s:1:1', self.approval())['kind'], 'already_done')
        self.assertEqual(Fake.count(), 1)

    def test_money_same_job_id_concurrent_starts_send_once(self):
        Fake.scenario = 'hang'
        self.start()
        approval = self.approval()
        replies = []
        threads = [threading.Thread(target=lambda: replies.append(self.job('s:1:1', approval))) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(all(r['accepted'] for r in replies), replies)
        self.assertTrue(Fake.entered.wait(4))
        time.sleep(0.2)
        self.assertEqual(Fake.count(), 1)

    def test_money_identical_request_under_new_id_refused(self):
        self.start()
        approval = self.approval()
        self.run_ok('s:1:1', approval, name='Same')
        reply = self.job('s:1:2', approval, name='Same')
        self.assertEqual(reply['kind'], 'duplicate_request')
        self.assertEqual(Fake.count(), 1)

    def test_money_approval_cannot_change_terms(self):
        self.start()
        self.run_ok('s:1:1', self.approval(pieces=2, usd=1, approval_id='fixed'))
        reply = self.job('s:1:2', self.approval(pieces=2, usd=9, approval_id='fixed'))
        self.assertEqual(reply['kind'], 'approval_mismatch')
        self.assertEqual(Fake.count(), 1)

    def test_money_over_reserve_settles_and_pauses(self):
        Fake.scenario = 'costly'
        self.start()
        approval = self.approval()
        reply = self.run_ok('s:1:1', approval)
        self.assertEqual(reply['status'], 'done')
        self.assertEqual(self.status()['pause']['kind'], 'over_reserve')
        self.assertEqual(self.job('s:1:2', approval)['kind'], 'over_reserve')
        self.assertEqual(Fake.count(), 1)

    def test_money_one_job_at_a_time(self):
        Fake.scenario = 'script'
        self.extra_env['SNIPPY_IMAGE_TIMEOUT'] = '15'
        self.start()
        approval = self.approval()
        self.job('s:1:1', approval)
        self.assertTrue(Fake.entered.wait(4))
        reply = self.job('s:1:2', approval)
        self.assertTrue(reply['accepted'], reply)  # It waits its turn instead of being refused.
        time.sleep(0.4)
        self.assertEqual(Fake.count(), 1)
        self.assertEqual(self.tool('job_status', job_id='s:1:2')['status'], 'queued')
        Fake.release.set()
        self.assertEqual(self.wait('s:1:2')['status'], 'done')
        self.assertEqual((Fake.count(), Fake.max_in_flight), (2, 1))

    def test_money_ledger_survives_restart(self):
        self.start()
        self.run_ok('s:1:1', self.approval())
        self.restart()
        self.assertAlmostEqual(self.status()['spent_usd'], REPORTED_COST)
        self.assertEqual(self.job('s:1:1', self.approval())['kind'], 'already_done')
        self.assertEqual(Fake.count(), 1)

    def test_money_damaged_ledger_refuses_to_start(self):
        self.start()
        self.run_ok('s:1:1', self.approval())
        self.kill()
        (self.data / 'state.json').write_text('{broken')
        process = subprocess.run([sys.executable, '-I', str(DAEMON)], env=self.env(), capture_output=True, timeout=8)
        self.assertEqual(process.returncode, 2)
        self.assertIn(b'ledger', process.stderr)

    def test_money_second_daemon_refused(self):
        self.start()
        env = self.env()
        env['SNIPPY_HELPER_PORT'] = str(free_port())
        process = subprocess.run([sys.executable, '-I', str(DAEMON)], env=env, capture_output=True, timeout=8)
        self.assertEqual(process.returncode, 2)
        self.assertIn(b'another helper', process.stderr)


class StartRules(Base):
    def test_start_older_queue_items_never_picked_up(self):
        self.data.mkdir(mode=0o700)
        (self.data / 'queue.json').write_text(json.dumps({'waiting': ['old:1:%d' % i for i in range(111)]}))
        self.start()
        time.sleep(1.2)
        self.assertEqual(Fake.count(), 0)
        self.assertEqual(self.state()['jobs'] if (self.data / 'state.json').exists() else {}, {})

    def test_start_unapproved_saved_job_not_run(self):
        self.data.mkdir(mode=0o700)
        state = {'version': 1, 'seq': 1, 'approvals': {}, 'pause': None,
                 'jobs': {'old:1:1': {'job_id': 'old:1:1', 'state': 'accepted', 'approval_id': 'gone'}},
                 'reservations': {'old:1:1': {'status': 'reserved', 'reserve_usd': '0.07', 'approval_id': 'gone'}}}
        (self.data / 'state.json').write_text(json.dumps(state))
        self.start()
        time.sleep(1.2)
        self.assertEqual(Fake.count(), 0)
        reply = self.tool('job_status', job_id='old:1:1')
        self.assertEqual((reply['status'], reply['kind']), ('failed', 'price_approval_required'))
        self.assertEqual(self.entry('old:1:1')['status'], 'released')

    def test_start_accepted_and_approved_job_runs_after_restart(self):
        self.start()
        approval = self.approval()
        self.run_ok('s:1:1', approval)
        self.kill()
        state = self.state()
        request = {'model': 'x', 'prompt': 'p'}
        import hashlib
        stem = hashlib.sha256(b's:1:2').hexdigest()[:32]
        (self.data / 'requests' / (stem + '.json')).write_text(json.dumps(request))
        state['jobs']['s:1:2'] = {'job_id': 's:1:2', 'state': 'accepted', 'approval_id': approval['approval_id']}
        state['reservations']['s:1:2'] = {'status': 'reserved', 'reserve_usd': '0.07',
                                          'approval_id': approval['approval_id'], 'request_hash': 'h'}
        (self.data / 'state.json').write_text(json.dumps(state))
        self.start()
        self.assertEqual(self.wait('s:1:2')['status'], 'done')
        self.assertEqual(Fake.count(), 2)

    def test_start_idle_daemon_sends_nothing(self):
        self.start()
        time.sleep(1.2)
        self.assertEqual(Fake.count(), 0)


class FailureCodes(Base):
    def check(self, scenario, code):
        Fake.scenario = scenario
        self.start()
        reply = self.run_ok('s:1:1', self.approval())
        self.assertEqual((reply['status'], reply['kind']), ('failed', code))
        self.assertExplains(reply)
        return reply

    def test_code_bad_gateway(self):
        self.check('502', 'bad_gateway')

    def test_code_garbage(self):
        self.check('garbage', 'garbage')

    def test_code_garbage_not_a_png_settles_reported_cost(self):
        reply = self.check('not_png', 'garbage')
        self.assertAlmostEqual(reply['cost_usd'], REPORTED_COST)

    def test_code_picture_without_price_is_held(self):
        self.check('no_usage', 'garbage')
        self.assertEqual(self.entry('s:1:1')['status'], 'held')

    def test_code_no_transparency_rgb(self):
        reply = self.check('noalpha', 'no_transparency')
        self.assertEqual(reply['charged'], 'yes')
        self.assertEqual(self.entry('s:1:1')['status'], 'settled')

    def test_code_no_transparency_fully_opaque_rgba(self):
        self.check('opaque', 'no_transparency')

    def test_code_empty_body(self):
        self.check('empty', 'empty')

    def test_code_empty_data(self):
        self.check('nodata', 'empty')

    def test_code_timeout(self):
        self.extra_env['SNIPPY_IMAGE_TIMEOUT'] = '0.5'
        self.check('hang', 'timeout')

    def test_code_dropped(self):
        self.check('drop', 'dropped')

    def test_code_dropped_mid_body(self):
        self.check('truncated', 'dropped')

    def test_code_401(self):
        self.assertEqual(self.check('401', '401')['charged'], 'no')

    def test_code_403(self):
        self.assertEqual(self.check('403', '403')['charged'], 'no')

    def test_code_429(self):
        self.assertEqual(self.check('429', '429')['charged'], 'no')

    def test_code_500_is_held(self):
        self.assertEqual(self.check('500', '500')['charged'], 'maybe')
        self.assertEqual(self.entry('s:1:1')['status'], 'held')

    def test_code_503_is_held(self):
        self.assertEqual(self.check('503', '503')['charged'], 'maybe')
        self.assertEqual(self.entry('s:1:1')['status'], 'held')

    def test_code_out_of_credit(self):
        self.assertEqual(self.check('quota', 'insufficient_quota')['charged'], 'no')

    def test_code_bad_request_holds_piece_only(self):
        self.check('400', 'bad_request')
        self.assertIsNone(self.status()['pause'])

    def test_code_redirect_not_followed_key_not_forwarded(self):
        self.check('redirect', 'service_error')
        self.assertEqual(Fake.redirect_hits, 0)
        self.assertEqual(self.entry('s:1:1')['status'], 'held')


class KeyRules(Base):
    def test_key_sent_only_as_bearer_to_the_service(self):
        self.start()
        self.run_ok('s:1:1', self.approval())
        self.assertEqual(Fake.calls[0]['auth'], 'Bearer ' + FAKE_KEY)
        self.assertNotIn(FAKE_KEY, json.dumps(Fake.calls[0]['body']))

    def test_key_read_only_at_call_time(self):
        self.key_file.unlink()
        self.start()  # No key on disk and the helper still starts.
        self.assertTrue(self.status()['ok'])
        self.key_file.write_text(FAKE_KEY)
        self.key_file.chmod(0o600)
        self.assertEqual(self.run_ok('s:1:1', self.approval())['status'], 'done')

    def test_key_missing_sends_nothing_and_pauses(self):
        self.key_file.unlink()
        self.start()
        reply = self.run_ok('s:1:1', self.approval())
        self.assertEqual((reply['kind'], reply['charged']), ('no_key', 'no'))
        self.assertEqual(self.status()['pause']['kind'], 'no_key')
        self.assertEqual(Fake.count(), 0)

    def test_key_with_open_permissions_refused(self):
        self.key_file.chmod(0o644)
        self.start()
        self.assertEqual(self.run_ok('s:1:1', self.approval())['kind'], 'key_problem')
        self.assertEqual(Fake.count(), 0)

    def test_key_never_stored_logged_or_returned(self):
        self.start()
        replies = [self.run_ok('s:1:1', self.approval())]
        Fake.scenario = '401'
        replies.append(self.run_ok('s:1:2', self.approval()))
        replies.append(self.status())
        self.kill()
        self.assertNotIn(FAKE_KEY, json.dumps(replies))
        self.assertNotIn(FAKE_KEY.encode(), self.log_path.read_bytes())
        for path in self.data.rglob('*'):
            if path.is_file():
                self.assertNotIn(FAKE_KEY.encode(), path.read_bytes(), path.name)

    def test_key_not_sent_to_other_hosts(self):
        self.extra_env['SNIPPY_IMAGE_URL'] = 'https://example.invalid/v1/images/edits'
        process = subprocess.run([sys.executable, '-I', str(DAEMON)], env=self.env(), capture_output=True, timeout=8)
        self.assertEqual(process.returncode, 2)
        self.assertIn(b'SNIPPY_IMAGE_URL', process.stderr)


class PiecesKitRules(Base):
    """Team kit additions: key setup, Drive copy, settings, per-user config folder."""

    def load_daemon(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('snippy_daemon_kit_test', DAEMON)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_kit_missing_key_fails_loudly_with_plain_message(self):
        self.key_file.unlink()
        self.start()
        status = self.status()
        self.assertFalse(status['setup']['key_ready'])
        self.assertIn('No OpenAI key set up. Run the Snippy setup.', status['setup']['problems'])
        reply = self.run_ok('s:1:1', self.approval())
        self.assertEqual(reply['kind'], 'no_key')
        self.assertIn('No OpenAI key set up. Run the Snippy setup.', reply['message'])
        self.assertEqual(Fake.count(), 0)
        self.kill()
        self.assertIn('No OpenAI key set up. Run the Snippy setup.', self.log_path.read_text())

    def test_kit_key_never_in_logs_status_or_job_records(self):
        self.start()
        replies = [self.run_ok('s:1:1', self.approval()), self.status()]
        self.kill()
        self.assertNotIn(FAKE_KEY, json.dumps(replies))
        self.assertNotIn(FAKE_KEY.encode(), self.log_path.read_bytes())
        for path in list(self.data.rglob('*')) + list(self.drive.rglob('*')) + list(self.pieces.rglob('*')):
            if path.is_file():
                self.assertNotIn(FAKE_KEY.encode(), path.read_bytes(), path.name)

    def test_kit_drive_copy_happens(self):
        self.start()
        reply = self.run_ok('s:1:1', self.approval(), source_name='Summer collage')
        self.assertEqual(reply['status'], 'done')
        copies = list((self.drive / 'Test Artist' / 'Summer collage').glob('*.png'))
        self.assertEqual([c.name for c in copies], ['Piece s_1_1.png'])
        self.assertEqual(copies[0].read_bytes(), png())
        self.assertEqual(list((self.drive / 'Test Artist').glob('*.png')), [])  # nothing loose beside the picture folders
        self.assertEqual(len(list((self.pieces / 'Summer collage').glob('*.png'))), 1)
        status = self.status()
        self.assertEqual((status['delivered_to_drive'], status['delivery_failed']), (1, []))

    def test_kit_drive_copy_without_a_picture_name_uses_a_visible_folder(self):
        self.start()
        self.run_ok('s:1:1', self.approval())
        self.assertEqual([c.name for c in (self.drive / 'Test Artist' / 'Untitled picture').glob('*.png')], ['Piece s_1_1.png'])

    def test_kit_drive_copy_one_folder_per_picture(self):
        self.start()
        self.run_ok('s:1:1', self.approval(), source_name='Summer collage')
        self.run_ok('t:1:1', self.approval(), source_name='Winter: sale?')  # characters Windows refuses become _
        self.assertEqual([c.name for c in (self.drive / 'Test Artist' / 'Summer collage').glob('*.png')], ['Piece s_1_1.png'])
        self.assertEqual([c.name for c in (self.drive / 'Test Artist' / 'Winter_ sale_').glob('*.png')], ['Piece t_1_1.png'])

    def test_kit_drive_copy_never_overwrites(self):
        folder = self.drive / 'Test Artist' / 'Summer collage'
        folder.mkdir(parents=True)
        (folder / 'Piece s_1_1.png').write_bytes(b'someone elses work')
        self.start()
        self.run_ok('s:1:1', self.approval(), source_name='Summer collage')
        self.assertEqual((folder / 'Piece s_1_1.png').read_bytes(), b'someone elses work')
        self.assertEqual((folder / 'Piece s_1_1-2.png').read_bytes(), png())

    def test_kit_drive_copy_failure_shows_in_status(self):
        blocker = self.tmp / 'not-a-folder'
        blocker.write_text('a file where the Drive folder should be')
        self.write_settings(drive_folder=str(blocker))
        self.start()
        reply = self.run_ok('s:1:1', self.approval())
        self.assertEqual(reply['status'], 'done')  # The cut is kept; only the copy failed.
        self.assertTrue(reply['delivery']['drive_error'])
        failed = self.status()['delivery_failed']
        self.assertEqual([f['job_id'] for f in failed], ['s:1:1'])
        self.assertTrue(failed[0]['drive_error'])

    def test_kit_drive_folder_not_set_up_still_cuts_and_says_so(self):
        self.write_settings(drive_folder=None)
        self.start()
        reply = self.run_ok('s:1:1', self.approval())
        self.assertEqual(reply['status'], 'done')
        status = self.status()
        self.assertIn('Drive folder not set up', status['setup']['problems'])
        self.assertEqual(status['delivery_failed'][0]['drive_error'], 'Drive folder not set up')

    def test_kit_no_balance_cuts_inside_the_limit(self):
        self.write_settings(budget_usd=5)
        self.start()
        reply = self.job('s:1:1', self.approval(), balance=None, source_name='Summer collage')
        self.assertTrue(reply['accepted'], reply)
        self.assertEqual(self.wait('s:1:1')['status'], 'done')
        status = self.status()
        self.assertEqual(status['budget_usd'], 5)
        self.assertAlmostEqual(status['budget_left_usd'], 5 - status['spent_usd'], places=4)

    def test_kit_no_balance_and_no_budget_still_refuses_loudly(self):
        self.write_settings(budget_usd=None)
        self.start()
        reply = self.job('s:1:1', self.approval(), balance=None)
        self.assertEqual((reply['accepted'], reply['kind']), (False, 'budget_not_set'))
        self.assertEqual(Fake.count(), 0)
        self.assertIsNone(self.status()['budget_usd'])
        self.assertIn('Snippy spending limit', reply['message'])
        self.assertNotIn('run budget', reply['message'])
        self.assertIn('Ask Claude to set your Snippy spending limit', reply['next_step'])

    def test_kit_no_balance_over_limit_says_it_in_plain_words(self):
        self.write_settings(budget_usd=0.10)
        self.start()
        self.assertTrue(self.job('s:1:1', self.approval(), balance=None)['accepted'])
        self.wait('s:1:1')
        reply = self.job('s:1:2', self.approval(), balance=None)
        self.assertEqual((reply['accepted'], reply['kind']), (False, 'over_budget'))
        self.assertIn('Your Snippy spending limit ($0.1) is used up', reply['message'])
        self.assertNotIn('budget_usd', reply['message'] + reply['next_step'])
        self.assertIn('Ask Claude', reply['next_step'])

    def test_kit_balance_when_given_is_still_checked(self):
        self.write_settings(budget_usd=100)
        self.start()
        reply = self.job('s:1:1', self.approval(), balance=0.01)
        self.assertEqual((reply['accepted'], reply['kind']), (False, 'over_balance'))

    def test_kit_default_port_is_not_the_owners(self):
        import importlib.util
        module = self.load_daemon()
        self.assertEqual(module.DEFAULT_PORT, 18830)
        spec = importlib.util.spec_from_file_location('snippy_mcp_default_port', MCP)
        mcp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mcp)
        self.assertEqual(mcp.DEFAULT_PORT, 18830)

    def test_kit_output_folder_has_one_folder_per_picture(self):
        self.start()
        self.run_ok('s:1:1', self.approval(), source_name='Summer collage')
        self.run_ok('t:1:1', self.approval(), source_name='Winter collage')
        self.assertEqual([c.name for c in (self.pieces / 'Summer collage').glob('*.png')], ['Piece s_1_1.png'])
        self.assertEqual([c.name for c in (self.pieces / 'Winter collage').glob('*.png')], ['Piece t_1_1.png'])
        self.assertEqual(list(self.pieces.glob('*.png')), [])

    def test_kit_no_budget_sends_nothing(self):
        self.write_settings(budget_usd=None)
        self.start()
        reply = self.job('s:1:1', self.approval())
        self.assertEqual((reply['accepted'], reply['kind']), (False, 'budget_not_set'))
        self.assertEqual(Fake.count(), 0)

    def test_kit_reserve_grows_to_one_and_a_half_times_the_dearest_cost(self):
        Fake.scenario = 'costly'
        self.start()
        self.assertEqual(self.run_ok('s:1:1', self.approval())['status'], 'done')
        status = self.status()
        spent = status['spent_usd']
        self.write_settings(budget_usd=spent * 2)  # spent + 0.07 fits; spent + 1.5 x spent does not
        self.assertGreater(spent, 0.07)
        self.assertEqual(status['pause']['kind'], 'over_reserve')
        self.assertAlmostEqual(status['reserve_usd_per_piece'], spent * 1.5, places=3)
        Fake.scenario = 'ok'
        # The old flat $0.07 reserve would pass here (spent + 0.07 is under 0.8); the grown reserve must not.
        self.assertLess(spent + 0.07, spent * 2)
        reply = self.job('s:1:2', self.approval())
        self.assertEqual((reply['accepted'], reply.get('kind')), (False, 'over_budget'), (reply, status))
        self.assertEqual(Fake.count(), 1)

    def test_kit_over_reserve_pause_blocks_sends_until_resume_is_approved(self):
        Fake.scenario = 'costly'
        self.start()
        self.run_ok('s:1:1', self.approval())
        Fake.scenario = 'ok'
        self.assertEqual(self.job('s:1:2', {**self.approval(), 'resume_pause_id': None})['kind'], 'over_reserve')
        self.assertEqual(Fake.count(), 1)
        self.assertTrue(self.job('s:1:3', self.approval())['accepted'])

    def test_kit_own_budget_is_a_hard_stop(self):
        self.write_settings(budget_usd=0.10)
        self.start()
        self.assertTrue(self.job('s:1:1', self.approval())['accepted'])
        self.wait('s:1:1')
        reply = self.job('s:1:2', self.approval())
        self.assertEqual((reply['accepted'], reply['kind']), (False, 'over_budget'))

    def test_kit_windows_config_dir_when_platform_is_win32(self):
        module = self.load_daemon()
        saved = dict(os.environ)
        try:
            os.environ.pop('SNIPPY_CONFIG_DIR', None)
            os.environ['APPDATA'] = 'C:\\Users\\x\\AppData\\Roaming'
            module.sys = type('S', (), {'platform': 'win32'})()
            result = module.config_dir()
            self.assertEqual(result.name, 'SnippyPieces')
            self.assertEqual(str(result.parent), str(Path('C:\\Users\\x\\AppData\\Roaming')))
            self.assertTrue(module.private_enough(Path(__file__)))
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_kit_lock_uses_msvcrt_when_no_fcntl_and_fails_loudly_with_neither(self):
        module = self.load_daemon()
        calls = []
        fake = type('M', (), {'LK_NBLCK': 2, 'locking': staticmethod(lambda fd, mode, n: calls.append((mode, n)))})
        handle = open(self.tmp / 'x.lock', 'a')
        self.addCleanup(handle.close)
        module.fcntl, module.msvcrt = None, fake
        module.lock_exclusive(handle)
        self.assertEqual(calls, [(2, 1)])
        module.msvcrt = None
        with self.assertRaises(module.LockUnavailable):
            module.lock_exclusive(handle)

    def test_kit_second_helper_refused_by_lock_and_pid_file_written(self):
        self.start()
        self.assertEqual(int((self.data / 'daemon.pid').read_text()), self.process.pid)
        second = subprocess.run([sys.executable, '-I', str(DAEMON)], env=self.env(), capture_output=True, text=True, timeout=20)
        self.assertEqual(second.returncode, 2)
        self.assertIn('already using', second.stderr)

    def test_kit_mac_and_linux_config_dirs(self):
        module = self.load_daemon()
        saved = dict(os.environ)
        try:
            os.environ.pop('SNIPPY_CONFIG_DIR', None)
            os.environ['HOME'] = str(self.tmp)
            os.environ['USERPROFILE'] = str(self.tmp)
            module.sys = type('S', (), {'platform': 'darwin'})()
            self.assertEqual(module.config_dir(), Path.home() / 'Library' / 'Application Support' / 'SnippyPieces')
            module.sys = type('S', (), {'platform': 'linux'})()
            self.assertEqual(module.config_dir(), Path.home() / '.config' / 'snippy-pieces')
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_kit_key_from_env_var_is_used(self):
        module = self.load_daemon()
        os.environ['SNIPPY_OPENAI_KEY'] = FAKE_KEY
        self.addCleanup(lambda: os.environ.pop('SNIPPY_OPENAI_KEY', None))
        self.assertEqual(module.read_key(self.tmp / 'no-such-file'), FAKE_KEY)

    def test_no_private_specifics_left(self):
        import re
        text = (HERE / 'daemon.py').read_text() + (HERE / 'mcp_server.py').read_text()
        for name, pattern in (('home path', r'/Users/\w'), ('Windows home path', r'[A-Za-z]:\\Users\\\w'),
                              ('key', r'sk-[A-Za-z0-9_\-]{20,}'), ('artifact id', r'claude\.ai/artifact/[\w-]+'),
                              ('email', r'[\w.+-]+@[\w-]+\.[\w.]+')):
            self.assertIsNone(re.search(pattern, text), name)

    def test_kit_helper_messages_have_no_desk_or_this_mac(self):
        module = self.load_daemon()
        shown = json.dumps(module.REASONS) + (HERE / 'mcp_server.py').read_text()
        for needle in ('this Mac',):
            self.assertNotIn(needle, shown, needle)

    def test_kit_too_big_picture_gets_a_plain_answer(self):
        self.start()
        token = (self.data / 'access-token').read_text().strip()
        body = b'{"job_id":"x:1:1","pad":"' + b'a' * (33 * 1024 * 1024) + b'"}'
        status, reply = self.raw('/tool/start_job', body, {'Authorization': 'Bearer ' + token})
        self.assertEqual(status, 413)
        self.assertFalse(reply['accepted'])
        self.assertEqual(reply['message'], 'This picture is too big to send (limit about 20 MB). Make it smaller and try again.')
        self.assertEqual(Fake.count(), 0)
        self.assertEqual(self.status()['setup']['key_ready'], True)  # the helper is still answering afterwards

    def test_kit_windows_device_names_get_a_trailing_underscore(self):
        module = self.load_daemon()
        for name in ('CON', 'prn', 'Aux', 'NUL', 'COM1', 'com9', 'LPT1', 'lpt9', 'con.txt', 'NUL.png'):
            self.assertEqual(module.clean_part(name, 'x'), name + '_', name)
        for name in ('Console', 'COM10', 'COM0', 'CONX', 'icon', 'Fox \U0001f98a', 'LPT'):
            self.assertEqual(module.clean_part(name, 'x'), name, name)

    def test_kit_device_named_picture_and_piece_land_in_safe_names(self):
        self.start()
        self.run_ok('s:1:1', self.approval(), source_name='CON', name='AUX')
        self.assertEqual([c.name for c in (self.drive / 'Test Artist' / 'CON_').glob('*.png')], ['AUX_.png'])


class ServiceRules(Base):
    def test_service_binds_only_to_loopback(self):
        self.start()
        out = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(self.process.pid), '-iTCP', '-sTCP:LISTEN'],
                             capture_output=True, text=True, timeout=10).stdout
        listeners = re.findall(r'TCP (\S+) \(LISTEN\)', out)
        self.assertEqual(listeners, ['127.0.0.1:%d' % self.port])

    def test_service_token_file_is_private(self):
        self.start()
        self.assertEqual(stat.S_IMODE((self.data / 'access-token').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.data / 'state.json').stat().st_mode) if (self.data / 'state.json').exists() else 0o600, 0o600)

    def test_service_token_survives_restart(self):
        self.start()
        before = (self.data / 'access-token').read_text()
        self.restart()
        self.assertEqual((self.data / 'access-token').read_text(), before)

    def test_service_refuses_wrong_token(self):
        self.start()
        status, reply = self.raw('/tool/status', b'{}', {'Authorization': 'Bearer wrong'})
        self.assertEqual((status, reply['kind']), (401, 'bad_token'))

    def test_service_refuses_web_origin_and_foreign_host(self):
        self.start()
        token = (self.data / 'access-token').read_text().strip()
        auth = {'Authorization': 'Bearer ' + token}
        self.assertEqual(self.raw('/tool/status', b'{}', {**auth, 'Origin': 'https://example.com'})[0], 403)
        self.assertEqual(self.raw('/tool/status', b'{}', {**auth, 'Host': 'evil.example'})[0], 403)

    def test_service_refuses_blocked_port(self):
        self.extra_env['SNIPPY_HELPER_PORT'] = '18792'
        process = subprocess.run([sys.executable, '-I', str(DAEMON)], env=self.env(), capture_output=True, timeout=8)
        self.assertEqual(process.returncode, 2)

    def test_service_get_result_chunks_round_trip(self):
        self.start()
        reply = self.run_ok('s:1:1', self.approval())
        text = ''
        offset = 0
        for _ in range(50):
            part = self.tool('get_result', job_id='s:1:1', offset=offset)
            text += part['data']
            self.assertAlmostEqual(part['cost_usd'], REPORTED_COST)
            if part['done']:
                break
            offset = part['next_offset']
        self.assertEqual(base64.b64decode(text), png())
        self.assertEqual(reply['status'], 'done')

    def test_service_get_result_large_file_in_chunks(self):
        self.start()
        self.run_ok('s:1:1', self.approval())
        import hashlib
        path = self.data / 'results' / (hashlib.sha256(b's:1:1').hexdigest()[:32] + '.png')
        blob = os.urandom(700001)
        path.write_bytes(blob)
        text = ''
        offset = 0
        parts = 0
        while True:
            part = self.tool('get_result', job_id='s:1:1', offset=offset)
            self.assertLessEqual(len(part['data']), 256 * 1024)
            text += part['data']
            parts += 1
            if part['done']:
                break
            offset = part['next_offset']
        self.assertGreater(parts, 1)
        self.assertEqual(base64.b64decode(text), blob)

    def test_service_unknown_job_status(self):
        self.start()
        self.assertEqual(self.tool('job_status', job_id='nope')['status'], 'unknown')


class CancelRules(Base):
    def test_cancel_during_call_holds_and_never_resends(self):
        Fake.scenario = 'hang'
        self.extra_env['SNIPPY_IMAGE_TIMEOUT'] = '20'
        self.start()
        self.job('s:1:1', self.approval())
        self.assertTrue(Fake.entered.wait(4))
        reply = self.tool('cancel', job_id='s:1:1')
        self.assertEqual((reply['status'], reply['charged']), ('cancelling', 'maybe'))
        final = self.wait('s:1:1', seconds=4)
        self.assertEqual((final['status'], final['kind'], final['charged']), ('cancelled', 'cancelled', 'maybe'))
        self.assertEqual(self.entry('s:1:1')['status'], 'held')
        Fake.scenario = 'ok'
        self.assertEqual(self.job('s:1:1', self.approval())['kind'], 'duplicate_job')
        self.assertEqual(Fake.count(), 1)

    def test_cancel_unknown_job(self):
        self.start()
        self.assertEqual(self.tool('cancel', job_id='nope')['kind'], 'unknown_job')

    def test_cancel_finished_job_changes_nothing(self):
        self.start()
        self.run_ok('s:1:1', self.approval())
        self.assertEqual(self.tool('cancel', job_id='s:1:1')['changed'], False)
        self.assertEqual(self.entry('s:1:1')['status'], 'settled')

    def test_cancel_unsent_job_with_no_key_releases_money(self):
        self.key_file.unlink()  # Nothing can be sent while this job is set up.
        self.start()
        approval = self.approval()
        self.kill()
        self.data.mkdir(exist_ok=True)
        import hashlib
        stem = hashlib.sha256(b's:1:9').hexdigest()[:32]
        (self.data / 'requests').mkdir(exist_ok=True)
        (self.data / 'requests' / (stem + '.json')).write_text('{}')
        state = {'version': 1, 'seq': 5, 'pause': None,
                 'approvals': {approval['approval_id']: {'approval_id': approval['approval_id'], 'piece_count': 5,
                                                          'approved_usd': '1.0', 'balance_usd': '50',
                                                          'settled_at_anchor': '0', 'seq': 1}},
                 'jobs': {'s:1:9': {'job_id': 's:1:9', 'state': 'accepted', 'approval_id': approval['approval_id']}},
                 'reservations': {'s:1:9': {'status': 'reserved', 'reserve_usd': '0.07',
                                            'approval_id': approval['approval_id'], 'request_hash': 'h'}}}
        (self.data / 'state.json').write_text(json.dumps(state))
        self.start()
        reply = self.wait('s:1:9')
        self.assertEqual(reply['charged'], 'no')
        self.assertEqual(self.entry('s:1:9')['status'], 'released')
        self.assertEqual(Fake.count(), 0)


class InProcess(Base):
    """The engine with no worker running, so nothing can be sent while a job waits."""

    def engine(self):
        import importlib.util
        from decimal import Decimal
        spec = importlib.util.spec_from_file_location('snippy_daemon_under_test', DAEMON)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.data.mkdir(mode=0o700, exist_ok=True)
        return module.Engine(self.data, IMAGE_URL, str(self.key_file), Decimal('0.07'), 2,
                             originals_dir=self.tmp / 'originals')

    def args(self, job_id='s:1:1'):
        return {'job_id': job_id, 'approval': self.approval(), 'balance_usd': 50, 'counted_job_ids': [], 'name': 'Piece',
                'source_image_data': SOURCE, 'box': [1, 2, 3, 4]}

    def test_cancel_before_send_releases_money_and_sends_nothing(self):
        engine = self.engine()
        self.assertTrue(engine.tool_start_job(self.args())['accepted'])
        reply = engine.tool_cancel({'job_id': 's:1:1'})
        self.assertEqual((reply['status'], reply['charged']), ('cancelled', 'no'))
        self.assertEqual(engine.state['reservations']['s:1:1']['status'], 'released')
        self.assertIsNone(engine.next_runnable())
        self.assertEqual(Fake.count(), 0)

    def test_money_reservation_that_cannot_be_saved_means_no_job(self):
        engine = self.engine()
        def broken():
            raise OSError('disk full')
        engine.save = broken
        reply = engine.tool_start_job(self.args())
        self.assertEqual((reply['accepted'], reply['kind']), (False, 'storage_error'))
        self.assertEqual(engine.state['jobs'], {})
        self.assertEqual(engine.state['reservations'], {})
        self.assertIsNone(engine.next_runnable())
        self.assertEqual(Fake.count(), 0)

    def test_money_unsaved_send_marker_means_no_call(self):
        engine = self.engine()
        self.assertTrue(engine.tool_start_job(self.args())['accepted'])
        def broken():
            raise OSError('disk full')
        engine.save = broken
        engine.run_job('s:1:1')
        self.assertEqual(Fake.count(), 0)
        self.assertEqual(engine.state['pause']['kind'], 'storage_error')
        self.assertEqual(engine.state['jobs']['s:1:1']['charged'], 'no')


class ResolveHold(Base):
    def held(self, job_id='h:1:1', approval=None, **extra):
        Fake.scenario = 'drop'
        if not self.process:
            self.start()
        reply = self.run_ok(job_id, approval or self.approval(), **extra)
        self.assertEqual(self.entry(job_id)['status'], 'held')
        Fake.scenario = 'ok'
        return reply

    def test_resolve_only_works_on_a_held_job(self):
        self.start()
        self.assertEqual(self.tool('resolve_hold', job_id='nope', outcome='charged')['kind'], 'unknown_job')
        self.run_ok('h:1:done', self.approval())  # finished and settled
        Fake.scenario = 'noalpha'
        self.run_ok('h:1:paid', self.approval())  # failed, but its cost was reported and settled
        Fake.scenario = '401'
        self.run_ok('h:1:refused', self.approval())  # refused, reservation already released
        for job_id in ('h:1:done', 'h:1:paid', 'h:1:refused'):
            with self.subTest(job_id):
                before = (self.data / 'state.json').read_bytes()
                reply = self.tool('resolve_hold', job_id=job_id, outcome='not_charged')
                self.assertEqual((reply['ok'], reply['kind']), (False, 'not_held'))
                self.assertExplains(reply)
                self.assertEqual((self.data / 'state.json').read_bytes(), before)

    def test_resolve_refuses_a_job_still_running(self):
        Fake.scenario = 'hang'
        self.start()
        self.job('h:1:1', self.approval())
        self.assertTrue(Fake.entered.wait(4))
        reply = self.tool('resolve_hold', job_id='h:1:1', outcome='not_charged')
        self.assertEqual((reply['ok'], reply['kind']), (False, 'not_held'))
        self.assertEqual(self.entry('h:1:1')['status'], 'reserved')

    def test_resolve_refuses_bad_input_without_changing_ledger(self):
        self.held()
        before = (self.data / 'state.json').read_bytes()
        for args in ({'outcome': 'maybe'}, {'outcome': None}, {'outcome': 'charged', 'charged_usd': 0},
                     {'outcome': 'charged', 'charged_usd': -1}, {'outcome': 'charged', 'charged_usd': 'NaN'},
                     {'outcome': 'charged', 'charged_usd': True}, {'outcome': 'charged', 'charged_usd': 1e30},
                     {'outcome': 'not_charged', 'charged_usd': 0.05}):
            with self.subTest(args):
                self.assertEqual(self.tool('resolve_hold', job_id='h:1:1', **args)['kind'], 'bad_input')
        self.assertEqual((self.data / 'state.json').read_bytes(), before)

    def test_resolve_not_charged_releases_reservation(self):
        self.held()
        self.assertAlmostEqual(self.status()['reserved_usd'], 0.07)
        reply = self.tool('resolve_hold', job_id='h:1:1', outcome='not_charged')
        self.assertEqual((reply['ok'], reply['reservation'], reply['charged_usd']), (True, 'released', 0))
        self.assertEqual(self.entry('h:1:1')['status'], 'released')
        status = self.status()
        self.assertEqual((status['reserved_usd'], status['spent_usd']), (0, 0))
        self.assertEqual(self.tool('job_status', job_id='h:1:1')['charged'], 'no')

    def test_resolve_charged_settles_at_given_amount(self):
        self.held()
        reply = self.tool('resolve_hold', job_id='h:1:1', outcome='charged', charged_usd=0.061)
        self.assertEqual((reply['ok'], reply['reservation']), (True, 'settled'))
        self.assertEqual(self.entry('h:1:1')['actual_usd'], '0.061')
        status = self.status()
        self.assertAlmostEqual(status['spent_usd'], 0.061)
        self.assertEqual(status['reserved_usd'], 0)
        final = self.tool('job_status', job_id='h:1:1')
        self.assertEqual(final['charged'], 'yes')
        self.assertAlmostEqual(final['cost_usd'], 0.061)

    def test_resolve_charged_defaults_to_reserved_amount(self):
        self.held()
        self.assertTrue(self.tool('resolve_hold', job_id='h:1:1', outcome='charged')['ok'])
        self.assertEqual(self.entry('h:1:1')['actual_usd'], '0.07')
        self.assertAlmostEqual(self.status()['spent_usd'], 0.07)

    def test_resolve_never_sends_or_requeues_and_retry_needs_new_approval(self):
        approval = self.approval()
        self.held(approval=approval)
        calls = Fake.count()
        self.assertTrue(self.tool('resolve_hold', job_id='h:1:1', outcome='not_charged')['ok'])
        time.sleep(1.2)
        self.assertEqual(Fake.count(), calls)
        self.assertEqual(self.tool('job_status', job_id='h:1:1')['status'], 'failed')
        self.assertEqual(self.job('h:1:1', self.approval())['kind'], 'duplicate_job')
        self.assertEqual(self.job('h:1:1:retry1', approval)['kind'], 'approval_stale')
        self.assertEqual(Fake.count(), calls)
        self.assertEqual(self.run_ok('h:1:1:retry1', self.approval())['status'], 'done')
        self.assertEqual(Fake.count(), calls + 1)

    def test_resolve_does_not_lift_a_pause(self):
        Fake.scenario = 'hang'
        self.start()
        approval = self.approval()
        self.job('h:1:1', approval)
        self.assertTrue(Fake.entered.wait(4))
        self.restart()
        Fake.scenario = 'ok'
        self.assertTrue(self.tool('resolve_hold', job_id='h:1:1', outcome='not_charged')['ok'])
        self.assertEqual(self.status()['pause']['kind'], 'needs_check')
        self.assertEqual(self.job('h:1:2', approval)['kind'], 'needs_check')
        time.sleep(1.2)
        self.assertEqual(Fake.count(), 1)

    def test_resolve_twice_cannot_change_ledger_twice(self):
        self.held('h:1:1')
        self.held('h:1:2')
        self.assertTrue(self.tool('resolve_hold', job_id='h:1:1', outcome='charged', charged_usd=0.05)['ok'])
        self.assertTrue(self.tool('resolve_hold', job_id='h:1:2', outcome='not_charged')['ok'])
        before = (self.data / 'state.json').read_bytes()
        repeats = ({'job_id': 'h:1:1', 'outcome': 'charged', 'charged_usd': 0.05},
                   {'job_id': 'h:1:1', 'outcome': 'charged', 'charged_usd': 0.09},
                   {'job_id': 'h:1:1', 'outcome': 'not_charged'},
                   {'job_id': 'h:1:2', 'outcome': 'not_charged'},
                   {'job_id': 'h:1:2', 'outcome': 'charged'})
        for _ in range(2):
            for args in repeats:
                reply = self.tool('resolve_hold', **args)
                self.assertEqual((reply['ok'], reply['changed'], reply['kind']), (False, False, 'already_resolved'))
            self.restart()
        self.assertEqual((self.data / 'state.json').read_bytes(), before)
        self.assertAlmostEqual(self.status()['spent_usd'], 0.05)

    def test_resolve_concurrent_calls_change_ledger_once(self):
        self.held()
        replies = []
        threads = [threading.Thread(target=lambda: replies.append(
            self.tool('resolve_hold', job_id='h:1:1', outcome='charged', charged_usd=0.05))) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for reply in replies if reply['ok']), 1)
        self.assertAlmostEqual(self.status()['spent_usd'], 0.05)

    def test_resolve_until_called_reserved_money_stays_counted(self):
        approval = self.approval(pieces=5, usd=5)
        self.held('h:1:1', approval=approval, balance=0.10)
        self.restart()  # The hold survives a restart and is still counted.
        self.assertAlmostEqual(self.status()['reserved_usd'], 0.07)
        self.assertEqual(self.job('h:1:2', approval, balance=0.10)['kind'], 'over_balance')
        self.assertEqual(Fake.count(), 1)
        self.assertTrue(self.tool('resolve_hold', job_id='h:1:1', outcome='not_charged')['ok'])
        self.assertEqual(self.run_ok('h:1:2', approval, balance=0.10)['status'], 'done')

    def test_resolve_unsaved_change_leaves_ledger_alone(self):
        import importlib.util
        from decimal import Decimal
        self.held()
        self.kill()
        spec = importlib.util.spec_from_file_location('snippy_daemon_resolve_test', DAEMON)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        engine = module.Engine(self.data, IMAGE_URL, str(self.key_file), Decimal('0.07'), 2)
        def broken():
            raise OSError('disk full')
        engine.save = broken
        reply = engine.tool_resolve_hold({'job_id': 'h:1:1', 'outcome': 'not_charged'})
        self.assertEqual((reply['ok'], reply['kind']), (False, 'storage_error'))
        self.assertEqual(engine.state['reservations']['h:1:1']['status'], 'held')


class QueueRules(Base):
    """Approved pieces wait in the helper and run after the page closes."""

    def setUp(self):
        super().setUp()
        self.extra_env['SNIPPY_IMAGE_TIMEOUT'] = '15'

    def fill(self, count, approval=None, prefix='q', names=None):
        """The first piece stays inside the fake service until the gate opens. The rest wait."""
        Fake.scenario = 'script'
        if not self.process:
            self.start()
        approval = approval or self.approval(pieces=count, usd=round(count * 0.07, 2))
        ids = ['%s:1:%s' % (prefix, name) for name in (names or range(count))]
        for job_id in ids:
            reply = self.job(job_id, approval)
            self.assertTrue(reply['accepted'], reply)
            if job_id == ids[0]:
                self.assertTrue(Fake.entered.wait(4))
        return ids, approval

    def open_gate(self, *script):
        with Fake.lock:
            Fake.script = list(script)
        Fake.release.set()

    def sent_names(self):
        with Fake.lock:
            prompts = [call['body']['prompt'] for call in Fake.calls]
        return [re.search(r'Piece (\S+)\. Its', prompt).group(1) for prompt in prompts]

    def assertReturned(self, job_id):
        reply = self.tool('job_status', job_id=job_id)
        self.assertEqual((reply['status'], reply['state'], reply['kind'], reply['charged'], reply['resendable']),
                         ('returned', 'returned', 'returned', 'no', True))
        self.assertExplains(reply)
        self.assertEqual(self.entry(job_id)['status'], 'released')

    def unsent_files(self):
        return list((self.data / 'requests').glob('*.json'))

    def direct_engine(self):
        import importlib.util
        from decimal import Decimal
        spec = importlib.util.spec_from_file_location('snippy_daemon_queue_test', DAEMON)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.data.mkdir(mode=0o700, exist_ok=True)
        return module.Engine(self.data, IMAGE_URL, str(self.key_file), Decimal('0.07'), 15)

    def direct_args(self, job_id, approval):
        return {'job_id': job_id, 'approval': approval, 'balance_usd': 50, 'counted_job_ids': [], 'name': 'Piece ' + job_id,
                'source_image_data': SOURCE, 'box': [1, 2, 3, 4]}

    # Rule 1: several wait, one runs, in the order received.
    def test_queue_runs_one_at_a_time_in_order_received(self):
        ids, _ = self.fill(5, names=['z', 'a', 'm', 'c', 'b'])
        time.sleep(0.3)
        self.assertEqual(Fake.count(), 1)
        self.open_gate()
        for job_id in ids:
            self.assertEqual(self.wait(job_id)['status'], 'done')
        self.assertEqual(self.sent_names(), ids)
        self.assertEqual(Fake.max_in_flight, 1)

    def test_queue_keeps_running_with_no_page_calls(self):
        ids, _ = self.fill(4)
        self.open_gate()
        deadline = time.time() + 6
        while Fake.count() < 4 and time.time() < deadline:  # Nothing polls the helper here.
            time.sleep(0.02)
        self.assertEqual(Fake.count(), 4)
        self.assertEqual(self.wait(ids[-1])['status'], 'done')

    def test_queue_bad_reply_holds_one_and_queue_moves_on(self):
        ids, _ = self.fill(3)
        self.open_gate('502')
        self.assertEqual(self.wait(ids[0])['kind'], 'bad_gateway')
        self.assertEqual([self.wait(job_id)['status'] for job_id in ids[1:]], ['done', 'done'])
        self.assertIsNone(self.status()['pause'])
        self.assertEqual(Fake.count(), 3)

    def test_queue_cancel_one_waiting_piece_others_run(self):
        ids, _ = self.fill(4)
        reply = self.tool('cancel', job_id=ids[2])
        self.assertEqual((reply['status'], reply['charged']), ('cancelled', 'no'))
        self.assertEqual(self.entry(ids[2])['status'], 'released')
        self.open_gate()
        for job_id in (ids[0], ids[1], ids[3]):
            self.assertEqual(self.wait(job_id)['status'], 'done')
        self.assertEqual(Fake.count(), 3)

    # Rule 2: money is reserved at acceptance.
    def test_queue_reserves_each_job_at_acceptance(self):
        ids, _ = self.fill(4)
        self.assertAlmostEqual(self.status()['reserved_usd'], 0.28)
        for job_id in ids:
            self.assertEqual(self.entry(job_id)['status'], 'reserved')

    def test_queue_unapproved_piece_cannot_wait(self):
        self.fill(2)
        reply = self.job('q:1:extra', None)
        self.assertEqual((reply['accepted'], reply['kind']), (False, 'price_approval_required'))
        self.assertEqual(self.status()['queue']['accepted'], 1)

    def test_queue_over_piece_count_refused(self):
        approval = self.approval(pieces=2, usd=5)
        self.fill(2, approval)
        reply = self.job('q:1:third', approval)
        self.assertEqual((reply['accepted'], reply['kind']), (False, 'approval_used_up'))
        self.assertAlmostEqual(self.status()['reserved_usd'], 0.14)

    def test_queue_over_approved_dollars_refused(self):
        approval = self.approval(pieces=5, usd=0.14)
        self.fill(2, approval)
        reply = self.job('q:1:third', approval)
        self.assertEqual((reply['accepted'], reply['kind']), (False, 'over_approved'))
        self.assertAlmostEqual(self.status()['reserved_usd'], 0.14)

    def test_queue_over_balance_refused(self):
        Fake.scenario = 'script'
        self.start()
        approval = self.approval(pieces=5, usd=5)
        self.assertTrue(self.job('q:1:0', approval, balance=0.14)['accepted'])
        self.assertTrue(Fake.entered.wait(4))
        self.assertTrue(self.job('q:1:1', approval, balance=0.14)['accepted'])
        reply = self.job('q:1:2', approval, balance=0.14)
        self.assertEqual((reply['accepted'], reply['kind']), (False, 'over_balance'))

    def test_queue_two_approvals_cannot_double_reserve_balance(self):
        Fake.scenario = 'script'
        self.start()
        first, second = self.approval(pieces=2, usd=1), self.approval(pieces=2, usd=1)
        self.assertTrue(self.job('q:1:0', first, balance=0.21)['accepted'])
        self.assertTrue(Fake.entered.wait(4))
        self.assertTrue(self.job('q:1:1', first, balance=0.21)['accepted'])
        self.assertTrue(self.job('q:1:2', second, balance=0.21)['accepted'])
        reply = self.job('q:1:3', second, balance=0.21)
        self.assertEqual((reply['accepted'], reply['kind']), (False, 'over_balance'))
        self.assertAlmostEqual(self.status()['reserved_usd'], 0.21)

    def test_queue_two_approvals_cannot_double_reserve_one_job(self):
        ids, _ = self.fill(2)
        reply = self.job(ids[1], self.approval(pieces=1, usd=1))
        self.assertTrue(reply.get('already_running'), reply)
        self.assertAlmostEqual(self.status()['reserved_usd'], 0.14)
        self.open_gate()
        self.assertEqual(self.wait(ids[1])['status'], 'done')
        self.assertEqual(Fake.count(), 2)

    def test_queue_new_approval_counts_spend_the_page_has_not_read(self):
        self.start()
        first = self.approval(pieces=2, usd=1)
        self.run_ok('q:1:0', first, balance=0.20)
        self.run_ok('q:1:1', first, balance=0.20)  # 0.107 spent while the page was closed
        second = self.approval(pieces=2, usd=1)
        self.assertEqual(self.run_ok('q:1:2', second, balance=0.20)['status'], 'done')  # 0.093 was left
        reply = self.job('q:1:3', second, balance=0.20)
        self.assertEqual(reply['kind'], 'over_balance')
        self.assertEqual(Fake.count(), 3)

    def test_queue_reading_a_result_writes_nothing(self):
        self.start()
        self.run_ok('q:1:0', self.approval(pieces=1, usd=1), balance=0.30)
        snapshot = lambda: {path: path.read_bytes() for path in self.data.rglob('*')
                            if path.is_file() and not path.name.endswith('.log')}
        before = snapshot()
        self.assertTrue(self.tool('get_result', job_id='q:1:0')['done'])
        self.assertEqual(snapshot(), before)

    def test_queue_read_the_page_did_not_count_is_still_subtracted(self):
        self.start()
        self.run_ok('q:1:0', self.approval(pieces=1, usd=1), balance=0.10)
        self.assertTrue(self.tool('get_result', job_id='q:1:0')['done'])
        # A read alone proves nothing: the page's 0.10 does not list this charge, so 0.0465 is left.
        reply = self.job('q:1:1', self.approval(pieces=1, usd=1), balance=0.10, counted_job_ids=[])
        self.assertEqual(reply['kind'], 'over_balance')
        self.assertEqual(Fake.count(), 1)

    def test_money_counted_jobs_list_required(self):
        self.start()
        for bad in (None, 'q:1:0', [1], ['']):
            reply = self.job('q:2:0', self.approval(pieces=1, usd=1), counted_job_ids=bad)
            self.assertEqual(reply['kind'], 'counted_jobs_required')
        self.assertEqual(Fake.count(), 0)

    def test_queue_read_result_is_not_subtracted_twice(self):
        self.start()
        self.run_ok('q:1:0', self.approval(pieces=1, usd=1), balance=0.30)
        self.assertTrue(self.tool('get_result', job_id='q:1:0')['done'])
        # The page has now counted it: 0.30 - 0.0535 = 0.2465, which fits two reserves (0.0802 each).
        approval = self.approval(pieces=2, usd=1)
        Fake.reset('script')
        self.assertTrue(self.job('q:1:1', approval, balance=0.2465)['accepted'])
        self.assertTrue(Fake.entered.wait(4))
        self.assertTrue(self.job('q:1:2', approval, balance=0.2465)['accepted'])

    # Rule 3: any pause returns every unsent job.
    def test_queue_five_queued_429_on_first_one_request_four_returned(self):
        ids, _ = self.fill(5)
        self.open_gate('429')
        self.assertEqual(self.wait(ids[0])['kind'], '429')
        time.sleep(1.2)
        self.assertEqual(Fake.count(), 1)
        for job_id in ids[1:]:
            self.assertReturned(job_id)
        status = self.status()
        self.assertEqual((status['queue']['returned'], status['queue']['accepted']), (4, 0))
        self.assertEqual(status['reserved_usd'], 0)
        self.assertEqual(self.unsent_files(), [])

    def test_queue_every_automatic_pause_returns_unsent(self):
        scenarios = list(PAUSE_FAILURES) + ['costly']
        for scenario in scenarios:
            with self.subTest(scenario):
                Fake.reset('script')
                ids, _ = self.fill(3, prefix='auto-' + scenario)
                self.open_gate(scenario)
                self.wait(ids[0])
                time.sleep(0.25)
                self.assertEqual(Fake.count(), 1)
                self.assertIsNotNone(self.status()['pause'])
                for job_id in ids[1:]:
                    self.assertReturned(job_id)
                self.assertEqual(self.unsent_files(), [])

    def test_queue_key_problem_returns_unsent_and_sends_nothing(self):
        engine = self.direct_engine()
        approval = self.approval()
        for job_id in ('k:1:0', 'k:1:1', 'k:1:2'):
            self.assertTrue(engine.tool_start_job(self.direct_args(job_id, approval))['accepted'])
        self.key_file.unlink()
        engine.run_job('k:1:0')
        self.assertEqual(engine.state['pause']['kind'], 'no_key')
        self.assertEqual([engine.state['jobs'][j]['state'] for j in ('k:1:1', 'k:1:2')], ['returned', 'returned'])
        self.assertIsNone(engine.next_runnable())
        self.assertEqual(Fake.count(), 0)

    def test_queue_returned_job_needs_a_new_approval(self):
        ids, old = self.fill(3)
        self.open_gate('429')
        self.wait(ids[0])
        time.sleep(0.2)
        self.assertEqual(self.job(ids[1], old)['kind'], '429')  # Still stopped.
        self.assertEqual(self.run_ok(ids[0], self.approval(pieces=1, usd=0.09))['status'], 'done')  # Resume
        self.assertEqual(self.job(ids[1], old)['kind'], 'approval_stale')  # Running again, old price still no good.
        self.assertEqual(Fake.count(), 2)
        self.assertEqual(self.run_ok(ids[1], self.approval(pieces=1, usd=0.09))['status'], 'done')
        self.assertEqual(Fake.count(), 3)

    def test_queue_approval_made_before_the_stop_cannot_lift_it(self):
        ids, _ = self.fill(2)
        early = {'approval_id': 'made-earlier-never-sent', 'piece_count': 1, 'approved_usd': 1}
        self.open_gate('429')
        self.wait(ids[0])
        time.sleep(0.2)
        for approval in (early, {**early, 'resume_pause_id': 'pause-1'}):
            reply = self.job('q:1:late', approval)
            self.assertEqual((reply['accepted'], reply['kind'], reply['paused']), (False, '429', True))
            self.assertTrue(reply['pause_id'])
        self.assertEqual(self.status()['pause']['kind'], '429')
        self.assertEqual(Fake.count(), 1)

    # Rule 4: stop_all.
    def test_stop_all_returns_unsent_aborts_in_flight_with_counts(self):
        ids, _ = self.fill(5)
        reply = self.tool('stop_all')
        self.assertEqual((reply['ok'], reply['paused'], reply['returned'], reply['aborted'], reply['aborted_job_id']),
                         (True, True, 4, 1, ids[0]))
        self.assertExplains(reply)
        final = self.wait(ids[0], seconds=4)
        self.assertEqual((final['status'], final['kind'], final['charged']), ('cancelled', 'cancelled', 'maybe'))
        self.assertEqual(self.entry(ids[0])['status'], 'held')
        for job_id in ids[1:]:
            self.assertReturned(job_id)
        self.assertEqual(self.status()['pause']['kind'], 'user_stop')
        self.assertEqual(Fake.count(), 1)

    def test_stop_all_then_restart_means_zero_more_requests(self):
        ids, _ = self.fill(5)
        self.tool('stop_all')
        self.wait(ids[0], seconds=4)
        for _ in range(2):
            self.restart()
            time.sleep(1.2)
            self.assertEqual(Fake.count(), 1)
            status = self.status()
            self.assertEqual((status['pause']['kind'], status['queue']['returned'], status['queue']['accepted']),
                             ('user_stop', 4, 0))

    def test_stop_all_killed_before_call_ends_still_holds_and_stays_stopped(self):
        ids, _ = self.fill(3)
        with self.subTest('stop then immediate kill'):
            self.tool('stop_all')
            self.restart()
            time.sleep(1)
            self.assertEqual(Fake.count(), 1)
            self.assertEqual(self.status()['pause']['kind'], 'user_stop')
            self.assertIn(self.entry(ids[0])['status'], ('held',))

    def test_stop_all_on_idle_helper_survives_restart(self):
        self.start()
        approval = self.approval()
        reply = self.tool('stop_all')
        self.assertEqual((reply['returned'], reply['aborted'], reply['already_paused']), (0, 0, False))
        self.restart()
        self.assertEqual(self.job('q:1:0', approval)['kind'], 'user_stop')
        self.assertEqual(Fake.count(), 0)

    def test_stop_all_twice_changes_nothing_more(self):
        self.fill(3)
        first = self.tool('stop_all')
        second = self.tool('stop_all')
        self.assertEqual((second['returned'], second['aborted'], second['already_paused']), (0, 0, True))
        self.assertNotEqual(second['pause_id'], first['pause_id'])
        self.assertEqual(second['kind'], first['kind'])

    def test_stop_all_again_cancels_a_resume_approved_before_it(self):
        self.start()
        self.tool('stop_all')
        before_stop = self.approval()  # Names the first stop, as a second open window would.
        self.tool('stop_all')
        reply = self.job('q:1:0', before_stop)
        self.assertEqual((reply['kind'], reply.get('paused')), ('user_stop', True))
        self.assertEqual(Fake.count(), 0)
        self.assertTrue(self.job('q:1:1', self.approval())['accepted'])

    def test_stop_all_keeps_the_earlier_stop_reason(self):
        ids, _ = self.fill(2)
        self.open_gate('401')
        self.wait(ids[0])
        time.sleep(0.2)
        reply = self.tool('stop_all')
        self.assertEqual((reply['kind'], reply['already_paused']), ('401', True))

    def test_stop_all_resume_needs_new_approval_and_sends_once(self):
        ids, old = self.fill(3)
        self.tool('stop_all')
        self.wait(ids[0], seconds=4)
        self.assertEqual(self.job(ids[1], old)['kind'], 'user_stop')
        self.assertEqual(Fake.count(), 1)
        Fake.reset('ok')
        self.assertEqual(self.run_ok(ids[1], self.approval(pieces=1, usd=0.07))['status'], 'done')
        self.assertIsNone(self.status()['pause'])
        self.assertEqual(Fake.count(), 1)

    # Rule 5: restart.
    def test_restart_with_no_pause_carries_on_in_order(self):
        engine = self.direct_engine()
        approval = self.approval(pieces=3, usd=1)
        ids = ['r:1:z', 'r:1:a', 'r:1:m']
        for job_id in ids:
            self.assertTrue(engine.tool_start_job(self.direct_args(job_id, approval))['accepted'])
        del engine
        self.assertEqual(Fake.count(), 0)
        self.start()  # Nothing from the page: the helper picks up what was approved.
        for job_id in ids:
            self.assertEqual(self.wait(job_id)['status'], 'done')
        self.assertEqual(self.sent_names(), ids)
        self.assertEqual(Fake.count(), 3)

    def test_restart_after_crash_mid_call_holds_pauses_and_returns_rest(self):
        ids, _ = self.fill(5)
        self.restart()
        self.assertEqual(self.tool('job_status', job_id=ids[0])['kind'], 'needs_check')
        self.assertEqual(self.entry(ids[0])['status'], 'held')
        for job_id in ids[1:]:
            self.assertReturned(job_id)
        self.assertEqual(self.status()['pause']['kind'], 'needs_check')
        time.sleep(1.2)
        self.assertEqual(Fake.count(), 1)
        self.assertEqual(self.unsent_files(), [])

    # Rule 6: status mirrors the queue.
    def test_status_returns_queue_counts_by_state(self):
        ids, _ = self.fill(4)
        status = self.status()
        self.assertEqual(status['engine'], 'running')
        self.assertEqual(set(status['queue']), {'accepted', 'sending', 'done', 'failed', 'needs_check', 'cancelled',
                                                'returned'})
        self.assertEqual((status['queue']['sending'], status['queue']['accepted']), (1, 3))
        self.assertEqual((status['active_job'], status['queued_job_ids']), (ids[0], ids[1:]))
        self.open_gate('429')
        self.wait(ids[0])
        time.sleep(0.2)
        status = self.status()
        self.assertEqual((status['queue']['failed'], status['queue']['returned'], status['queue']['accepted']),
                         (1, 3, 0))
        self.assertEqual(status['returned_job_ids'], sorted(ids[1:]))
        self.assertTrue(status['pause']['pause_id'])


class HandoverChecks(Base):
    """Checks 1 to 6 from the full-test handover README."""

    def test_check1_bad_reply_holds_only_that_piece_and_queue_moves_on(self):
        self.extra_env['SNIPPY_IMAGE_TIMEOUT'] = '0.6'
        self.start()
        approval = self.approval(pieces=14, usd=2)
        for index, (scenario, code) in enumerate(PIECE_FAILURES.items()):
            with self.subTest(code):
                Fake.reset(scenario)
                bad, good = 'c1:%d:bad' % index, 'c1:%d:good' % index
                reply = self.run_ok(bad, approval)
                self.assertEqual((reply['status'], reply['kind']), ('failed', code))
                self.assertIsNone(self.status()['pause'])
                Fake.reset('ok')
                self.assertEqual(self.run_ok(good, approval)['status'], 'done')
                self.assertEqual(Fake.count(), 1)

    def test_check1_held_piece_does_not_freeze_after_restart(self):
        Fake.scenario = 'garbage'
        self.start()
        approval = self.approval()
        self.run_ok('c1:1:1', approval)
        self.restart()
        Fake.reset('ok')
        self.assertEqual(self.run_ok('c1:1:2', approval)['status'], 'done')

    def test_check2_paid_errors_pause_with_resume_and_worker_stays_alive(self):
        self.start()
        for scenario, code in PAUSE_FAILURES.items():
            with self.subTest(code):
                Fake.reset(scenario)
                approval = self.approval()
                job_id = 'c2-%s:1:1' % scenario
                self.assertEqual(self.run_ok(job_id, approval)['kind'], code)
                status = self.status()
                self.assertEqual((status['engine'], status['pause']['kind']), ('paused', code))
                self.assertIn('Resume', status['pause']['next_step'])
                self.assertIsNone(self.process.poll())
                Fake.reset('ok')
                blocked = self.job('c2-%s:1:2' % scenario, approval)
                self.assertEqual((blocked['accepted'], blocked['kind']), (False, code))
                self.assertEqual(Fake.count(), 0)
                # Resume: a fresh price approval lifts the pause, with the same running worker.
                self.assertEqual(self.run_ok('c2-%s:1:3' % scenario, self.approval())['status'], 'done')
                self.assertIsNone(self.status()['pause'])

    def test_check2_refused_piece_can_run_again_only_after_resume(self):
        Fake.scenario = '429'
        self.start()
        approval = self.approval()
        self.run_ok('c2:1:1', approval)
        Fake.reset('ok')
        self.assertEqual(self.job('c2:1:1', approval)['kind'], '429')
        self.assertEqual(Fake.count(), 0)
        self.assertEqual(self.run_ok('c2:1:1', self.approval())['status'], 'done')
        self.assertEqual(Fake.count(), 1)

    def test_check2_500_piece_is_not_resent_under_same_id(self):
        Fake.scenario = '500'
        self.start()
        self.run_ok('c2:1:1', self.approval())
        Fake.reset('ok')
        reply = self.job('c2:1:1', self.approval())
        self.assertEqual((reply['kind'], reply['retry_job_id']), ('duplicate_job', 'c2:1:1:retry1'))
        self.assertEqual(Fake.count(), 0)

    def test_check3_crash_during_request_holds_and_pauses(self):
        Fake.scenario = 'hang'
        self.start()
        approval = self.approval()
        self.job('c3:1:1', approval)
        self.assertTrue(Fake.entered.wait(4))
        self.restart()
        self.assertEqual(self.tool('job_status', job_id='c3:1:1')['kind'], 'needs_check')
        status = self.status()
        self.assertEqual((status['engine'], status['pause']['kind']), ('paused', 'needs_check'))
        self.assertEqual(self.entry('c3:1:1')['status'], 'held')

    def test_check3_never_carries_on_spending_after_restart(self):
        Fake.scenario = 'hang'
        self.start()
        approval = self.approval()
        self.job('c3:1:1', approval)
        self.assertTrue(Fake.entered.wait(4))
        self.restart()
        Fake.scenario = 'ok'
        time.sleep(1.5)
        self.assertEqual(Fake.count(), 1)
        self.assertEqual(self.job('c3:1:2', approval)['kind'], 'needs_check')
        self.assertEqual(Fake.count(), 1)

    def test_check4_stop_stays_a_stop_across_restarts(self):
        Fake.scenario = '503'
        self.start()
        approval = self.approval()
        self.run_ok('c4:1:1', approval)
        for _ in range(3):
            self.restart()
            self.assertEqual(self.status()['pause']['kind'], '503')
        Fake.reset('ok')
        self.assertEqual(self.job('c4:1:2', approval)['kind'], '503')
        time.sleep(1.2)
        self.assertEqual(Fake.count(), 0)
        self.assertEqual(self.run_ok('c4:1:3', self.approval())['status'], 'done')

    def test_check5_every_stop_says_why_and_what_next(self):
        self.extra_env['SNIPPY_IMAGE_TIMEOUT'] = '0.6'
        self.start()
        for scenario in list(PIECE_FAILURES) + list(PAUSE_FAILURES):
            with self.subTest(scenario):
                Fake.reset(scenario)
                job_id = 'c5-%s:1:1' % scenario
                self.assertExplains(self.run_ok(job_id, self.approval()))
                pause = self.status()['pause']
                if scenario in PAUSE_FAILURES:
                    self.assertExplains(pause)
        for refusal in (self.tool('start_job', job_id='x'), self.job('y:1:1', self.approval(), balance=0)):
            self.assertExplains(refusal)

    def test_check5_credit_balance_exhausted_is_out_of_credit(self):
        for scenario in ('credit', 'credit400'):
            with self.subTest(scenario):
                Fake.reset(scenario)
                if not self.process:
                    self.start()
                reply = self.run_ok('c5-%s:1:1' % scenario, self.approval())
                self.assertEqual(reply['kind'], 'insufficient_quota')
                self.assertNotIn('rate', reply['message'].lower())
                self.assertIn('credit', reply['message'].lower())
                self.assertEqual(self.status()['pause']['kind'], 'insufficient_quota')

    def test_check6_retry_returns_count_and_price_keeps_finished(self):
        self.start()
        first = self.approval(pieces=3, usd=1)
        self.assertEqual(self.run_ok('c6:1:1', first)['status'], 'done')
        Fake.scenario = 'garbage'
        failed = self.run_ok('c6:1:2', first)
        self.assertEqual(failed['retry_job_id'], 'c6:1:2:retry1')
        quote = self.status()['retry_quote']
        self.assertEqual((quote['count'], quote['finished_kept']), (1, 1))
        self.assertAlmostEqual(quote['price_usd'], 0.0802)  # 1.5x the 0.0535 already settled
        self.assertIn('kept', quote['note'])
        Fake.reset('ok')
        retry_approval = self.approval(pieces=1, usd=0.09)
        reply = self.job('c6:1:2:retry1', retry_approval)
        self.assertTrue(reply['accepted'], reply)
        self.assertEqual((reply['retry']['count'], reply['retry']['price_usd'], reply['retry']['finished_kept']),
                         (1, 0.09, 1))
        self.assertIn('kept', reply['retry']['note'])
        self.assertEqual(self.wait('c6:1:2:retry1')['status'], 'done')
        self.assertEqual(Fake.count(), 1)
        self.assertEqual(self.job('c6:1:1', self.approval())['kind'], 'already_done')
        self.assertEqual(self.job('c6:1:1:retry1', self.approval())['kind'], 'already_done')
        self.assertEqual(self.job('c6:1:2:retry2', self.approval())['kind'], 'already_done')
        self.assertEqual(Fake.count(), 1)

    def test_check6_retry_needs_a_fresh_approval(self):
        Fake.scenario = 'garbage'
        self.start()
        first = self.approval(pieces=3, usd=1)
        self.run_ok('c6:1:1', first)
        Fake.reset('ok')
        self.assertEqual(self.job('c6:1:1:retry1', first)['kind'], 'approval_stale')
        self.assertEqual(Fake.count(), 0)


class RecutRules(Base):
    """A finished piece can be cut again as its own paid job, only with a re-cut price approval."""

    def recut_approval(self, **extra):
        approval = self.approval(pieces=1, usd=0.09)  # reserve grows to 1.5x the dearest settled cost (0.0802)
        approval['kind'] = 'recut'
        approval.update(extra)
        return approval

    def test_recut_of_a_finished_piece_is_accepted_and_the_old_file_stays(self):
        self.start()
        self.run_ok('s:1:1', self.approval())
        old = (self.data / 'results').glob('*.png')
        old = {path.name: path.read_bytes() for path in old}
        self.assertEqual(len(old), 1)
        self.assertEqual(self.job('s:1:1', self.approval())['kind'], 'already_done')  # unchanged
        reply = self.job('s:1:1~recut1', self.recut_approval(), recut_note='the roof is cut off')
        self.assertTrue(reply.get('accepted') and reply.get('recut'), reply)
        self.assertEqual(self.wait('s:1:1~recut1')['status'], 'done')
        self.assertEqual(Fake.count(), 2)
        prompt = Fake.calls[1]['body']['prompt']
        self.assertIn('the roof is cut off', prompt)
        self.assertNotIn('the roof is cut off', Fake.calls[0]['body']['prompt'])
        now = {path.name: path.read_bytes() for path in (self.data / 'results').glob('*.png')}
        self.assertEqual(len(now), 2)
        for name, data in old.items():
            self.assertEqual(now[name], data)  # the old cutout was not touched

    def test_recut_note_empty_still_a_different_request(self):
        self.start()
        self.run_ok('s:1:1', self.approval())
        self.run_ok('s:1:1~recut1', self.recut_approval())
        self.assertEqual(Fake.count(), 2)

    def test_recut_same_id_is_never_sent_twice(self):
        self.start()
        self.run_ok('s:1:1', self.approval())
        self.run_ok('s:1:1~recut1', self.recut_approval(), recut_note='x')
        self.assertEqual(self.job('s:1:1~recut1', self.recut_approval(), recut_note='x')['kind'], 'already_done')
        self.assertEqual(Fake.count(), 2)

    def test_recut_id_needs_the_recut_kind(self):
        self.start()
        self.run_ok('s:1:1', self.approval())
        self.assertEqual(self.job('s:1:1~recut1', self.approval())['kind'], 'bad_input')
        self.assertEqual(Fake.count(), 1)

    def test_recut_kind_needs_a_recut_id(self):
        self.start()
        self.assertEqual(self.job('s:1:2', self.recut_approval())['kind'], 'bad_input')
        self.assertEqual(self.job('s:1:2', self.approval(), recut_note='a note')['kind'], 'bad_input')
        self.assertEqual(Fake.count(), 0)

    def test_recut_approval_cannot_be_reused_as_another_kind(self):
        self.start()
        approval = self.recut_approval(approval_id='same-id')
        self.run_ok('s:1:1~recut1', approval)
        plain = self.approval(pieces=1, usd=0.07, approval_id='same-id')
        self.assertEqual(self.job('s:1:2', plain)['kind'], 'approval_mismatch')

    def test_recut_retry_follows_its_own_chain(self):
        self.start()
        Fake.reset('401')
        self.job('s:1:1~recut1', self.recut_approval(), recut_note='n')
        self.wait('s:1:1~recut1')
        Fake.reset('ok')
        reply = self.job('s:1:1~recut1:retry1', self.recut_approval(), recut_note='n')
        self.assertTrue(reply.get('accepted') or reply.get('paused'), reply)


class McpRules(Base):
    def session(self, messages):
        lines = ''.join(json.dumps(message) + '\n' for message in messages)
        done = subprocess.run([sys.executable, '-I', str(MCP)], input=lines, env=self.env(), capture_output=True,
                              text=True, timeout=15)
        return {reply['id']: reply for reply in map(json.loads, done.stdout.splitlines())}

    def call(self, tool, **arguments):
        replies = self.session([{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                 'params': {'name': tool, 'arguments': arguments}}])
        return replies[1]['result']

    def test_mcp_names_tools_and_read_only_marks(self):
        replies = self.session([
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}])
        self.assertEqual(replies[1]['result']['serverInfo']['name'], 'snippy-engine')
        self.assertEqual(replies[1]['result']['protocolVersion'], '2025-06-18')
        tools = {tool['name']: tool for tool in replies[2]['result']['tools']}
        self.assertEqual(set(tools), {'status', 'start_job', 'job_status', 'get_result', 'cancel', 'resolve_hold',
                                      'stop_all'})
        for name in ('status', 'job_status', 'get_result'):
            self.assertTrue(tools[name]['annotations']['readOnlyHint'], name)
        for name in ('start_job', 'cancel', 'resolve_hold', 'stop_all'):
            self.assertFalse(tools[name]['annotations']['readOnlyHint'], name)

    def test_mcp_forwards_full_job(self):
        self.start()
        args = {'job_id': 'm:1:1', 'approval': self.approval(), 'balance_usd': 50, 'counted_job_ids': [], 'name': 'Piece',
                'source_image_data': SOURCE, 'box': [1, 2, 3, 4]}
        self.assertTrue(self.call('start_job', **args)['structuredContent']['accepted'])
        self.wait('m:1:1')
        self.assertEqual(self.call('job_status', job_id='m:1:1')['structuredContent']['status'], 'done')
        part = self.call('get_result', job_id='m:1:1')['structuredContent']
        self.assertEqual(base64.b64decode(part['data']), png())
        self.assertTrue(part['done'])

    def test_mcp_resolve_hold_forwards(self):
        Fake.scenario = 'drop'
        self.start()
        self.run_ok('m:1:1', self.approval())
        Fake.scenario = 'ok'
        reply = self.call('resolve_hold', job_id='m:1:1', outcome='charged', charged_usd=0.06)['structuredContent']
        self.assertEqual((reply['ok'], reply['reservation']), (True, 'settled'))
        self.assertEqual(self.entry('m:1:1')['actual_usd'], '0.06')

    def test_mcp_stop_all_forwards(self):
        self.start()
        reply = self.call('stop_all')['structuredContent']
        self.assertEqual((reply['ok'], reply['kind'], reply['returned'], reply['aborted']), (True, 'user_stop', 0, 0))
        self.assertEqual(self.status()['pause']['kind'], 'user_stop')

    def test_mcp_text_matches_structured(self):
        self.start()
        result = self.call('status')
        self.assertEqual(json.loads(result['content'][0]['text']), result['structuredContent'])

    def test_mcp_daemon_down_gives_clear_code_for_every_tool(self):
        self.data.mkdir(mode=0o700)
        (self.data / 'access-token').write_text('x' * 40)
        for tool in ('status', 'start_job', 'job_status', 'get_result', 'cancel', 'resolve_hold', 'stop_all'):
            with self.subTest(tool):
                reply = self.call(tool, job_id='a:1:1')['structuredContent']
                self.assertEqual((reply['ok'], reply['kind']), (False, 'daemon_down'))
                self.assertTrue(reply['next_step'])
        self.assertEqual(self.call('start_job', job_id='a:1:1')['structuredContent']['accepted'], False)
        self.assertEqual(Fake.count(), 0)

    def test_mcp_no_token_file_is_daemon_down(self):
        self.assertEqual(self.call('status')['structuredContent']['kind'], 'daemon_down')


class BatchRules(Base):
    """One press of Send hands every approved piece over in one call; each job keeps every money check."""

    def item(self, job_id, approval, with_image=False):
        item = {'job_id': job_id, 'approval': approval, 'source_id': job_id.split(':')[0],
                'region_id': job_id.split(':')[-1], 'name': 'Piece ' + job_id, 'reconstruct': False, 'comment': '',
                'overall_comment': '', 'source_image': 'blob-id', 'box': [1, 2, 3, 4],
                'reconstruction_reason': '', 'source_note': ''}
        if with_image:
            item['source_image_data'] = SOURCE
        return item

    def batch(self, items, images=None, balance=50.0):
        return self.tool('start_job', job_id=items[0]['job_id'], jobs=items,
                         images={'b': SOURCE} if images is None else images, balance_usd=balance)

    def test_batch_one_call_queues_every_approved_piece(self):
        self.start()
        approval = self.approval(pieces=3, usd=0.21)
        reply = self.batch([self.item('b:1:%d' % i, approval) for i in (1, 2, 3)])
        self.assertEqual((reply['ok'], reply['batch'], reply['accepted_count']), (True, True, 3), reply)
        self.assertEqual([r['job_id'] for r in reply['results']], ['b:1:1', 'b:1:2', 'b:1:3'])
        for i in (1, 2, 3):
            self.assertEqual(self.wait('b:1:%d' % i)['status'], 'done')
        self.assertEqual(Fake.count(), 3)

    def test_batch_never_exceeds_the_approved_piece_count(self):
        self.start()
        approval = self.approval(pieces=2, usd=1.0)
        reply = self.batch([self.item('b:1:%d' % i, approval) for i in (1, 2, 3)])
        self.assertEqual(reply['accepted_count'], 2, reply)
        self.assertEqual(reply['results'][2]['kind'], 'approval_used_up')
        for i in (1, 2):
            self.wait('b:1:%d' % i)
        time.sleep(0.5)
        self.assertEqual(Fake.count(), 2)

    def test_batch_never_exceeds_the_approved_dollars(self):
        self.start()
        approval = self.approval(pieces=5, usd=0.14)
        reply = self.batch([self.item('b:1:%d' % i, approval) for i in (1, 2, 3)])
        self.assertEqual(reply['accepted_count'], 2, reply)
        self.assertEqual(reply['results'][2]['kind'], 'over_approved')

    def test_batch_never_exceeds_the_balance(self):
        self.start()
        approval = self.approval(pieces=5, usd=1.0)
        reply = self.batch([self.item('b:1:%d' % i, approval) for i in (1, 2, 3)], balance=0.15)
        self.assertEqual(reply['accepted_count'], 2, reply)
        self.assertEqual(reply['results'][2]['kind'], 'over_balance')

    def test_batch_sent_twice_never_charges_twice(self):
        self.start()
        approval = self.approval(pieces=3, usd=0.21)
        items = [self.item('b:1:%d' % i, approval) for i in (1, 2, 3)]
        self.batch(items)
        for i in (1, 2, 3):
            self.wait('b:1:%d' % i)
        again = self.batch(items)
        self.assertEqual(again['accepted_count'], 0, again)
        self.assertEqual({r['kind'] for r in again['results']}, {'already_done'})
        self.assertEqual(Fake.count(), 3)

    def test_batch_after_stop_sends_nothing(self):
        self.start()
        approval = self.approval(pieces=3, usd=0.21)
        self.tool('stop_all')
        reply = self.batch([self.item('b:1:%d' % i, approval) for i in (1, 2, 3)])
        self.assertEqual(reply['accepted_count'], 0, reply)
        self.assertTrue(all(r.get('paused') for r in reply['results']), reply)
        time.sleep(0.5)
        self.assertEqual(Fake.count(), 0)

    def test_batch_stop_still_returns_queued_pieces(self):
        self.start()
        approval = self.approval(pieces=4, usd=1.0)
        Fake.scenario = 'script'
        reply = self.batch([self.item('b:1:%d' % i, approval) for i in (1, 2, 3, 4)])
        self.assertEqual(reply['accepted_count'], 4, reply)
        self.assertTrue(Fake.entered.wait(4))
        stop = self.tool('stop_all')
        self.assertEqual((stop['ok'], stop['returned'], stop['aborted']), (True, 3, 1), stop)
        time.sleep(0.5)
        self.assertEqual(Fake.count(), 1)

    def test_batch_missing_picture_refuses_only_that_piece(self):
        self.start()
        approval = self.approval(pieces=3, usd=1.0)
        items = [self.item('b:1:1', approval), self.item('c:1:2', approval), self.item('b:1:3', approval)]
        reply = self.batch(items, images={'b': SOURCE})
        self.assertEqual(reply['accepted_count'], 2, reply)
        self.assertEqual(reply['results'][1]['kind'], 'missing_source')

    def test_batch_first_picture_rides_at_top_level(self):
        self.start()
        approval = self.approval(pieces=3, usd=1.0)
        items = [self.item('b:1:1', approval), self.item('b:1:2', approval), self.item('c:1:3', approval)]
        reply = self.tool('start_job', job_id='b:1:1', source_id='b', source_image_data=SOURCE, jobs=items,
                          images={'c': SOURCE}, balance_usd=50.0)
        self.assertEqual(reply['accepted_count'], 3, reply)

    def test_batch_status_says_batch(self):
        self.start()
        self.assertIn('batch', self.status()['features'])

    def test_batch_bad_shapes_refused_whole(self):
        self.start()
        approval = self.approval()
        for jobs in ([], 'x', [1], [self.item('b:1:1', approval), self.item('b:1:1', approval)]):
            with self.subTest(jobs=jobs):
                reply = self.tool('start_job', job_id='b:1:1', jobs=jobs, images={'b': SOURCE}, balance_usd=50.0)
                self.assertEqual(reply['kind'], 'bad_input', reply)
        reply = self.tool('start_job', job_id='b:1:1', jobs=[self.item('b:1:1', approval)], images=[], balance_usd=50.0)
        self.assertEqual(reply['kind'], 'bad_input')
        self.assertEqual(Fake.count(), 0)


class LocalOriginals(InProcess):
    """A job may name a stored original by id instead of carrying the picture through Claude's host."""

    def put(self, data=b'\x89PNG fake original bytes', ext='png', tamper=None):
        import hashlib
        original_id = hashlib.sha256(data).hexdigest()[:32]
        folder = self.tmp / 'originals'
        folder.mkdir(exist_ok=True)
        (folder / (original_id + '.' + ext)).write_bytes(tamper if tamper is not None else data)
        return original_id

    def item(self, job_id, approval, original_id, **extra):
        item = {'job_id': job_id, 'approval': approval, 'source_id': job_id.split(':')[0],
                'region_id': job_id.split(':')[-1], 'name': 'Piece ' + job_id, 'reconstruct': False, 'comment': '',
                'overall_comment': '', 'source_image': original_id, 'box': [1, 2, 3, 4],
                'reconstruction_reason': '', 'source_note': ''}
        item.update(extra)
        return item

    def test_local_id_alone_loads_the_stored_file(self):
        self.start()
        original_id = self.put(b'\x89PNG stored picture')
        approval = self.approval(pieces=1, usd=0.07)
        reply = self.tool('start_job', balance_usd=50.0, **self.item('s:1:1', approval, original_id))
        self.assertTrue(reply.get('accepted'), reply)
        self.assertEqual(self.wait('s:1:1')['status'], 'done')
        sent = Fake.calls[0]['body']['images'][0]['image_url']
        self.assertEqual(sent, 'data:image/png;base64,' + base64.b64encode(b'\x89PNG stored picture').decode())

    def test_local_id_uses_the_right_mime_for_the_extension(self):
        self.start()
        original_id = self.put(b'jpeg-ish bytes', ext='jpg')
        approval = self.approval(pieces=1, usd=0.07)
        self.assertTrue(self.tool('start_job', balance_usd=50.0, **self.item('s:1:1', approval, original_id)).get('accepted'))
        self.wait('s:1:1')
        self.assertTrue(Fake.calls[0]['body']['images'][0]['image_url'].startswith('data:image/jpeg;base64,'))

    def test_local_id_missing_file_is_missing_source(self):
        self.start()
        approval = self.approval(pieces=1, usd=0.07)
        for original_id in ('0' * 32, 'not-a-hex-id', '../../etc/passwd'):
            with self.subTest(original_id=original_id):
                reply = self.tool('start_job', balance_usd=50.0, **self.item('s:1:1', approval, original_id))
                self.assertEqual((reply['accepted'], reply['kind']), (False, 'missing_source'), reply)
                self.assertExplains(reply)
        self.assertEqual(Fake.count(), 0)
        self.assertEqual(self.status()['queue']['accepted'], 0)

    def test_local_id_tampered_file_is_missing_source(self):
        self.start()
        original_id = self.put(b'\x89PNG real', tamper=b'\x89PNG someone changed this')
        approval = self.approval(pieces=1, usd=0.07)
        reply = self.tool('start_job', balance_usd=50.0, **self.item('s:1:1', approval, original_id))
        self.assertEqual((reply['accepted'], reply['kind']), (False, 'missing_source'), reply)
        self.assertIn('does not match', reply['message'])
        self.assertEqual(Fake.count(), 0)

    def test_local_id_batch_reads_the_file_once_and_accepts_all(self):
        engine = self.engine()
        original_id = self.put()
        reads = []
        real = engine.read_original
        engine.read_original = lambda i: (reads.append(i), real(i))[1]
        approval = self.approval(pieces=3, usd=1.0)
        items = [self.item('s:1:%d' % n, approval, original_id) for n in (1, 2, 3)]
        reply = engine.tool_start_job({'job_id': 's:1:1', 'jobs': items, 'balance_usd': 50, 'counted_job_ids': []})
        self.assertEqual((reply['batch'], reply['accepted_count']), (True, 3), reply)
        self.assertEqual(reads, [original_id])

    def test_local_id_batch_one_bad_id_refuses_only_that_piece(self):
        self.start()
        good = self.put()
        approval = self.approval(pieces=2, usd=1.0)
        items = [self.item('s:1:1', approval, good), self.item('t:1:2', approval, 'f' * 32)]
        reply = self.tool('start_job', job_id='s:1:1', jobs=items, balance_usd=50.0)
        self.assertEqual(reply['accepted_count'], 1, reply)
        self.assertEqual(reply['results'][1]['kind'], 'missing_source')

    def test_local_id_given_picture_data_still_wins(self):
        self.start()
        original_id = self.put(b'\x89PNG stored picture')
        approval = self.approval(pieces=1, usd=0.07)
        reply = self.tool('start_job', balance_usd=50.0,
                          **self.item('s:1:1', approval, original_id, source_image_data=SOURCE))
        self.assertTrue(reply.get('accepted'), reply)
        self.wait('s:1:1')
        self.assertEqual(Fake.calls[0]['body']['images'][0]['image_url'], SOURCE)
        # Data given, so a wrong id does not matter either.
        approval = self.approval(pieces=1, usd=0.09)
        reply = self.tool('start_job', balance_usd=50.0,
                          **self.item('s:1:2', approval, 'a' * 32, source_image_data=SOURCE))
        self.assertTrue(reply.get('accepted'), reply)

    def test_local_status_says_local_originals_and_counts_files(self):
        self.start()
        status = self.status()
        self.assertIn('local_originals', status['features'])
        self.assertIn('batch', status['features'])
        self.assertEqual(status['originals'], 0)
        self.put(b'one')
        self.put(b'two', ext='webp')
        self.put(b'three', ext='jpg')
        (self.tmp / 'originals' / 'notes.txt').write_text('not a picture')
        self.assertEqual(self.status()['originals'], 3)


class Result(unittest.TextTestResult):
    pass


def main():
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(resultclass=Result, verbosity=1).run(suite)
    bad = {test.id() for test, _ in result.failures + result.errors}
    every = []

    def walk(item):
        for child in item:
            if isinstance(child, unittest.TestSuite):
                walk(child)
            else:
                every.append(child.id())
    walk(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    print('\nHandover checks:')
    for number in range(1, 7):
        tests = [name for name in every if '.test_check%d_' % number in name]
        failed = [name for name in tests if name in bad or any(b.startswith(name) for b in bad)]
        print('  Check %d: %s (%d tests)' % (number, 'FAIL' if failed or not tests else 'PASS', len(tests)))
    print('Fake image service only: %s' % IMAGE_URL)
    print('TOTAL %d run, %d failed, %d errors' % (result.testsRun, len(result.failures), len(result.errors)))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
