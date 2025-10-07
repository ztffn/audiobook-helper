#!/usr/bin/env python3
#
# End‑to‑end pipeline driver.
#
# Responsibilities:
# - Optionally download with audiobook‑dl (supports combine or parts).
# - If a single combined file was produced, tag it via make_audiobook.py --single.
# - Otherwise, build robust chapters from .aac parts via concat_aac.py and tag.
# - Look up metadata by ISBN (Google/Open Library) to auto‑fill title/author/year.
# Keep I/O and branching here; push media work to the helper scripts.
import argparse
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Dict, Optional


def run(cmd):
    print("$", " ".join(shlex.quote(c) for c in cmd))
    return subprocess.call(cmd)


def fetch_metadata(isbn: str) -> Dict[str, str]:
    """Best‑effort metadata lookup.
    Keep timeouts short; pipeline should not block on network availability.
    """
    import urllib.request

    res: Dict[str, str] = {}
    # Try Google Books
    try:
        with urllib.request.urlopen(f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}", timeout=15) as r:
            g = json.load(r)
        if g.get("totalItems", 0) > 0 and g.get("items"):
            info = g["items"][0]["volumeInfo"]
            res["title"] = info.get("title") or ""
            authors = info.get("authors") or []
            if authors:
                res["author"] = ", ".join(authors)
            date = info.get("publishedDate") or ""
            res["year"] = (date[:4] if len(date) >= 4 else "")
    except Exception:
        pass
    # Fallback Open Library
    if not res.get("title"):
        try:
            with urllib.request.urlopen(f"https://openlibrary.org/isbn/{isbn}.json", timeout=15) as r:
                o = json.load(r)
            res["title"] = o.get("title") or ""
            date = o.get("publish_date") or ""
            # try last 4 numeric chars as year
            y = "".join([c for c in date if c.isdigit()])
            res["year"] = y[:4]
            # resolve authors
            authors = []
            for a in o.get("authors", []):
                key = a.get("key")
                if not key:
                    continue
                try:
                    with urllib.request.urlopen(f"https://openlibrary.org{key}.json", timeout=15) as ar:
                        ad = json.load(ar)
                    nm = ad.get("name")
                    if nm:
                        authors.append(nm)
                except Exception:
                    pass
            if authors:
                res["author"] = ", ".join(authors)
        except Exception:
            pass
    return res


def ensure_prefix_from_dir(prefix: Optional[str], input_dir: Path) -> str:
    if prefix:
        return prefix
    return input_dir.name.replace(" ", "_")


def main():
    ap = argparse.ArgumentParser(description="End-to-end audiobook pipeline: parts -> chapters -> single with metadata")
    ap.add_argument("--input-dir", type=Path, required=True, help="Directory with .aac parts")
    ap.add_argument("--output-dir", type=Path, required=True, help="Directory to write outputs")
    ap.add_argument("--prefix", type=str, default=None, help="Base name for outputs (defaults to input directory name)")
    ap.add_argument("--chunks", type=int, default=12, help="Number of chapter outputs to create")
    ap.add_argument("--bitrate", type=str, default="128k", help="AAC bitrate for re-encode")
    ap.add_argument("--isbn", type=str, default="", help="ISBN to fetch metadata (optional but recommended)")
    ap.add_argument("--title", type=str, default="", help="Override title")
    ap.add_argument("--author", type=str, default="", help="Override author")
    ap.add_argument("--year", type=str, default="", help="Override year")
    ap.add_argument("--cover", type=Path, default=None, help="Cover image (jpg/png); autodetected if omitted")
    ap.add_argument("--skip-build", action="store_true", help="Skip chapter build step (if already built)")
    # Download step (audiobook-dl)
    ap.add_argument("--source-url", type=str, default="", help="Source URL for audiobook-dl (step 1)")
    ap.add_argument("--cookies", type=Path, default=None, help="Cookies file for audiobook-dl")
    ap.add_argument("--username", type=str, default="", help="Username for audiobook-dl")
    ap.add_argument("--password", type=str, default="", help="Password for audiobook-dl")
    ap.add_argument("--library", type=str, default="", help="Library for audiobook-dl (if required)")
    ap.add_argument("--adl-format", type=str, default="m4a", help="Output format for audiobook-dl (m4a/m4b/mp3)")
    ap.add_argument("--adl-combine", action="store_true", help="Ask audiobook-dl to combine into a single file")

    args = ap.parse_args()
    input_dir = args.input_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = ensure_prefix_from_dir(args.prefix, input_dir)

    # Step 0: optional download via audiobook-dl
    single_input = None
    if args.source_url:
        # Note: We build the cmd based on provided auth; if combine fails,
        # the easy wrapper fallback (rawcat) is still available outside.
        cmd = ["audiobook-dl", "--output", str(output_dir), "--output-format", args.adl_format]
        if args.adl_combine:
            cmd += ["--combine"]
        if args.cookies is not None:
            cmd += ["--cookies", str(args.cookies)]
        if args.username:
            cmd += ["--username", args.username]
        if args.password:
            cmd += ["--password", args.password]
        if args.library:
            cmd += ["--library", args.library]
        cmd += [args.source_url]
        rc = run(cmd)
        if rc != 0:
            raise SystemExit(rc)
        # Try to find a combined file
        cands = list(output_dir.rglob("*.m4a")) + list(output_dir.rglob("*.m4b"))
        if cands:
            # pick newest
            cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            single_input = cands[0]

    # Step 1: build chapters unless skipped or single_input is provided
    if not args.skip_build:
        if single_input is None:
            rc = run([
                "python3", str(Path(__file__).with_name("concat_aac.py")),
                "--input-dir", str(input_dir),
                "--output-dir", str(output_dir),
                "--chunks", str(args.chunks),
                "--prefix", prefix,
                "--container", "m4a",
                "--method", "rawcat",
                "--reencode",
                "--bitrate", args.bitrate,
                "--loglevel", "warning",
            ])
            if rc != 0:
                raise SystemExit(rc)

    # Step 2: collect metadata
    meta = {"title": args.title, "author": args.author, "year": args.year}
    if args.isbn:
        looked = fetch_metadata(args.isbn)
        # Fill only missing fields
        for k in ("title", "author", "year"):
            if not meta.get(k):
                meta[k] = looked.get(k, meta.get(k))
    # Reasonable defaults
    if not meta["title"]:
        meta["title"] = prefix
    if not meta["author"]:
        meta["author"] = "Unknown"

    # Step 3: merge + tag
    cmd = ["python3", str(Path(__file__).with_name("make_audiobook.py")), "--dir", str(output_dir), "--prefix", prefix,
           "--title", meta["title"], "--artist", meta["author"], "--album-artist", meta["author"], "--album", meta["title"],
           "--year", meta.get("year", ""), "--isbn", args.isbn or ""]
    if single_input is not None:
        cmd += ["--single", str(single_input)]
    if args.cover is not None:
        cmd += ["--cover", str(args.cover)]
    rc = run(cmd)
    if rc != 0:
        raise SystemExit(rc)

    print("Done. See:")
    print(f"  Chapters: {output_dir}/{prefix}_01.m4a … {output_dir}/{prefix}_{args.chunks:02d}.m4a")
    print(f"  Single:   {output_dir}/{prefix}_all.m4a")


if __name__ == "__main__":
    main()
