Audiobook Helper (macOS App)

Purpose

- A friendly macOS wrapper for audiobook-dl with a guided interface and built‑in installer to help non‑technical users download audiobooks for offline use.
- Robust merge feature to combine many small audio parts into a single playable file with chapters and cover art.

Features

- Download via `[audiobook-dl](url=https://github.com/jo1gi/audiobook-dl)` (Nextory, Storytel, Audible, BookBeat, etc.).
- Auto‑fallback merge when providers export many tiny AAC segments.
- Optional cover + metadata fetch by ISBN (Google Books / Open Library).
- Remembers output location and login (passwords in macOS Keychain).
- Bootstrap wizard to install prerequisites on a fresh Mac.

Download

- Get the latest DMG: https://github.com/ztffn/audiobook-helper/releases/latest
- Open the DMG and drag “Audiobook Helper.app” to Applications.
- First launch: if macOS blocks, right‑click → Open → Open (Gatekeeper bypass once).

Alternative (advanced):

- Command‑line usage via Python scripts remains available in `scripts/` for power users.

Packaging

- macOS app: see `scripts/package_app.sh` (copies scripts into the app bundle Resources).
- DMG: `hdiutil create -volname "Audiobook Helper" -srcfolder "Audiobook Helper.app" -ov -format UDZO "AudiobookHelper.dmg"`

Notes

- The UI uses a minimal 256‑color theme for broad terminal compatibility.
- Fallback merge uses a robust ADTS frame scanner and re‑encode to stabilize long, noisy part sets.
