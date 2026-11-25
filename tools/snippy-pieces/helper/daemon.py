#!/usr/bin/env python3
"""Snippy helper: a local service that runs approved paid cutting jobs, one at a time.

Bound to 127.0.0.1 only. Python 3 standard library only.
Money logic follows work/extraction_queue.py (copied, not imported):
reserve before the call, settle on the reported cost, hold anything ambiguous,
and never send a paid request again on its own.
"""
import base64
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import ssl
import sys
import threading
from urllib.parse import urlsplit
import zlib

try:  # Unix file locking; Windows uses msvcrt below.
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

HOST = '127.0.0.1'
DEFAULT_PORT = 18830  # the kit's own port; 18810 is the owner's Snippy helper
# Ports that belong to other work on this Mac.
BLOCKED_PORTS = {8765, 8791, 8792, 18791, 18792, 18793, 18801}
DEFAULT_IMAGE_URL = 'https://api.openai.com/v1/images/edits'
# Neutral label for a future login item. Constant only: this kit never installs or loads one.
LOGIN_ITEM_LABEL = 'com.snippypieces.helper'
NO_KEY_MESSAGE = 'No OpenAI key set up. Run the Snippy setup.'
KEY_ENV = 'SNIPPY_OPENAI_KEY'


def config_dir():
    """Per-user config folder: macOS ~/Library/Application Support/SnippyPieces, Windows %APPDATA%\\SnippyPieces,
    Linux ~/.config/snippy-pieces. SNIPPY_CONFIG_DIR overrides it (used by tests)."""
    override = os.environ.get('SNIPPY_CONFIG_DIR')
    if override:
        return Path(override).expanduser()
    if sys.platform == 'win32':
        appdata = os.environ.get('APPDATA')
        base = Path(appdata) if appdata else Path.home() / 'AppData' / 'Roaming'
        return base / 'SnippyPieces'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'SnippyPieces'
    return Path.home() / '.config' / 'snippy-pieces'


# The key file lives in the per-user config folder as `api-key`.
def default_key_file():
    return config_dir() / 'api-key'


def default_data_dir():
    return config_dir() / 'helper'


# Where source pictures are looked for (see ORIGINAL_ID below for their names).
def default_originals():
    return config_dir() / 'originals'


SETTINGS_FIELDS = ('drive_folder', 'output_folder', 'budget_usd', 'artist_name')


def read_settings():
    """settings.json from the config folder, read fresh each time. Returns (settings dict, problem or None).
    A missing or broken file is reported, never hidden."""
    path = config_dir() / 'settings.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}, 'settings.json not found. Run the Snippy setup.'
    except (OSError, ValueError):
        return {}, 'settings.json could not be read. Run the Snippy setup.'
    if not isinstance(data, dict):
        return {}, 'settings.json could not be read. Run the Snippy setup.'
    return {k: data.get(k) for k in SETTINGS_FIELDS}, None


UNTITLED_PICTURE = 'Untitled picture'  # Drive subfolder when a page did not send the picture's name


def clean_part(text, fallback):
    """One safe file or folder name: no separators, no characters Windows refuses."""
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(text or '')).strip(' .')
    text = text[:100] or fallback
    # Windows refuses these device names as a file or folder name, with or without an extension: a trailing _ makes them safe.
    if re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?', text):
        text += '_'
    return text


def unique_copy(data, folder, filename):
    """Write data into folder under filename, never overwriting: a taken name gets -2, -3 ... before the extension."""
    folder.mkdir(parents=True, exist_ok=True)
    stem, suffix = os.path.splitext(filename)
    number = 1
    while True:
        target = folder / (filename if number == 1 else '%s-%d%s' % (stem, number, suffix))
        try:
            with open(target, 'xb') as handle:
                handle.write(data)
            return target
        except FileExistsError:
            number += 1
        except BaseException:
            try:
                target.unlink()
            except OSError:
                pass
            raise


def private_enough(path):
    """Unix: the file must not be open to group or others. Windows has no such mode bits, so the check is skipped there."""
    return sys.platform == 'win32' or not path.stat().st_mode & 0o077


class MissingKey(Exception):
    pass


class LockUnavailable(Exception):
    """This Python has neither fcntl nor msvcrt, so the one-helper lock cannot be taken."""


def lock_exclusive(handle):
    """Take the one-helper lock without waiting. Raises OSError when another helper holds it, and LockUnavailable
    (never a silent skip) when this system offers no file locking at all."""
    if fcntl:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif msvcrt:
        handle.seek(0)  # msvcrt locks from the current position; always lock byte 0
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        raise LockUnavailable('no file locking (fcntl or msvcrt) is available in this Python')


def read_key(path):
    """The env var SNIPPY_OPENAI_KEY wins, else the api-key file. Missing means MissingKey, never a silent fallback."""
    from_env = os.environ.get(KEY_ENV, '').strip()
    if from_env:
        if not from_env.startswith('sk-') or any(c.isspace() for c in from_env):
            raise PermissionError('key env')
        return from_env
    if not Path(path).expanduser().is_file():
        raise MissingKey()
    return load_key(path)


# Source pictures already on this Mac, named <id>.<ext> where id is the first 32 hex of the sha256 of the file bytes (as pack.py makes them).
ORIGINAL_MIME = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'webp': 'image/webp'}
ORIGINAL_ID = re.compile(r'[0-9a-f]{32}')
MODEL = 'gpt-image-2.5-sunburst-2026-09-08'
# A typical piece costs about 6 cents; the reserve also grows to 1.5x the dearest cost seen. Override with SNIPPY_RESERVE_USD.
DEFAULT_RESERVE_USD = '0.07'
DEFAULT_TIMEOUT_SECONDS = 300
CHUNK_CHARS = 256 * 1024  # base64 characters per get_result reply; a multiple of 4
MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_BATCH_JOBS = 500
MAX_REPLY_BYTES = 64 * 1024 * 1024
MAX_PIXEL_BYTES = 256 * 1024 * 1024
MAX_RESOLVED_USD = Decimal('1000')  # Far above any one image; catches a mistyped amount.

ACTIVE = ('accepted', 'sending')
FAILED_STATES = ('failed', 'needs_check', 'cancelled')
# Returned pieces were never sent, so a later attempt may follow them.
RETRYABLE_STATES = FAILED_STATES + ('returned',)
QUEUE_STATES = ('accepted', 'sending', 'done', 'failed', 'needs_check', 'cancelled', 'returned')
# The service refused these outright, so the request cost nothing.
NOT_CHARGED = {'401', '403', '429', 'insufficient_quota'}
# These stop the whole engine until a fresh price approval (Resume).
PAUSE_KINDS = {'401', '403', '429', '500', '503', 'insufficient_quota', 'service_error',
               'key_problem', 'no_key', 'needs_check', 'over_reserve', 'storage_error'}
OUT_OF_CREDIT_CODES = {'insufficient_quota', 'credit_balance_exhausted', 'billing_hard_limit_reached'}

RETRY_STEP = 'Press Retry on this piece and approve its price. Finished pieces are kept.'
RESUME_STEP = 'Press Resume and approve the price to carry on. Finished pieces are kept.'
REASONS = {
    'bad_gateway': ('The image service dropped this piece (bad gateway). It may have been charged.', RETRY_STEP),
    'garbage': ('The image service sent back something that is not a usable PNG. It may have been charged.', RETRY_STEP),
    'no_transparency': ('The image came back with no transparent background.', RETRY_STEP),
    'empty': ('The image service sent back an empty reply. It may have been charged.', RETRY_STEP),
    'timeout': ('This piece took too long and was stopped. It may have been charged.', RETRY_STEP),
    'dropped': ('The connection dropped while this piece was running. It may have been charged.', RETRY_STEP),
    'bad_request': ('The image service refused this piece. It may have been charged.',
                    'Change the note or outline for this piece, then press Retry and approve its price.'),
    'cancelled': ('You cancelled this piece. A request already sent may still be charged.', RETRY_STEP),
    '401': ('OpenAI refused the API key. Nothing was charged.', 'Replace the API key, then press Resume and approve the price.'),
    '403': ('OpenAI says this key may not use the image model. Nothing was charged.',
            'Check the key\'s model access, then press Resume and approve the price.'),
    '429': ('OpenAI is rate limiting this key. Nothing was charged.', 'Wait a minute, then press Resume and approve the price.'),
    'insufficient_quota': ('The OpenAI credit has run out. Nothing was charged.',
                           'Add credit in OpenAI billing, then press Resume and approve the price.'),
    '500': ('OpenAI had a server error. This piece may have been charged, so it is held.',
            'Check OpenAI billing, then press Resume and approve the price. Retry this piece separately.'),
    '503': ('OpenAI is temporarily unavailable. This piece may have been charged, so it is held.',
            'Wait a few minutes, then press Resume and approve the price. Retry this piece separately.'),
    'service_error': ('The image service gave an unexpected answer. This piece may have been charged, so it is held.',
                      'Check OpenAI billing, then press Resume and approve the price.'),
    'no_key': (NO_KEY_MESSAGE + ' Nothing was sent.', 'Run the Snippy setup, then press Resume and approve the price.'),
    'over_budget': ('Your Snippy spending limit is used up (this piece, plus what is already spent and set aside, would pass it), so nothing was sent.',
                    'Ask Claude to raise your Snippy spending limit, then approve the price again.'),
    'budget_not_set': ('No Snippy spending limit is set up, so nothing was sent.',
                       'Ask Claude to set your Snippy spending limit, then approve the price again.'),
    'key_problem': ('The API key file is missing, unreadable or not private. Nothing was sent.',
                    'Fix the saved API key, then press Resume and approve the price.'),
    'needs_check': ('The helper stopped during a paid request, so this piece may have been charged. It will not be sent again.',
                    'Check OpenAI billing, then press Retry on this piece or Resume and approve the price.'),
    'over_reserve': ('The last request cost more than was set aside for it. Spending has stopped.',
                     'Check OpenAI billing, then press Resume and approve the price.'),
    'storage_error': ('The helper could not save its ledger, so it stopped before spending more.',
                      'Free some disk space, then press Resume and approve the price.'),
    'result_missing': ('The finished image file is missing from this computer.',
                       'Check the helper folder, then press Retry on this piece and approve its price.'),
    'not_sent': ('This piece was never sent and cost nothing.', 'Press Retry on this piece and approve its price.'),
    'price_approval_required': ('This job has no price approval, so nothing was sent.',
                                'Approve the price at Extract, Retry or Resume.'),
    'approval_mismatch': ('This approval id was already used with a different count or price, so nothing was sent.',
                          'Approve the price again.'),
    'approval_stale': ('This approval was given before the stop, so nothing was sent.',
                       'Approve the price again at Retry or Resume.'),
    'approval_used_up': ('Every piece in this price approval has been used, so nothing was sent.',
                         'Approve the price for the remaining pieces.'),
    'over_approved': ('This piece would pass the dollars you approved, so nothing was sent.',
                      'Approve a new price for the remaining pieces.'),
    'over_balance': ('This piece would pass the remaining balance, so nothing was sent.',
                     'Add credit in OpenAI billing, update the balance, then approve the price.'),
    'balance_unknown': ('The job did not say the remaining balance, so nothing was sent.',
                        'Reload the page and approve the price again.'),
    'counted_jobs_required': ('The job did not say which charges its balance includes, so nothing was sent.',
                              'Reload the page and approve the price again.'),
    'duplicate_job': ('This job id has already been sent, so it will not be charged again.', RETRY_STEP),
    'duplicate_request': ('An identical request is already on record, so nothing was sent.',
                          'Check the earlier piece, then use Retry on it.'),
    'already_done': ('This piece is already finished and is kept. Nothing was sent.', 'No action needed.'),
    'retry_out_of_order': ('This retry does not follow the last attempt of the piece, so nothing was sent.',
                           'Reload the page, then press Retry on the piece.'),
    'retry_parent_open': ('The earlier attempt of this piece has not finished, so nothing was sent.',
                          'Wait for it to finish, then press Retry.'),
    'returned': ('The queue stopped before this piece was sent. Nothing was charged.',
                 'Press Resume and approve the price to send it again.'),
    'user_stop': ('You stopped the queue. Pieces not yet sent were returned and cost nothing.', RESUME_STEP),
    'missing_source': ('The job did not include the source image and piece outline, so nothing was sent.',
                       'Reload the page and try again.'),
    'too_big': ('This picture is too big to send (limit about 20 MB). Make it smaller and try again.',
                'Use a smaller picture.'),
    'bad_input': ('The helper could not read that request, so nothing was sent.', 'Reload the page and try again.'),
    'unknown_job': ('The helper does not know this job.', 'Reload the page.'),
    'not_held': ('This job has no held charge to resolve, so the ledger did not change.',
                 'Only a held piece can be marked charged or not charged.'),
    'already_resolved': ('This held charge was already resolved, so the ledger did not change.',
                         'To cut this piece again, press Retry and approve its price.'),
    'not_ready': ('This job has no finished image to read.', 'Wait for the job to finish.'),
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def reason(kind):
    message, next_step = REASONS.get(kind, REASONS['service_error'])
    return {'kind': kind, 'message': message, 'next_step': next_step}


def refuse(kind, **extra):
    return {'ok': False, 'accepted': False, **reason(kind), **extra}


def money(value):
    return format(value.quantize(Decimal('0.000001')), 'f')


def parse_money(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


# ---------- copied from work/extraction_queue.py ----------
def retry_parent(job_id):
    head, separator, tail = job_id.rpartition(':retry')
    if not separator or not tail.isdigit():
        return None
    return head if tail == '1' else head + ':retry' + str(int(tail) - 1)


def next_retry_id(job_id):
    head, separator, tail = job_id.rpartition(':retry')
    if separator and tail.isdigit():
        return head + ':retry' + str(int(tail) + 1)
    return job_id + ':retry1'


# A re-cut is a new job beside a finished piece: its id ends ~recutN (a retry of it adds :retryN after).
RECUT_ID = re.compile(r'~recut[0-9]+(:retry[0-9]+)?$')
MAX_RECUT_NOTE = 2000


def is_recut_id(job_id):
    return bool(RECUT_ID.search(job_id))


def build_request(source, region, job):
    prompt = ('Extract exactly this reusable component from the supplied illustration: ' + region['name'] +
        '. Its approximate bounding box as percentages x,y,width,height is ' + json.dumps(region['box']) +
        '. Preserve original visible pixels, pose, gesture, proportions, colors, texture and illustration style. '
        'Keep attached equipment and the approved group together. Remove unrelated objects and all background. '
        'Use real alpha transparency outside the asset and keep the asset interior fully opaque. Preserve crisp original boundaries: no added glow, halo, haze, feathering or colored spill. No labels, outlines or added decorations. '
        'Preserve visible hands and interaction gestures exactly. ' +
        ('Reconstruct only missing or occluded portions to produce a complete usable asset. ' if job['reconstruct'] else
         'Do not redesign or invent new parts. ') +
        'Required reconstruction instructions: '+str(region.get('reconstruction_reason',''))+'. Source guidance: ' + str(source.get('note', '')) +
        '. Overall user feedback: ' + str(job['overallComment']) +
        '. Component feedback: ' + str(job['comment']) +
        ('. This piece was already cut once and the result was judged wrong. Cut it again and fix this: '
         + str(job['recutNote']) if job.get('recut') else ''))
    return {'model':MODEL, 'images':[{'image_url':source['image']}], 'prompt':prompt,
            'n':1, 'size':'1024x1024', 'quality':'high', 'background':'transparent', 'output_format':'png'}


def reported_cost(response):
    usage = response['usage']
    detail = usage['input_tokens_details']
    return (Decimal(detail['image_tokens'])*8 + Decimal(detail['text_tokens'])*5 +
            Decimal(usage['output_tokens'])*30) / Decimal(1000000)
# ---------- end of copied logic ----------


def safe_cost(response):
    """The reported cost, or None when the reply does not state one we can trust."""
    try:
        usage = response['usage']
        detail = usage['input_tokens_details']
        for value in (detail['image_tokens'], detail['text_tokens'], usage['output_tokens']):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
        cost = reported_cost(response)
    except (KeyError, TypeError, InvalidOperation):
        return None
    return cost if cost.is_finite() and cost > 0 else None


def has_real_transparency(raw):
    """True when a PNG has both clear and solid pixels, the same test the page uses."""
    if not raw.startswith(b'\x89PNG\r\n\x1a\n') or len(raw) < 33 or raw[12:16] != b'IHDR':
        return False
    width = int.from_bytes(raw[16:20], 'big')
    height = int.from_bytes(raw[20:24], 'big')
    depth, colour, interlace = raw[24], raw[25], raw[28]
    if colour not in (4, 6):
        return False
    if interlace or depth not in (8, 16) or not width or not height:
        # Not decoded here. The colour type allows alpha and the page checks the pixels.
        return True
    channels = 2 if colour == 4 else 4
    sample = depth // 8
    pixel = channels * sample
    stride = width * pixel
    expected = (stride + 1) * height
    if expected > MAX_PIXEL_BYTES:
        return True
    at = 8
    compressed = bytearray()
    while at + 8 <= len(raw):
        size = int.from_bytes(raw[at:at + 4], 'big')
        tag = raw[at + 4:at + 8]
        if tag == b'IDAT':
            compressed += raw[at + 8:at + 8 + size]
        if tag == b'IEND':
            break
        at += 12 + size
    try:
        data = zlib.decompressobj().decompress(bytes(compressed), expected)
    except zlib.error:
        return False
    if len(data) < expected:
        return False
    previous = bytearray(stride)
    clear = solid = False
    for row in range(height):
        start = row * (stride + 1)
        kind = data[start]
        line = bytearray(data[start + 1:start + 1 + stride])
        for i in range(stride):
            left = line[i - pixel] if i >= pixel else 0
            up = previous[i]
            corner = previous[i - pixel] if i >= pixel else 0
            if kind == 1:
                line[i] = (line[i] + left) & 255
            elif kind == 2:
                line[i] = (line[i] + up) & 255
            elif kind == 3:
                line[i] = (line[i] + (left + up) // 2) & 255
            elif kind == 4:
                estimate = left + up - corner
                a, b, c = abs(estimate - left), abs(estimate - up), abs(estimate - corner)
                best = left if a <= b and a <= c else up if b <= c else corner
                line[i] = (line[i] + best) & 255
            elif kind != 0:
                return False
        for i in range(pixel - sample, stride, pixel):
            if line[i] < 16:
                clear = True
            elif line[i] > 200:
                solid = True
        if clear and solid:
            return True
        previous = line
    return False


def error_detail(body):
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return {}
    detail = parsed.get('error') if isinstance(parsed, dict) else None
    return detail if isinstance(detail, dict) else {}


def classify_http(status, body):
    """Plain failure code for a non-200 answer. Out of credit is never read as a rate limit."""
    detail = error_detail(body)
    if {detail.get('code'), detail.get('type')} & OUT_OF_CREDIT_CODES:
        return 'insufficient_quota'
    if status in (401, 403, 429, 500, 503):
        return str(status)
    if status in (502, 504):
        return 'bad_gateway'
    if status == 400:
        return 'bad_request'
    return 'service_error'


def interpret_reply(status, body):
    """Returns (failure kind or None, reported cost or None, PNG bytes or None)."""
    if status != 200:
        return classify_http(status, body), None, None
    if not body or not body.strip():
        return 'empty', None, None
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return 'garbage', None, None
    if not isinstance(parsed, dict):
        return 'garbage', None, None
    if 'error' in parsed and not parsed.get('data'):
        detail = parsed['error'] if isinstance(parsed['error'], dict) else {}
        if {detail.get('code'), detail.get('type')} & OUT_OF_CREDIT_CODES:
            return 'insufficient_quota', None, None
        return 'garbage', safe_cost(parsed), None
    cost = safe_cost(parsed)
    data = parsed.get('data')
    if not isinstance(data, list) or not data or not isinstance(data[0], dict) or not data[0].get('b64_json'):
        return 'empty', cost, None
    try:
        raw = base64.b64decode(data[0]['b64_json'], validate=True)
    except (ValueError, TypeError):
        return 'garbage', cost, None
    if not raw.startswith(b'\x89PNG\r\n\x1a\n') or len(raw) < 33:
        return 'garbage', cost, None
    if not has_real_transparency(raw):
        return 'no_transparency', cost, None
    if cost is None:
        # A picture with no stated price cannot be settled, so it stays held.
        return 'garbage', None, None
    return None, cost, raw


def check_image_url(url):
    """The key only ever goes to OpenAI's edits address or to this Mac."""
    if url == DEFAULT_IMAGE_URL:
        return True
    parts = urlsplit(url)
    return parts.scheme == 'http' and parts.hostname == '127.0.0.1' and not parts.username and not parts.password


def load_key(path):
    """Read at the moment of a real call. Errors never contain the file's content."""
    path = Path(path).expanduser()
    if not path.is_file() or not private_enough(path):
        raise PermissionError('key file')
    key = path.read_text(encoding='utf-8').strip()
    if not key.startswith('sk-') or any(c.isspace() for c in key):
        raise PermissionError('key file')
    return key


class PaidCall:
    """One POST with no redirects, no proxy and a hard overall deadline."""

    def __init__(self, url, payload, key, timeout):
        self.parts = urlsplit(url)
        self.payload = payload
        self.key = key
        self.timeout = timeout
        self.abort_reason = None
        self.connection = None
        self.guard = threading.Lock()

    def abort(self, why):
        with self.guard:
            if self.abort_reason is None:
                self.abort_reason = why
            connection = self.connection
        sock = connection.sock if connection else None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def run(self):
        """Returns (status, body, request id) or (None, failure kind, None)."""
        timer = threading.Timer(self.timeout, self.abort, ('timeout',))
        timer.daemon = True
        host, port = self.parts.hostname, self.parts.port
        if self.parts.scheme == 'https':
            connection = http.client.HTTPSConnection(host, port, timeout=self.timeout,
                                                     context=ssl.create_default_context())
        else:
            connection = http.client.HTTPConnection(host, port, timeout=self.timeout)
        with self.guard:
            self.connection = connection
        timer.start()
        try:
            connection.connect()
            if self.abort_reason:
                return None, self.abort_reason, None
            path = self.parts.path or '/'
            connection.request('POST', path, body=json.dumps(self.payload).encode(),
                               headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            request_id = re.sub(r'[^A-Za-z0-9_-]', '', response.getheader('x-request-id') or '')[:64]
            declared = response.getheader('Content-Length')
            body = bytearray()
            while True:
                part = response.read(65536)
                if not part:
                    break
                body += part
                if len(body) > MAX_REPLY_BYTES:
                    return None, 'garbage', request_id
            if self.abort_reason:
                return None, self.abort_reason, request_id
            if declared and declared.isdigit() and len(body) < int(declared):
                return None, 'dropped', request_id
            return response.status, bytes(body), request_id
        except (TimeoutError, socket.timeout):
            return None, self.abort_reason or 'timeout', None
        except (http.client.HTTPException, OSError):
            return None, self.abort_reason or 'dropped', None
        finally:
            timer.cancel()
            self.key = None
            connection.close()


class Engine:
    def __init__(self, data_dir, image_url, key_file, reserve_usd, timeout, originals_dir=None, enforce_settings=True):
        # False only for tests that exercise the money rules alone.
        self.enforce_settings = enforce_settings
        self.data_dir = Path(data_dir)
        self.originals_dir = Path(originals_dir or os.environ.get('SNIPPY_ORIGINALS') or default_originals()).expanduser()
        self.image_url = image_url
        self.key_file = key_file
        self.reserve = reserve_usd
        self.timeout = timeout
        self.state_path = self.data_dir / 'state.json'
        self.requests_dir = self.data_dir / 'requests'
        self.results_dir = self.data_dir / 'results'
        self.requests_dir.mkdir(mode=0o700, exist_ok=True)
        self.results_dir.mkdir(mode=0o700, exist_ok=True)
        self.lock = threading.RLock()
        self.wake = threading.Condition(self.lock)
        self.current_call = None
        self.state = self.load_state()
        self.recover()

    # ----- storage -----
    def load_state(self):
        if not self.state_path.exists():
            return {'version': 1, 'seq': 0, 'approvals': {}, 'jobs': {}, 'reservations': {}, 'pause': None}
        # A damaged ledger must stop the helper. Starting empty would forget real charges.
        state = json.loads(self.state_path.read_text(encoding='utf-8'))
        if not isinstance(state, dict) or state.get('version') != 1:
            raise ValueError('unrecognised ledger')
        for name in ('approvals', 'jobs', 'reservations'):
            if not isinstance(state.get(name), dict):
                raise ValueError('unrecognised ledger')
        return state

    def write_atomic(self, path, data):
        temporary = path.with_name(path.name + '.' + secrets.token_hex(4) + '.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        if sys.platform != 'win32':  # Windows cannot open a folder to sync it.
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def save(self):
        self.write_atomic(self.state_path, json.dumps(self.state, indent=1, sort_keys=True).encode())

    def file_stem(self, job_id):
        return hashlib.sha256(job_id.encode()).hexdigest()[:32]

    def request_path(self, job_id):
        return self.requests_dir / (self.file_stem(job_id) + '.json')

    def result_path(self, job_id):
        return self.results_dir / (self.file_stem(job_id) + '.png')

    def drop_request(self, job_id):
        try:
            self.request_path(job_id).unlink()
        except FileNotFoundError:
            pass

    def next_seq(self):
        self.state['seq'] += 1
        return self.state['seq']

    # ----- recovery -----
    def recover(self):
        """Nothing runs on start unless it was already accepted and approved."""
        changed = False
        with self.lock:
            for job_id, job in self.state['jobs'].items():
                if job['state'] == 'sending':
                    # The call may have reached the service. It is never sent again.
                    entry = self.state['reservations'].get(job_id)
                    if entry and entry['status'] == 'reserved':
                        entry.update(status='held', hold_reason='needs_check')
                    self.mark_failed(job, 'needs_check', charged='maybe', state='needs_check')
                    self.drop_request(job_id)
                    self.set_pause('needs_check', job_id)
                    changed = True
            # A stop stays a stop: nothing unsent survives a restart under a pause.
            if self.state['pause'] and self.return_unsent():
                changed = True
            for job_id, job in self.state['jobs'].items():
                if job['state'] != 'accepted':
                    continue
                entry = self.state['reservations'].get(job_id)
                approval = self.state['approvals'].get(job.get('approval_id'))
                valid = (entry and entry['status'] == 'reserved' and approval
                         and entry.get('approval_id') == job.get('approval_id')
                         and self.request_path(job_id).is_file())
                if valid and not self.state['pause']:
                    continue
                if entry and entry['status'] == 'reserved':
                    entry['status'] = 'released'
                self.mark_failed(job, 'not_sent' if approval else 'price_approval_required', charged='no',
                                 resendable=True)
                self.drop_request(job_id)
                changed = True
            if changed:
                self.save()

    # ----- state helpers -----
    def mark_failed(self, job, kind, charged, state='failed', resendable=False, cost=None):
        job.update(state=state, charged=charged, finished_at=utc_now(), failed_seq=self.next_seq(),
                   resendable=resendable, **reason(kind))
        if cost is not None:
            job['cost_usd'] = money(cost)
        if resendable:
            job.pop('retry_job_id', None)
        else:
            job['retry_job_id'] = next_retry_id(job['job_id'])

    def set_pause(self, kind, job_id):
        """Every pause returns all unsent jobs. The first reason stays until a Resume."""
        if not self.state['pause']:
            seq = self.next_seq()
            self.state['pause'] = {**reason(kind), 'pause_id': 'pause-%d' % seq, 'job_id': job_id,
                                   'seq': seq, 'at': utc_now()}
        return self.return_unsent()

    def return_unsent(self):
        returned = []
        for job_id, job in self.state['jobs'].items():
            if job['state'] != 'accepted':
                continue
            entry = self.state['reservations'].get(job_id)
            if entry and entry['status'] == 'reserved':
                entry['status'] = 'released'
            self.mark_failed(job, 'returned', charged='no', state='returned', resendable=True)
            self.drop_request(job_id)
            returned.append(job_id)
        return returned

    def spend_not_on_page(self, counted_job_ids):
        """Charges the page has not read yet, so its balance figure cannot include them.

        The page names the charges its balance includes on every start_job. Reads never
        change this, because Claude may answer a read-only call from its cache.
        """
        return sum((Decimal(entry['actual_usd']) for job_id, entry in self.state['reservations'].items()
                    if entry['status'] == 'settled' and job_id not in counted_job_ids), Decimal(0))

    def current_reserve(self):
        """Per-request reserve: the configured one, or 1.5x the dearest actual cost in this ledger if larger."""
        top = max((Decimal(e['actual_usd']) for e in self.state['reservations'].values()
                   if e['status'] == 'settled'), default=Decimal(0))
        return max(self.reserve, (top * Decimal('1.5')).quantize(Decimal('0.0001')))

    def totals(self):
        settled = open_amount = Decimal(0)
        for entry in self.state['reservations'].values():
            if entry['status'] == 'settled':
                settled += Decimal(entry['actual_usd'])
            elif entry['status'] in ('reserved', 'held'):
                open_amount += Decimal(entry['reserve_usd'])
        return settled, open_amount

    def approval_use(self, approval_id):
        used = 0
        committed = Decimal(0)
        for entry in self.state['reservations'].values():
            if entry.get('approval_id') != approval_id or entry['status'] == 'released':
                continue
            used += 1
            committed += Decimal(entry['actual_usd'] if entry['status'] == 'settled' else entry['reserve_usd'])
        return used, committed

    def active_job(self):
        for job_id, job in self.state['jobs'].items():
            if job['state'] == 'sending':
                return job_id
        return None

    def queued_ids(self):
        waiting = [(job.get('queue_seq', 0), job_id) for job_id, job in self.state['jobs'].items()
                   if job['state'] == 'accepted']
        return [job_id for _, job_id in sorted(waiting)]

    def family_done(self, job_id):
        base = job_id
        while retry_parent(base):
            base = retry_parent(base)
        for other, job in self.state['jobs'].items():
            other_base = other
            while retry_parent(other_base):
                other_base = retry_parent(other_base)
            if other_base == base and job['state'] == 'done':
                return other
        return None

    # ----- local originals -----
    def read_original(self, original_id):
        """The data URL for a source picture stored on this Mac, or None when it is missing or its bytes do not match its id."""
        for ext, mime in ORIGINAL_MIME.items():
            path = self.originals_dir / (original_id + '.' + ext)
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if hashlib.sha256(raw).hexdigest()[:32] != original_id:
                return None
            return 'data:' + mime + ';base64,' + base64.b64encode(raw).decode()
        return None

    def count_originals(self):
        try:
            return sum(1 for item in self.originals_dir.iterdir()
                       if item.suffix[1:] in ORIGINAL_MIME and item.is_file())
        except OSError:
            return 0

    # ----- tools -----
    def tool_status(self, _args):
        with self.lock:
            settled, open_amount = self.totals()
            budget = parse_money(read_settings()[0].get('budget_usd'))
            held = [self.public_hold(job) for job in self.state['jobs'].values() if job['state'] in FAILED_STATES
                    and job.get('retry_job_id') and job['retry_job_id'] not in self.state['jobs']
                    and not self.family_done(job['job_id'])]
            finished = sum(1 for job in self.state['jobs'].values() if job['state'] == 'done')
            active = self.active_job()
            pause = self.state['pause']
            queued = self.queued_ids()
            counts = {name: 0 for name in QUEUE_STATES}
            for job in self.state['jobs'].values():
                counts[job['state']] += 1
            return {'ok': True, 'service': 'snippy-engine', 'features': ['batch', 'local_originals'],
                    'originals': self.count_originals(),
                    'engine': 'paused' if pause else 'running' if active or queued else 'idle',
                    'last_stop': self.state.get('last_stop'),
                    'active_job': active,
                    'queue': counts, 'queued_job_ids': queued,
                    'returned_job_ids': sorted(job_id for job_id, job in self.state['jobs'].items()
                                               if job['state'] == 'returned'),
                    'pause': {k: pause.get(k) for k in ('pause_id', 'kind', 'message', 'next_step', 'job_id', 'at')}
                             if pause else None,
                    'spent_usd': float(settled), 'reserved_usd': float(open_amount),
                    'budget_usd': None if budget is None else float(budget),
                    'budget_left_usd': None if budget is None else float(max(budget - settled - open_amount, 0)),
                    'reserve_usd_per_piece': float(self.current_reserve()),
                    'held': held, 'finished': finished,
                    'setup': self.setup_report(),
                    'delivery_failed': [{'job_id': j['job_id'], 'drive_error': j['delivery']['drive_error'],
                                         'local_error': j['delivery']['local_error']}
                                        for j in self.state['jobs'].values()
                                        if j.get('delivery') and (j['delivery']['drive_error'] or j['delivery']['local_error'])],
                    'delivered_to_drive': sum(1 for j in self.state['jobs'].values()
                                              if (j.get('delivery') or {}).get('drive')),
                    'retry_quote': {'count': len(held), 'price_usd': float(self.current_reserve() * len(held)),
                                    'finished_kept': finished,
                                    'note': 'Finished pieces are kept and are not redone.'}}

    def public_hold(self, job):
        return {'job_id': job['job_id'], 'kind': job.get('kind'), 'message': job.get('message'),
                'next_step': job.get('next_step'), 'charged': job.get('charged'),
                'retry_job_id': job.get('retry_job_id'), 'resolution': job.get('resolution')}

    def tool_start_job(self, args):
        # One press of Send can hand over a whole approved batch, so the helper cuts it with the page hidden.
        if 'jobs' in args:
            return self.start_batch(args)
        return self.start_one(args)

    def start_batch(self, args):
        jobs = args.get('jobs')
        images = args.get('images', {})
        if (not isinstance(jobs, list) or not 1 <= len(jobs) <= MAX_BATCH_JOBS
                or any(not isinstance(item, dict) for item in jobs)
                or not isinstance(images, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in images.items())):
            return refuse('bad_input')
        ids = [item.get('job_id') for item in jobs]
        if len(set(map(str, ids))) != len(ids):
            return refuse('bad_input')
        results = []
        cache = {}  # Each stored picture is read once for the whole batch.
        for item in jobs:
            one = dict(item)
            # Shared fields are sent once per press; each job keeps its own price approval.
            for key in ('balance_usd', 'counted_job_ids'):
                if key not in one and key in args:
                    one[key] = args[key]
            if 'source_image_data' not in one and isinstance(one.get('source_id'), str):
                # The first job's picture rides at the top level, so a host that drops unknown fields still sends it.
                shared = args.get('source_image_data') if one['source_id'] == args.get('source_id') else None
                one['source_image_data'] = images.get(one['source_id']) or shared
            # Each job runs every money check on its own, in order, so Stop between two jobs refuses the rest.
            reply = self.start_one(one, cache)
            results.append({'job_id': item.get('job_id'), **reply})
        accepted = sum(1 for r in results if r.get('accepted') is True)
        return {'ok': True, 'batch': True, 'accepted_count': accepted, 'results': results}

    def start_one(self, args, cache=None):
        job_id = args.get('job_id')
        if not isinstance(job_id, str) or not job_id or len(job_id) > 200:
            return refuse('bad_input')
        approval_in = args.get('approval')
        if not isinstance(approval_in, dict):
            return refuse('price_approval_required')
        approval_id = approval_in.get('approval_id')
        pieces = approval_in.get('piece_count')
        approved = parse_money(approval_in.get('approved_usd'))
        if (not isinstance(approval_id, str) or not approval_id or len(approval_id) > 200
                or isinstance(pieces, bool) or not isinstance(pieces, int) or pieces < 1
                or approved is None or approved <= 0):
            return refuse('price_approval_required')
        # The artist kit's hard stop is the artist's own Snippy spending limit (settings budget_usd); an OpenAI
        # balance is optional, and when the page does send one it is still checked.
        balance = parse_money(args.get('balance_usd'))
        if balance is None and not self.enforce_settings:
            return refuse('balance_unknown')
        # A re-cut needs the price approval to say so, and only a re-cut id may carry that kind.
        kind = approval_in.get('kind')
        recut = is_recut_id(job_id)
        note = args.get('recut_note', '')
        if kind not in (None, 'recut') or (kind == 'recut') != recut:
            return refuse('bad_input')
        if not isinstance(note, str) or len(note) > MAX_RECUT_NOTE or (note and not recut):
            return refuse('bad_input')
        counted_in = args.get('counted_job_ids')
        if balance is None and counted_in is None:
            counted_in = []  # no balance sent, so no list of charges it includes
        if (not isinstance(counted_in, list) or len(counted_in) > 20000
                or any(not isinstance(item, str) or not item or len(item) > 200 for item in counted_in)):
            return refuse('counted_jobs_required')
        counted_job_ids = set(counted_in)

        with self.wake:
            existing = self.state['jobs'].get(job_id)
            if existing and existing['state'] in ACTIVE:
                return {'ok': True, 'accepted': True, 'job_id': job_id, 'already_running': True}

            known = self.state['approvals'].get(approval_id)
            if known and (known['piece_count'] != pieces or Decimal(known['approved_usd']) != approved
                          or known.get('kind') != kind):
                return refuse('approval_mismatch')
            pause = self.state['pause']
            if pause and (known or not pause.get('pause_id')
                          or approval_in.get('resume_pause_id') != pause['pause_id']):
                # A stop lifts only with a price approval given after it, naming this exact stop.
                # An approval the helper has not seen yet may predate the stop, so being new is not enough.
                return {'ok': False, 'accepted': False, 'kind': pause['kind'], 'message': pause['message'],
                        'next_step': pause['next_step'], 'paused': True, 'pause_id': pause.get('pause_id')}
            # An approval given before the latest Stop can never send, even after a later Resume lifted the pause.
            last_stop = self.state.get('last_stop')
            # With no mark, only a Resume that names this exact Stop counts as given after it.
            if last_stop and approval_in.get('stop_mark', approval_in.get('resume_pause_id')) != last_stop:
                return refuse('approval_stale')
            fresh_after = lambda seq: known is None or known['seq'] > seq

            # A re-cut has its own id and so its own family: finishing the piece it redoes never blocks it,
            # and a second press on the same re-cut id is still refused as already_done.
            done = self.family_done(job_id)
            if done:
                return refuse('already_done', finished_job_id=done)
            if existing:
                if not existing.get('resendable'):
                    return refuse('duplicate_job', retry_job_id=existing.get('retry_job_id'))
                if not fresh_after(existing.get('failed_seq', 0)):
                    return refuse('approval_stale')

            parent = retry_parent(job_id)
            chain = set()
            ancestor = parent
            while ancestor:
                chain.add(ancestor)
                ancestor = retry_parent(ancestor)
            if parent:
                parent_job = self.state['jobs'].get(parent)
                if parent_job:
                    if parent_job['state'] not in RETRYABLE_STATES:
                        return refuse('retry_parent_open')
                    if not fresh_after(parent_job.get('failed_seq', 0)):
                        return refuse('approval_stale')
                elif any(a in self.state['jobs'] for a in chain):
                    return refuse('retry_out_of_order')

            image = args.get('source_image_data')
            if not image and isinstance(args.get('source_image'), str) and ORIGINAL_ID.fullmatch(args['source_image']):
                # No picture sent: use the stored original, but only if its bytes are the ones the id names.
                original_id = args['source_image']
                cache = {} if cache is None else cache
                if original_id not in cache:
                    cache[original_id] = self.read_original(original_id)
                image = cache[original_id]
                if image is None:
                    return refuse('missing_source', message='The stored original picture ' + original_id
                                  + ' is missing from this computer or does not match its id, so nothing was sent.')
            name = args.get('name')
            box = args.get('box')
            if (not isinstance(image, str) or not re.match(r'data:image/(png|jpeg|webp);base64,', image)
                    or not isinstance(name, str) or not name
                    or not isinstance(box, list) or len(box) != 4
                    or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in box)):
                return refuse('missing_source')
            request = build_request(
                {'image': image, 'note': args.get('source_note', '')},
                {'name': name, 'box': box, 'reconstruction_reason': args.get('reconstruction_reason', '')},
                {'reconstruct': bool(args.get('reconstruct')), 'overallComment': args.get('overall_comment', ''),
                 'comment': args.get('comment', ''), 'recut': recut, 'recutNote': note})
            request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
            for prior_id, prior in self.state['reservations'].items():
                if prior_id == job_id or prior['status'] == 'released' or prior.get('request_hash') != request_hash:
                    continue
                prior_job = self.state['jobs'].get(prior_id, {})
                if not (prior_id in chain and prior_job.get('state') in FAILED_STATES):
                    return refuse('duplicate_request')

            reserve = self.current_reserve()
            # Your own run budget from settings.json: none set means nothing is sent, said loudly.
            budget = parse_money(read_settings()[0].get('budget_usd'))
            if self.enforce_settings:
                if budget is None or budget <= 0:
                    return refuse('budget_not_set')
                spent_now, open_now = self.totals()
                if spent_now + open_now + reserve > budget:
                    refusal = refuse('over_budget', budget_usd=float(budget),
                                     committed_usd=float(spent_now + open_now), reserve_usd=float(reserve))
                    refusal['message'] = ('Your Snippy spending limit ($%s) is used up (this piece, plus what is already spent and set aside, would pass it), so nothing was sent.'
                                          % format(budget.normalize(), 'f'))
                    refusal['next_step'] = 'Ask Claude to raise your Snippy spending limit, then approve the price again.'
                    return refusal
            # Hard stops: the approved dollars, the piece count and the remaining balance.
            used, committed = self.approval_use(approval_id)
            if used >= pieces:
                return refuse('approval_used_up')
            if committed + reserve > approved:
                return refuse('over_approved', needed_usd=float(committed + reserve), approved_usd=float(approved))
            settled, open_amount = self.totals()
            # Jobs finish while the page is closed, so its figure may not include them yet.
            current = None
            if balance is not None:
                current = balance - self.spend_not_on_page(counted_job_ids)
                if known and known.get('balance_usd') is not None:
                    anchored = Decimal(known['balance_usd']) - (settled - Decimal(known['settled_at_anchor']))
                    left = min(anchored, current)
                else:
                    left = current
                if open_amount + reserve > left:
                    return refuse('over_balance', balance_usd=float(left), committed_usd=float(open_amount))

            # All or nothing: if the reservation cannot reach the disk, the job does not exist.
            before = json.dumps(self.state)
            try:
                self.write_atomic(self.request_path(job_id), json.dumps(request).encode())
                if not known:
                    known = {'approval_id': approval_id, 'piece_count': pieces, 'approved_usd': str(approved),
                             'balance_usd': None if current is None else str(current), 'settled_at_anchor': str(settled),
                             'seq': self.next_seq(), 'first_seen': utc_now(), 'kind': kind}
                    self.state['approvals'][approval_id] = known
                self.state['pause'] = None
                attempts = (existing or {}).get('attempts', 0) + 1
                self.state['jobs'][job_id] = {
                    'job_id': job_id, 'state': 'accepted', 'approval_id': approval_id, 'attempts': attempts,
                    'queue_seq': self.next_seq(),
                    'source_id': args.get('source_id'), 'region_id': args.get('region_id'), 'accepted_at': utc_now(),
                    'piece_name': clean_part(args.get('name') or job_id, 'piece'),
                    'source_name': clean_part(args.get('source_name'), UNTITLED_PICTURE)}
                self.state['reservations'][job_id] = {'status': 'reserved', 'reserve_usd': str(reserve),
                                                      'approval_id': approval_id, 'request_hash': request_hash}
                self.save()
            except OSError:
                self.state = json.loads(before)
                self.drop_request(job_id)
                return refuse('storage_error')
            self.wake.notify_all()
            reply = {'ok': True, 'accepted': True, 'job_id': job_id, 'reserved_usd': float(reserve)}
            if recut:
                reply['recut'] = True
            if parent:
                kept = sum(1 for job in self.state['jobs'].values()
                           if job['state'] == 'done' and job.get('source_id') == args.get('source_id'))
                reply['retry'] = {'count': pieces, 'price_usd': float(approved), 'finished_kept': kept,
                                  'note': 'Finished pieces are kept and are not redone.'}
            return reply

    def tool_job_status(self, args):
        job_id = args.get('job_id')
        with self.lock:
            job = self.state['jobs'].get(job_id) if isinstance(job_id, str) else None
            if not job:
                return {'ok': True, 'job_id': job_id, 'status': 'unknown', **reason('unknown_job')}
            shown = {'accepted': 'queued', 'sending': 'running', 'done': 'done', 'failed': 'failed',
                     'needs_check': 'failed', 'cancelled': 'cancelled', 'returned': 'returned'}[job['state']]
            reply = {'ok': True, 'job_id': job_id, 'status': shown, 'state': job['state']}
            for field in ('kind', 'message', 'next_step', 'charged', 'retry_job_id', 'resendable', 'resolution', 'delivery'):
                if field in job:
                    reply[field] = job[field]
            if 'cost_usd' in job:
                reply['cost_usd'] = float(job['cost_usd'])
            return reply

    def tool_get_result(self, args):
        job_id = args.get('job_id')
        offset = args.get('offset', 0)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset % 4:
            return {'ok': False, 'data': '', 'done': False, **reason('bad_input')}
        with self.lock:
            job = self.state['jobs'].get(job_id) if isinstance(job_id, str) else None
            if not job:
                return {'ok': False, 'data': '', 'done': False, **reason('unknown_job')}
            if job['state'] != 'done':
                return {'ok': False, 'data': '', 'done': False, **reason('not_ready')}
            cost = float(job['cost_usd'])
            path = self.result_path(job_id)
        start = offset // 4 * 3
        count = CHUNK_CHARS // 4 * 3
        try:
            size = path.stat().st_size
            with path.open('rb') as handle:
                handle.seek(start)
                part = handle.read(count)
        except OSError:
            return {'ok': False, 'data': '', 'done': False, **reason('result_missing')}
        total = 4 * ((size + 2) // 3)
        data = base64.b64encode(part).decode()
        next_offset = offset + len(data)
        return {'ok': True, 'data': data, 'done': next_offset >= total, 'next_offset': next_offset,
                'total': total, 'cost_usd': cost}

    def tool_cancel(self, args):
        job_id = args.get('job_id')
        with self.lock:
            job = self.state['jobs'].get(job_id) if isinstance(job_id, str) else None
            if not job:
                return {'ok': False, **reason('unknown_job')}
            if job['state'] == 'accepted':
                self.state['reservations'][job_id]['status'] = 'released'
                self.mark_failed(job, 'cancelled', charged='no', state='cancelled')
                job['message'] = 'You cancelled this piece before it was sent. Nothing was charged.'
                self.drop_request(job_id)
                self.save()
                return {'ok': True, 'job_id': job_id, 'status': 'cancelled', 'charged': 'no',
                        'message': job['message'], 'next_step': job['next_step']}
            if job['state'] == 'sending':
                job['cancel_requested'] = True
                self.save()
                if self.current_call:
                    self.current_call.abort('cancelled')
                return {'ok': True, 'job_id': job_id, 'status': 'cancelling', 'charged': 'maybe',
                        **{k: v for k, v in reason('cancelled').items() if k != 'kind'}}
            return {'ok': True, 'job_id': job_id, 'status': job['state'], 'changed': False}

    def tool_stop_all(self, _args):
        """The user's stop: survives restart, returns unsent jobs, aborts the call in flight."""
        with self.wake:
            already = bool(self.state['pause'])
            if already:
                # A Resume approved before this Stop names the old id, so it can no longer lift the pause.
                seq = self.next_seq()
                self.state['pause'].update(pause_id='pause-%d' % seq, seq=seq)
            returned = self.set_pause('user_stop', None)
            self.state['last_stop'] = self.state['pause']['pause_id']
            in_flight = None
            aborted = 0
            for job_id, job in self.state['jobs'].items():
                if job['state'] == 'sending':
                    in_flight = job_id
                    if not job.get('cancel_requested'):
                        job['cancel_requested'] = True
                        aborted = 1
            saved = True
            try:
                self.save()
            except OSError:
                saved = False  # Stopped in memory all the same.
            if in_flight and self.current_call:
                self.current_call.abort('cancelled')
            self.wake.notify_all()
            pause = self.state['pause']
            reply = {'ok': saved, 'paused': True, 'already_paused': already, 'returned': len(returned),
                     'returned_job_ids': returned, 'aborted': aborted, 'aborted_job_id': in_flight,
                     'pause_id': pause['pause_id'], 'kind': pause['kind'], 'message': pause['message'],
                     'next_step': pause['next_step'], 'last_stop': pause['pause_id']}
            if not saved:
                reply.update(reason('storage_error'))
            return reply

    def tool_resolve_hold(self, args):
        """Records what billing showed for a held piece. Ledger only: never sends, never re-queues."""
        job_id = args.get('job_id')
        outcome = args.get('outcome')
        supplied = args.get('charged_usd')
        if not isinstance(job_id, str) or not isinstance(outcome, str) or outcome not in ('not_charged', 'charged'):
            return {'ok': False, **reason('bad_input')}
        amount = None
        if supplied is not None:
            amount = parse_money(supplied)
            if outcome != 'charged' or amount is None or amount <= 0 or amount > MAX_RESOLVED_USD:
                return {'ok': False, **reason('bad_input')}
        with self.lock:
            job = self.state['jobs'].get(job_id)
            if not job:
                return {'ok': False, **reason('unknown_job')}
            entry = self.state['reservations'].get(job_id)
            if entry and entry.get('resolution'):
                return {'ok': False, 'changed': False, 'resolution': entry['resolution'], **reason('already_resolved')}
            if not entry or entry['status'] != 'held' or job['state'] not in FAILED_STATES:
                return {'ok': False, 'changed': False, 'state': job['state'], **reason('not_held')}
            before = json.dumps(self.state)
            resolution = {'outcome': outcome, 'at': utc_now()}
            if outcome == 'charged':
                actual = amount if amount is not None else Decimal(entry['reserve_usd'])
                resolution['charged_usd'] = str(actual)
                entry.update(status='settled', actual_usd=str(actual), resolution=resolution)
                job.update(charged='yes', cost_usd=money(actual), resolution=resolution)
            else:
                entry.update(status='released', resolution=resolution)
                job.update(charged='no', resolution=resolution)
            try:
                self.save()
            except OSError:
                self.state = json.loads(before)
                return {'ok': False, 'changed': False, **reason('storage_error')}
            settled, open_amount = self.totals()
            return {'ok': True, 'changed': True, 'job_id': job_id, 'outcome': outcome,
                    'charged_usd': float(resolution.get('charged_usd', 0)), 'reservation': entry['status'],
                    'spent_usd': float(settled), 'reserved_usd': float(open_amount),
                    'retry_job_id': job.get('retry_job_id'),
                    'next_step': 'To cut this piece again, press Retry and approve its price.'}

    # ----- the one place a paid request is sent -----
    def worker(self):
        while True:
            try:
                with self.wake:
                    job_id = self.next_runnable()
                    while job_id is None:
                        self.wake.wait(1)
                        job_id = self.next_runnable()
                self.run_job(job_id)
            except Exception as error:  # The worker must outlive any single job.
                print('Worker error: ' + type(error).__name__, file=sys.stderr, flush=True)
                self.contain_failure()

    def next_runnable(self):
        if self.state['pause']:
            return None
        queued = self.queued_ids()
        return queued[0] if queued else None

    def contain_failure(self):
        with self.lock:
            for job_id, job in self.state['jobs'].items():
                if job['state'] == 'sending':
                    entry = self.state['reservations'].get(job_id)
                    if entry and entry['status'] == 'reserved':
                        entry.update(status='held', hold_reason='needs_check')
                    self.mark_failed(job, 'needs_check', charged='maybe', state='needs_check')
                    self.drop_request(job_id)
                    self.set_pause('needs_check', job_id)
                    self.try_save()
            self.current_call = None

    def try_save(self):
        """After a paid call, memory stays right even if the disk fails, and spending stops."""
        try:
            self.save()
        except OSError:
            if not self.state['pause']:
                self.set_pause('storage_error', None)

    def run_job(self, job_id):
        key = None
        try:
            key = read_key(self.key_file)
        except (OSError, UnicodeDecodeError, MissingKey) as error:
            missing = isinstance(error, MissingKey)
            with self.lock:
                job = self.state['jobs'][job_id]
                if job['state'] == 'accepted':
                    self.state['reservations'][job_id]['status'] = 'released'
                    self.mark_failed(job, 'no_key' if missing else 'key_problem', charged='no', resendable=True)
                    self.drop_request(job_id)
                    self.set_pause('no_key' if missing else 'key_problem', job_id)
                    self.save()
            return
        with self.lock:
            job = self.state['jobs'][job_id]
            if job['state'] != 'accepted' or self.state['pause']:
                return
            try:
                payload = json.loads(self.request_path(job_id).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                self.state['reservations'][job_id]['status'] = 'released'
                self.mark_failed(job, 'not_sent', charged='no', resendable=True)
                self.save()
                return
            # Written to disk before the only network call, so a crash can never re-send it.
            job.update(state='sending', sent_at=utc_now())
            try:
                self.save()
            except OSError:
                # Not on disk means not sent.
                job.pop('sent_at', None)
                job['state'] = 'accepted'
                self.state['reservations'][job_id]['status'] = 'released'
                self.mark_failed(job, 'not_sent', charged='no', resendable=True)
                self.drop_request(job_id)
                self.set_pause('storage_error', job_id)
                self.try_save()
                return
            call = PaidCall(self.image_url, payload, key, self.timeout)
            self.current_call = call
        key = None
        status, body, request_id = call.run()
        # Checking every pixel takes a moment, so it happens outside the lock.
        outcome = (body, None, None) if status is None else interpret_reply(status, body)
        with self.lock:
            self.current_call = None
            self.finish(job_id, outcome, request_id)

    def deliver_piece(self, job, raw):
        """Save the finished PNG into output_folder, then copy it into drive_folder/<artist_name>/<picture name>/<piece>.png.
        Each step is recorded on the piece; a failure is shown in status, never swallowed. Never overwrites."""
        settings, _problem = read_settings()
        filename = clean_part(job.get('piece_name'), 'piece') + '.png'
        record = {'local': None, 'local_error': None, 'drive': None, 'drive_error': None}
        output_folder = settings.get('output_folder')
        if output_folder:
            try:
                record['local'] = str(unique_copy(raw, Path(str(output_folder)).expanduser()
                                                  / clean_part(job.get('source_name'), UNTITLED_PICTURE), filename))
            except Exception as error:
                record['local_error'] = type(error).__name__ + ': ' + str(error)
        drive_folder, artist = settings.get('drive_folder'), settings.get('artist_name')
        if not drive_folder:
            record['drive_error'] = 'Drive folder not set up'
        elif not artist or not str(artist).strip():
            record['drive_error'] = 'Artist name not set up'
        else:
            try:
                # Same layout a shared team folder usually uses: artist, then the picture the piece was cut from.
                target = unique_copy(raw, Path(str(drive_folder)).expanduser() / clean_part(artist, 'artist')
                                     / clean_part(job.get('source_name'), UNTITLED_PICTURE), filename)
                record['drive'] = str(target)
            except Exception as error:
                record['drive_error'] = type(error).__name__ + ': ' + str(error)
        job['delivery'] = record

    def setup_report(self):
        """What is and is not set up, in plain words. Never contains the key."""
        settings, problem = read_settings()
        key_ready = bool(os.environ.get(KEY_ENV, '').strip()) or Path(self.key_file).expanduser().is_file()
        todo = []
        if not key_ready:
            todo.append(NO_KEY_MESSAGE)
        if problem:
            todo.append(problem)
        if not settings.get('drive_folder'):
            todo.append('Drive folder not set up')
        if not settings.get('output_folder'):
            todo.append('Output folder not set up')
        if parse_money(settings.get('budget_usd')) is None:
            todo.append('Run budget not set up')
        if not str(settings.get('artist_name') or '').strip():
            todo.append('Artist name not set up')
        return {'key_ready': key_ready, 'problems': todo}

    def finish(self, job_id, outcome, request_id):
        job = self.state['jobs'][job_id]
        entry = self.state['reservations'][job_id]
        if request_id:
            job['request_id'] = request_id
        kind, cost, raw = outcome
        if job.get('cancel_requested'):
            kind, raw = 'cancelled', None

        if kind is None:
            # Save the picture before the ledger says done.
            self.write_atomic(self.result_path(job_id), raw)
            entry.update(status='settled', actual_usd=str(cost))
            job.update(state='done', cost_usd=money(cost), charged='yes', finished_at=utc_now())
            self.deliver_piece(job, raw)
        elif cost is not None:
            entry.update(status='settled', actual_usd=str(cost))
            self.mark_failed(job, kind, charged='yes', cost=cost,
                             state='cancelled' if kind == 'cancelled' else 'failed')
        elif kind in NOT_CHARGED:
            entry['status'] = 'released'
            self.mark_failed(job, kind, charged='no', resendable=True)
        else:
            entry.update(status='held', hold_reason=kind)
            self.mark_failed(job, kind, charged='maybe',
                             state='cancelled' if kind == 'cancelled' else 'failed')
        self.drop_request(job_id)

        if kind in PAUSE_KINDS:
            self.set_pause(kind, job_id)
        elif cost is not None and cost > Decimal(entry['reserve_usd']):
            print('Request %s cost $%s, more than the $%s set aside for it. Sending is paused until you press Resume and approve the price.'
                  % (job_id, cost, entry['reserve_usd']), file=sys.stderr, flush=True)
            self.set_pause('over_reserve', job_id)
        self.try_save()
        self.wake.notify_all()


TOOLS = {'status': 'tool_status', 'start_job': 'tool_start_job', 'job_status': 'tool_job_status',
         'get_result': 'tool_get_result', 'cancel': 'tool_cancel', 'resolve_hold': 'tool_resolve_hold',
         'stop_all': 'tool_stop_all'}


class QuietServer(ThreadingHTTPServer):
    address_family = socket.AF_INET
    daemon_threads = True

    def handle_error(self, request, client_address):
        print('Request error: ' + type(sys.exc_info()[1]).__name__, file=sys.stderr, flush=True)


def make_handler(engine, token, port):
    allowed_hosts = {'127.0.0.1:%d' % port, 'localhost:%d' % port}

    class Handler(BaseHTTPRequestHandler):
        timeout = 60
        server_version = 'snippy-helper'
        sys_version = ''

        def log_message(self, *_):
            pass

        def reply(self, status, body):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self.reply(405, {'ok': False, 'kind': 'bad_input', 'message': 'Use POST.'})

        def do_POST(self):
            # Web pages send Origin, and a rebinding page sends a foreign Host. Neither gets in.
            if self.client_address[0] != HOST or self.headers.get('Host') not in allowed_hosts \
                    or self.headers.get('Origin') is not None:
                self.reply(403, {'ok': False, 'kind': 'forbidden', 'message': 'Local helper only.'})
                return
            supplied = self.headers.get('Authorization', '')
            if not hmac.compare_digest(supplied.encode(), ('Bearer ' + token).encode()):
                self.reply(401, {'ok': False, 'kind': 'bad_token', 'message': 'Wrong helper access token.'})
                return
            name = self.path[len('/tool/'):] if self.path.startswith('/tool/') else ''
            if name not in TOOLS:
                self.reply(404, {'ok': False, 'kind': 'bad_input', 'message': 'No such tool.'})
                return
            try:
                length = int(self.headers.get('Content-Length', ''))
            except ValueError:
                length = -1
            if length < 0:
                self.reply(400, {'ok': False, 'accepted': False, **reason('bad_input')})
                return
            if length > MAX_BODY_BYTES:
                # Read and discard the oversize body (up to 256 MB) so the caller is still listening and gets this plain answer, not a dropped connection.
                left = min(length, 256 * 1024 * 1024)
                while left > 0:
                    chunk = self.rfile.read(min(left, 1024 * 1024))
                    if not chunk:
                        break
                    left -= len(chunk)
                self.close_connection = True
                self.reply(413, {'ok': False, 'accepted': False, **reason('too_big')})
                return
            try:
                args = json.loads(self.rfile.read(length) or b'{}')
            except (ValueError, UnicodeDecodeError):
                args = None
            if not isinstance(args, dict):
                self.reply(400, {'ok': False, 'accepted': False, **reason('bad_input')})
                return
            self.reply(200, getattr(engine, TOOLS[name])(args))

    return Handler


def data_dir_path():
    return Path(os.environ.get('SNIPPY_HELPER_DATA') or default_data_dir()).expanduser()


def helper_port():
    return int(os.environ.get('SNIPPY_HELPER_PORT') or DEFAULT_PORT)


def ensure_token(path):
    """A random local access token in a 0600 file. Kept across restarts."""
    if path.is_file() and private_enough(path):
        token = path.read_text(encoding='utf-8').strip()
        if len(token) >= 32:
            return token
    token = secrets.token_urlsafe(32)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(4) + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
        handle.write(token)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return token


def fail(message):
    print('Snippy helper cannot start: ' + message, file=sys.stderr, flush=True)
    return 2


def main():
    # pythonw (the Windows login item) has no console: stdout/stderr are None. Send them to a log, never lose them silently.
    if sys.stdout is None or sys.stderr is None:
        log_dir = config_dir() / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / 'daemon.log', 'a', buffering=1, encoding='utf-8')
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    try:
        port = helper_port()
    except ValueError:
        return fail('SNIPPY_HELPER_PORT is not a number.')
    if port in BLOCKED_PORTS or not 1024 <= port <= 65535:
        return fail('port %d is not allowed.' % port)
    image_url = os.environ.get('SNIPPY_IMAGE_URL') or DEFAULT_IMAGE_URL
    if not check_image_url(image_url):
        return fail('SNIPPY_IMAGE_URL must be OpenAI\'s edits address or http://127.0.0.1.')
    reserve = parse_money(os.environ.get('SNIPPY_RESERVE_USD') or DEFAULT_RESERVE_USD)
    if reserve is None or reserve <= 0:
        return fail('SNIPPY_RESERVE_USD must be a positive amount.')
    try:
        timeout = float(os.environ.get('SNIPPY_IMAGE_TIMEOUT') or DEFAULT_TIMEOUT_SECONDS)
    except ValueError:
        return fail('SNIPPY_IMAGE_TIMEOUT is not a number.')
    if not 0 < timeout <= 600:
        return fail('SNIPPY_IMAGE_TIMEOUT must be between 0 and 600 seconds.')
    key_file = os.environ.get('SNIPPY_KEY_FILE') or default_key_file()

    data_dir = data_dir_path()
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if sys.platform != 'win32':
        os.chmod(data_dir, 0o700)
    # Windows: chmod 0o700 does nothing there (no Unix permission bits). The folder is private because it sits in
    # the per-user %APPDATA% folder, which other Windows accounts cannot open.
    # Two helpers on one ledger could spend twice, so only one may hold this lock.
    # 'a' not 'w': a second helper must not truncate the file the first one has locked.
    lock_handle = open(data_dir / 'daemon.lock', 'a')
    try:
        lock_exclusive(lock_handle)
    except LockUnavailable as error:
        return fail('%s. Nothing was started.' % error)
    except OSError:
        return fail('another helper is already using this data folder.')
    # The setup tool reads this to stop the helper on Windows (no launchctl there). Written only after the lock is ours.
    (data_dir / 'daemon.pid').write_text(str(os.getpid()), encoding='utf-8')
    try:
        engine = Engine(data_dir, image_url, key_file, reserve, timeout)
    except (ValueError, KeyError, TypeError, OSError) as error:
        return fail('the saved ledger could not be read (' + type(error).__name__ + '). Nothing was run.')
    token = ensure_token(data_dir / 'access-token')
    try:
        server = QuietServer((HOST, port), make_handler(engine, token, port))
    except OSError as error:
        return fail('could not listen on %s:%d (%s).' % (HOST, port, type(error).__name__))
    threading.Thread(target=engine.worker, name='snippy-worker', daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    if not os.environ.get(KEY_ENV) and not Path(key_file).expanduser().is_file():
        print(NO_KEY_MESSAGE, file=sys.stderr, flush=True)
    print('Snippy helper listening on %s:%d' % (HOST, port), flush=True)
    server.serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main())
