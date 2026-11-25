#!/usr/bin/env python3
"""Snippy Pieces guided setup (Mac and Windows). Python standard library only.

Subcommands: check, install, key, settings, drive-find, login-item, connect-claude, uninstall.
Every subcommand prints short plain lines and exits non-zero on any problem. The key is never printed,
never taken on the command line and never written anywhere but the private api-key file.

Global options (put them before or after the subcommand):
  --home PATH     re-root every path under a fake home (or env SNIPPY_SETUP_HOME). Used by tests.
  --test-mode     never call launchctl, osascript, powershell, taskkill or the network. Used by tests.
"""
import argparse
from datetime import datetime
from decimal import Decimal, InvalidOperation
import getpass
import http.client
import json
import os
from pathlib import Path
import plistlib
import shutil
import string
import threading
import subprocess
import sys
import time
import urllib.error
import urllib.request

LOGIN_ITEM_LABEL = 'com.snippypieces.helper'  # same label as helper/daemon.py
SERVER_NAME = 'snippy-engine'
KEY_ENV = 'SNIPPY_OPENAI_KEY'
TEST_KEY_ENV = 'SNIPPY_SETUP_TEST_KEY'  # honoured ONLY with --test-mode (a test has no dialog to type into)
MIN_PYTHON = (3, 9)
SETTINGS_FIELDS = ('artist_name', 'drive_folder', 'output_folder', 'budget_usd')
KIT_DIR = Path(__file__).resolve().parent.parent
HELPER_SRC = KIT_DIR / 'helper'
WIN_STARTUP_NAME = 'SnippyPieces.cmd'
START_WAIT_SECONDS = 5     # how long to wait for a freshly started helper to answer
START_POLL_SECONDS = 0.25
DRIVE_PROBE_SECONDS = 2   # a drive letter that does not answer in this long is skipped (dead network drive)
DRIVE_LIST_SECONDS = 5
DETACHED_PROCESS = 0x00000008  # Windows process creation flags, spelled out so this file also loads on a Mac
CREATE_NO_WINDOW = 0x08000000
DRIVE_HELP_WIN = ('Google Drive for desktop not found. Install it from https://www.google.com/drive/download/ , '
                  'sign in with the Google account your team uses, wait until "My Drive" appears (a drive letter such as G: in File '
                  'Explorer), then try again.')
DRIVE_HELP = ('Google Drive for desktop not found. Install it from https://www.google.com/drive/download/ , '
              'sign in with the Google account your team uses, wait for the Drive folder to appear in Finder, then try again.')


class SetupError(Exception):
    """A problem to say out loud; main() prints it and exits non-zero."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


class Ctx:
    """Where everything lives. A fake home re-roots every path; nothing else in the file hard-codes a location."""

    def __init__(self, home=None, test_mode=False):
        self.test_mode = test_mode
        self.fake_home = home is not None
        self.home = Path(os.path.expanduser(str(home))).resolve() if home else Path.home()

    def expand(self, value):
        """A path as typed, with a leading ~ meaning THIS context's home (the fake home in tests)."""
        text = str(value)
        if text == '~' or text.startswith('~/') or text.startswith('~\\'):
            return self.home / text[2:] if len(text) > 1 else self.home
        if text.startswith('~'):
            raise SetupError('Paths of the form ~name/... are not supported. Use a full path.')
        return Path(text)

    def absolute(self, value, what):
        path = self.expand(value)
        if not path.is_absolute():
            raise SetupError('%s must be a full path (or start with ~/), not a relative one: %s' % (what, value))
        return path

    @property
    def is_windows(self):
        return sys.platform == 'win32'

    @property
    def appdata(self):
        """Windows per-user %APPDATA% (Roaming). A fake home re-roots it under the fake home."""
        appdata = os.environ.get('APPDATA') if not self.fake_home else None
        return Path(appdata) if appdata else self.home / 'AppData' / 'Roaming'

    @property
    def config_dir(self):
        # Mirrors config_dir() in helper/daemon.py.
        if not self.fake_home and os.environ.get('SNIPPY_CONFIG_DIR'):
            return self.expand(os.environ['SNIPPY_CONFIG_DIR'])
        if sys.platform == 'win32':
            return self.appdata / 'SnippyPieces'
        if sys.platform == 'darwin':
            return self.home / 'Library' / 'Application Support' / 'SnippyPieces'
        return self.home / '.config' / 'snippy-pieces'

    @property
    def engine_dir(self):
        return self.config_dir / 'engine'

    @property
    def key_file(self):
        return self.config_dir / 'api-key'

    @property
    def settings_file(self):
        return self.config_dir / 'settings.json'

    # Mac path.
    @property
    def plist_file(self):
        return self.home / 'Library' / 'LaunchAgents' / (LOGIN_ITEM_LABEL + '.plist')

    # Windows path: the per-user Startup folder (shell:startup). Files here run at login, no admin rights needed.
    @property
    def startup_file(self):
        return (self.appdata / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup' / WIN_STARTUP_NAME)

    @property
    def login_item_file(self):
        return self.startup_file if self.is_windows else self.plist_file

    @property
    def local_appdata(self):
        local = os.environ.get('LOCALAPPDATA') if not self.fake_home else None
        return Path(local) if local else self.appdata.parent / 'Local'

    def claude_config_candidates(self):
        """Where the Claude app may keep its config, most usual first. Windows has two homes: the normal installer
        (%APPDATA%\\Claude) and the Microsoft Store version (%LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude)."""
        name = 'claude_desktop_config.json'
        if not self.is_windows:
            return [self.home / 'Library' / 'Application Support' / 'Claude' / name]
        found = [self.appdata / 'Claude' / name]
        packages = self.local_appdata / 'Packages'
        if packages.is_dir():
            found += [p / 'LocalCache' / 'Roaming' / 'Claude' / name for p in sorted(packages.glob('Claude_*'))]
        return found

    @property
    def claude_config(self):
        """The config file to use: an existing one (the newest if several), else the one whose Claude folder exists,
        else the usual path (which connect-claude then refuses, because the Claude app has never run). Never creates a folder."""
        candidates = self.claude_config_candidates()
        files = [c for c in candidates if c.is_file()]
        if files:
            return max(files, key=lambda c: c.stat().st_mtime)
        for c in candidates:
            if c.parent.is_dir():
                return c
        return candidates[0]

    @property
    def helper_dir(self):
        """Where the helper keeps its ledger and its daemon.pid (default data folder of helper/daemon.py)."""
        return self.config_dir / 'helper'

    def windows_drive_roots(self):
        """Drive letters that may hold Google Drive for desktop (stream mode shows My Drive on a letter like G:).
        A fake home looks in <home>/driveletters/<LETTER> instead, so tests never read real drives."""
        if self.fake_home:
            base = self.home / 'driveletters'
            return sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
        return [Path(letter + ':\\') for letter in string.ascii_uppercase[3:]]  # D: to Z: (A, B, C are never Drive)

    @property
    def cloud_storage(self):
        return self.home / 'Library' / 'CloudStorage'


def say(line=''):
    print(line)


def need_supported(ctx, what):
    # Mac (LaunchAgent) and Windows (Startup folder) are built. Anything else refuses outside test mode.
    if sys.platform not in ('darwin', 'win32') and not ctx.test_mode:
        raise SetupError('%s works on Mac and Windows only.' % what)


# ---------- small file helpers ----------
def ensure_config_dir(ctx):
    """Whichever command creates the config folder makes it private (700). A folder that already exists is left alone."""
    if ctx.config_dir.is_dir():
        return
    ctx.config_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform != 'win32':
        os.chmod(ctx.config_dir, 0o700)


def write_atomic(path, text, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp-%d' % os.getpid())
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(tmp), flags, mode if mode is not None else 0o644)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def backup(path):
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    target = path.with_name('%s.bak-%s' % (path.name, stamp))
    n = 2
    while target.exists():
        target = path.with_name('%s.bak-%s-%d' % (path.name, stamp, n))
        n += 1
    shutil.copy2(path, target)
    return target


def read_json_object(path, what):
    """An existing JSON object, or SetupError. Never guesses."""
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))  # utf-8-sig also accepts a BOM
    except (OSError, ValueError) as exc:  # ValueError includes a bad JSON text and a bad UTF-8 byte
        raise SetupError('%s could not be read (%s). Nothing was changed. Fix or move the file, then try again.'
                         % (what, type(exc).__name__))
    if not isinstance(data, dict):
        raise SetupError('%s is not a JSON object. Nothing was changed.' % what)
    return data


def read_settings(ctx):
    if not ctx.settings_file.is_file():
        return {}
    return read_json_object(ctx.settings_file, 'settings.json')


def writable_dir(path):
    return path.is_dir() and os.access(str(path), os.W_OK | os.X_OK)


# ---------- check ----------
def desired_server(ctx):
    return {'command': sys.executable, 'args': ['-B', str(ctx.engine_dir / 'mcp_server.py')]}


def gather_checks(ctx):
    """List of dicts: name, ok, detail, fix (the subcommand that fixes it)."""
    items = []

    def add(name, ok, detail, fix=''):
        items.append({'name': name, 'ok': bool(ok), 'detail': detail, 'fix': '' if ok else fix})

    py_ok = sys.version_info >= MIN_PYTHON
    add('Python', py_ok, '%d.%d.%d' % sys.version_info[:3] if py_ok else
        'Python %d.%d or newer is needed (found %d.%d)' % (MIN_PYTHON + sys.version_info[:2]), 'install Python 3 (Windows: python.org, tick Add to PATH)')
    kit_ok = all((HELPER_SRC / n).is_file() for n in ('daemon.py', 'mcp_server.py'))
    add('Kit files', kit_ok, 'helper/ found' if kit_ok else 'helper/daemon.py or mcp_server.py missing from the kit',
        're-download the kit')
    add('Config folder', ctx.config_dir.is_dir(), str(ctx.config_dir), 'install')
    engine_ok = all((ctx.engine_dir / n).is_file() for n in ('daemon.py', 'mcp_server.py'))
    add('Engine installed', engine_ok, str(ctx.engine_dir), 'install')

    # Key: present means env var or a non-empty file. The value is never read into the output.
    env_key = bool(os.environ.get(KEY_ENV, '').strip())
    file_key = ctx.key_file.is_file() and ctx.key_file.stat().st_size > 0
    if file_key and sys.platform != 'win32' and (ctx.key_file.stat().st_mode & 0o077):
        add('OpenAI key', False, 'key file is readable by other users (must be private)', 'key')
    elif env_key or file_key:
        add('OpenAI key', True, 'set (%s)' % ('environment' if env_key else 'private file'))
    else:
        add('OpenAI key', False, 'not set', 'key')

    try:
        settings = read_settings(ctx)
        problem = None
    except SetupError as exc:
        settings, problem = {}, str(exc)
    if problem:
        add('Settings file', False, problem, 'settings')
    else:
        for field in SETTINGS_FIELDS:
            value = settings.get(field)
            have = value not in (None, '')
            add('Setting: ' + field, have, 'set' if have else 'not set', 'settings')
        drive = settings.get('drive_folder')
        if drive:
            ok = writable_dir(ctx.expand(drive))
            add('Drive folder', ok, str(drive) if ok else '%s is missing or not writable' % drive,
                'settings (pick a Drive folder from drive-find)')
        else:
            add('Drive folder', False, 'not set', 'settings')

    item_ok = ctx.login_item_file.is_file()
    add('Login item (starts Snippy at login)', item_ok, str(ctx.login_item_file) if item_ok else 'not installed',
        'login-item')

    server_ok, detail = False, 'not connected'
    if ctx.claude_config.is_file():
        try:
            entry = (read_json_object(ctx.claude_config, 'Claude config').get('mcpServers') or {}).get(SERVER_NAME)
            if entry == desired_server(ctx):
                server_ok, detail = True, 'connected'
            elif entry:
                detail = 'a different %s entry is there (run connect-claude --replace)' % SERVER_NAME
        except SetupError as exc:
            detail = str(exc)
    add('Claude app knows Snippy', server_ok, detail, 'connect-claude')
    return items


def cmd_check(ctx, args):
    items = gather_checks(ctx)
    missing = [i for i in items if not i['ok']]
    if args.json:
        say(json.dumps({'ok': not missing, 'checks': items}, indent=2))
    else:
        for i in items:
            say('%s %s: %s' % ('[ok]  ' if i['ok'] else '[todo]', i['name'], i['detail']))
        say('All set.' if not missing else '%d thing(s) still to do.' % len(missing))
    return 0 if not missing else 1


# ---------- install ----------
def cmd_install(ctx, args):
    sources = sorted(p for p in HELPER_SRC.glob('*.py') if not p.name.startswith('test_'))
    if not sources:
        raise SetupError('No helper files found in %s. The kit is incomplete.' % HELPER_SRC)
    ensure_config_dir(ctx)
    ctx.engine_dir.mkdir(parents=True, exist_ok=True)
    for src in sources:
        dest = ctx.engine_dir / src.name
        if dest.is_file() and dest.read_bytes() == src.read_bytes():
            say('unchanged: %s' % dest.name)
        else:
            shutil.copy2(src, dest)
            say('copied: %s' % dest.name)
    say('Engine installed in %s' % ctx.engine_dir)
    return 0


# ---------- key ----------
def ask_key(ctx):
    """Returns (key, how). Never echoes."""
    if ctx.test_mode:
        value = os.environ.get(TEST_KEY_ENV, '')
        if not value:
            raise SetupError('Test mode: set %s to give the test key.' % TEST_KEY_ENV)
        return value, 'test input'
    if sys.platform == 'win32':
        value, how = ask_key_windows()
        if value is not None:
            return value, how
        # Nothing could show a private box: fall through to the terminal prompt below (only with a terminal attached).
    elif sys.platform == 'darwin':
        script = ('text returned of (display dialog "Paste your OpenAI key here. It stays on this Mac and is not shown." '
                  'default answer "" with hidden answer with title "Snippy setup" '
                  'buttons {"Cancel", "Save"} default button "Save")')
        cause = None
        try:
            result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=600)
            if result.returncode == 0:
                return result.stdout.strip(), 'private dialog (osascript)'
            if 'User canceled' in (result.stderr or ''):
                raise SetupError('Cancelled. No key was saved.')
            cause = 'osascript exited %d: %s' % (result.returncode, (result.stderr or '').strip()[:300])
        except subprocess.TimeoutExpired:
            cause = 'the key box was not answered within 10 minutes'
        except OSError as exc:
            cause = 'osascript could not start (%s)' % exc
        # Dialog unavailable: fall through to the terminal prompt only if a terminal is attached.
        say('Could not show the private key box: %s' % cause)
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass('Paste your OpenAI key (it will not show): ').strip(), 'terminal prompt (getpass)'
    raise SetupError('No private way to ask for the key here (no dialog, no terminal). Nothing was saved.')


WIN_CREDENTIAL_SCRIPT = (
    "$c = Get-Credential -UserName 'OpenAI key' -Message 'Paste your OpenAI key as the password. It is not shown.'; "
    "if ($null -eq $c) { exit 3 }; [Console]::Out.Write($c.GetNetworkCredential().Password)")


def tk_key_box():
    """Windows: a small always-on-top window with a masked entry (tkinter ships with python.org Python).
    Returns the text, or None when cancelled. Raises ImportError / tkinter.TclError when no window can be shown.
    The key stays in this process's memory only: never in argv, stdout or a log."""
    import tkinter
    root = tkinter.Tk()
    root.title('Snippy setup')
    root.attributes('-topmost', True)
    result = {'value': None}
    tkinter.Label(root, text='Paste your OpenAI key here. It is not shown and stays on this computer.',
                  padx=16, pady=10).pack()
    entry = tkinter.Entry(root, show='*', width=48)
    entry.pack(padx=16)

    def save(_event=None):
        result['value'] = entry.get()
        root.destroy()

    row = tkinter.Frame(root)
    row.pack(pady=12)
    tkinter.Button(row, text='Save', width=10, command=save).pack(side='left', padx=6)
    tkinter.Button(row, text='Cancel', width=10, command=root.destroy).pack(side='left', padx=6)
    root.bind('<Return>', save)
    root.bind('<Escape>', lambda _e: root.destroy())
    entry.focus_force()
    root.mainloop()
    return result['value']


def ask_key_windows():
    """Private key box on Windows. First a tkinter password box; if that cannot open, the PowerShell Get-Credential
    dialog (stock Windows 10/11). Returns (key, which box), or (None, None) when neither could be shown (the cause is printed).
    Cancelling either box raises SetupError: no key saved, no second box."""
    try:
        value = tk_key_box()
        if value is None:
            raise SetupError('Cancelled. No key was saved.')
        return value.strip(), 'masked Python window (tkinter)'
    except ImportError as exc:
        say('Could not show the Python key box: %s' % exc)
    except Exception as exc:  # tkinter.TclError (no display) and anything the toolkit throws; the cause is shown
        if isinstance(exc, SetupError):
            raise
        say('Could not show the Python key box: %s: %s' % (type(exc).__name__, exc))
    cause = None
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', WIN_CREDENTIAL_SCRIPT],
                                capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            return result.stdout.strip(), 'PowerShell password box (Get-Credential)'
        if result.returncode == 3:
            raise SetupError('Cancelled. No key was saved.')
        cause = 'powershell exited %d: %s' % (result.returncode, (result.stderr or '').strip()[:300])
    except subprocess.TimeoutExpired:
        cause = 'the key box was not answered within 10 minutes'
    except OSError as exc:
        cause = 'powershell could not start (%s)' % exc
    say('Could not show the private key box: %s' % cause)
    return None, None


def check_key_online(key):
    """FREE call: list models. Returns None when fine, else a plain message and exit code."""
    request = urllib.request.Request('https://api.openai.com/v1/models', headers={'Authorization': 'Bearer ' + key})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read(1)
        return None
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return ('OpenAI says this key is not valid (401). Check you copied the whole key, or make a new one at '
                    'https://platform.openai.com/api-keys', 2)
        return ('OpenAI answered with an error (%d) while checking the key. Try again in a minute.' % exc.code, 3)
    except (urllib.error.URLError, OSError, TimeoutError):
        return ('Could not reach OpenAI to check the key. Check your internet connection.', 3)


def cmd_key(ctx, args):
    key, how = ask_key(ctx)
    if not key.startswith('sk-') or any(c.isspace() for c in key) or len(key) < 20:
        raise SetupError('That does not look like an OpenAI key (it should start with sk- and have no spaces). '
                         'Nothing was saved.')
    say('Asked for the key with: %s' % how)
    # Safest order: a key OpenAI rejects never replaces a working one. With no network it is saved, unverified.
    problem = None
    if args.no_network or ctx.test_mode:
        say('Skipped the online check (no-network).')
    else:
        problem = check_key_online(key)
        if problem and problem[1] == 2:
            raise SetupError(problem[0] + ' Nothing was saved.', 2)
    ensure_config_dir(ctx)
    write_atomic(ctx.key_file, key, 0o600)
    if ctx.is_windows:
        # Windows has no 600 mode bits: the file is private because it sits in this account's own %APPDATA% folder.
        say('Key saved privately (%s, in your own Windows account folder).' % ctx.key_file)
    else:
        say('Key saved privately (%s, mode 600).' % ctx.key_file)
    if problem:
        raise SetupError(problem[0] + ' The key was saved but is not checked yet. Run the key step again later.',
                         problem[1])
    if not (args.no_network or ctx.test_mode):
        say('OpenAI accepted the key (free check, nothing was charged).')
    return 0


# ---------- settings / drive-find ----------
def clean_artist(name):
    name = (name or '').strip()
    if not name or any(c in name for c in '/\\:*?"<>|') or name in ('.', '..'):
        raise SetupError('Artist name must be a plain name (letters, numbers, spaces), no slashes or symbols.')
    return name


def cmd_settings(ctx, args):
    if all(v is None for v in (args.artist, args.drive_folder, args.output_folder, args.budget)):
        raise SetupError('Give at least one of --artist, --drive-folder, --output-folder, --budget.')
    settings = read_settings(ctx)
    if args.artist is not None:
        settings['artist_name'] = clean_artist(args.artist)
    if args.drive_folder is not None:
        drive = ctx.absolute(args.drive_folder, 'Drive folder')
        if not writable_dir(drive):
            raise SetupError('Drive folder %s does not exist or is not writable. Pick one from drive-find.' % drive)
        settings['drive_folder'] = str(drive)
    if args.output_folder is not None:
        out = ctx.absolute(args.output_folder, 'Output folder')
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SetupError('Could not create the output folder %s (%s).' % (out, type(exc).__name__))
        if not writable_dir(out):
            raise SetupError('Output folder %s is not writable.' % out)
        settings['output_folder'] = str(out)
    if args.budget is not None:
        try:
            amount = Decimal(str(args.budget))
        except InvalidOperation:
            amount = None
        if amount is None or not amount.is_finite() or amount <= 0:
            raise SetupError('Budget must be a positive number of US dollars, for example 20.')
        settings['budget_usd'] = float(amount)
    ensure_config_dir(ctx)
    write_atomic(ctx.settings_file, json.dumps(settings, indent=2, sort_keys=True) + '\n', 0o600)
    say('Settings saved in %s' % ctx.settings_file)
    for field in SETTINGS_FIELDS:
        if settings.get(field) not in (None, ''):
            say('  %s: %s' % (field, settings[field]))
    return 0


def _has_drive_dirs(root):
    try:
        return (root / 'My Drive').is_dir() or (root / 'Shared drives').is_dir()
    except OSError:  # an unplugged or disconnected network drive can refuse to answer: not a Drive
        return False


def _within(seconds, func, *args):
    """Run func(*args) in a helper thread. Returns (answered, value, error). A call that does not finish in time is
    left behind (daemon thread) and reported as not answered, so one dead network drive cannot stall setup."""
    box = {}

    def work():
        try:
            box['value'] = func(*args)
        except Exception as exc:  # handed back to the caller, which says it in plain words
            box['error'] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        return False, None, None
    return True, box.get('value'), box.get('error')


def _list_root(root, only_drive_dirs):
    found = []
    label_root = root.name or str(root)
    for child in sorted(root.iterdir()):
        if child.name.startswith('.') or not child.is_dir():
            continue
        if only_drive_dirs and child.name not in ('My Drive', 'Shared drives'):
            continue  # a drive letter also holds system folders; only the Drive ones count
        if child.name == 'Shared drives':
            for shared in sorted(child.iterdir()):
                if shared.is_dir() and not shared.name.startswith('.'):
                    found.append({'label': 'Shared drive: %s (%s)' % (shared.name, label_root), 'path': str(shared)})
        else:
            found.append({'label': '%s (%s)' % (child.name, label_root), 'path': str(child)})
    return found


def find_drives(ctx):
    """Likely Google Drive for desktop folders (Mac CloudStorage, Windows drive letters or mirrored folder).
    Returns [{'label', 'path'}]. Anything skipped (a drive that did not answer, a folder that could not be read) is
    added to ctx.drive_notes in plain words."""
    ctx.drive_notes = []
    found = []
    letter_roots = []
    roots = sorted(ctx.cloud_storage.glob('GoogleDrive-*')) if ctx.cloud_storage.is_dir() else []
    legacy = ctx.home / 'Google Drive'
    if legacy.is_dir():
        roots.append(legacy)
    if ctx.is_windows:
        # Stream mode: a drive letter (G:\) holding "My Drive" and "Shared drives". Mirror mode: a folder in the profile.
        for root in ctx.windows_drive_roots():
            answered, has, _ = _within(DRIVE_PROBE_SECONDS, _has_drive_dirs, root)
            if not answered:
                ctx.drive_notes.append('Skipped drive %s: it did not answer within %d seconds.'
                                       % (root.name or str(root), DRIVE_PROBE_SECONDS))
            elif has:
                letter_roots.append(root)
        roots = letter_roots + roots
        mirrored = ctx.home / 'My Drive'
        if mirrored.is_dir():
            found.append({'label': 'My Drive (mirrored in your user folder)', 'path': str(mirrored)})
    for root in roots:
        answered, items, error = _within(DRIVE_LIST_SECONDS, _list_root, root, root in letter_roots)
        name = root.name or str(root)
        if not answered:
            ctx.drive_notes.append('Skipped %s: it did not answer within %d seconds.' % (name, DRIVE_LIST_SECONDS))
        elif error is not None:
            ctx.drive_notes.append('Could not read %s (%s). Skipped it.' % (name, type(error).__name__))
        else:
            found += items
    return found


def cmd_drive_find(ctx, args):
    found = find_drives(ctx)
    for note in ctx.drive_notes:  # stderr, so --json output stays clean
        print(note, file=sys.stderr)
    if not found:
        raise SetupError(DRIVE_HELP_WIN if ctx.is_windows else DRIVE_HELP)
    if args.json:
        say(json.dumps(found, indent=2))
    else:
        for n, item in enumerate(found, 1):
            say('%d. %s\n   %s' % (n, item['label'], item['path']))
    return 0


# ---------- login item (Mac: LaunchAgent; Windows: Startup folder) ----------
def launchctl(ctx, *argv):
    """Only ever called outside test mode."""
    if ctx.test_mode:
        raise AssertionError('launchctl must not run in test mode')
    return subprocess.run(['launchctl'] + list(argv), capture_output=True, text=True)


def plist_data(ctx):
    logs = ctx.config_dir / 'logs'
    return {
        'Label': LOGIN_ITEM_LABEL,
        'ProgramArguments': [sys.executable, '-B', str(ctx.engine_dir / 'daemon.py')],
        'WorkingDirectory': str(ctx.engine_dir),
        'RunAtLoad': True,
        'KeepAlive': True,
        'StandardOutPath': str(logs / 'daemon.out.log'),
        'StandardErrorPath': str(logs / 'daemon.err.log'),
    }


def pythonw_path():
    """pythonw.exe sits next to python.exe; it runs a script with no console window."""
    path = Path(sys.executable).with_name('pythonw.exe')
    if not path.is_file():
        raise SetupError('pythonw.exe was not found next to %s. Reinstall Python from https://www.python.org/downloads/ '
                         '(tick "Add python.exe to PATH") and try again.' % sys.executable)
    return path


def env_form(ctx, path):
    """A path for the Startup script. If it sits under %APPDATA%, %LOCALAPPDATA% or %USERPROFILE%, it is written with
    that variable, so a user name with accents or other non-ASCII letters never has to be written into the script
    (cmd.exe reads script files in the OEM code page, not UTF-8). Anything left must be plain ASCII or it is refused."""
    bases = [('APPDATA', ctx.appdata), ('LOCALAPPDATA', ctx.local_appdata), ('USERPROFILE', ctx.home)]
    best = None
    for name, base in bases:
        try:
            rel = path.relative_to(base)
        except ValueError:
            continue
        if best is None or len(str(base)) > len(str(best[1])):
            best = (name, base, rel)
    text = str(path)
    if best:
        text = '%' + best[0] + '%' + ''.join('\\' + part for part in best[2].parts)
    if not text.isascii():
        raise SetupError('A folder name here has letters Windows startup scripts cannot hold safely (%s) and it is not '
                         'under your user folder. Nothing was written.' % path)
    return text


def startup_cmd_text(ctx):
    """The Startup-folder script, plain ASCII with CRLF line ends. start "" runs pythonw detached with no console
    window of its own; the small cmd window that runs this script closes at once (a .cmd always flashes for a moment;
    a .vbs would hide that but Windows Script Host is switched off on some PCs and is being removed, so .cmd is the
    more robust choice)."""
    parts = [pythonw_path(), ctx.engine_dir, ctx.engine_dir / 'daemon.py']
    for part in parts:
        if '"' in str(part) or '%' in str(part) or '\n' in str(part):
            raise SetupError('A folder name here has a quote or a percent sign, which a Windows startup script cannot '
                             'hold safely: %s' % part)
    exe, engine, daemon = [env_form(ctx, part) for part in parts]
    return ('@echo off\r\n'
            'rem Starts the Snippy Pieces helper at login (no console window). Written by Snippy setup.\r\n'
            'rem To remove it: ask Claude to remove Snippy, or delete this file.\r\n'
            'start "" /D "%s" "%s" -B "%s"\r\n' % (engine, exe, daemon))


def windows_tool(ctx, *argv):
    """Only ever called outside test mode (powershell / taskkill)."""
    if ctx.test_mode:
        raise AssertionError('windows tools must not run in test mode')
    return subprocess.run(list(argv), capture_output=True, text=True, encoding='utf-8')


# Prints the command line of one process id, as UTF-8 (so accents survive); prints nothing when there is no such process.
WIN_COMMANDLINE_SCRIPT = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                          "$p = Get-CimInstance Win32_Process -Filter 'ProcessId = %s'; "
                          "if ($null -ne $p) { [Console]::Out.Write($p.CommandLine) }")


def stop_helper_windows(ctx):
    """Stop a running helper (found through its daemon.pid) before removing or replacing files. Returns True when
    a process was stopped. Only a process whose command line holds THIS kit's engine daemon.py is ever stopped: a stale
    pid that now belongs to some other program is refused loudly and nothing is touched. No such process = stale file, deleted."""
    pid_file = ctx.helper_dir / 'daemon.pid'
    if not pid_file.is_file():
        return False
    text = pid_file.read_text(encoding='utf-8').strip()
    if not text.isdigit():
        raise SetupError('The helper pid file %s does not hold a number. Nothing was removed; delete that file and '
                         'try again.' % pid_file)
    listing = windows_tool(ctx, 'powershell.exe', '-NoProfile', '-Command', WIN_COMMANDLINE_SCRIPT % text)
    if listing.returncode != 0:
        raise SetupError('Could not ask Windows about the Snippy helper (powershell said: %s). Nothing was removed.'
                         % ((listing.stderr or '').strip()[:300] or 'no message'))
    command_line = (listing.stdout or '').strip()
    if not command_line:
        pid_file.unlink()  # no process with that number any more: the pid file was left over
        return False
    wanted = str(ctx.engine_dir / 'daemon.py').replace('/', '\\').lower()
    if wanted not in command_line.replace('/', '\\').lower():
        raise SetupError('Process %s is not the Snippy helper (its command line is not %s), so it was not stopped. '
                         'Nothing was removed. If the helper is not running, delete %s and try again.'
                         % (text, ctx.engine_dir / 'daemon.py', pid_file))
    result = windows_tool(ctx, 'taskkill', '/PID', text, '/F')
    if result.returncode != 0:
        raise SetupError('Could not stop the Snippy helper (taskkill said: %s). Nothing was removed.'
                         % ((result.stderr or result.stdout or '').strip()[:300] or 'no message'))
    pid_file.unlink()
    return True


def helper_port():
    """The port the helper listens on, asked of the helper's own code (default and environment override live there)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('snippy_daemon_port', HELPER_SRC / 'daemon.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.helper_port()


def helper_answers(port):
    """True when a Snippy helper answers on 127.0.0.1:port. Any reply from it counts (a plain GET gets a 405 from the
    helper; no key or token is sent). It must identify itself as snippy-helper, so some other program on that port does not pass."""
    try:
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
        try:
            connection.request('GET', '/')
            return 'snippy-helper' in (connection.getresponse().getheader('Server') or '')
        finally:
            connection.close()
    except (OSError, http.client.HTTPException):
        return False


def log_tail(paths, lines=8):
    out = []
    for path in paths:
        try:
            text = Path(path).read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        out += [line for line in text.splitlines() if line.strip()][-lines:]
    return '\n'.join(out[-lines:]) if out else '(the log is empty)'


def wait_for_helper(ctx, process, logs):
    """Wait up to START_WAIT_SECONDS for the helper to be alive (process still running, when we hold it) AND answering
    on its port. Only then return. Otherwise raise a loud SetupError with the end of the log and its path."""
    port = helper_port()
    deadline = time.monotonic() + START_WAIT_SECONDS
    while True:
        alive = process is None or process.poll() is None
        if alive and helper_answers(port):
            return
        if not alive or time.monotonic() >= deadline:
            break
        time.sleep(START_POLL_SECONDS)
    why = 'it stopped right away' if process is not None and process.poll() is not None else \
        'it did not answer on port %d within %d seconds' % (port, START_WAIT_SECONDS)
    raise SetupError('The Snippy helper did not start: %s. Last lines of its log (%s):\n%s'
                     % (why, ', '.join(str(p) for p in logs), log_tail(logs)))


def cmd_login_item(ctx, args):
    need_supported(ctx, 'The login item')
    if ctx.is_windows:
        return login_item_windows(ctx, args)
    domain = 'gui/%d' % os.getuid()
    if args.remove:
        if not ctx.test_mode:
            result = launchctl(ctx, 'bootout', '%s/%s' % (domain, LOGIN_ITEM_LABEL))
            text = ((result.stderr or '') + (result.stdout or '')).lower()
            not_loaded = result.returncode in (3, 113) or 'no such process' in text or 'could not find' in text
            if result.returncode != 0 and not not_loaded:
                raise SetupError('Could not stop the Snippy helper (launchctl said: %s). The login item file was kept '
                                 'and nothing was removed.' % ((result.stderr or '').strip()[:300] or 'no message'))
        if ctx.plist_file.is_file():
            ctx.plist_file.unlink()
            say('Login item removed.')
        else:
            say('Login item was not installed.')
        return 0
    if not (ctx.engine_dir / 'daemon.py').is_file():
        raise SetupError('The engine is not installed yet. Run install first.')
    ensure_config_dir(ctx)
    (ctx.config_dir / 'logs').mkdir(parents=True, exist_ok=True)
    ctx.plist_file.parent.mkdir(parents=True, exist_ok=True)
    with open(ctx.plist_file, 'wb') as handle:
        plistlib.dump(plist_data(ctx), handle)
    say('Login item written: %s' % ctx.plist_file)
    if ctx.test_mode:
        say('Test mode: not loaded with launchctl.')
        return 0
    launchctl(ctx, 'bootout', '%s/%s' % (domain, LOGIN_ITEM_LABEL))  # reload cleanly; ignore "not loaded"
    result = launchctl(ctx, 'bootstrap', domain, str(ctx.plist_file))
    if result.returncode != 0:
        raise SetupError('launchctl could not load the login item: %s' % (result.stderr.strip() or 'unknown error'))
    wait_for_helper(ctx, None, [ctx.config_dir / 'logs' / 'daemon.err.log', ctx.config_dir / 'logs' / 'daemon.out.log'])
    say('Snippy helper is running and will start at every login.')
    return 0


def login_item_windows(ctx, args):
    """A .cmd in the user's own Startup folder that starts the helper with pythonw. No admin rights, no Task Scheduler."""
    item = ctx.startup_file
    if args.remove:
        if not ctx.test_mode and stop_helper_windows(ctx):
            say('Snippy helper stopped.')
        if item.is_file():
            item.unlink()
            say('Login item removed.')
        else:
            say('Login item was not installed.')
        return 0
    if not (ctx.engine_dir / 'daemon.py').is_file():
        raise SetupError('The engine is not installed yet. Run install first.')
    text = startup_cmd_text(ctx)  # may refuse: nothing is written then
    ensure_config_dir(ctx)
    (ctx.config_dir / 'logs').mkdir(parents=True, exist_ok=True)
    item.parent.mkdir(parents=True, exist_ok=True)
    with open(item, 'w', newline='', encoding='ascii') as handle:  # ASCII only (checked above); newline='' keeps CRLF
        handle.write(text)
    say('Login item written: %s' % item)
    if ctx.test_mode:
        say('Test mode: helper not started.')
        return 0
    if stop_helper_windows(ctx):  # reload cleanly, like the Mac path
        say('Stopped the old helper.')
    log_path = ctx.config_dir / 'logs' / 'daemon.out.log'
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')  # the helper's output is written as UTF-8
    with open(log_path, 'ab') as log:  # the child inherits this handle; the parent's copy closes here
        process = subprocess.Popen([str(pythonw_path()), '-B', str(ctx.engine_dir / 'daemon.py')],
                                   cwd=str(ctx.engine_dir), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW, close_fds=True, env=env)
    wait_for_helper(ctx, process, [log_path])
    say('Snippy helper started and will start at every login.')
    return 0


# ---------- connect-claude (config path differs per system) ----------
def cmd_connect_claude(ctx, args):
    need_supported(ctx, 'Connecting the Claude app')
    if not (ctx.engine_dir / 'mcp_server.py').is_file():
        raise SetupError('The engine is not installed yet. Run install first.')
    path = ctx.claude_config
    wanted = desired_server(ctx)
    if not path.is_file() and not path.parent.is_dir():
        raise SetupError('The Claude app has not made its settings folder yet (looked for %s). Open the Claude app '
                         'once, then try again. Nothing was changed.' % path.parent)
    if path.is_file():
        data = read_json_object(path, 'Claude config (%s)' % path)
        servers = data.get('mcpServers', {})
        if not isinstance(servers, dict):
            raise SetupError('Claude config has an mcpServers that is not an object. Nothing was changed.')
        existing = servers.get(SERVER_NAME)
        if existing == wanted:
            say('Claude is already connected to Snippy. Nothing to change.')
            return 0
        if existing is not None and not args.replace:
            raise SetupError('A different %s entry already exists in the Claude config. Nothing was changed. '
                             'Run again with --replace if it is yours to replace.' % SERVER_NAME)
        say('Backup: %s' % backup(path))
        servers[SERVER_NAME] = wanted
        data['mcpServers'] = servers
    else:
        data = {'mcpServers': {SERVER_NAME: wanted}}
    write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    say('Snippy added to the Claude app config (%s).' % path)
    say('Now restart the Claude app (quit it fully and open it again).')
    return 0


# ---------- uninstall ----------
def plan_claude_removal(ctx):
    """Read-only look at the Claude config. Returns the parsed data when the entry must be removed, else None.
    Raises SetupError (changing nothing) when the file cannot be trusted."""
    path = ctx.claude_config
    if not path.is_file():
        return None
    data = read_json_object(path, 'Claude config (%s)' % path)
    servers = data.get('mcpServers')
    if servers is not None and not isinstance(servers, dict):
        raise SetupError('Claude config has an mcpServers that is not an object. Nothing was changed.')
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return None
    entry = servers[SERVER_NAME]
    mine = isinstance(entry, dict) and entry.get('args') == ['-B', str(ctx.engine_dir / 'mcp_server.py')]
    if not mine:
        say('The %s entry in the Claude config is not the one this setup wrote, so it was left alone.' % SERVER_NAME)
        return None
    return data


def cmd_uninstall(ctx, args):
    need_supported(ctx, 'Uninstalling')
    # Check everything first; change nothing if any check fails.
    data = plan_claude_removal(ctx)
    if args.all and ctx.config_dir.name not in ('SnippyPieces', 'snippy-pieces'):
        raise SetupError('Refusing to delete %s: it is not the Snippy folder. Nothing was changed.' % ctx.config_dir)
    removed = []
    cmd_login_item(ctx, argparse.Namespace(remove=True))
    if data is not None:
        say('Backup: %s' % backup(ctx.claude_config))
        del data['mcpServers'][SERVER_NAME]
        write_atomic(ctx.claude_config, json.dumps(data, indent=2, ensure_ascii=False) + '\n')
        removed.append('Claude app entry')
        say('Snippy removed from the Claude app config. Restart the Claude app.')
    else:
        say('No %s entry in the Claude config: nothing to remove there.' % SERVER_NAME)
    if ctx.engine_dir.is_dir():
        shutil.rmtree(ctx.engine_dir)
        removed.append('engine')
        say('Engine removed.')
    if args.all:
        if ctx.config_dir.is_dir():
            shutil.rmtree(ctx.config_dir)
        removed.append('key, settings and helper records')
        say('Removed the key, settings and helper records (%s).' % ctx.config_dir)
        say('Your finished pieces in the output and Drive folders were not touched.')
    else:
        say('Kept your key, settings and pieces. Add --all to remove the key, settings and helper records too.')
    return 0


# ---------- main ----------
def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--home', default=argparse.SUPPRESS, help='re-root every path under this folder (tests)')
    common.add_argument('--test-mode', action='store_true', default=argparse.SUPPRESS,
                        help='never call launchctl, osascript, powershell, taskkill or the network')
    parser = argparse.ArgumentParser(description='Snippy Pieces guided setup (Mac and Windows).', parents=[common])
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('check', parents=[common], help='show what is set up and what is missing')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_check)
    p = sub.add_parser('install', parents=[common], help='copy the helper into the config folder')
    p.set_defaults(func=cmd_install)
    p = sub.add_parser('key', parents=[common], help='ask for the OpenAI key in a private box and save it')
    p.add_argument('--no-network', action='store_true', help='skip the free online check')
    p.set_defaults(func=cmd_key)
    p = sub.add_parser('settings', parents=[common], help='save name, folders and budget')
    p.add_argument('--artist')
    p.add_argument('--drive-folder')
    p.add_argument('--output-folder')
    p.add_argument('--budget')
    p.set_defaults(func=cmd_settings)
    p = sub.add_parser('drive-find', parents=[common], help='list Google Drive folders on this computer')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_drive_find)
    p = sub.add_parser('login-item', parents=[common], help='start the helper at login (Mac or Windows)')
    p.add_argument('--remove', action='store_true')
    p.set_defaults(func=cmd_login_item)
    p = sub.add_parser('connect-claude', parents=[common], help='add Snippy to the Claude app')
    p.add_argument('--replace', action='store_true', help='replace a different snippy-engine entry')
    p.set_defaults(func=cmd_connect_claude)
    p = sub.add_parser('uninstall', parents=[common], help='remove login item and Claude entry')
    p.add_argument('--all', action='store_true', help='also remove key, settings and helper records')
    p.set_defaults(func=cmd_uninstall)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    home = getattr(args, 'home', None) or os.environ.get('SNIPPY_SETUP_HOME') or None
    ctx = Ctx(home, getattr(args, 'test_mode', False))
    try:
        return args.func(ctx, args)
    except SetupError as exc:
        print('PROBLEM: %s' % exc, file=sys.stderr)
        return exc.code


if __name__ == '__main__':
    sys.exit(main())
