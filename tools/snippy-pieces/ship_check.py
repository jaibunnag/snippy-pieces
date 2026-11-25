#!/usr/bin/env python3
"""Ship check for the Snippy Pieces artist kit (stdlib only).

Scans every text file in the kit for things that must never reach an artist:
home paths, the Claude app folder, keys, emails, money caps/grants/balances and artifact ids,
plus an optional private list (env SNIPPY_PRIVATE_PATTERNS, a file outside the repo). Prints file:line for each hit
and exits 1 if there is any; exits 0 when clean.

Run:  python3 ship_check.py [kit_folder]    (default: the kit this file sits in)
"""
import os
import re
import sys

KIT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))

RULES = [
    ('home path', re.compile(r'/Users/\w')),
    ('Windows home path', re.compile(r'[A-Za-z]:\\Users\\\w')),
    ('Claude app folder', re.compile(r'Library/Application Support/Claude')),
    ('API key', re.compile(r'sk-[A-Za-z0-9_\-]{20,}')),
    ('money next to cap/grant/balance',
     re.compile(r'(?:cap|grant|balance)\w*[^\n]{0,40}\$\s?(?!0\b)\d|\$\s?(?!0\b)\d[\d,.]*[^\n]{0,40}(?:cap|grant|balance)', re.I)),
    ('artifact id', re.compile(r'claude\.ai/artifact/[\w-]+')),
    ('email', re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+')),
]

# Optional private list: a file OUTSIDE the repo, one regex per line (names, ids you never want published).
PRIVATE = os.environ.get('SNIPPY_PRIVATE_PATTERNS')
if PRIVATE:
    with open(PRIVATE, encoding='utf-8') as _handle:
        for _line in _handle:
            _line = _line.strip()
            if _line and not _line.startswith('#'):
                RULES.append(('private pattern', re.compile(_line, re.I)))
else:
    print('ship_check: SNIPPY_PRIVATE_PATTERNS is not set, so the private name list was not checked (generic checks only).')

# Allowed hits: (file suffix inside the repo, rule name, text that must be on the line).
# Only the setup's own generic Claude config path (built from the artist's own home in code)
# and its test's fake key are allowed.
ALLOW = [
    ('setup/snippy_setup.py', 'Claude app folder', 'Library/Application Support/Claude'),
    ('setup/test_setup.py', 'API key', "FAKE_KEY = 'sk-test-"),
    ('setup/test_setup.py', 'email', 'GoogleDrive-a@b.com'),
]
SKIP_DIRS = {'__pycache__', '.git'}
SKIP_FILES = {'ship_check.py'}  # this file spells out the patterns it searches for


def is_text(path):
    with open(path, 'rb') as handle:
        chunk = handle.read(4096)
    if b'\0' in chunk:
        return False
    try:
        chunk.decode('utf-8')
    except UnicodeDecodeError as error:
        if error.start < len(chunk) - 4:
            return False
    return True


def allowed(rel, rule, line):
    for suffix, name, text in ALLOW:
        if rel.endswith(suffix) and name in ('*', rule) and text in line:
            return True
    return False


def main():
    hits = []
    for root, dirs, files in os.walk(KIT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in sorted(files):
            if name in SKIP_FILES:
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, KIT)
            if not is_text(path):
                continue
            with open(path, encoding='utf-8', errors='replace') as handle:
                for number, raw in enumerate(handle, 1):
                    # Picture data is random letters: mask it so it cannot fake a word hit.
                    line = re.sub(r'[A-Za-z0-9+/=]{100,}', '<DATA>', raw)
                    for rule, pattern in RULES:
                        if pattern.search(line) and not allowed(rel, rule, line):
                            hits.append('%s:%d: %s: %s' % (rel, number, rule, line.strip()[:140]))
    for hit in hits:
        print(hit)
    print('ship_check: %d hit(s)' % len(hits))
    return 1 if hits else 0


if __name__ == '__main__':
    sys.exit(main())
