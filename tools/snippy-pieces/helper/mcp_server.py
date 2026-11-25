#!/usr/bin/env python3
"""snippy-engine: a stdio MCP server that forwards five tools to the local Snippy helper."""
import http.client
import json
import os
from pathlib import Path
import sys

HOST = '127.0.0.1'
DEFAULT_PORT = 18830  # the kit's own port; 18810 is the owner's Snippy helper


def default_data_dir():
    """Same per-user config folder the helper uses (kept in step with daemon.py config_dir)."""
    override = os.environ.get('SNIPPY_CONFIG_DIR')
    if override:
        return Path(override).expanduser() / 'helper'
    if sys.platform == 'win32':
        appdata = os.environ.get('APPDATA')
        return (Path(appdata) if appdata else Path.home() / 'AppData' / 'Roaming') / 'SnippyPieces' / 'helper'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'SnippyPieces' / 'helper'
    return Path.home() / '.config' / 'snippy-pieces' / 'helper'


SERVER_NAME = 'snippy-engine'
SERVER_VERSION = '1.0.0'
PROTOCOL_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
# A start_job can carry a whole source image, so allow for a slow local write.
CALL_TIMEOUT_SECONDS = 60

JOB_ID = {'job_id': {'type': 'string', 'description': 'The Snippy piece id.'}}
TOOLS = [
    {'name': 'status',
     'description': 'Check that the Snippy helper is running. Shows whether it is paused, why, and what is held.',
     'inputSchema': {'type': 'object', 'properties': {}},
     'annotations': {'title': 'Snippy status', 'readOnlyHint': True, 'destructiveHint': False,
                     'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'start_job',
     'description': 'Queue price-approved cutting jobs: one job, or a whole batch in jobs. Jobs run one at a time, '
                    'in order, after the page closes. Spends real credit. Refused without a price approval.',
     'inputSchema': {'type': 'object', 'required': ['job_id'], 'properties': {
         **JOB_ID,
         'approval': {'type': 'object', 'description': 'The price approval from Extract, Retry or Resume.',
                      'properties': {'approval_id': {'type': 'string'}, 'piece_count': {'type': 'integer'},
                                     'approved_usd': {'type': 'number'},
                                     'stop_mark': {'type': 'string',
                                                   'description': 'The latest Stop this approval was given after.'},
                                     'kind': {'type': 'string', 'description': "'recut' for a re-cut of a finished piece."},
                                     'resume_pause_id': {'type': 'string',
                                                         'description': 'To Resume: the pause_id from status.'}}},
         'balance_usd': {'type': 'number', 'description': 'Remaining balance shown to the user.'},
         'counted_job_ids': {'type': 'array', 'items': {'type': 'string'},
                             'description': 'Required. Every job whose charge balance_usd already includes.'},
         'source_id': {'type': 'string'}, 'region_id': {'type': 'string'}, 'name': {'type': 'string'},
         'reconstruct': {'type': 'boolean'}, 'comment': {'type': 'string'},
         'overall_comment': {'type': 'string'}, 'source_image': {'type': ['string', 'null']},
         'source_image_data': {'type': 'string', 'description': 'The source image as a base64 data URL.'},
         'box': {'type': 'array', 'items': {'type': 'number'}, 'description': 'x, y, width, height in percent.'},
         'reconstruction_reason': {'type': 'string'}, 'source_note': {'type': 'string'},
         'source_name': {'type': 'string', 'description': "The picture's name: finished pieces are copied into a folder of this name."},
         'recut_note': {'type': 'string', 'description': 'A re-cut only: what was wrong with the first cut.'},
         'jobs': {'type': 'array', 'items': {'type': 'object'},
                  'description': 'A batch: each item is one job with its own job_id and approval. '
                                 'balance_usd and counted_job_ids are shared. The reply lists each job.'},
         'images': {'type': 'object', 'additionalProperties': {'type': 'string'},
                    'description': 'For a batch: source_id to the source image as a base64 data URL, sent once.'}}},
     'annotations': {'title': 'Start a paid cutting job', 'readOnlyHint': False, 'destructiveHint': False,
                     'idempotentHint': False, 'openWorldHint': True}},
    {'name': 'job_status',
     'description': 'Read the state of one job, with the reason and next step if it stopped.',
     'inputSchema': {'type': 'object', 'required': ['job_id'], 'properties': JOB_ID},
     'annotations': {'title': 'Snippy job status', 'readOnlyHint': True, 'destructiveHint': False,
                     'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'get_result',
     'description': 'Read a finished PNG as base64, one chunk at a time. Pass next_offset to continue.',
     'inputSchema': {'type': 'object', 'required': ['job_id'],
                     'properties': {**JOB_ID, 'offset': {'type': 'integer', 'minimum': 0}}},
     'annotations': {'title': 'Read a finished image', 'readOnlyHint': True, 'destructiveHint': False,
                     'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'cancel',
     'description': 'Cancel a job. A request already sent may still be charged.',
     'inputSchema': {'type': 'object', 'required': ['job_id'], 'properties': JOB_ID},
     'annotations': {'title': 'Cancel a job', 'readOnlyHint': False, 'destructiveHint': True,
                     'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'stop_all',
     'description': 'Stop everything. Returns unsent pieces at no charge and aborts the request in flight, which may '
                    'still be charged. The stop survives restarts until a new price approval resumes it.',
     'inputSchema': {'type': 'object', 'properties': {}},
     'annotations': {'title': 'Stop all Snippy work', 'readOnlyHint': False, 'destructiveHint': True,
                     'idempotentHint': True, 'openWorldHint': False}},
    {'name': 'resolve_hold',
     'description': 'Record what billing showed for a held piece. Changes the ledger only: it never sends an image '
                    'request and never re-queues the piece. A retry still needs a new price approval.',
     'inputSchema': {'type': 'object', 'required': ['job_id', 'outcome'], 'properties': {
         **JOB_ID,
         'outcome': {'type': 'string', 'enum': ['not_charged', 'charged']},
         'charged_usd': {'type': 'number', 'exclusiveMinimum': 0,
                         'description': 'Only with charged. Leave out to settle at the reserved amount.'}}},
     'annotations': {'title': 'Resolve a held charge', 'readOnlyHint': False, 'destructiveHint': True,
                     'idempotentHint': True, 'openWorldHint': False}},
]
TOOL_NAMES = {tool['name'] for tool in TOOLS}


def down_reply(tool):
    reply = {'ok': False, 'kind': 'daemon_down',
             'message': 'The Snippy helper is not answering on this computer. Your approved work is saved.',
             'next_step': 'Wait 15 seconds, then press Check again. If this stays, tell a teammate.'}
    if tool == 'start_job':
        reply['accepted'] = False
    if tool == 'get_result':
        reply.update(data='', done=False)
    return reply


def forward(tool, arguments):
    data_dir = Path(os.environ.get('SNIPPY_HELPER_DATA') or default_data_dir()).expanduser()
    try:
        port = int(os.environ.get('SNIPPY_HELPER_PORT') or DEFAULT_PORT)
        token = (data_dir / 'access-token').read_text(encoding='utf-8').strip()
    except (OSError, ValueError):
        return down_reply(tool)
    connection = http.client.HTTPConnection(HOST, port, timeout=CALL_TIMEOUT_SECONDS)
    try:
        connection.request('POST', '/tool/' + tool, body=json.dumps(arguments).encode(),
                           headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        response = connection.getresponse()
        reply = json.loads(response.read())
    except (OSError, http.client.HTTPException, ValueError):
        return down_reply(tool)
    finally:
        connection.close()
    if not isinstance(reply, dict):
        return down_reply(tool)
    return reply


def call_tool(params):
    name = params.get('name')
    arguments = params.get('arguments') or {}
    if name not in TOOL_NAMES or not isinstance(arguments, dict):
        return {'content': [{'type': 'text', 'text': json.dumps({'ok': False, 'kind': 'bad_request'})}],
                'structuredContent': {'ok': False, 'kind': 'bad_request'}, 'isError': True}
    reply = forward(name, arguments)
    result = {'content': [{'type': 'text', 'text': json.dumps(reply)}], 'structuredContent': reply}
    # A failed read must not look like an empty picture.
    if name == 'get_result' and reply.get('kind') == 'daemon_down':
        result['isError'] = True
    return result


def handle(message):
    method = message.get('method')
    params = message.get('params') or {}
    if method == 'initialize':
        asked = params.get('protocolVersion')
        return {'protocolVersion': asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': {'name': SERVER_NAME, 'version': SERVER_VERSION}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': TOOLS}
    if method == 'tools/call':
        return call_tool(params)
    raise LookupError(method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
            sys.stdout.write(json.dumps(reply) + '\n')
            sys.stdout.flush()
            continue
        if not isinstance(message, dict) or 'id' not in message:
            continue  # Notifications get no reply.
        try:
            reply = {'jsonrpc': '2.0', 'id': message['id'], 'result': handle(message)}
        except LookupError:
            reply = {'jsonrpc': '2.0', 'id': message['id'],
                     'error': {'code': -32601, 'message': 'Method not found'}}
        except Exception as error:
            reply = {'jsonrpc': '2.0', 'id': message['id'],
                     'error': {'code': -32603, 'message': 'Internal error: ' + type(error).__name__}}
        sys.stdout.write(json.dumps(reply) + '\n')
        sys.stdout.flush()


if __name__ == '__main__':
    main()
