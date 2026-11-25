---
name: set-up-snippy
description: Guided setup of Snippy Pieces on an artist's own Mac or Windows PC. Use when the artist says "set up Snippy" (or Snippy is not working after install). Walks one missing step at a time in plain words.
---

# Set up Snippy

Snippy Pieces cuts a collage picture into separate pieces using the artist's own OpenAI account. This skill sets it up on this computer, a Mac or a Windows PC. If it is neither (for example Linux), say so and stop.

Setup script: `tools/snippy-pieces/setup/snippy_setup.py` in this folder (the repo root). Run it with `python3 -B <that path> <command>` on a Mac. On Windows use the Python launcher the same way: try `py -3 --version` first, and if that fails try `python --version` (if it opens the Microsoft Store or prints nothing useful, Python is not installed: see step 2). Use whichever worked, as `py -3 -B <path> <command>` or `python -B <path> <command>`. Below, `<command>` means that whole line. Claude runs every command itself. Never hand the artist Terminal commands to type.

## Rules (always)
- Never ask the artist to paste the OpenAI key into the chat. The key goes only into the private box that the `key` command opens. If they paste it in chat anyway, tell them to make a new key and do not repeat or save it.
- Never run a paid request during setup. The `key` check is free (it only lists models).
- The first test piece (about 6 cents on their own OpenAI account) happens only if the artist says yes in plain words, and it is NOT part of setup. Offer it once at the end, do not start it.
- Say what each step does in one short line before you do it. Plain words, no jargon.
- If any command fails (non-zero exit or a PROBLEM line), tell the artist what it said in plain words, fix that one thing, and do not skip ahead.

## Steps

1. Say in one line: "I will set up Snippy on this computer so you can cut collage pieces with your own OpenAI account. It takes about five minutes."
2. If Python is missing:
   - Mac (`python3` fails, or the Mac shows a box asking to install developer tools): "Your Mac will show a box asking to install some free Apple tools. Click Install and wait until it finishes, then say 'carry on'."
   - Windows (neither `py -3` nor `python` works): "Please install Python from https://www.python.org/downloads/ : press the big yellow Download button, open the file, and on the first screen tick the box **Add python.exe to PATH**, then press Install Now. When it says Setup was successful, close it and say 'carry on'." If typing `python` opens the Microsoft Store instead, that only means Python is not installed yet: install it from python.org as above.
   Do nothing else until they say so, then run `check` again to confirm it works.
3. Run `check`. Show the artist a short plain list of what is done and what is missing. Only do the steps that are missing.
4. **OpenAI key** (if missing). Tell them: open https://platform.openai.com/api-keys, sign in, press "Create new secret key", and keep that page open. Suggest they also set a monthly spending limit on their OpenAI account (Settings, Billing, Limits) so a surprise cannot cost them. Then run `key`. A private box opens on screen (on Windows a small window titled Snippy setup with one line for the key, or, if that cannot open, a small box asking for a user name and a password: the key goes in the password line and the user name stays as it is; setup tells you which of the two it used): they paste the key there (it shows dots or stars; if a user name is asked for, leave it as it is). Wait for the result. 401 means the key is wrong: ask them to make a new one and run `key` again. "Could not reach OpenAI" means check the internet, then run `key` again.
5. **Name, Drive folder, spending limit** (if missing). Ask their name (used as their folder name). Run `drive-find` and show the list in plain words; ask which one is where your team wants finished pieces (a shared Google Drive folder your team uses). If it says Google Drive for desktop is not found, give them its install link and wait. Ask for a spending limit for Snippy on this computer in US dollars: Snippy stops when its total spend reaches it and does not reset by itself (about 6 cents a piece: 20 dollars is roughly 300 pieces). Pick an output folder on this computer, by default `~/Documents/Snippy Pieces` (always give a full path or one starting with `~/`, never a bare relative name). Then run `settings --artist "<name>" --drive-folder "<path>" --output-folder "<path>" --budget <amount>`. Tell them: "Your page shows this limit as 'Your Snippy limit' and Snippy stops by itself when it is used up. If you ever need more, tell me and I will raise it." To raise it later, Claude runs `settings --budget <new total in dollars>` (it replaces the old limit with the new total, it does not add to it), then tells them to try the cut again.
6. Run `install` (copies the helper to a private folder on this computer).
7. Run `login-item` (starts the helper by itself whenever they log in; they can remove it later). On Windows this puts one small file in their own Startup folder and starts the helper right away with no window; nothing needs administrator rights.
8. Run `connect-claude` (adds Snippy to the Claude app). If it refuses because a different snippy-engine entry exists, ask the artist before using `--replace`. If it refuses because the Claude config is unreadable, do not touch it: tell them and ask a teammate.
8b. Windows only: if `connect-claude` says to open the Claude app once, ask them to open Claude, close it again, and run it again. If they installed Claude from the Microsoft Store, setup finds that copy by itself; nothing for them to do.
9. Tell them to quit the Claude app fully and open it again (Windows: closing the window is not enough; right-click the Claude icon near the clock and choose Quit, then open it again).
10. **Publish their own Snippy page** (only if the artist has none yet: ask them, or look for a "Snippy Pieces" page of theirs). First read `tools/snippy-pieces/page/index.html` and `tools/snippy-pieces/page/README-page.md` (the page ships with this repo, you did not write it). Then, with the Artifact tool, publish `tools/snippy-pieces/page/index.html` plus `tools/snippy-pieces/page/a/hero/09-pieces.jpg` beside it (same relative path `a/hero/09-pieces.jpg`) as a NEW private artifact on the artist's own Claude account, titled "Snippy Pieces", declaring the capabilities README-page.md lists: db, assets, downloads, and mcp with the server host `snippy-engine`. Never publish over an existing page of someone else's. Open it for them and say: "This page is private to you unless you share it. Please do not share the link: viewers would see your spend and folder names."
11. Run `check` again. If everything is ok, say "Snippy is ready." Then offer once: "Want to try one test piece? It costs about 6 cents on your OpenAI account." Do nothing more unless they say yes.
12. Last line to say: "To cut your own collage: drop it on your page, then tell me 'outline my new collage'."

## To undo
`uninstall` stops the helper and removes the login item and the Claude entry and keeps their key, settings and pieces. `uninstall --all` also removes the key, settings and helper records (never the finished pieces). Ask before running `--all`.
