Audiobook Helper (TUI)

Purpose

- Personal backup and offline access for audiobooks you already have access to.
- Simple, friendly terminal UI (arrow keys + Enter) with automatic fallbacks when sources are tricky.

Features

- Download via `audiobook-dl` (Nextory, Storytel, Audible, BookBeat, etc.).
- Auto‑fallback merge when providers export many tiny AAC segments.
- Optional cover + metadata fetch by ISBN (Google Books / Open Library).
- Remembers output location and login (passwords in macOS Keychain).
- Bootstrap wizard to install prerequisites on a fresh Mac.

Quick Start

1) Install prerequisites (Homebrew recommended):

```
brew install pipx ffmpeg
pipx ensurepath
pipx install audiobook-dl
```

2) Run the easy helper:

```
python3 scripts/audiobook_easy.py
```

Packaging

- macOS app: see `scripts/package_app.sh` (copies scripts into the app bundle Resources).
- DMG: `hdiutil create -volname "Audiobook Helper" -srcfolder "Audiobook Helper.app" -ov -format UDZO "AudiobookHelper.dmg"`

Notes

- The UI uses a minimal 256‑color theme for broad terminal compatibility.
- Fallback merge uses a robust ADTS frame scanner and re‑encode to stabilize long, noisy part sets.

