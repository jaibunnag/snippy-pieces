# Snippy Pieces

![Snippy Pieces cutting a collage into separate pieces](tools/snippy-pieces/page/a/hero/09-pieces.jpg)

Snippy Pieces cuts a collage picture into separate pieces. You drop a collage on your own private page, Claude draws the outlines, you check them, and the pieces are cut and saved on your computer (and copied into a shared Google Drive folder your team uses, if you pick one).

**Platforms:** Mac (tested) and Windows (built, not yet tried on a real Windows PC).

**Cost:** it uses your own OpenAI key, about 6 cents a piece. You set a spending limit, and Snippy stops when it is used up. Set a monthly limit on your OpenAI account too. Your OpenAI key stays on your computer; never paste it in chat.

## Start
1. Click Code, then Download ZIP (or `git clone`), and unzip it.
2. Open Claude Code (or the Code tab of the Claude desktop app) in that folder.
3. Say: **set up Snippy**

Later: **outline my new collage**. To undo everything: **remove Snippy**.

You will need Python 3 (on a Mac, free Apple tools; Claude tells you what to click), Google Drive for desktop signed in (only to copy pieces to a shared folder), and an OpenAI account.

## What setup installs
- A private folder (Mac: `~/Library/Application Support/SnippyPieces/`, Windows: `%APPDATA%\SnippyPieces\`) with the helper program, your key (private file) and your settings (name, folders, spending limit).
- A login item that starts the helper when you log in (Mac: `~/Library/LaunchAgents/com.snippypieces.helper.plist`; Windows: `SnippyPieces.cmd` in your own Startup folder, which runs with no window and needs no administrator rights).
- An entry named `snippy-engine` in the Claude app's config file, so Claude can use Snippy (a backup of the file is made first).

Nothing is installed outside those. Setup never spends money; the first test piece (about 6 cents) happens only if you say yes.

## Remove it
Say **remove Snippy**. The helper stops, the login item and the Claude entry go; your key, settings and pieces stay. Ask for the full removal (`uninstall --all`) to remove the key and settings too. Finished pieces are never deleted.

## What is in this repo
- `tools/snippy-pieces/helper/` the helper (Python standard library only)
- `tools/snippy-pieces/setup/snippy_setup.py` the setup commands Claude runs for you
- `tools/snippy-pieces/page/` your private Snippy page
- `.claude/skills/` the steps Claude follows (`set-up-snippy`, `outline-snippy`)

## Developers
Tests (on Windows use `py -3` instead of `python3`):

    python3 -B tools/snippy-pieces/setup/test_setup.py
    SNIPPY_IMAGE_URL=http://127.0.0.1:18811/v1/images/edits python3 -B tools/snippy-pieces/helper/test_helper.py
    python3 -B tools/snippy-pieces/ship_check.py

The setup tests use a fake home and never touch real files; the helper tests use a fake image service. `ship_check.py` scans the repo for leftovers that must not be published.

Licence: MIT.
