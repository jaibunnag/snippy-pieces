#!/usr/bin/env python3
"""Tests for snippy_setup.py. Every run uses a temp fake home and test mode: no real files, launchctl or network."""
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import snippy_setup as S  # noqa: E402

FAKE_KEY = 'sk-test-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456'


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='snippy-setup-test-')).resolve()
        self.home = self.tmp / 'home'
        self.home.mkdir()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        env = {k: v for k, v in os.environ.items() if k not in ('SNIPPY_CONFIG_DIR', S.KEY_ENV, S.TEST_KEY_ENV)}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = S.Ctx(self.home, True)

    def run_cmd(self, *argv, key=None):
        args = ['--home', str(self.home), '--test-mode'] + list(argv)
        out, err = io.StringIO(), io.StringIO()
        env = {S.TEST_KEY_ENV: key} if key else {}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = S.main(args)
        return code, out.getvalue(), err.getvalue()

    def make_drive(self):
        drive = self.home / 'Library' / 'CloudStorage' / 'GoogleDrive-a@b.com'
        (drive / 'My Drive').mkdir(parents=True)
        (drive / 'Shared drives' / 'Team Art').mkdir(parents=True)
        return drive

    def make_claude_folder(self):
        """The Claude app has run once, so its settings folder exists (connect-claude never makes that folder)."""
        self.ctx.claude_config.parent.mkdir(parents=True, exist_ok=True)

    def write_claude(self, data):
        path = self.ctx.claude_config
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path


class Paths(Base):
    def test_all_paths_under_fake_home(self):
        for p in (self.ctx.config_dir, self.ctx.plist_file, self.ctx.claude_config, self.ctx.key_file):
            self.assertTrue(str(p).startswith(str(self.home)), p)

    def test_env_home(self):
        with mock.patch.dict(os.environ, {'SNIPPY_SETUP_HOME': str(self.home)}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                S.main(['--test-mode', 'install'])
        self.assertTrue((self.ctx.engine_dir / 'daemon.py').is_file())


class Install(Base):
    def test_install_copies_helper_not_tests_and_is_idempotent(self):
        code, out, _ = self.run_cmd('install')
        self.assertEqual(code, 0)
        names = sorted(p.name for p in self.ctx.engine_dir.iterdir())
        self.assertEqual(names, ['daemon.py', 'mcp_server.py'])
        code, out, _ = self.run_cmd('install')
        self.assertEqual(code, 0)
        self.assertIn('unchanged: daemon.py', out)
        self.assertNotIn('copied', out)

    def test_install_refreshes_changed_file(self):
        self.run_cmd('install')
        (self.ctx.engine_dir / 'daemon.py').write_text('old')
        _, out, _ = self.run_cmd('install')
        self.assertIn('copied: daemon.py', out)


class Key(Base):
    def test_key_saved_0600_and_never_printed(self):
        code, out, err = self.run_cmd('key', '--no-network', key=FAKE_KEY)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ctx.key_file.read_text(), FAKE_KEY)
        self.assertEqual(stat.S_IMODE(self.ctx.key_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.ctx.config_dir.stat().st_mode), 0o700)
        self.assertNotIn(FAKE_KEY, out + err)
        self.assertNotIn('ABCDEFGHIJKLMNOP', out + err)
        # nothing else under the fake home holds the key
        for path in self.home.rglob('*'):
            if path.is_file() and path != self.ctx.key_file:
                self.assertNotIn(FAKE_KEY, path.read_text(errors='ignore'), path)

    def test_bad_key_refused_and_not_saved(self):
        code, out, err = self.run_cmd('key', '--no-network', key='hello world')
        self.assertNotEqual(code, 0)
        self.assertFalse(self.ctx.key_file.exists())
        self.assertNotIn('hello world', out + err)

    def test_no_key_given_fails_loudly(self):
        code, _, err = self.run_cmd('key', '--no-network')
        self.assertNotEqual(code, 0)
        self.assertIn('PROBLEM', err)

    def test_test_mode_never_opens_network_or_dialog(self):
        with mock.patch.object(S.urllib.request, 'urlopen') as net, mock.patch.object(S.subprocess, 'run') as run:
            code, _, _ = self.run_cmd('key', key=FAKE_KEY)  # no --no-network, still no call in test mode
        self.assertEqual(code, 0)
        net.assert_not_called()
        run.assert_not_called()

    def test_online_401_does_not_save_and_no_network_saves_with_warning(self):
        ctx = S.Ctx(self.home, False)  # real-mode logic, but the network call is patched
        err401 = S.urllib.error.HTTPError('u', 401, 'no', {}, None)
        with mock.patch.object(S.urllib.request, 'urlopen', side_effect=err401):
            self.assertEqual(S.check_key_online(FAKE_KEY)[1], 2)
        with mock.patch.object(S.urllib.request, 'urlopen', side_effect=S.urllib.error.URLError('down')):
            msg, code = S.check_key_online(FAKE_KEY)
        self.assertEqual(code, 3)
        self.assertNotIn(FAKE_KEY, msg)
        # full flow with ask_key patched and the online check returning 401
        args = S.build_parser().parse_args(['key'])
        with mock.patch.object(S, 'ask_key', return_value=(FAKE_KEY, 'x')), \
                mock.patch.object(S, 'check_key_online', return_value=('bad', 2)):
            with self.assertRaises(S.SetupError) as cm:
                S.cmd_key(ctx, args)
        self.assertEqual(cm.exception.code, 2)
        self.assertFalse(ctx.key_file.exists())
        with mock.patch.object(S, 'ask_key', return_value=(FAKE_KEY, 'x')), \
                mock.patch.object(S, 'check_key_online', return_value=('offline', 3)):
            with self.assertRaises(S.SetupError) as cm:
                S.cmd_key(ctx, args)
        self.assertEqual(cm.exception.code, 3)
        self.assertTrue(ctx.key_file.exists())


class ConfigFolderMode(Base):
    def test_every_creating_command_makes_it_700(self):
        for argv, kw in ((('install',), {}), (('key', '--no-network'), {'key': FAKE_KEY}),
                         (('settings', '--artist', 'Mira'), {})):
            shutil.rmtree(self.ctx.config_dir, ignore_errors=True)
            code, _, err = self.run_cmd(*argv, **kw)
            self.assertEqual(code, 0, err)
            self.assertEqual(stat.S_IMODE(self.ctx.config_dir.stat().st_mode), 0o700, argv)

    def test_existing_folder_mode_left_alone(self):
        self.ctx.config_dir.mkdir(parents=True)
        os.chmod(self.ctx.config_dir, 0o755)
        self.run_cmd('install')
        self.assertEqual(stat.S_IMODE(self.ctx.config_dir.stat().st_mode), 0o755)


class KeyDialog(Base):
    def test_osascript_failure_cause_is_shown(self):
        ctx = S.Ctx(self.home, False)
        bad = mock.Mock(returncode=1, stdout='', stderr='execution error: boom')
        out = io.StringIO()
        with mock.patch.object(S.subprocess, 'run', return_value=bad), \
                mock.patch.object(S.sys, 'platform', 'darwin'), \
                mock.patch.object(S.sys.stdin, 'isatty', return_value=False), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(S.SetupError):
                S.ask_key(ctx)
        self.assertIn('boom', out.getvalue())


class Settings(Base):
    def test_settings_written_and_merged_keeping_unknown_keys(self):
        drive = self.make_drive() / 'My Drive'
        out_dir = self.tmp / 'pieces'
        self.ctx.config_dir.mkdir(parents=True)
        self.ctx.settings_file.write_text(json.dumps({'future_key': 7, 'artist_name': 'Old'}))
        code, _, err = self.run_cmd('settings', '--artist', 'Mira Ash', '--drive-folder', str(drive),
                                    '--output-folder', str(out_dir), '--budget', '20')
        self.assertEqual(code, 0, err)
        data = json.loads(self.ctx.settings_file.read_text())
        self.assertEqual(data, {'future_key': 7, 'artist_name': 'Mira Ash', 'drive_folder': str(drive),
                                'output_folder': str(out_dir), 'budget_usd': 20.0})
        self.assertTrue(out_dir.is_dir())
        # a second call changing one field keeps the rest
        self.run_cmd('settings', '--budget', '15.5')
        data = json.loads(self.ctx.settings_file.read_text())
        self.assertEqual(data['budget_usd'], 15.5)
        self.assertEqual(data['artist_name'], 'Mira Ash')

    def test_bad_values_refused(self):
        for argv in (['--budget', 'abc'], ['--budget', '0'], ['--budget', '-3'], ['--artist', 'a/b'],
                     ['--drive-folder', str(self.tmp / 'nope')], []):
            code, _, err = self.run_cmd('settings', *argv)
            self.assertNotEqual(code, 0, argv)
            self.assertIn('PROBLEM', err)
        self.assertFalse(self.ctx.settings_file.exists())

    def test_unreadable_settings_refused(self):
        self.ctx.config_dir.mkdir(parents=True)
        self.ctx.settings_file.write_text('{nope')
        code, _, err = self.run_cmd('settings', '--budget', '5')
        self.assertNotEqual(code, 0)
        self.assertEqual(self.ctx.settings_file.read_text(), '{nope')


class TildeAndGuards(Base):
    def test_tilde_paths_land_under_fake_home_only(self):
        real_home = Path.home()
        probe = 'snippy-test-probe-%d' % os.getpid()
        (self.home / 'Drv').mkdir()
        code, _, err = self.run_cmd('settings', '--output-folder', '~/' + probe, '--drive-folder', '~/Drv')
        self.assertEqual(code, 0, err)
        self.assertTrue((self.home / probe).is_dir())
        self.assertFalse((real_home / probe).exists())
        data = json.loads(self.ctx.settings_file.read_text())
        self.assertEqual(data['output_folder'], str(self.home / probe))
        self.assertEqual(data['drive_folder'], str(self.home / 'Drv'))
        # check also resolves ~ in a saved setting against the fake home
        self.ctx.settings_file.write_text(json.dumps({'drive_folder': '~/Drv'}))
        _, out, _ = self.run_cmd('check', '--json')
        by = {c['name']: c for c in json.loads(out)['checks']}
        self.assertTrue(by['Drive folder']['ok'])

    def test_relative_folder_refused(self):
        code, _, err = self.run_cmd('settings', '--output-folder', 'Documents/Snippy Pieces')
        self.assertNotEqual(code, 0)
        self.assertIn('full path', err)
        self.assertFalse(Path('Documents/Snippy Pieces').exists())

    def test_env_config_dir_tilde_under_fake_home_when_no_home(self):
        ctx = S.Ctx(None, True)
        with mock.patch.dict(os.environ, {'SNIPPY_CONFIG_DIR': str(self.tmp / 'x')}):
            self.assertEqual(ctx.config_dir, self.tmp / 'x')

    def test_uninstall_all_keeps_name_guard_with_env_override(self):
        victim = self.tmp / 'precious'
        victim.mkdir()
        (victim / 'f').write_text('x')
        ctx = S.Ctx(None, True)
        with mock.patch.dict(os.environ, {'SNIPPY_CONFIG_DIR': str(victim)}):
            with self.assertRaises(S.SetupError):
                S.cmd_uninstall(ctx, S.build_parser().parse_args(['uninstall', '--all']))
        self.assertTrue((victim / 'f').exists())


class DriveFind(Base):
    def test_lists_my_drive_and_shared_drives(self):
        self.make_drive()
        code, out, _ = self.run_cmd('drive-find', '--json')
        self.assertEqual(code, 0)
        items = json.loads(out)
        paths = [i['path'] for i in items]
        self.assertTrue(any(p.endswith('My Drive') for p in paths))
        self.assertTrue(any(p.endswith('Shared drives/Team Art') for p in paths))
        code, out, _ = self.run_cmd('drive-find')
        self.assertIn('My Drive', out)

    def test_none_found(self):
        code, out, err = self.run_cmd('drive-find')
        self.assertNotEqual(code, 0)
        self.assertIn('Google Drive for desktop not found', err)
        self.assertIn('google.com/drive/download', err)


class LoginItem(Base):
    def test_plist_content_and_no_launchctl(self):
        self.run_cmd('install')
        with mock.patch.object(S.subprocess, 'run') as run, mock.patch.object(S.subprocess, 'Popen') as popen:
            code, out, err = self.run_cmd('login-item')
        self.assertEqual(code, 0, err)
        run.assert_not_called()
        popen.assert_not_called()
        data = plistlib.loads(self.ctx.plist_file.read_bytes())
        self.assertEqual(data['Label'], 'com.snippypieces.helper')
        self.assertEqual(data['ProgramArguments'],
                         [sys.executable, '-B', str(self.ctx.engine_dir / 'daemon.py')])
        self.assertTrue(data['RunAtLoad'])
        self.assertNotIn('sk-', json.dumps(data))
        self.assertTrue(str(self.ctx.plist_file).startswith(str(self.home)))

    def test_needs_engine_first(self):
        code, _, err = self.run_cmd('login-item')
        self.assertNotEqual(code, 0)
        self.assertFalse(self.ctx.plist_file.exists())

    def test_remove(self):
        self.run_cmd('install')
        self.run_cmd('login-item')
        with mock.patch.object(S.subprocess, 'run') as run:
            code, out, _ = self.run_cmd('login-item', '--remove')
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertFalse(self.ctx.plist_file.exists())

    def test_real_mode_calls_launchctl_through_patch_only(self):
        # Real-mode logic with subprocess patched: proves the load commands, never runs launchctl.
        ctx = S.Ctx(self.home, False)
        (ctx.engine_dir).mkdir(parents=True)
        (ctx.engine_dir / 'daemon.py').write_text('x')
        calls = []
        done = mock.Mock(returncode=0, stderr='', stdout='')
        with mock.patch.object(S.subprocess, 'run', side_effect=lambda a, **k: calls.append(a) or done), \
                mock.patch.object(S, 'helper_answers', return_value=True):
            with contextlib.redirect_stdout(io.StringIO()):
                S.cmd_login_item(ctx, S.build_parser().parse_args(['login-item']))
        self.assertTrue(any(c[:2] == ['launchctl', 'bootstrap'] for c in calls))

    def test_mac_loaded_but_silent_helper_is_loud_with_log_tail(self):
        ctx = S.Ctx(self.home, False)
        ctx.engine_dir.mkdir(parents=True)
        (ctx.engine_dir / 'daemon.py').write_text('x')
        (ctx.config_dir / 'logs').mkdir(parents=True)
        (ctx.config_dir / 'logs' / 'daemon.err.log').write_text('line one\nTraceback boom\n', encoding='utf-8')
        done = mock.Mock(returncode=0, stderr='', stdout='')
        with mock.patch.object(S.subprocess, 'run', return_value=done), mock.patch.object(S, 'helper_answers', return_value=False), \
                mock.patch.object(S, 'START_WAIT_SECONDS', 0.3), mock.patch.object(S, 'START_POLL_SECONDS', 0.05), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(S.SetupError) as cm:
                S.cmd_login_item(ctx, S.build_parser().parse_args(['login-item']))
        self.assertIn('did not start', str(cm.exception))
        self.assertIn('Traceback boom', str(cm.exception))
        self.assertIn('daemon.err.log', str(cm.exception))
        self.assertNotIn('is running', out.getvalue())


class HelperAnswers(Base):
    def serve(self, server_name):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = server_name
            sys_version = ''

            def do_GET(self):
                self.send_response(405)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *_):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_snippy_helper_answer_counts_other_programs_do_not(self):
        self.assertTrue(S.helper_answers(self.serve('snippy-helper')))
        self.assertFalse(S.helper_answers(self.serve('something-else')))

    def test_nothing_listening_is_false(self):
        import socket
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        self.assertFalse(S.helper_answers(port))

    def test_port_comes_from_the_helper_code_and_its_env_override(self):
        with mock.patch.dict(os.environ, {'SNIPPY_HELPER_PORT': '19999'}):
            self.assertEqual(S.helper_port(), 19999)
        os.environ.pop('SNIPPY_HELPER_PORT', None)
        self.assertEqual(S.helper_port(), 18830)


class ConnectClaude(Base):
    def setUp(self):
        super().setUp()
        self.run_cmd('install')
        self.entry = {'command': sys.executable, 'args': ['-B', str(self.ctx.engine_dir / 'mcp_server.py')]}

    def test_creates_config_when_missing(self):
        self.make_claude_folder()
        code, out, _ = self.run_cmd('connect-claude')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.ctx.claude_config.read_text())['mcpServers']['snippy-engine'], self.entry)
        self.assertIn('restart the Claude app', out)

    def test_merge_keeps_everything_and_backs_up(self):
        original = {'theme': 'x', 'mcpServers': {'other': {'command': 'node', 'args': ['a']}}}
        self.write_claude(original)
        code, out, _ = self.run_cmd('connect-claude')
        self.assertEqual(code, 0)
        data = json.loads(self.ctx.claude_config.read_text())
        self.assertEqual(data['theme'], 'x')
        self.assertEqual(data['mcpServers']['other'], original['mcpServers']['other'])
        self.assertEqual(data['mcpServers']['snippy-engine'], self.entry)
        baks = list(self.ctx.claude_config.parent.glob('claude_desktop_config.json.bak-*'))
        self.assertEqual(len(baks), 1)
        self.assertEqual(json.loads(baks[0].read_text()), original)

    def test_unreadable_refused_untouched(self):
        path = self.write_claude('{ not json')
        code, _, err = self.run_cmd('connect-claude')
        self.assertNotEqual(code, 0)
        self.assertIn('could not be read', err)
        self.assertEqual(path.read_text(), '{ not json')
        self.assertEqual(list(path.parent.glob('*.bak-*')), [])

    def test_conflicting_entry_refused_unless_replace(self):
        other = {'mcpServers': {'snippy-engine': {'command': 'other', 'args': []}}}
        path = self.write_claude(other)
        code, _, err = self.run_cmd('connect-claude')
        self.assertNotEqual(code, 0)
        self.assertEqual(json.loads(path.read_text()), other)
        code, _, _ = self.run_cmd('connect-claude', '--replace')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(path.read_text())['mcpServers']['snippy-engine'], self.entry)

    def test_same_entry_is_noop(self):
        self.make_claude_folder()
        self.run_cmd('connect-claude')
        before = self.ctx.claude_config.read_text()
        code, out, _ = self.run_cmd('connect-claude')
        self.assertEqual(code, 0)
        self.assertEqual(self.ctx.claude_config.read_text(), before)
        self.assertEqual(list(self.ctx.claude_config.parent.glob('*.bak-*')), [])

    def test_needs_engine(self):
        shutil.rmtree(self.ctx.engine_dir)
        code, _, _ = self.run_cmd('connect-claude')
        self.assertNotEqual(code, 0)


class Uninstall(Base):
    def full_setup(self):
        self.run_cmd('install')
        self.run_cmd('key', '--no-network', key=FAKE_KEY)
        self.run_cmd('settings', '--artist', 'Mira')
        self.write_claude({'mcpServers': {'other': {'command': 'x'}}})
        self.run_cmd('connect-claude')
        self.run_cmd('login-item')

    def test_uninstall_keeps_key_and_settings(self):
        self.full_setup()
        with mock.patch.object(S.subprocess, 'run') as run:
            code, out, _ = self.run_cmd('uninstall')
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertFalse(self.ctx.plist_file.exists())
        self.assertFalse(self.ctx.engine_dir.exists())
        data = json.loads(self.ctx.claude_config.read_text())
        self.assertNotIn('snippy-engine', data['mcpServers'])
        self.assertIn('other', data['mcpServers'])
        self.assertTrue(self.ctx.key_file.is_file())
        self.assertTrue(self.ctx.settings_file.is_file())
        self.assertTrue(list(self.ctx.claude_config.parent.glob('*.bak-*')))

    def test_uninstall_all_removes_key(self):
        self.full_setup()
        code, _, _ = self.run_cmd('uninstall', '--all')
        self.assertEqual(code, 0)
        self.assertFalse(self.ctx.config_dir.exists())

    def test_uninstall_leaves_a_different_snippy_engine_entry(self):
        other = {'mcpServers': {'snippy-engine': {'command': 'x', 'args': ['/elsewhere/mcp_server.py']}}}
        path = self.write_claude(other)
        code, out, _ = self.run_cmd('uninstall')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(path.read_text()), other)
        self.assertIn('left alone', out)
        self.assertEqual(list(path.parent.glob('*.bak-*')), [])

    def test_uninstall_bootout_failure_is_loud_and_changes_nothing(self):
        self.run_cmd('install')
        self.run_cmd('login-item')
        self.make_claude_folder()
        self.run_cmd('connect-claude')
        ctx = S.Ctx(self.home, False)
        before = ctx.claude_config.read_text()
        bad = mock.Mock(returncode=5, stderr='Boot-out failed: 5: Input/output error', stdout='')
        out = io.StringIO()
        with mock.patch.object(S.subprocess, 'run', return_value=bad), contextlib.redirect_stdout(out):
            with self.assertRaises(S.SetupError) as cm:
                S.cmd_uninstall(ctx, S.build_parser().parse_args(['uninstall', '--all']))
        self.assertIn('nothing was removed', str(cm.exception))
        self.assertTrue(ctx.plist_file.is_file())
        self.assertTrue(ctx.engine_dir.is_dir())
        self.assertTrue(ctx.config_dir.is_dir())
        self.assertEqual(ctx.claude_config.read_text(), before)
        self.assertNotIn('removed', out.getvalue().lower())

    def test_uninstall_bootout_not_loaded_is_fine(self):
        self.run_cmd('install')
        self.run_cmd('login-item')
        ctx = S.Ctx(self.home, False)
        notloaded = mock.Mock(returncode=3, stderr='Boot-out failed: 3: No such process', stdout='')
        with mock.patch.object(S.subprocess, 'run', return_value=notloaded), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(S.cmd_uninstall(ctx, S.build_parser().parse_args(['uninstall'])), 0)
        self.assertFalse(ctx.plist_file.exists())

    def test_uninstall_with_nothing_installed(self):
        code, out, _ = self.run_cmd('uninstall')
        self.assertEqual(code, 0)

    def test_uninstall_unreadable_claude_config_changes_nothing(self):
        self.run_cmd('install')
        self.run_cmd('login-item')
        path = self.write_claude('{bad')
        code, out, err = self.run_cmd('uninstall', '--all')
        self.assertNotEqual(code, 0)
        self.assertIn('Nothing was changed', err)
        self.assertTrue(self.ctx.plist_file.is_file())
        self.assertTrue(self.ctx.engine_dir.is_dir())
        self.assertTrue(self.ctx.config_dir.is_dir())
        self.assertEqual(path.read_text(), '{bad')
        self.assertEqual(out.strip(), '')

    def test_uninstall_says_what_it_removed(self):
        self.full_setup()
        _, out, _ = self.run_cmd('uninstall')
        self.assertIn('Login item removed', out)
        self.assertIn('Snippy removed from the Claude app config', out)
        self.assertIn('Engine removed', out)


class Check(Base):
    def checks(self):
        code, out, _ = self.run_cmd('check', '--json')
        data = json.loads(out)
        return code, {c['name']: c for c in data['checks']}, data

    def test_empty_home_reports_every_missing_piece(self):
        code, by, data = self.checks()
        self.assertEqual(code, 1)
        self.assertFalse(data['ok'])
        for name in ('Config folder', 'Engine installed', 'OpenAI key', 'Setting: artist_name',
                     'Setting: drive_folder', 'Setting: output_folder', 'Setting: budget_usd', 'Drive folder',
                     'Login item (starts Snippy at login)', 'Claude app knows Snippy'):
            self.assertFalse(by[name]['ok'], name)
            self.assertTrue(by[name]['fix'], name)
        self.assertTrue(by['Python']['ok'])
        self.assertTrue(by['Kit files']['ok'])

    def test_plain_output(self):
        code, out, _ = self.run_cmd('check')
        self.assertEqual(code, 1)
        self.assertIn('[todo] OpenAI key: not set', out)

    def test_fully_set_up_passes_and_never_shows_key(self):
        drive = self.make_drive() / 'My Drive'
        self.run_cmd('install')
        self.run_cmd('key', '--no-network', key=FAKE_KEY)
        self.run_cmd('settings', '--artist', 'Mira', '--drive-folder', str(drive),
                     '--output-folder', str(self.tmp / 'o'), '--budget', '10')
        self.run_cmd('login-item')
        self.make_claude_folder()
        self.run_cmd('connect-claude')
        code, out, _ = self.run_cmd('check')
        self.assertEqual(code, 0, out)
        self.assertNotIn(FAKE_KEY, out)
        self.assertIn('All set.', out)

    def test_loose_key_permissions_flagged(self):
        self.run_cmd('key', '--no-network', key=FAKE_KEY)
        os.chmod(self.ctx.key_file, 0o644)
        _, by, _ = self.checks()
        self.assertFalse(by['OpenAI key']['ok'])

    def test_unwritable_or_missing_drive_flagged(self):
        self.ctx.config_dir.mkdir(parents=True)
        self.ctx.settings_file.write_text(json.dumps({'drive_folder': str(self.tmp / 'gone')}))
        _, by, _ = self.checks()
        self.assertFalse(by['Drive folder']['ok'])

    def test_unreadable_claude_config_flagged(self):
        self.write_claude('{bad')
        _, by, _ = self.checks()
        self.assertFalse(by['Claude app knows Snippy']['ok'])

    def test_env_key_counts(self):
        with mock.patch.dict(os.environ, {S.KEY_ENV: FAKE_KEY}):
            _, by, _ = self.checks()
        self.assertTrue(by['OpenAI key']['ok'])


class WinBase(Base):
    """Runs the setup as Windows: sys.platform patched to win32, a fake %APPDATA% under the fake home, a fake
    python folder holding python.exe and pythonw.exe. Real Windows was never available; see the build notes."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(sys, 'platform', 'win32')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pydir = self.tmp / 'Python312'
        self.pydir.mkdir()
        (self.pydir / 'python.exe').write_text('')
        (self.pydir / 'pythonw.exe').write_text('')
        exe = mock.patch.object(sys, 'executable', str(self.pydir / 'python.exe'))
        exe.start()
        self.addCleanup(exe.stop)

    def full_setup(self):
        self.run_cmd('install')
        self.run_cmd('key', '--no-network', key=FAKE_KEY)
        self.run_cmd('settings', '--artist', 'Mira')
        self.write_claude({'mcpServers': {'other': {'command': 'x'}}})
        self.run_cmd('connect-claude')
        self.run_cmd('login-item')

    def fake_powershell(self, ctx, command_line, calls, kill_ok=True):
        """A subprocess.run stand-in: powershell prints command_line for the pid; taskkill succeeds or fails."""
        def fake_run(argv, **kw):
            calls.append(argv[0])
            if argv[0] == 'powershell.exe':
                return mock.Mock(returncode=0, stdout=command_line, stderr='')
            return mock.Mock(returncode=0 if kill_ok else 1, stdout='', stderr='' if kill_ok else 'Access is denied')
        return fake_run


class WindowsPaths(WinBase):
    def test_everything_under_fake_appdata(self):
        appdata = self.home / 'AppData' / 'Roaming'
        self.assertEqual(self.ctx.config_dir, appdata / 'SnippyPieces')
        self.assertEqual(self.ctx.claude_config, appdata / 'Claude' / 'claude_desktop_config.json')
        self.assertEqual(self.ctx.startup_file, appdata / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs'
                         / 'Startup' / 'SnippyPieces.cmd')

    def test_real_mode_uses_appdata_env(self):
        with mock.patch.dict(os.environ, {'APPDATA': str(self.tmp / 'Roam')}):
            ctx = S.Ctx(None, False)
            self.assertEqual(ctx.config_dir, self.tmp / 'Roam' / 'SnippyPieces')
            self.assertEqual(ctx.claude_config, self.tmp / 'Roam' / 'Claude' / 'claude_desktop_config.json')

    def test_commands_no_longer_refuse_on_windows(self):
        ctx = S.Ctx(self.home, False)  # real mode: the old Mac-only refusal must be gone
        S.need_supported(ctx, 'x')
        with mock.patch.object(sys, 'platform', 'freebsd13'):
            with self.assertRaises(S.SetupError):
                S.need_supported(ctx, 'x')


class WindowsKey(WinBase):
    def test_key_saved_privately_and_never_echoed(self):
        code, out, err = self.run_cmd('key', '--no-network', key=FAKE_KEY)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ctx.key_file.read_text(), FAKE_KEY)
        self.assertNotIn(FAKE_KEY, out + err)
        self.assertIn('Windows account folder', out)
        for path in self.home.rglob('*'):
            if path.is_file() and path != self.ctx.key_file:
                self.assertNotIn(FAKE_KEY, path.read_text(errors='ignore'), path)

    def real_ctx(self):
        return S.Ctx(self.home, False)

    def test_tk_box_is_the_first_choice_and_key_not_in_argv(self):
        with mock.patch.object(S, 'tk_key_box', return_value='  ' + FAKE_KEY + ' '), \
                mock.patch.object(S.subprocess, 'run') as run:
            key, how = S.ask_key(self.real_ctx())
        self.assertEqual(key, FAKE_KEY)
        self.assertIn('tkinter', how)
        run.assert_not_called()

    def test_tk_cancel_is_loud_and_final(self):
        with mock.patch.object(S, 'tk_key_box', return_value=None), mock.patch.object(S.subprocess, 'run') as run:
            with self.assertRaises(S.SetupError) as cm:
                S.ask_key(self.real_ctx())
        self.assertIn('Cancelled', str(cm.exception))
        run.assert_not_called()

    def test_powershell_fallback_when_tk_missing(self):
        done = mock.Mock(returncode=0, stdout=FAKE_KEY + '\r\n', stderr='')
        out = io.StringIO()
        with mock.patch.object(S, 'tk_key_box', side_effect=ImportError('no tkinter')), \
                mock.patch.object(S.subprocess, 'run', return_value=done) as run, contextlib.redirect_stdout(out):
            key, how = S.ask_key(self.real_ctx())
        self.assertEqual(key, FAKE_KEY)
        self.assertIn('PowerShell', how)
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], 'powershell.exe')
        self.assertNotIn(FAKE_KEY, ' '.join(argv))
        self.assertIn('Get-Credential', ' '.join(argv))
        self.assertIn('no tkinter', out.getvalue())
        self.assertNotIn(FAKE_KEY, out.getvalue())

    def test_powershell_cancel_and_failure(self):
        for code, word in ((3, 'Cancelled'), (1, 'No private way')):
            done = mock.Mock(returncode=code, stdout='', stderr='boom')
            with mock.patch.object(S, 'tk_key_box', side_effect=ImportError('x')), \
                    mock.patch.object(S.subprocess, 'run', return_value=done), \
                    mock.patch.object(sys, 'stdin', None), contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(S.SetupError) as cm:
                    S.ask_key(self.real_ctx())
            self.assertIn(word, str(cm.exception))
            if code == 1:
                self.assertIn('boom', out.getvalue())

    def test_no_box_possible_refuses_loudly(self):
        with mock.patch.object(S, 'tk_key_box', side_effect=ImportError('x')), \
                mock.patch.object(S.subprocess, 'run', side_effect=OSError('no powershell')), \
                mock.patch.object(sys, 'stdin', None), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(S.SetupError) as cm:
                S.ask_key(self.real_ctx())
        self.assertIn('Nothing was saved', str(cm.exception))


class WindowsDrive(WinBase):
    def test_drive_letter_and_mirrored_folder(self):
        g = self.home / 'driveletters' / 'G'
        (g / 'My Drive').mkdir(parents=True)
        (g / 'Shared drives' / 'Team Art').mkdir(parents=True)
        (g / '$RECYCLE.BIN').mkdir()
        (self.home / 'driveletters' / 'H' / 'Photos').mkdir(parents=True)  # not Google Drive
        (self.home / 'My Drive').mkdir()
        code, out, err = self.run_cmd('drive-find', '--json')
        self.assertEqual(code, 0, err)
        paths = [i['path'] for i in json.loads(out)]
        self.assertIn(str(g / 'My Drive'), paths)
        self.assertIn(str(g / 'Shared drives' / 'Team Art'), paths)
        self.assertIn(str(self.home / 'My Drive'), paths)
        self.assertEqual(len(paths), 3)
        self.assertTrue(any('(G)' in i['label'] for i in json.loads(out)))

    def test_none_found_says_drive_letter(self):
        code, _, err = self.run_cmd('drive-find')
        self.assertNotEqual(code, 0)
        self.assertIn('drive letter', err)

    def test_a_drive_that_does_not_answer_is_skipped_and_said(self):
        import time
        for letter in 'GH':
            (self.home / 'driveletters' / letter / 'My Drive').mkdir(parents=True)
        real = S._has_drive_dirs

        def slow_for_h(root):
            if root.name == 'H':
                time.sleep(2)
            return real(root)

        errs = io.StringIO()
        with mock.patch.object(S, '_has_drive_dirs', slow_for_h), mock.patch.object(S, 'DRIVE_PROBE_SECONDS', 0.2), \
                contextlib.redirect_stderr(errs):
            code, out, err = self.run_cmd('drive-find', '--json')
        self.assertEqual(code, 0)
        paths = [i['path'] for i in json.loads(out)]
        self.assertEqual(paths, [str(self.home / 'driveletters' / 'G' / 'My Drive')])
        self.assertIn('Skipped drive H', err + errs.getvalue())

    def test_unreadable_folder_gives_a_plain_message_not_a_traceback(self):
        (self.home / 'driveletters' / 'G' / 'My Drive').mkdir(parents=True)
        (self.home / 'driveletters' / 'K' / 'My Drive').mkdir(parents=True)
        real = S._list_root

        def broken_for_k(root, only):
            if root.name == 'K':
                raise PermissionError('denied')
            return real(root, only)

        with mock.patch.object(S, '_list_root', broken_for_k):
            code, out, err = self.run_cmd('drive-find', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)), 1)
        self.assertIn('Could not read K (PermissionError). Skipped it.', err)
        self.assertNotIn('Traceback', err)

    def test_real_mode_scans_letters_d_to_z_only(self):
        roots = S.Ctx(None, False).windows_drive_roots()
        self.assertEqual(len(roots), 23)
        self.assertEqual(str(roots[0])[0], 'D')


class WindowsLoginItem(WinBase):
    def test_startup_cmd_content_and_no_subprocess(self):
        self.run_cmd('install')
        with mock.patch.object(S.subprocess, 'run') as run, mock.patch.object(S.subprocess, 'Popen') as popen:
            code, out, err = self.run_cmd('login-item')
        self.assertEqual(code, 0, err)
        run.assert_not_called()
        popen.assert_not_called()
        raw = self.ctx.startup_file.read_bytes()
        self.assertIn(b'\r\n', raw)
        self.assertNotIn(b'\n', raw.replace(b'\r\n', b''))
        text = raw.decode()
        self.assertTrue(raw.isascii())
        self.assertIn('start "" /D "%APPDATA%\\SnippyPieces\\engine"', text)
        self.assertIn('-B "%APPDATA%\\SnippyPieces\\engine\\daemon.py"', text)
        self.assertIn('"%s" -B' % (self.pydir / 'pythonw.exe'), text)
        self.assertNotIn('python.exe', text)
        self.assertNotIn('sk-', text)
        self.assertTrue(str(self.ctx.startup_file).startswith(str(self.home)))
        self.assertFalse(self.ctx.plist_file.exists())

    def test_needs_engine_and_pythonw(self):
        code, _, _ = self.run_cmd('login-item')
        self.assertNotEqual(code, 0)
        self.run_cmd('install')
        (self.pydir / 'pythonw.exe').unlink()
        code, _, err = self.run_cmd('login-item')
        self.assertNotEqual(code, 0)
        self.assertIn('pythonw.exe was not found', err)
        self.assertFalse(self.ctx.startup_file.exists())

    def test_percent_in_path_refused(self):
        self.run_cmd('install')
        odd = self.tmp / 'Py%PATH%'
        odd.mkdir()
        (odd / 'python.exe').write_text('')
        (odd / 'pythonw.exe').write_text('')
        with mock.patch.object(sys, 'executable', str(odd / 'python.exe')):
            code, _, err = self.run_cmd('login-item')
        self.assertNotEqual(code, 0)
        self.assertFalse(self.ctx.startup_file.exists())

    def test_remove(self):
        self.run_cmd('install')
        self.run_cmd('login-item')
        with mock.patch.object(S.subprocess, 'run') as run:
            code, out, _ = self.run_cmd('login-item', '--remove')
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertFalse(self.ctx.startup_file.exists())
        code, out, _ = self.run_cmd('login-item', '--remove')
        self.assertIn('not installed', out)

    def test_real_mode_starts_helper_with_pythonw_no_window_and_stops_old_one(self):
        ctx = S.Ctx(self.home, False)
        ctx.engine_dir.mkdir(parents=True)
        (ctx.engine_dir / 'daemon.py').write_text('x')
        ctx.helper_dir.mkdir(parents=True)
        (ctx.helper_dir / 'daemon.pid').write_text('4242')
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            if argv[0] == 'powershell.exe':
                return mock.Mock(returncode=0, stdout='"%s" -B "%s"\r\n' % (self.pydir / 'pythonw.exe', ctx.engine_dir / 'daemon.py'), stderr='')
            return mock.Mock(returncode=0, stdout='', stderr='')

        proc = mock.Mock()
        proc.poll.return_value = None
        with mock.patch.object(S.subprocess, 'run', side_effect=fake_run), mock.patch.object(S, 'helper_answers', return_value=True), \
                mock.patch.object(S.subprocess, 'Popen', return_value=proc) as popen, contextlib.redirect_stdout(io.StringIO()):
            S.cmd_login_item(ctx, S.build_parser().parse_args(['login-item']))
        self.assertIn(['taskkill', '/PID', '4242', '/F'], calls)
        self.assertEqual(popen.call_args[1]['env']['PYTHONUTF8'], '1')
        self.assertEqual(popen.call_args[1]['env']['PYTHONIOENCODING'], 'utf-8')
        self.assertNotEqual(popen.call_args[1]['stdout'], S.subprocess.DEVNULL)  # a log file, not the void
        self.assertEqual(popen.call_args[1]['stderr'], S.subprocess.STDOUT)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], str(self.pydir / 'pythonw.exe'))
        self.assertEqual(popen.call_args[1]['creationflags'], S.DETACHED_PROCESS | S.CREATE_NO_WINDOW)
        self.assertFalse((ctx.helper_dir / 'daemon.pid').exists())

    def start_real(self, poll, answers, log_text=None):
        ctx = S.Ctx(self.home, False)
        ctx.engine_dir.mkdir(parents=True)
        (ctx.engine_dir / 'daemon.py').write_text('x')
        proc = mock.Mock()
        proc.poll.side_effect = poll
        out = io.StringIO()

        def fake_popen(argv, **kw):
            if log_text:
                kw['stdout'].write(log_text.encode('utf-8'))
            return proc

        with mock.patch.object(S.subprocess, 'Popen', side_effect=fake_popen), mock.patch.object(S, 'helper_answers', side_effect=answers), \
                mock.patch.object(S, 'START_WAIT_SECONDS', 0.3), mock.patch.object(S, 'START_POLL_SECONDS', 0.05), \
                contextlib.redirect_stdout(out):
            try:
                S.cmd_login_item(ctx, S.build_parser().parse_args(['login-item']))
            except S.SetupError as exc:
                return ctx, str(exc), out.getvalue()
        return ctx, None, out.getvalue()

    def test_started_only_when_alive_and_answering(self):
        ctx, error, out = self.start_real(lambda: None, [False, False, True])
        self.assertIsNone(error)
        self.assertIn('helper started', out)

    def test_process_that_dies_at_once_is_loud_with_log_tail(self):
        ctx, error, out = self.start_real(lambda: 1, lambda port: True, 'Snippy helper cannot start: port 18830 busy \u00e9\n')
        self.assertIn('did not start', error)
        self.assertIn('stopped right away', error)
        self.assertIn('port 18830 busy \u00e9', error)
        self.assertIn('daemon.out.log', error)
        self.assertNotIn('started', out)

    def test_alive_but_never_answers_is_loud(self):
        ctx, error, out = self.start_real(lambda: None, lambda port: False)
        self.assertIn('did not answer', error)
        self.assertNotIn('started', out)

    def test_reused_pid_of_another_program_is_refused_and_nothing_removed(self):
        self.full_setup()
        ctx = S.Ctx(self.home, False)
        ctx.helper_dir.mkdir(parents=True)
        (ctx.helper_dir / 'daemon.pid').write_text('77')
        calls = []
        other = r'"C:\Python312\python.exe" "C:\Other\tool\server.py"'
        with mock.patch.object(S.subprocess, 'run', side_effect=self.fake_powershell(ctx, other, calls)), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(S.SetupError) as cm:
                S.cmd_uninstall(ctx, S.build_parser().parse_args(['uninstall', '--all']))
        self.assertIn('is not the Snippy helper', str(cm.exception))
        self.assertEqual(calls, ['powershell.exe'])  # taskkill never ran
        self.assertTrue((ctx.helper_dir / 'daemon.pid').is_file())
        self.assertTrue(ctx.startup_file.is_file())
        self.assertTrue(ctx.engine_dir.is_dir())
        self.assertTrue(ctx.key_file.is_file())

    def test_stale_pid_file_with_no_process_is_just_removed(self):
        ctx = S.Ctx(self.home, False)
        ctx.helper_dir.mkdir(parents=True)
        (ctx.helper_dir / 'daemon.pid').write_text('77')
        calls = []
        with mock.patch.object(S.subprocess, 'run', side_effect=self.fake_powershell(ctx, '', calls)):
            self.assertFalse(S.stop_helper_windows(ctx))
        self.assertEqual(calls, ['powershell.exe'])
        self.assertFalse((ctx.helper_dir / 'daemon.pid').exists())

    def test_powershell_command_holds_only_the_digits_of_the_pid(self):
        ctx = S.Ctx(self.home, False)
        ctx.helper_dir.mkdir(parents=True)
        (ctx.helper_dir / 'daemon.pid').write_text('1; calc')
        with mock.patch.object(S.subprocess, 'run') as run:
            with self.assertRaises(S.SetupError):
                S.stop_helper_windows(ctx)
        run.assert_not_called()

    def test_taskkill_failure_is_loud_and_keeps_everything(self):
        self.run_cmd('install')
        self.run_cmd('login-item')
        ctx = S.Ctx(self.home, False)
        ctx.helper_dir.mkdir(parents=True)
        (ctx.helper_dir / 'daemon.pid').write_text('4242')

        def fake_run(argv, **kw):
            if argv[0] == 'powershell.exe':
                return mock.Mock(returncode=0, stdout='"x\\pythonw.exe" -B "%s"' % (ctx.engine_dir / 'daemon.py'), stderr='')
            return mock.Mock(returncode=1, stdout='', stderr='Access is denied')

        with mock.patch.object(S.subprocess, 'run', side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(S.SetupError) as cm:
                S.cmd_uninstall(ctx, S.build_parser().parse_args(['uninstall', '--all']))
        self.assertIn('Nothing was removed', str(cm.exception))
        self.assertTrue(ctx.startup_file.is_file())
        self.assertTrue(ctx.engine_dir.is_dir())


class WindowsEncodingAndAccents(WinBase):
    def test_startup_script_is_ascii_for_an_accented_user_name(self):
        accented = self.tmp / 'Jos\u00e9 \u00c1lvarez'
        accented.mkdir()
        ctx = S.Ctx(accented, True)
        local_py = ctx.local_appdata / 'Programs' / 'Python' / 'Python312'
        local_py.mkdir(parents=True)
        (local_py / 'python.exe').write_text('')
        (local_py / 'pythonw.exe').write_text('')
        args = ['--home', str(accented), '--test-mode']
        with mock.patch.object(sys, 'executable', str(local_py / 'python.exe')), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(S.main(args + ['install']), 0)
            self.assertEqual(S.main(args + ['login-item']), 0)
        raw = ctx.startup_file.read_bytes()
        raw.decode('ascii')  # raises if any byte is not plain ASCII
        text = raw.decode('ascii')
        self.assertIn('%LOCALAPPDATA%\\Programs\\Python\\Python312\\pythonw.exe', text)
        self.assertIn('%APPDATA%\\SnippyPieces\\engine\\daemon.py', text)
        self.assertNotIn('\u00e9', text)

    def test_non_ascii_path_outside_user_folders_is_refused(self):
        self.run_cmd('install')
        odd = self.tmp / 'Pyth\u00f6n'
        odd.mkdir()
        (odd / 'python.exe').write_text('')
        (odd / 'pythonw.exe').write_text('')
        with mock.patch.object(sys, 'executable', str(odd / 'python.exe')):
            code, _, err = self.run_cmd('login-item')
        self.assertNotEqual(code, 0)
        self.assertIn('Nothing was written', err)
        self.assertFalse(self.ctx.startup_file.exists())

    def test_claude_config_with_accents_is_read_and_kept_as_utf8(self):
        self.run_cmd('install')
        text = json.dumps({'mcpServers': {'other': {'command': 'C:\\Users\\Jos\u00e9\\node.exe'}}}, ensure_ascii=False)
        path = self.ctx.claude_config
        path.parent.mkdir(parents=True)
        path.write_bytes(('\ufeff' + text).encode('utf-8'))  # even with a BOM
        code, _, err = self.run_cmd('connect-claude')
        self.assertEqual(code, 0, err)
        raw = path.read_bytes().decode('utf-8')
        self.assertIn('Jos\u00e9', raw)
        self.assertNotIn('\\u00e9', raw)
        self.assertIn('snippy-engine', raw)

    def test_non_utf8_claude_config_is_refused_untouched(self):
        self.run_cmd('install')
        path = self.ctx.claude_config
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"a": "Jos\xe9"}')
        code, _, err = self.run_cmd('connect-claude')
        self.assertNotEqual(code, 0)
        self.assertEqual(path.read_bytes(), b'{"a": "Jos\xe9"}')


class WindowsStoreClaude(WinBase):
    def store_dir(self):
        return self.home / 'AppData' / 'Local' / 'Packages' / 'Claude_pzs8sxrjxfjjc' / 'LocalCache' / 'Roaming' / 'Claude'

    def test_refuses_when_claude_never_ran_and_creates_nothing(self):
        self.run_cmd('install')
        code, out, err = self.run_cmd('connect-claude')
        self.assertNotEqual(code, 0)
        self.assertIn('Open the Claude app once', err)
        self.assertNotIn('Snippy added', out)
        self.assertFalse((self.home / 'AppData' / 'Roaming' / 'Claude').exists())
        self.assertFalse((self.home / 'AppData' / 'Local' / 'Packages').exists())

    def test_store_config_is_used_when_it_exists(self):
        self.run_cmd('install')
        folder = self.store_dir()
        folder.mkdir(parents=True)
        (folder / 'claude_desktop_config.json').write_text('{"mcpServers": {"other": {"command": "x"}}}')
        code, out, err = self.run_cmd('connect-claude')
        self.assertEqual(code, 0, err)
        data = json.loads((folder / 'claude_desktop_config.json').read_text())
        self.assertIn('snippy-engine', data['mcpServers'])
        self.assertIn('other', data['mcpServers'])
        self.assertFalse((self.home / 'AppData' / 'Roaming' / 'Claude').exists())
        self.assertEqual(self.ctx.claude_config, folder / 'claude_desktop_config.json')

    def test_store_folder_without_a_file_gets_the_file_there(self):
        self.run_cmd('install')
        self.store_dir().mkdir(parents=True)
        self.assertEqual(self.run_cmd('connect-claude')[0], 0)
        self.assertTrue((self.store_dir() / 'claude_desktop_config.json').is_file())

    def test_newest_of_two_configs_wins(self):
        std = self.home / 'AppData' / 'Roaming' / 'Claude'
        std.mkdir(parents=True)
        (std / 'claude_desktop_config.json').write_text('{}')
        self.store_dir().mkdir(parents=True)
        store = self.store_dir() / 'claude_desktop_config.json'
        store.write_text('{}')
        os.utime(std / 'claude_desktop_config.json', (1000, 1000))
        self.assertEqual(self.ctx.claude_config, store)

    def test_mac_never_creates_the_claude_folder_either(self):
        with mock.patch.object(sys, 'platform', 'darwin'):
            self.run_cmd('install')
            code, _, err = self.run_cmd('connect-claude')
            self.assertNotEqual(code, 0)
            self.assertIn('Open the Claude app once', err)
            self.assertFalse((self.home / 'Library' / 'Application Support' / 'Claude').exists())


class WindowsConnectClaude(WinBase):
    def test_creates_config_in_appdata_claude(self):
        self.run_cmd('install')
        self.make_claude_folder()
        code, out, err = self.run_cmd('connect-claude')
        self.assertEqual(code, 0, err)
        path = self.home / 'AppData' / 'Roaming' / 'Claude' / 'claude_desktop_config.json'
        self.assertEqual(self.ctx.claude_config, path)
        entry = json.loads(path.read_text())['mcpServers']['snippy-engine']
        self.assertEqual(entry['command'], str(self.pydir / 'python.exe'))
        self.assertEqual(entry['args'], ['-B', str(self.ctx.engine_dir / 'mcp_server.py')])

    def test_merge_backup_conflict_and_unreadable_rules(self):
        self.run_cmd('install')
        path = self.write_claude({'theme': 'dark', 'mcpServers': {'other': {'command': 'x'}}})
        code, out, _ = self.run_cmd('connect-claude')
        self.assertEqual(code, 0)
        data = json.loads(path.read_text())
        self.assertEqual(data['theme'], 'dark')
        self.assertIn('other', data['mcpServers'])
        self.assertEqual(len(list(path.parent.glob('*.bak-*'))), 1)
        code, out, _ = self.run_cmd('connect-claude')
        self.assertIn('already connected', out)
        self.assertEqual(len(list(path.parent.glob('*.bak-*'))), 1)
        data['mcpServers']['snippy-engine'] = {'command': 'other'}
        path.write_text(json.dumps(data))
        code, _, err = self.run_cmd('connect-claude')
        self.assertNotEqual(code, 0)
        self.assertIn('--replace', err)
        self.assertEqual(self.run_cmd('connect-claude', '--replace')[0], 0)
        path.write_text('{bad')
        code, _, err = self.run_cmd('connect-claude')
        self.assertNotEqual(code, 0)
        self.assertEqual(path.read_text(), '{bad')


class WindowsUninstallAndCheck(WinBase):
    def test_uninstall_keeps_key_and_settings(self):
        self.full_setup()
        with mock.patch.object(S.subprocess, 'run') as run:
            code, out, _ = self.run_cmd('uninstall')
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertFalse(self.ctx.startup_file.exists())
        self.assertFalse(self.ctx.engine_dir.exists())
        data = json.loads(self.ctx.claude_config.read_text())
        self.assertNotIn('snippy-engine', data['mcpServers'])
        self.assertIn('other', data['mcpServers'])
        self.assertTrue(self.ctx.key_file.is_file())
        self.assertTrue(self.ctx.settings_file.is_file())

    def test_uninstall_all_removes_config_folder_only(self):
        self.full_setup()
        out_dir = self.tmp / 'pieces'
        out_dir.mkdir()
        (out_dir / 'piece.png').write_text('x')
        code, _, _ = self.run_cmd('uninstall', '--all')
        self.assertEqual(code, 0)
        self.assertFalse(self.ctx.config_dir.exists())
        self.assertTrue((out_dir / 'piece.png').is_file())

    def test_uninstall_unreadable_claude_config_changes_nothing(self):
        self.run_cmd('install')
        self.run_cmd('login-item')
        path = self.write_claude('{bad')
        code, out, err = self.run_cmd('uninstall', '--all')
        self.assertNotEqual(code, 0)
        self.assertIn('Nothing was changed', err)
        self.assertTrue(self.ctx.startup_file.is_file())
        self.assertTrue(self.ctx.engine_dir.is_dir())
        self.assertEqual(path.read_text(), '{bad')

    def test_uninstall_all_stops_helper_before_deleting(self):
        self.full_setup()
        ctx = S.Ctx(self.home, False)
        ctx.helper_dir.mkdir(parents=True)
        (ctx.helper_dir / 'daemon.pid').write_text('4242')
        calls = []

        line = '"x\\pythonw.exe" -B "%s"' % (ctx.engine_dir / 'daemon.py')
        with mock.patch.object(S.subprocess, 'run', side_effect=self.fake_powershell(ctx, line, calls)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(S.cmd_uninstall(ctx, S.build_parser().parse_args(['uninstall', '--all'])), 0)
        self.assertEqual(calls, ['powershell.exe', 'taskkill'])
        self.assertFalse(ctx.config_dir.exists())

    def test_check_all_set_on_windows(self):
        self.full_setup()
        drive = self.home / 'driveletters' / 'G' / 'My Drive'
        drive.mkdir(parents=True)
        self.run_cmd('settings', '--drive-folder', str(drive), '--output-folder', '~/Documents/Snippy Pieces',
                     '--budget', '20')
        code, out, err = self.run_cmd('check')
        self.assertEqual(code, 0, out + err)
        self.assertIn('All set.', out)
        self.assertNotIn(FAKE_KEY, out)


class Cli(Base):
    def test_real_process_check_json_in_fake_home(self):
        result = subprocess.run([sys.executable, '-B', str(HERE / 'snippy_setup.py'), '--home', str(self.home),
                                 '--test-mode', 'check', '--json'], capture_output=True, text=True,
                                env={'PATH': '/usr/bin:/bin'})
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)['ok'])

    def test_no_secrets_or_owner_paths_in_kit_setup_files(self):
        for name in ('snippy_setup.py', 'test_setup.py'):
            text = (HERE / name).read_text()
            if name == 'snippy_setup.py':
                self.assertNotIn('/Users/', text)
                self.assertNotRegex(text, r'sk-[A-Za-z0-9]{20,}')

    def test_kit_uses_its_own_helper_port_everywhere(self):
        # 18810 is the owner's own Snippy helper. Setup sets no port, so the helper, the login item and the Claude entry all use the helper's default.
        helper = HERE.parent / 'helper'
        self.assertNotIn('18810', (HERE / 'snippy_setup.py').read_text())
        for name in ('daemon.py', 'mcp_server.py'):
            text = (helper / name).read_text()
            self.assertIn('DEFAULT_PORT = 18830', text)
            self.assertNotIn('18810', text.replace("18810 is the owner's Snippy helper", ''))
        self.assertNotIn('SNIPPY_HELPER_PORT', (HERE / 'snippy_setup.py').read_text())


if __name__ == '__main__':
    unittest.main()
