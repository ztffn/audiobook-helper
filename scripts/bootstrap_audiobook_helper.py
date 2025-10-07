#!/usr/bin/env python3
#
# Bootstrap Installer (fancy TUI)
#
# Purpose: set up required command‑line tools on a fresh macOS machine with
# user consent. We keep every step explicit (Install / Skip) and never hide
# commands. Use short, readable actions so contributors can extend steps.
#
import os
import shutil
import subprocess
import sys
from pathlib import Path
import curses


APP_BUNDLE = Path.home() / "Ripping" / "Audiobook Helper.app"


def run(cmd, check=True):
    print("$", " ".join(cmd))
    return subprocess.run(cmd, check=check)


def which(name: str) -> bool:
    return shutil.which(name) is not None


THEME = {"bg": 236, "fg": 252, "accent": 180}
_INIT = {"done": False}

def _setup_colors(stdscr):
    """Minimal 256‑color theme so the wizard looks consistent without coupling
    to the downloader theme logic. Favor portability over aesthetics here.
    """
    if _INIT["done"]:
        return
    try:
        curses.start_color(); curses.use_default_colors()
        curses.init_pair(1, THEME["fg"], THEME["bg"])  # base
        curses.init_pair(2, THEME["accent"], THEME["bg"])  # title
        curses.init_pair(3, THEME["bg"], THEME["fg"])  # highlight
    except Exception:
        pass
    _INIT["done"] = True

def _fancy_menu(title: str, body: str, options: list[str], default_idx: int = 0):
    def _menu(stdscr):
        _setup_colors(stdscr)
        try:
            curses.curs_set(0)
        except Exception:
            pass
        stdscr.keypad(True)
        idx = max(0, min(default_idx, len(options) - 1))
        while True:
            try:
                stdscr.bkgdset(' ', curses.color_pair(1))
                stdscr.erase()
                h, w = stdscr.getmaxyx()
                stdscr.addstr(h-1, max(0, w-1), " ", curses.color_pair(1))
                stdscr.refresh()
            except Exception:
                stdscr.clear()
            # Banner / Title
            stdscr.addstr(0, 0, "Audiobook Helper — Installer", curses.color_pair(2))
            stdscr.addstr(2, 0, title, curses.color_pair(2))
            # Body text lines
            row = 4
            for line in body.splitlines():
                stdscr.addstr(row, 0, line, curses.color_pair(1))
                row += 1
            row += 1
            for i, opt in enumerate(options):
                if i == idx:
                    stdscr.attron(curses.color_pair(3))
                stdscr.addstr(row + i, 2, opt)
                if i == idx:
                    stdscr.attroff(curses.color_pair(3))
            stdscr.addstr(row + len(options) + 2, 0, "Use ↑/↓ to move, Enter to select", curses.color_pair(1))
            ch = stdscr.getch()
            if ch in (curses.KEY_UP, ord('k')):
                idx = (idx - 1) % len(options)
            elif ch in (curses.KEY_DOWN, ord('j')):
                idx = (idx + 1) % len(options)
            elif ch == curses.KEY_RESIZE:
                continue
            elif ch in (10, 13, curses.KEY_ENTER):
                return idx
            elif ch in (27, ord('q')):
                return None
    try:
        return curses.wrapper(_menu)
    except Exception:
        return None


def press_enter(msg: str = "Press Enter to continue…"):
    # Fancy continue screen; fallback to plain input
    title = "Welcome"
    body = (
        "This wizard will set up required tools and install the app.\n\n"
        "You can cancel any step and return later."
    )
    idx = _fancy_menu(title, body, [msg], default_idx=0)
    if idx is None:
        input(msg)


def step_confirm(title: str, body: str, ok_text: str = "OK", cancel_text: str = "Cancel") -> bool:
    # Fancy menu Yes/No
    idx = _fancy_menu(title, body, [ok_text, cancel_text], default_idx=0)
    if idx is None:
        # Fallback plain prompt
        print(f"\n{title}\n{'-'*len(title)}\n{body}")
        val = input(f"[{ok_text}/{cancel_text}]: ").strip().lower()
        return True if val in ("", "ok", "o", "y", "yes") else False
    return idx == 0


def install_homebrew() -> bool:
    if which("brew"):
        print("✓ Homebrew is already installed.")
        return True
    title = "Step 1/4 — Install Homebrew"
    body = (
        "Homebrew is a popular package manager for macOS.\n"
        "We use it to install ffmpeg (needed for merging and tagging audio)\n"
        "and optionally pipx.\n\n"
        "We can run the official installer now, or you can skip and install later."
    )
    if not step_confirm(title, body, ok_text="Install", cancel_text="Skip"):
        print("Skipping Homebrew.")
        return False
    print("Launching Homebrew installer…")
    try:
        cmd = [
            "/bin/bash",
            "-c",
            "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)",
        ]
        subprocess.run(" ".join(cmd), shell=True, check=True)
        print("✓ Homebrew installed.")
        return True
    except subprocess.CalledProcessError:
        print("Homebrew install failed. You can try again or visit https://brew.sh")
        return False


def install_pipx() -> bool:
    if which("pipx"):
        print("✓ pipx is already installed.")
        return True
    title = "Step 2/4 — Install pipx"
    body = (
        "pipx installs Python CLI tools in isolated environments.\n"
        "We use it to install audiobook-dl cleanly."
    )
    if not step_confirm(title, body, ok_text="Install", cancel_text="Skip"):
        print("Skipping pipx.")
        return False
    if which("brew"):
        try:
            run(["brew", "install", "pipx"])
            run(["pipx", "ensurepath"], check=False)
            print("✓ pipx installed.")
            return True
        except subprocess.CalledProcessError:
            print("pipx install via Homebrew failed.")
    # Fallback: try pip user install
    try:
        run([sys.executable, "-m", "pip", "install", "--user", "pipx"])
        run(["pipx", "ensurepath"], check=False)
        print("✓ pipx installed.")
        return True
    except subprocess.CalledProcessError:
        print("pipx installation failed. You can install later with 'brew install pipx' or 'python3 -m pip install --user pipx'.")
        return False


def install_audiobook_dl() -> bool:
    if which("audiobook-dl"):
        print("✓ audiobook-dl is already installed.")
        return True
    title = "Step 3/4 — Install audiobook-dl"
    body = (
        "audiobook-dl downloads audiobooks from supported services.\n"
        "We use it for the one-click experience."
    )
    if not step_confirm(title, body, ok_text="Install", cancel_text="Skip"):
        print("Skipping audiobook-dl.")
        return False
    if which("pipx"):
        try:
            run(["pipx", "install", "audiobook-dl"])
            print("✓ audiobook-dl installed.")
            return True
        except subprocess.CalledProcessError:
            print("audiobook-dl install via pipx failed.")
    # Fallback: pip user install
    try:
        run([sys.executable, "-m", "pip", "install", "--user", "--upgrade", "audiobook-dl"])
        print("✓ audiobook-dl installed (user site).")
        return True
    except subprocess.CalledProcessError:
        print("audiobook-dl installation failed.")
        return False


def install_ffmpeg() -> bool:
    if which("ffmpeg") and which("ffprobe"):
        print("✓ ffmpeg/ffprobe are already installed.")
        return True
    title = "Step 4/4 — Install ffmpeg"
    body = (
        "ffmpeg is required to merge audio, add chapters, and embed covers.\n"
        "We recommend installing via Homebrew."
    )
    if not step_confirm(title, body, ok_text="Install", cancel_text="Skip"):
        print("Skipping ffmpeg.")
        return False
    if which("brew"):
        try:
            run(["brew", "install", "ffmpeg"])
            print("✓ ffmpeg installed.")
            return True
        except subprocess.CalledProcessError:
            print("ffmpeg install via Homebrew failed.")
    print("If Homebrew is not available, download ffmpeg for macOS from:\n  https://evermeet.cx/ffmpeg/ or https://ffmpeg.org/download.html#build-mac")
    return False


# No app bundle copy step; DMG installation will handle this.


def main():
    press_enter()

    hb = install_homebrew()
    px = install_pipx()
    adl = install_audiobook_dl()
    ff = install_ffmpeg()
    # App copy is handled by DMG manual install; no step here.

    # Fancy verification screen
    items = [
        ("brew", which("brew")),
        ("pipx", which("pipx")),
        ("audiobook-dl", which("audiobook-dl")),
        ("ffmpeg", which("ffmpeg")),
        ("ffprobe", which("ffprobe")),
    ]
    lines = []
    for name, ok in items:
        lines.append(f"{'✓' if ok else '✗'} {name}")
    _fancy_menu("Verification", "\n".join(lines) + "\n\nYou can re-run this installer any time.", ["Finish"], default_idx=0)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
