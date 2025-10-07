#!/usr/bin/env python3
#
# Audiobook Helper (Easy)
#
# This is the user‑facing TUI. It intentionally avoids external deps and
# implements a small set of primitives (themed curses screens, menus, spinner)
# so contributors can reason about flow without a framework.
#
# Key design notes for contributors:
# - Curses color pairs are set once per screen via _setup_colors; the palette
#   is intentionally simple (fg/bg + accent + highlight) to keep portability.
# - We do not try to pixel‑perfect fill the entire terminal; background paint
#   uses bkgd/clear on each draw. If some terminals paint late, we prefer
#   simplicity over invasive workarounds.
# - Long‑running tasks use run_cmd_spinner(cmd, title) which logs stdout/err
#   to a temp file surfaced on the spinner screen for debugging.
# - Preflight is tolerant: if offline or commands are missing we present clear
#   actions and never block on network. Version checks are opportunistic.
# - Login persistence: usernames/paths live in a small JSON config; secrets
#   (passwords) are stored in macOS Keychain via `security`. Never print them.
# - Robustness: If audiobook‑dl combine fails but .aac parts were downloaded,
#   we fall back to a resilient local merge (rawcat + reencode) and proceed.
#
import os
import re
import sys
import shlex
import subprocess
from datetime import datetime
import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, List
import curses
import shutil
import webbrowser
import tempfile
import time
from urllib.parse import urlparse
import time as _time
from getpass import getpass
import json as _json


LIBRARY_HINTS = {
    "nextory.com": "nextory",
    "storytel": "storytel",
    "audible": "audible",
    "bookbeat": "bookbeat",
}


APP_NAME = "audiobook-helper"


def config_path() -> Path:
    base = Path.home() / "Library" / "Application Support" / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base / "config.json"


def load_config() -> Dict[str, Any]:
    p = config_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def save_config(cfg: Dict[str, Any]) -> None:
    p = config_path()
    p.write_text(json.dumps(cfg, indent=2))


def kc_get_password(service: str, account: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return (proc.stdout or "").strip()
    except Exception:
        pass
    return None


def kc_set_password(service: str, account: str, password: str, label: str) -> bool:
    try:
        proc = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                service,
                "-a",
                account,
                "-w",
                password,
                "-l",
                label,
            ],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0
    except Exception:
        return False


def detect_library(url: str) -> str:
    u = url.lower()
    for key, lib in LIBRARY_HINTS.items():
        if key in u:
            return lib
    return ""


BANNER = [
    "▞▀▖     ▌▗    ▌        ▌   ▌ ▌   ▜         ",
    "▙▄▌▌ ▌▞▀▌▄ ▞▀▖▛▀▖▞▀▖▞▀▖▌▗▘ ▙▄▌▞▀▖▐ ▛▀▖▞▀▖▙▀▖",
    "▌ ▌▌ ▌▌ ▌▐ ▌ ▌▌ ▌▌ ▌▌ ▌▛▚  ▌ ▌▛▀ ▐ ▙▄▘▛▀ ▌  ",
    "▘ ▘▝▀▘▝▀▘▀▘▝▀ ▀▀ ▝▀ ▝▀ ▘ ▘ ▘ ▘▝▀▘ ▘▌  ▝▀▘▘   ",
]
TAGLINE = "For personal backup and offline access of your audiobooks."

# Themes (256-color indices)
THEMES = {
    # Dark (requested palette)
    "dark": {"bg": 236, "fg": 252, "accent": 180, "blue": 73, "green": 77, "red": 167},
    # Dim (slightly lighter bg closer to ~#282C34)
    "dim":  {"bg": 237, "fg": 254, "accent": 186, "blue": 73, "green": 114, "red": 174},
    # Light theme
    "light": {"bg": 231, "fg": 235, "accent": 94,  "blue": 31, "green": 28,  "red": 160},
}

_COLOR_INIT = {"done": False, "theme": "dark"}

def _current_theme():
    # Env wins
    env = os.getenv("ABH_THEME")
    if env and env.lower() in THEMES:
        return env.lower()
    # Config fallback
    try:
        cfg = load_config()
        name = (cfg.get("ui", {}) or {}).get("theme")
        if isinstance(name, str) and name.lower() in THEMES:
            return name.lower()
    except Exception:
        pass
    # Default to a softer dim theme to avoid pure black
    return "dim"

def _setup_colors(stdscr):
    """Initialize curses color pairs for the current theme.
    Keep this minimal and 256‑color friendly so it works on stock macOS Terminal.
    """
    name = _current_theme()
    if _COLOR_INIT["done"] and _COLOR_INIT["theme"] == name:
        return
    try:
        curses.start_color()
        curses.use_default_colors()
        pal = THEMES.get(name, THEMES["light"])
        # Define pairs: 1=fg/bg, 2=accent on bg, 3=highlight (bg/fg swap)
        bg = pal["bg"]; fg = pal["fg"]; ac = pal["accent"]
        curses.init_pair(1, fg, bg)
        curses.init_pair(2, ac, bg)
        curses.init_pair(3, bg, fg)
        curses.init_pair(4, pal["blue"], bg)
        curses.init_pair(5, pal["green"], bg)
        curses.init_pair(6, pal["red"], bg)
    except Exception:
        pass
    _COLOR_INIT["done"] = True
    _COLOR_INIT["theme"] = name


# Minimal background handling to keep code simple and maintainable


def run_cmd_spinner(cmd: list[str], title: str) -> tuple[int, str]:
    """Run a command while showing a spinner.
    Returns (returncode, log_path). The log path captures combined stdout/stderr and
    is printed on screen to help users/debuggers inspect failures without re‑running.
    Keep UI work in this function strictly presentational; do not mutate global state.
    """
    log_path = None
    def _spin(stdscr):
        nonlocal log_path
        _setup_colors(stdscr)
        try:
            curses.curs_set(0)
        except Exception:
            pass
        stdscr.keypad(True)
        stdscr.bkgd(' ', curses.color_pair(1))
        stdscr.clear()
        # Banner and title
        for i, line in enumerate(BANNER):
            stdscr.addstr(i, 0, line, curses.color_pair(4))
        stdscr.addstr(len(BANNER), 0, TAGLINE, curses.color_pair(4))
        row = len(BANNER) + 2
        stdscr.addstr(row, 0, title, curses.color_pair(2)); row += 2
        stdscr.addstr(row, 0, "Working… This can take a while.", curses.color_pair(1)); row += 2
        # Prepare log file
        lf = tempfile.NamedTemporaryFile(prefix="abh_", suffix=".log", delete=False)
        log_path = lf.name
        lf.close()
        # Launch process with stdout/stderr to log
        log_out = open(log_path, 'w')
        proc = subprocess.Popen(cmd, stdout=log_out, stderr=subprocess.STDOUT)
        # Open log for reading to parse PROGRESS lines
        log_in = open(log_path, 'r')
        spinner = ['-', '\\', '|', '/']
        idx = 0
        start = time.time()
        last_progress = {}
        bar_width = 40
        while True:
            rc = proc.poll()
            # Read any new log content and parse progress lines
            try:
                data = log_in.read()
                if data:
                    for line in data.splitlines():
                        if line.startswith('PROGRESS '):
                            # parse key=value tokens
                            tokens = line.split()[1:]
                            for kv in tokens:
                                if '=' in kv:
                                    k, v = kv.split('=', 1)
                                    last_progress[k] = v
            except Exception:
                pass
            # Render either progress bar or spinner
            if last_progress.get('pct') is not None:
                try:
                    pct = int(last_progress.get('pct', '0'))
                except Exception:
                    pct = 0
                filled = int((pct * bar_width) / 100)
                bar = '[' + '#' * filled + '-' * (bar_width - filled) + ']'
                eta = last_progress.get('eta_s') or '0'
                elapsed_s = int(time.time() - start)
                stdscr.addstr(row, 0, f"{bar} {pct:3d}%  {elapsed_s}s / ~{eta}s", curses.color_pair(1))
            else:
                stdscr.addstr(row, 0, f"{spinner[idx % len(spinner)]}  Elapsed: {int(time.time()-start)}s", curses.color_pair(1))
            stdscr.addstr(row+2, 0, f"Log: {log_path}", curses.color_pair(1))
            stdscr.refresh()
            idx += 1
            if rc is not None:
                try:
                    log_in.close()
                except Exception:
                    pass
                try:
                    log_out.close()
                except Exception:
                    pass
                return rc
            time.sleep(0.1)
    try:
        rc = curses.wrapper(_spin)
    except Exception:
        # Fallback without spinner
        with tempfile.NamedTemporaryFile(prefix="abh_", suffix=".log", delete=False, mode='w') as lf:
            log_path = lf.name
        with open(log_path, 'w') as out:
            proc = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT)
            rc = proc.returncode
    return rc, (log_path or "")


def prompt(msg: str, default: str = "", hidden: bool = False) -> str:
    """Single‑line text prompt in a fancy screen.
    For password input we mask the field; ESC returns the default.
    """
    def _inp(stdscr):
        _setup_colors(stdscr)
        try:
            curses.curs_set(1)
        except Exception:
            pass
        stdscr.keypad(True)
        # Draw banner
        stdscr.bkgd(' ', curses.color_pair(1))
        stdscr.clear()
        for i, line in enumerate(BANNER):
            stdscr.addstr(i, 0, line, curses.color_pair(4))
        # Tagline under banner
        stdscr.addstr(len(BANNER), 0, TAGLINE, curses.color_pair(4))
        row = len(BANNER) + 2
        stdscr.addstr(row, 0, msg + (f" [{default}]" if default else "") + ": ", curses.color_pair(1))
        row += 2
        stdscr.addstr(row, 0, "Press Enter to confirm. Esc to cancel.", curses.color_pair(1))
        buf = list(default)
        col = len(msg) + 2 + (len(default) + 2 if default else 0)
        # Input line
        row = len(BANNER) + 2
        while True:
            # Render current input
            shown = ("*" * len(buf)) if hidden else "".join(buf)
            stdscr.move(row, len(msg) + 2)
            stdscr.clrtoeol()
            if default:
                stdscr.addstr(row, 0, msg + f" [{default}]: ", curses.color_pair(1))
            else:
                stdscr.addstr(row, 0, msg + ": ", curses.color_pair(1))
            stdscr.addstr(row, len(msg) + 2, shown, curses.color_pair(1))
            ch = stdscr.getch()
            if ch in (10, 13):
                return "".join(buf)
            if ch in (27,):
                return default
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
                continue
            # Accept basic printable ASCII
            if 32 <= ch <= 126:
                buf.append(chr(ch))
                continue
    try:
        val = curses.wrapper(_inp)
        return (val if val != "" else (default or ""))
    except Exception:
        # Fallback to plain input
        if hidden:
            # Basic hidden fallback
            return input(f"{msg}: ") or default
        if default:
            val = input(f"{msg} [{default}]: ").strip()
            return val or default
        return input(f"{msg}: ").strip()


def yesno(msg: str, default_yes: bool = True) -> bool:
    idx = choose_menu(msg, ["Yes", "No"], default_idx=0 if default_yes else 1)
    return True if idx in (None, 0) else False


def slugify(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return text or "audiobook"


def select_menu(title: str, options: list[str], default_idx: int = 0) -> Optional[int]:
    """Interactive arrow‑key menu (↑/↓ + Enter, q/Esc to cancel).
    Returns selected index or None on cancel. We keep this generic and stateless
    so other flows (bootstrap, preflight) can reuse it.
    """
    def _menu(stdscr):
        _setup_colors(stdscr)
        try:
            curses.curs_set(0)
        except Exception:
            pass
        stdscr.keypad(True)
        idx = max(0, min(default_idx, len(options) - 1))
        while True:
            stdscr.bkgd(' ', curses.color_pair(1))
            stdscr.clear()
            stdscr.addstr(0, 0, title, curses.color_pair(4))
            stdscr.addstr(1, 0, "Use ↑/↓ to move, Enter to select, q to cancel", curses.color_pair(1))
            for i, opt in enumerate(options):
                row = i + 3
                if i == idx:
                    stdscr.attron(curses.color_pair(3))
                stdscr.addstr(row, 2, opt)
                if i == idx:
                    stdscr.attroff(curses.color_pair(3))
            ch = stdscr.getch()
            if ch in (curses.KEY_UP, ord('k')):
                idx = (idx - 1) % len(options)
            elif ch in (curses.KEY_DOWN, ord('j')):
                idx = (idx + 1) % len(options)
            elif ch == curses.KEY_RESIZE:
                continue
            elif ch in (curses.KEY_ENTER, 10, 13):
                return idx
            elif ch in (27, ord('q')):
                return None
    try:
        return curses.wrapper(_menu)
    except Exception:
        return None


def print_banner():
    # No-op; banner is drawn by each fancy screen
    pass


def which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def preflight_check() -> bool:
    """Ensure required tools are present and optionally offer upgrades.
    The upgrade prompt is opportunistic: we skip quietly on network issues.
    Keep this fast (no blocking) so launch feels instant.
    """
    missing = []
    # python3 is required but we're already running under it; still show info if path missing
    if not which("python3"):
        missing.append("python3 interpreter")
    if not which("audiobook-dl"):
        missing.append("audiobook-dl (CLI)")
    if not which("ffmpeg") or not which("ffprobe"):
        missing.append("ffmpeg/ffprobe")
    if not missing:
        # Optional: prompt to update if versions are outdated
        try:
            maybe_prompt_updates()
        except Exception:
            pass
        return True

    # Build guidance
    has_brew = which("brew")
    bootstrap = Path(__file__).with_name("bootstrap_audiobook_helper.py")
    homebrew_cmds = [
        "brew install pipx",
        "pipx ensurepath",
        "pipx install audiobook-dl",
        "brew install ffmpeg",
    ]
    pip_only_cmds = [
        "python3 -m pip install --user --upgrade audiobook-dl",
        "# Install ffmpeg via Homebrew (preferred) or from https://ffmpeg.org/download.html#build-mac",
    ]

    title = "Dependencies missing: " + ", ".join(missing)
    opts = []
    if bootstrap.exists():
        opts.append("Run the Audiobook Helper bootstrap installer now")
    if has_brew:
        opts.append("Show Homebrew + pipx install commands (recommended)")
    opts.append("Show pip-only commands (audiobook-dl only)")
    opts.append("Open Homebrew website")
    opts.append("Re-check after installing")
    idx = choose_menu(title, opts, default_idx=0)
    cursor = 0
    if bootstrap.exists() and idx == cursor:
        # Launch bootstrap installer in a subshell
        try:
            subprocess.run([sys.executable, str(bootstrap)], check=False)
        except Exception:
            pass
        return preflight_check()
    if bootstrap.exists():
        cursor += 1
    if has_brew and idx == cursor:
        print("\nRun these commands in Terminal:")
        for c in homebrew_cmds:
            print("  ", c)
        # Copy to clipboard if possible
        try:
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            p.communicate(("\n" + "\n".join(homebrew_cmds)).encode())
        except Exception:
            pass
        return False
    # Adjust selection offsets if brew option not present
    base = cursor if has_brew else (cursor - 1)
    if idx == base + 1:
        print("\nRun these commands in Terminal:")
        for c in pip_only_cmds:
            print("  ", c)
        return False
    if idx == base + 2:
        try:
            webbrowser.open("https://brew.sh")
        except Exception:
            pass
        return False
    # Re-check chosen
    return preflight_check()


def is_valid_url(u: str) -> bool:
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def _likely_audio_file(p: Path) -> bool:
    try:
        if '_all' in p.stem.lower():
            return False
        if p.suffix.lower() in ('.m4b', '.m4a', '.mp3'):
            # Basic size sanity to avoid tiny provider artifacts
            try:
                if p.stat().st_size < 500_000:  # ~0.5MB
                    return False
            except Exception:
                pass
            return True
        # Probe with ffprobe as a fallback
        proc = subprocess.run([
            'ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries', 'stream=codec_type', '-of', 'json', str(p)
        ], capture_output=True, text=True, timeout=3)
        return proc.returncode == 0 and 'codec_type' in (proc.stdout or '')
    except Exception:
        return False


def _find_audio_in_dir(out_dir: Path, wait_secs: float = 2.0) -> Optional[Path]:
    deadline = _time.time() + wait_secs
    best: Optional[Path] = None
    while True:
        all_files = [p for p in out_dir.rglob('*') if p.is_file()]
        candidates = [p for p in all_files if _likely_audio_file(p)]
        if candidates:
            candidates.sort(key=lambda p: (p.stat().st_mtime, p.stat().st_size), reverse=True)
            best = candidates[0]
            break
        if _time.time() >= deadline:
            break
        _time.sleep(0.25)
    return best


def _largest_audio_guess(out_dir: Path) -> Optional[Path]:
    try:
        files = [p for p in out_dir.rglob('*') if p.is_file()]
        # Prefer audio-looking files first
        audioish = [p for p in files if p.suffix.lower() in ('.m4b', '.m4a', '.mp3')]
        # Explicitly exclude provider aggregates like *_all.m4a
        audioish = [p for p in audioish if '_all' not in p.stem.lower()]
        candidates = audioish if audioish else files
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
        top = candidates[0]
        # Sanity: require at least ~1MB
        if top.stat().st_size > 1_000_000:
            return top
    except Exception:
        pass
    return None


# Utilities for strict part selection and ordering
_PART_RE = re.compile(r"^Part[\s\-_]*(\d+)\.(aac|m4a|m4b|mp4)$", re.IGNORECASE)


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _strict_find_parts(base: Path) -> Tuple[List[Path], List[Path], List[Path]]:
    """Return (aac_parts, container_parts, excluded) using strict rules.
    - Accept only files matching Part <num>.(aac|m4a|m4b|mp4)
    - Exclude anything with '_all' in the stem
    - Natural sort by the captured number
    """
    all_files = [p for p in base.rglob('*') if p.is_file()]
    excluded: List[Path] = []
    aac: List[Tuple[int, Path]] = []
    cont: List[Tuple[int, Path]] = []
    for p in all_files:
        stem = p.stem.lower()
        if '_all' in stem:
            excluded.append(p)
            continue
        m = _PART_RE.match(p.name)
        if not m:
            continue
        idx = int(m.group(1))
        ext = p.suffix.lower()
        if ext == '.aac':
            aac.append((idx, p))
        elif ext in ('.m4a', '.m4b', '.mp4'):
            cont.append((idx, p))
    aac_parts = [p for _, p in sorted(aac, key=lambda t: t[0])]
    cont_parts = [p for _, p in sorted(cont, key=lambda t: t[0])]
    return aac_parts, cont_parts, excluded


def _ffprobe_duration_ms(p: Path) -> int:
    try:
        out = subprocess.check_output([
            'ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',str(p)
        ], text=True, timeout=15)
        return int(float(out.strip())*1000)
    except Exception:
        return 0


def _verify_duration(parts: List[Path], output: Path, label: str = "") -> Tuple[bool, str]:
    total_ms = sum(_ffprobe_duration_ms(p) for p in parts)
    out_ms = _ffprobe_duration_ms(output)
    if total_ms <= 0 or out_ms <= 0:
        return False, f"Verification unavailable (parts: {total_ms} ms, out: {out_ms} ms)."
    ok = out_ms >= int(0.99 * total_ms)
    detail = f"{label} duration {out_ms/1000:.1f}s vs parts {total_ms/1000:.1f}s ({out_ms/total_ms*100:.2f}%)"
    return ok, detail


def _find_cover_recursively(out_dir: Path) -> Optional[Path]:
    """Search recursively for a plausible cover image and return the best candidate.
    Preference order: filenames containing 'cover'/'folder'/'front' (case-insensitive),
    then by largest file size among jpg/jpeg/png.
    """
    try:
        imgs = [p for p in out_dir.rglob('*') if p.suffix.lower() in ('.jpg', '.jpeg', '.png') and p.is_file()]
        if not imgs:
            return None
        def score(p: Path) -> tuple[int, int]:
            name = p.name.lower()
            priority = 0
            if any(k in name for k in ('cover', 'folder', 'front')):
                priority = 1
            return (priority, p.stat().st_size)
        imgs.sort(key=score, reverse=True)
        return imgs[0]
    except Exception:
        return None


def _wait_for_combined(out_dir: Path, timeout: float = 60.0) -> Optional[Path]:
    """Wait for a combined single output file to finish writing.
    Looks for non-part audio files (m4a/m4b/mp3) and returns the largest stable one.
    Stability = size unchanged for >= 2 seconds.
    """
    deadline = _time.time() + timeout
    last_sizes: dict[Path, tuple[int, float]] = {}
    best: Optional[Path] = None
    while _time.time() < deadline:
        cands = [p for p in out_dir.rglob('*') if p.is_file() and p.suffix.lower() in ('.m4a', '.m4b', '.mp3') and not _PART_RE.match(p.name)]
        now = _time.time()
        stable: list[Path] = []
        for p in cands:
            try:
                sz = p.stat().st_size
            except Exception:
                continue
            prev = last_sizes.get(p)
            if prev is None or prev[0] != sz:
                last_sizes[p] = (sz, now)
            else:
                # size unchanged since prev record
                if (now - prev[1]) >= 2.0 and sz > 500_000:
                    stable.append(p)
        if stable:
            stable.sort(key=lambda p: p.stat().st_size, reverse=True)
            best = stable[0]
            break
        _time.sleep(0.5)
    # As a fallback, pick the largest candidate even if not strictly stable
    if best is None:
        try:
            cands = [p for p in out_dir.rglob('*') if p.is_file() and p.suffix.lower() in ('.m4a', '.m4b', '.mp3') and not _PART_RE.match(p.name)]
            if cands:
                cands.sort(key=lambda p: p.stat().st_size, reverse=True)
                return cands[0]
        except Exception:
            pass
    return best


def _has_embedded_cover(audio_path: Path) -> bool:
    try:
        prob = subprocess.run([
            "ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(audio_path)
        ], capture_output=True, text=True)
        if prob.returncode != 0:
            return False
        data = json.loads(prob.stdout or '{}')
        for s in data.get("streams", []):
            if s.get("disposition", {}).get("attached_pic", 0) == 1:
                return True
    except Exception:
        pass
    return False


def _extract_embedded_cover(audio_path: Path, out_dir: Path) -> Optional[Path]:
    """Extract embedded cover from an audio file into out_dir as cover.jpg/png.
    Returns the path if extracted, else None.
    """
    if not _has_embedded_cover(audio_path):
        return None
    for name in ("cover.jpg", "cover.png"):
        try:
            dest = Path(out_dir) / name
            rc = subprocess.run([
                'ffmpeg','-hide_banner','-nostdin','-y','-loglevel','error',
                '-i', str(audio_path), '-an', '-map', '0:v', '-c', 'copy', str(dest)
            ]).returncode
            if rc == 0 and dest.exists() and dest.stat().st_size > 0:
                return dest
        except Exception:
            continue
    return None


def _read_tags(audio_path: Path) -> Dict[str, str]:
    try:
        out = subprocess.check_output([
            'ffprobe','-v','error','-show_entries','format_tags','-of','json', str(audio_path)
        ], text=True)
        data = _json.loads(out or '{}')
        return (data.get('format', {}).get('tags') or {})
    except Exception:
        return {}


def _read_any_metadata_json(out_dir: Path) -> Dict[str, str]:
    """Scan for a JSON metadata file written by audiobook-dl and extract core fields."""
    try:
        for p in out_dir.rglob('*.json'):
            if not p.is_file():
                continue
            try:
                data = _json.loads(p.read_text())
            except Exception:
                continue
            # Heuristics for common keys
            title = data.get('title') or data.get('book_title') or data.get('name')
            author = data.get('author') or data.get('authors') or data.get('artist')
            if isinstance(author, list):
                author = ", ".join([str(a) for a in author if a])
            album = data.get('album') or title
            year = str(data.get('year') or data.get('date') or '')[:4]
            res = {k: v for k, v in {
                'title': title or '', 'author': author or '', 'album': album or '', 'year': year or ''
            }.items() if v}
            if res:
                return res
    except Exception:
        pass
    return {}


def _parse_version_tuple(s: str) -> tuple:
    import re
    parts = re.findall(r"\d+", s)
    if not parts:
        return ()
    return tuple(int(p) for p in parts)


def _installed_audiobook_dl_version() -> str:
    """Return audiobook‑dl reported version (stdout or stderr), or empty string."""
    try:
        proc = subprocess.run(["audiobook-dl", "--version"], capture_output=True, text=True, check=False)
        out = (proc.stdout or proc.stderr or "").strip()
        return out
    except Exception:
        return ""


def _pypi_latest_version(pkg: str, timeout: float = 1.5) -> str:
    """Return latest version string for a PyPI package. Short timeout by design."""
    try:
        import urllib.request, json
        with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=timeout) as r:
            data = json.load(r)
        return data.get("info", {}).get("version", "")
    except Exception:
        return ""


def _brew_ffmpeg_outdated() -> bool:
    """Return True if Homebrew reports ffmpeg is outdated; False otherwise."""
    if not which("brew") or not which("ffmpeg"):
        return False
    try:
        proc = subprocess.run(["brew", "outdated", "--quiet", "ffmpeg"], capture_output=True, text=True, check=False)
        # If ffmpeg appears in the list, it's outdated
        return "ffmpeg" in (proc.stdout or "")
    except Exception:
        return False


def maybe_prompt_updates() -> None:
    """If we can detect newer versions for core tools, offer to update.
    Never auto‑update; we show explicit choices and use spinner‑wrapped commands.
    """
    updates = []
    # audiobook-dl
    inst = _installed_audiobook_dl_version()
    latest = _pypi_latest_version("audiobook-dl")
    if inst and latest and _parse_version_tuple(inst) < _parse_version_tuple(latest):
        updates.append(("audiobook-dl", inst, latest))
    # ffmpeg via brew
    if _brew_ffmpeg_outdated():
        updates.append(("ffmpeg", "installed", "latest via Homebrew"))

    if not updates:
        return

    lines = []
    for name, cur, new in updates:
        lines.append(f"{name}: {cur} → {new}")
    title = "Updates available"
    body = "\n".join(lines)
    opts = ["Update all", "Skip"]
    # Add per-item options
    for name, _, _ in updates:
        if name == "audiobook-dl":
            opts.insert(1, "Update audiobook-dl only")
        if name == "ffmpeg":
            opts.insert(1, "Update ffmpeg (Homebrew) only")
    sel = choose_menu(title + "\n" + body, opts, default_idx=0)
    if sel is None:
        return

    def update_adl():
        if which("pipx"):
            cmd = ["pipx", "install", "--force", "audiobook-dl"]
        else:
            cmd = [sys.executable, "-m", "pip", "install", "--user", "--upgrade", "audiobook-dl"]
        run_cmd_spinner(cmd, "Updating audiobook-dl…")

    def update_ffmpeg():
        if which("brew"):
            run_cmd_spinner(["brew", "upgrade", "ffmpeg"], "Updating ffmpeg via Homebrew…")

    choice = opts[sel]
    if choice == "Update all":
        if any(n == "audiobook-dl" for n, _, _ in updates):
            update_adl()
        if any(n == "ffmpeg" for n, _, _ in updates):
            update_ffmpeg()
    elif choice == "Update audiobook-dl only":
        update_adl()
    elif choice == "Update ffmpeg (Homebrew) only":
        update_ffmpeg()


def use_fancy_menus() -> bool:
    # Always use fancy (full-screen) menus for a consistent experience
    return True


def choose_menu(title: str, options: list[str], default_idx: int = 0) -> Optional[int]:
    """Inline menu with numeric selection; uses curses if fancy menus enabled."""
    if use_fancy_menus():
        return select_menu(title, options, default_idx)
    # Inline numbered menu
    print(title)
    for i, opt in enumerate(options, start=1):
        marker = "*" if (i - 1) == default_idx else " "
        print(f"  {marker} {i}) {opt}")
    while True:
        raw = input(f"Enter a number [default {default_idx+1}]: ").strip()
        if raw == "":
            return default_idx
        if raw.isdigit():
            val = int(raw)
            if 1 <= val <= len(options):
                return val - 1
        print("Please enter a valid option number.")

def _download_bytes(url: str, timeout: int = 15) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        ctype = r.headers.get("Content-Type", "")
    return data, ctype


def fetch_cover_by_isbn(isbn: str, out_dir: Path) -> Path | None:
    # Try Google Books for imageLinks
    try:
        with urllib.request.urlopen(
            f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}", timeout=10
        ) as r:
            g = json.load(r)
        if g.get("totalItems", 0) > 0 and g.get("items"):
            info = g["items"][0].get("volumeInfo", {})
            links = info.get("imageLinks") or {}
            for key in ("large", "medium", "thumbnail", "smallThumbnail"):
                url = links.get(key)
                if not url:
                    continue
                try:
                    data, ctype = _download_bytes(url)
                    if data and ctype.startswith("image/"):
                        ext = ".jpg" if "jpeg" in ctype or "jpg" in ctype else ".png"
                        p = out_dir / f"cover{ext}"
                        p.write_bytes(data)
                        return p
                except Exception:
                    continue
    except Exception:
        pass

    # Fallback: Open Library covers API
    for size in ("L", "M"):
        url = f"https://covers.openlibrary.org/b/isbn/{isbn}-{size}.jpg"
        try:
            data, ctype = _download_bytes(url)
            if data and ctype.startswith("image/") and len(data) > 512:
                p = out_dir / "cover.jpg"
                p.write_bytes(data)
                return p
        except Exception:
            continue
    return None


def main():
    # Preflight dependencies
    if not preflight_check():
        return 1
    # First prompt draws a fancy screen including banner

    # URL input + validation
    while True:
        url = prompt("Paste the audiobook URL")
        if is_valid_url(url):
            break
        choice = choose_menu("That doesn't look like a valid URL.", ["Try again", "Cancel"], default_idx=0)
        if choice == 1:
            return 1

    lib_guess = detect_library(url)
    known = ["nextory", "storytel", "audible", "bookbeat", "Other (enter manually)"]
    default_idx = known.index(lib_guess) if lib_guess in known else 0
    lib_sel = choose_menu("Choose audiobook service:", [s.capitalize() for s in known], default_idx=default_idx)
    if lib_sel is None:
        library = lib_guess or "nextory"
    else:
        if known[lib_sel].startswith("Other"):
            library = prompt("Enter service name (e.g., nextory, storytel, audible)", lib_guess or "nextory")
        else:
            library = known[lib_sel]

    # Load stored auth for this library if present
    cfg = load_config()
    stored = cfg.get(library, {}) if isinstance(cfg, dict) else {}
    username = password = cookies = ""
    used_stored = False

    if stored:
        if stored.get("auth") == "password" and stored.get("username"):
            acc = f"{library}:{stored['username']}"
            pw = kc_get_password(APP_NAME, acc)
            if pw:
                if yesno(f"Use saved login for {library} ({stored['username']})?", True):
                    username = stored['username']
                    password = pw
                    used_stored = True
        elif stored.get("auth") == "cookies" and stored.get("cookies"):
            cpath = Path(stored.get("cookies")).expanduser()
            if cpath.exists() and yesno(f"Use saved cookies for {library}? ({cpath})", True):
                cookies = str(cpath)
                used_stored = True

    if not used_stored:
        # Interactive auth method selection
        auth_idx = choose_menu(
            "Sign in method:",
            [
                "Username / Password (most users)",
                "Cookies file (advanced)",
                "No sign-in (public)",
            ],
            default_idx=0,
        )
        auth_method = "1" if auth_idx is None else {0: "1", 1: "2", 2: "3"}[auth_idx]
        if auth_method == "1":
            username = prompt(f"{library} username/email")
            password = getpass(f"{library} password: ")
        elif auth_method == "2":
            # Loop until a cookies file exists or user cancels
            while True:
                cookies = prompt("Path to cookies.txt", str(Path.home() / "Ripping" / "cookies.txt"))
                if Path(cookies).expanduser().exists():
                    break
                sel = choose_menu(f"Cookies file not found at {cookies}", ["Try again", "Continue without cookies"], default_idx=0)
                if sel == 1:
                    cookies = ""
                    break
        # Offer to remember (only if we collected fresh auth)
        if auth_method in ("1", "2") and yesno(f"Remember this login for {library}?", False):
            entry: Dict[str, Any] = {"auth": ("password" if auth_method == "1" else "cookies")}
            if auth_method == "1":
                entry["username"] = username
                if password:
                    kc_set_password(APP_NAME, f"{library}:{username}", password, f"Audiobook Helper {library}")
            elif auth_method == "2":
                entry["cookies"] = cookies
            cfg[library] = entry
            save_config(cfg)

    # Output method selection (interactive)
    method_idx = choose_menu(
        "How should we build the final file?",
        [
            "Robust local merge (recommended)",
            "Let audiobook-dl combine",
            "Keep as multiple files",
        ],
        default_idx=0,
    )
    if method_idx is None:
        method_idx = 0
    robust_merge = (method_idx == 0)
    combine = (method_idx == 1)

    # Output format selection (interactive)
    fmt_options = [
        "m4b — audiobook container (recommended)",
        "m4a — AAC audio",
        "mp3 — broad compatibility",
    ]
    fmt_idx = choose_menu("Choose output format:", fmt_options, default_idx=0)
    out_fmt = "m4b" if fmt_idx is None else ["m4b", "m4a", "mp3"][fmt_idx]

    # Optional ISBN for metadata lookup
    while True:
        isbn = prompt("ISBN (optional, press Enter to skip)")
        if isbn:
            break
        # Warn if missing ISBN
        if yesno("No ISBN provided. Continue without metadata lookup?", True):
            break
    # We will fetch metadata later if ISBN is provided

    # Preferred default output base
    paths_cfg = (cfg.get("paths") if isinstance(cfg, dict) else None) or {}
    default_out_base = paths_cfg.get("output_base", str((Path.home() / "Music" / "Audiobooks" / "Offline").expanduser()))
    # Output base selection and write test
    while True:
        out_base = prompt("Choose where to save files (Output base folder)", default_out_base)
        p = Path(out_base).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
            # Write test
            testf = p / ".abh_write_test"
            with open(testf, "w") as tf:
                tf.write("ok")
            testf.unlink(missing_ok=True)
            break
        except Exception as e:
            choice = choose_menu(f"Cannot write to {out_base} ({e}).", ["Choose another folder", "Cancel"], default_idx=0)
            if choice == 1:
                return 1
    # Remember chosen base
    cfg.setdefault("paths", {})
    cfg["paths"]["output_base"] = out_base
    try:
        save_config(cfg)
    except Exception:
        pass

    # Create a unique output folder name from URL tail + timestamp
    tail = url.rstrip("/").split("/")[-1]
    out_dir = Path(out_base) / f"{slugify(tail)}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    # Optional flags for audiobook-dl
    skip_downloaded = False
    keep_json_meta = False

    # If the target folder already exists and is not empty, offer --skip-downloaded
    try:
        if out_dir.exists():
            any_files = any(p.is_file() for p in out_dir.rglob('*'))
            if any_files and yesno("Downloads already present in target folder. Skip already downloaded?", True):
                skip_downloaded = True
    except Exception:
        pass

    # We'll always ask audiobook-dl to write JSON metadata so we can tag properly,
    # but only keep the JSON files if the user opts in here (default: No)
    if yesno("Keep JSON metadata files?", False):
        keep_json_meta = True

    # For robust merge, prefer downloading raw AAC parts if possible
    dl_fmt = 'aac' if robust_merge else out_fmt
    cmd = ["audiobook-dl", "--output", str(out_dir), "--output-format", dl_fmt]
    if skip_downloaded:
        cmd += ["--skip-downloaded"]
    # Always request json so we can extract proper tags; we'll delete later unless user wanted to keep it
    cmd += ["--write-json-metadata"]
    if library:
        cmd += ["--library", library]
    if combine:
        cmd += ["--combine"]
    if username:
        cmd += ["--username", username]
    if password:
        cmd += ["--password", password]
    if cookies:
        cmd += ["--cookies", cookies]
    cmd += [url]

    print("\nRunning:")
    print(" ".join(shlex.quote(c) if c != password else "<hidden>" for c in cmd))
    print()

    # Run audiobook-dl in passthrough mode to preserve its progress UI
    while True:
        dl_failed = False
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            dl_failed = True
            # If parts were downloaded, we'll handle fallback combine below
            parts = list(Path(out_dir).rglob("*.aac"))
            if parts:
                # Inform the user and proceed
                # (Spinner screen will be replaced by next steps.)
                break
            # Otherwise, offer to retry or open the URL so the user can add to list / fix auth
            idx = choose_menu(
                "audiobook-dl failed before downloading parts.",
                ["Retry", "Open the book page", "Cancel"],
                default_idx=0,
            )
            if idx == 1:
                try:
                    subprocess.run(["open", url], check=False)
                except Exception:
                    pass
                continue
            if idx == 0:
                continue
            return 1
        # Success
        break

    # Respect the chosen method
    audio: Optional[Path] = None
    robust_used: bool = False
    aac_parts, cont_parts, excluded = _strict_find_parts(out_dir)
    if excluded:
        print(f"Excluded {len(excluded)} non-part files (e.g., *_all.m4a).")

    def _fast_verify(parts: List[Path], out_file: Path) -> Tuple[bool, str]:
        # Write a concat list and run a single ffprobe for total duration
        try:
            lst = Path(out_dir) / 'verify_list.txt'
            with lst.open('w') as f:
                for p in parts:
                    f.write(f"file {shlex.quote(str(p.resolve()))}\n")
            # ffprobe total parts duration via concat demuxer
            parts_ms = 0
            try:
                out = subprocess.check_output([
                    'ffprobe','-v','error','-f','concat','-safe','0','-i',str(lst),
                    '-show_entries','format=duration','-of','default=nw=1:nk=1'
                ], text=True)
                parts_ms = int(float(out.strip())*1000)
            except Exception:
                # Fallback to per-part if concat probe fails
                parts_ms = sum(_ffprobe_duration_ms(p) for p in parts)
            out_ms = _ffprobe_duration_ms(out_file)
            if parts_ms <= 0 or out_ms <= 0:
                return False, "Verification unavailable."
            ok = out_ms >= int(0.99 * parts_ms)
            return ok, f"{out_ms/1000:.1f}s vs {parts_ms/1000:.1f}s ({out_ms/parts_ms*100:.2f}%)"
        except Exception:
            return False, "Verification error."

    if combine:
        # Wait for audiobook-dl’s combined output to settle
        cand = _wait_for_combined(Path(out_dir), timeout=90.0)
        if cand is None and dl_failed:
            return 1
        audio = cand
        # If parts are present, verify the combined output; fallback to robust if short
        if audio is not None and (len(aac_parts) >= 2 or len(cont_parts) >= 2):
            parts = aac_parts if len(aac_parts) >= 2 else cont_parts
            ok, detail = _fast_verify(parts, audio)
            print(f"Combine verification: {detail}")
            if not ok and len(aac_parts) >= 2:
                # Fallback to robust AAC path
                print("Combined output seems short. Falling back to robust merge…")
                # Try to extract cover from the combined file for later tagging
                try:
                    cov = _extract_embedded_cover(Path(audio), Path(out_dir))
                    if cov:
                        print(f"Extracted cover from combined file: {cov}")
                except Exception:
                    pass
                concat = Path(__file__).with_name('concat_aac.py')
                rc, _ = run_cmd_spinner([
                    sys.executable, str(concat),
                    '--input-dir', str(out_dir),
                    '--output-dir', str(out_dir),
                    '--chunks', '1', '--prefix', 'book', '--container', 'm4a',
                    '--method', 'rawcat', '--reencode', '--bitrate', '128k', '--verify', '--loglevel', 'warning', '--progress',
                ], 'Combining downloaded parts (robust)…')
                if rc == 0:
                    audio = Path(out_dir) / 'book_01.m4a'
                    robust_used = True
            elif not ok and len(cont_parts) >= 2:
                print("Combined output seems short. Rebuilding from container parts…")
                # Try to extract cover from the combined file for later tagging
                try:
                    cov = _extract_embedded_cover(Path(audio), Path(out_dir))
                    if cov:
                        print(f"Extracted cover from combined file: {cov}")
                except Exception:
                    pass
                lst = Path(out_dir) / 'book_list.txt'
                with lst.open('w') as f:
                    for p in cont_parts:
                        f.write(f"file {shlex.quote(str(p.resolve()))}\n")
                outp = Path(out_dir) / 'book_01.m4a'
                rc = subprocess.run([
                    'ffmpeg','-hide_banner','-nostdin','-y','-loglevel','warning','-f','concat','-safe','0','-i',str(lst),
                    '-c:a','aac','-b:a','128k','-movflags','+faststart',str(outp)
                ]).returncode
                if rc == 0:
                    audio = outp
                    robust_used = True
    elif robust_merge:
        # Robust path: AAC parts preferred, else container parts
        if len(aac_parts) >= 2:
            print(f"\nDetected {len(aac_parts)} AAC parts. Attempting robust combine…")
            concat = Path(__file__).with_name('concat_aac.py')
            rc, _ = run_cmd_spinner([
                sys.executable, str(concat), '--input-dir', str(out_dir), '--output-dir', str(out_dir),
                '--chunks', '1', '--prefix', 'book', '--container', 'm4a', '--method', 'rawcat', '--reencode', '--progress',
                '--bitrate', '128k', '--verify', '--loglevel', 'warning',
            ], 'Combining downloaded parts (robust)…')
            if rc == 0:
                audio = Path(out_dir) / 'book_01.m4a'
                robust_used = True
            else:
                rc2, _ = run_cmd_spinner([
                    sys.executable, str(concat), '--input-dir', str(out_dir), '--output-dir', str(out_dir),
                    '--chunks', '1', '--prefix', 'book', '--container', 'm4a', '--method', 'demux', '--reencode', '--progress',
                    '--bitrate', '128k', '--verify', '--loglevel', 'warning',
                ], 'Combining downloaded parts (alternate)…')
                if rc2 == 0:
                    audio = Path(out_dir) / 'book_01.m4a'
                    robust_used = True
                else:
                    print("Combine failed. The downloaded parts are in the folder for manual recovery.")
                    print(f"  {out_dir}")
                    return 1
        elif len(cont_parts) >= 2:
            print(f"\nDetected {len(cont_parts)} container parts. Re-encoding to a single m4a…")
            lst = Path(out_dir) / 'book_list.txt'
            with lst.open('w') as f:
                for p in cont_parts:
                    f.write(f"file {shlex.quote(str(p.resolve()))}\n")
            outp = Path(out_dir) / 'book_01.m4a'
            rc = subprocess.run([
                'ffmpeg','-hide_banner','-nostdin','-y','-loglevel','warning','-f','concat','-safe','0','-i',str(lst),
                '-c:a','aac','-b:a','128k','-movflags','+faststart',str(outp)
            ]).returncode
            if rc == 0:
                audio = outp
                robust_used = True
        elif dl_failed:
            return 1
    else:
        # Keep multiple files: do nothing; try to pick an output only if a single exists
        audio = None

    # If we still have no audio and no parts, stop here
    # Refresh file list for parts check
    all_files = [p for p in Path(out_dir).rglob('*') if p.is_file()]
    if audio is None and not any(p.suffix.lower()=='.aac' for p in all_files) and not any(p.suffix.lower() in ('.m4a','.m4b','.mp4') and _PART_RE.match(p.name) for p in all_files):
        # Last‑ditch: pick the largest file as the output guess
        guess = _largest_audio_guess(out_dir)
        if guess is not None:
            print(f"Assuming output file: {guess}")
            audio = guess
        else:
            print("No audio file was found in the output folder, and no parts were downloaded. Exiting.")
            # Print a short directory listing for debugging
            try:
                print("\nDirectory contents:")
                for p in sorted(all_files, key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
                    sz = p.stat().st_size
                    print(f"  {p}  ({sz/1_000_000:.1f} MB)")
            except Exception:
                pass
            return 1

    # Move provider aggregate files aside to reduce confusion, but keep the chosen output if it's the combined file
    try:
        excluded_dir = Path(out_dir) / 'Excluded'
        moved = 0
        for p in list(Path(out_dir).rglob('*')):
            if not (p.is_file() and p.suffix.lower() in ('.m4a', '.m4b', '.mp4') and '_all' in p.stem.lower()):
                continue
            # Do not move the selected audio file
            if audio is not None and p.resolve() == Path(audio).resolve():
                continue
            excluded_dir.mkdir(parents=True, exist_ok=True)
            p.rename(excluded_dir / p.name)
            moved += 1
        if moved:
            print(f"Moved {moved} provider aggregate file(s) to: {excluded_dir}")
    except Exception:
        pass

    # Cover detection; if missing and ISBN present, try to fetch; else warn
    cover = None
    cover = _find_cover_recursively(Path(out_dir))
    if cover is None and isbn:
        print("\nNo local cover found; attempting to fetch by ISBN…")
        fetched = fetch_cover_by_isbn(isbn, Path(out_dir))
        if fetched is not None:
            cover = fetched
            print(f"Fetched cover: {cover}")
    if cover is None and audio is not None:
        # If audio already has embedded cover, skip warning
        try:
            import json as _j, subprocess as _sp
            prob = _sp.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(audio)], capture_output=True, text=True)
            if prob.returncode == 0:
                data = _j.loads(prob.stdout or '{}')
                for s in data.get("streams", []):
                    if s.get("disposition", {}).get("attached_pic", 0) == 1:
                        print("\nCover already embedded in the audio; proceeding.")
                        cover = Path("(embedded)")
                        break
        except Exception:
            pass
    if cover is None:
        print("\nNo cover image found.")
        while True:
            choice = input("Type 'c' to continue, or 't' to try adding a cover: ").strip().lower() or 'c'
            if choice == 'c':
                break
            if choice == 't':
                cov_path = prompt("Provide path to a cover image (or press Enter to cancel)")
                if cov_path:
                    p = Path(cov_path).expanduser()
                    if p.exists():
                        dest = Path(out_dir) / p.name
                        try:
                            subprocess.run(["/bin/cp", str(p), str(dest)], check=True)
                            cover = dest
                            print(f"Added cover: {cover}")
                        except subprocess.CalledProcessError:
                            print("Failed to copy cover. You can try again or continue.")
                        continue
                continue

    # Optional metadata tagging if we have a single audio file
    if audio is not None and audio.exists():
        # Sanity: if robust_merge was selected but audiobook-dl also produced a single file,
        # prefer the robust output (book_01.m4a) if it exists and is larger.
        try:
            # Prefer verified robust output if present and not an aggregate
            robust_path = Path(out_dir) / "book_01.m4a"
            if robust_path.exists() and '_all' not in robust_path.stem.lower():
                if robust_path.stat().st_size > max(1, audio.stat().st_size):
                    print(f"Using robust merged output: {robust_path}")
                    audio = robust_path
        except Exception:
            pass
        # Build metadata from best available sources: audiobook-dl JSON, existing tags, ISBN lookup
        meta = {'title': '', 'author': '', 'album': '', 'year': ''}
        adl_meta = _read_any_metadata_json(Path(out_dir))
        for k in meta:
            if adl_meta.get(k):
                meta[k] = adl_meta[k]
        # existing tags on the audio file
        tags = _read_tags(Path(audio))
        def _pick(*keys):
            for k in keys:
                v = tags.get(k)
                if v:
                    return v
            return ''
        meta.setdefault('title', meta.get('title') or _pick('title'))
        meta.setdefault('album', meta.get('album') or _pick('album'))
        artist_tag = _pick('artist', 'album_artist', 'author')
        if artist_tag and not meta.get('author'):
            meta['author'] = artist_tag
        # ISBN as last resort
        if isbn and not meta.get('title'):
            try:
                with urllib.request.urlopen(f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}", timeout=10) as r:
                    g = json.load(r)
                if g.get("totalItems", 0) > 0 and g.get("items"):
                    info = g["items"][0]["volumeInfo"]
                    meta['title'] = info.get("title") or meta.get('title')
                    authors = info.get("authors") or []
                    if authors and not meta.get('author'):
                        meta['author'] = ", ".join(authors)
                    date = info.get("publishedDate") or ""
                    if date and not meta.get('year'):
                        meta['year'] = date[:4]
            except Exception:
                pass
            if not meta.get('title'):
                try:
                    with urllib.request.urlopen(f"https://openlibrary.org/isbn/{isbn}.json", timeout=10) as r:
                        o = json.load(r)
                    meta['title'] = meta.get('title') or (o.get("title") or "")
                    meta['year'] = meta.get('year') or (o.get("publish_date") or "")[:4]
                except Exception:
                    pass

        # Derive album from title if missing
        if not meta.get('album') and meta.get('title'):
            meta['album'] = meta['title']

        # If Combine was chosen and audio already has cover + title/artist, keep as-is (audiobook-dl tags are correct)
        has_cover = _has_embedded_cover(Path(audio))
        has_basic_tags = bool(meta.get('title')) and bool(meta.get('author'))
        if combine and has_cover and has_basic_tags:
            # No retagging needed; but normalize filename if we can
            desired_ext = out_fmt if out_fmt in ('m4a', 'm4b', 'mp3') else audio.suffix.lstrip('.')
            target_name = f"{slugify(meta['author'])}-{slugify(meta['title'])}.{desired_ext}"
            out_target = Path(out_dir) / target_name
            try:
                if out_target.resolve() != Path(audio).resolve():
                    Path(audio).rename(out_target)
                    audio = out_target
            except Exception:
                pass
        else:
            # Tag using make_audiobook.py with --single (preserves chapters)
            maker = Path(__file__).with_name("make_audiobook.py")
            # Decide final output extension based on user's choice when possible
            desired_ext = out_fmt if out_fmt in ('m4a', 'm4b', 'mp3') else audio.suffix.lstrip('.')
            # Prefer author-title naming when available
            base_name = None
            if meta.get('author') and meta.get('title'):
                base_name = f"{slugify(meta['author'])}-{slugify(meta['title'])}"
            else:
                base_name = slugify(Path(audio).stem)
            out_target = Path(out_dir) / f"{base_name}.{desired_ext}"
            orig_audio = Path(audio)
            cmd_tag = [
                sys.executable, str(maker),
                "--dir", str(out_dir),
                "--prefix", slugify(out_target.stem),
                "--single", str(audio),
                "--output", str(out_target),
            ]
            # Only add metadata we have
            if meta.get('title'):
                cmd_tag += ["--title", meta['title'], "--album", meta.get('album') or meta['title']]
            if meta.get('author'):
                cmd_tag += ["--artist", meta['author'], "--album-artist", meta['author']]
            if meta.get('year'):
                cmd_tag += ["--year", meta['year']]
            if isbn:
                cmd_tag += ["--isbn", isbn]
            if cover is not None and cover.exists():
                cmd_tag += ["--cover", str(cover)]
            rc, _ = run_cmd_spinner(cmd_tag, "Tagging audiobook and embedding cover…")
            if rc == 0:
                print("\nTagged audiobook with available metadata.")
                # Replace audio with the tagged output and remove the old file if different
                audio = out_target
                try:
                    if orig_audio.exists() and orig_audio.resolve() != out_target.resolve():
                        orig_audio.unlink(missing_ok=True)
                except Exception:
                    pass
            else:
                print("Tagging step failed. The audio file is still available.")

    # Offer cleanup of part files if we built a robust single file
    if audio is not None and robust_used:
        try:
            aac_parts2, cont_parts2, _ = _strict_find_parts(out_dir)
            total_parts = len(aac_parts2) + len(cont_parts2)
            if total_parts > 0:
                if yesno(f"Clean up {total_parts} downloaded part file(s) before opening the folder?", True):
                    removed = 0
                    for p in aac_parts2 + cont_parts2:
                        try:
                            if p.resolve() == Path(audio).resolve():
                                continue
                            p.unlink(missing_ok=True)
                            removed += 1
                        except Exception:
                            pass
                    # Remove list files used for concat/verify
                    for pat in ("*_list_*.txt", "verify_list.txt", "book_list.txt"):
                        for lf in Path(out_dir).glob(pat):
                            try:
                                lf.unlink(missing_ok=True)
                            except Exception:
                                pass
                    print(f"Removed {removed} part files.")
        except Exception:
            pass

    # Remove JSON metadata files unless the user chose to keep them
    try:
        if not keep_json_meta:
            removed = 0
            for jf in Path(out_dir).rglob('*.json'):
                try:
                    jf.unlink(missing_ok=True)
                    removed += 1
                except Exception:
                    pass
            if removed:
                print(f"Removed {removed} JSON metadata file(s).")
    except Exception:
        pass

    # Final prompt: open the output folder
    end_choice = choose_menu("All done.", ["Open the output folder", "Finish"], default_idx=0)
    if end_choice == 0:
        try:
            subprocess.run(["open", str(Path(out_base))], check=False)
        except Exception:
            pass

    print("\nDone. Output saved in:")
    print(f"  {out_dir}")
    if audio:
        print(f"  File: {audio}")
    print("Open the folder to find the audiobook file.")
    if library == "nextory":
        print("Note: For Nextory, the book must be added to your 'want to read' list.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
