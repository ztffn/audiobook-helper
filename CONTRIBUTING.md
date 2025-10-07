Contributing
============

Thanks for considering a contribution! This repo is a small, dependency‑light TUI that wraps `audiobook-dl` and `ffmpeg` with safe fallbacks. A few guardrails to help you get productive quickly.

Project layout

- `scripts/audiobook_easy.py` — main TUI. Fancy menus, inputs, spinner, preflight.
- `scripts/bootstrap_audiobook_helper.py` — installer wizard (Homebrew/pipx/audiobook-dl/ffmpeg).
- `scripts/concat_aac.py` — robust AAC part merger (demux or frames‑only rawcat).
- `scripts/make_audiobook.py` — single file + chapters + cover/metadata.
- `scripts/audiobook_pipeline.py` — orchestration for scripted workflows.

Dev setup

```
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install --upgrade pip
# External tools (macOS):
brew install pipx ffmpeg
pipx ensurepath
pipx install audiobook-dl
```

Run the TUI

```
python3 scripts/audiobook_easy.py
```

Coding guidelines

- Keep the TUI primitives (menu/prompt/spinner) simple and self‑contained.
- Favor readable, defensive code over cleverness. When in doubt, log to the spinner’s temp file.
- Never block on network; timeouts should be short and errors should lead to clear user choices.
- Secrets (passwords) must stay in macOS Keychain; never print them.
- Keep colors 256‑palette friendly.

Key design notes

- Fallback merge: when providers export thousands of tiny AAC segments, the concat demuxer can fail. The frames‑only scanner in `concat_aac.py` ensures we only write valid ADTS frames, then re‑encode/remux to `.m4a`.
- Chapters: `make_audiobook.py` builds ffmetadata with millisecond timebase to preserve chapter navigation across players.
- Updates: preflight opportunistically checks for newer audiobook‑dl/ffmpeg and prompts to update. It skips quietly offline.

Submitting changes

1. Open a small PR focused on one change. Include a short rationale.
2. Confirm the TUI still runs end‑to‑end:
   - Preflight OK
   - One happy‑path download
   - Fallback merge path (can be simulated by removing `--combine`)
3. If you change colors or terminal behavior, validate on stock macOS Terminal.

Releases

- Package app Resources: `zsh scripts/package_app.sh`
- Create DMG: `hdiutil create -volname "Audiobook Helper" -srcfolder "Audiobook Helper.app" -ov -format UDZO "AudiobookHelper.dmg"`

Thanks again — small improvements are very welcome!

