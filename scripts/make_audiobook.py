#!/usr/bin/env python3
# Merge chapter .m4a files into one audiobook and embed metadata/cover.
import argparse
# Contributors: see write_ffmetadata() for chapter timing model (ms timebase)
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple


def find_cover_image(dir_path: Path) -> Optional[Path]:
    for name in ["cover.jpg", "cover.jpeg", "cover.png", "Cover.jpg", "Cover.jpeg", "Cover.png"]:
        p = dir_path / name
        if p.exists():
            return p
    # fallback: any image
    for ext in ["*.jpg", "*.jpeg", "*.png"]:
        imgs = sorted(dir_path.glob(ext))
        if imgs:
            return imgs[0]
    return None


def sorted_chunks(output_dir: Path, prefix: str) -> List[Path]:
    files = sorted(output_dir.glob(f"{prefix}_*.m4a"))
    return files


def write_concat_list(path: Path, files: List[Path]):
    with path.open("w", encoding="utf-8") as f:
        for p in files:
            f.write(f"file {shlex.quote(str(p.resolve()))}\n")


def ffprobe_duration_ms(path: Path) -> int:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    out = subprocess.check_output(cmd)
    data = json.loads(out)
    dur = float(data["format"]["duration"]) if "format" in data and "duration" in data["format"] else 0.0
    return int(round(dur * 1000))


def write_ffmetadata(path: Path, chapter_durations_ms: List[int], titles: List[str], global_tags: List[Tuple[str, str]]):
    with path.open("w", encoding="utf-8") as f:
        f.write(";FFMETADATA1\n")
        for k, v in global_tags:
            f.write(f"{k}={v}\n")
        start = 0
        for i, dur in enumerate(chapter_durations_ms, start=1):
            end = start + max(0, dur)
            f.write("\n[CHAPTER]\nTIMEBASE=1/1000\n")
            f.write(f"START={start}\nEND={end}\n")
            f.write(f"title={titles[i-1]}\n")
            start = end


def run(cmd: List[str]):
    print("$", " ".join(shlex.quote(c) for c in cmd))
    subprocess.check_call(cmd)


def main():
    ap = argparse.ArgumentParser(description="Merge chunked .m4a chapters into a single audiobook with metadata and cover.")
    ap.add_argument("--dir", type=Path, required=True, help="Directory containing chapter .m4a files and where to write outputs")
    ap.add_argument("--prefix", type=str, required=True, help="Prefix of chapter files (e.g., 'uperfekte')")
    ap.add_argument("--single", type=Path, default=None, help="If provided, use this single input file instead of merging chapters")
    ap.add_argument("--title", type=str, default=None, help="Audiobook title")
    ap.add_argument("--artist", type=str, default="", help="Artist/Author (often narrator or author)")
    ap.add_argument("--album-artist", dest="album_artist", type=str, default="", help="Album artist (often the author)")
    ap.add_argument("--album", type=str, default=None, help="Album name; defaults to title")
    ap.add_argument("--year", type=str, default="", help="Year")
    ap.add_argument("--isbn", type=str, default="", help="ISBN to store in comment")
    ap.add_argument("--comment", type=str, default="", help="Extra comment text to include (e.g., source URL)")
    ap.add_argument("--cover", type=Path, default=None, help="Path to cover image (jpg/png)")
    ap.add_argument("--output", type=Path, default=None, help="Final output path (default: <prefix>_all.m4a in dir)")

    args = ap.parse_args()
    outdir = args.dir.expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    merged_path = None
    durations = []
    titles = []
    if args.single is not None:
        merged_path = args.single.resolve()
    else:
        chapters = sorted_chunks(outdir, args.prefix)
        if not chapters:
            raise SystemExit(f"No chapters found matching {args.prefix}_*.m4a in {outdir}")
        list_path = outdir / f"{args.prefix}_all_list.txt"
        write_concat_list(list_path, chapters)
        merged_path = outdir / f"{args.prefix}_all_merged.m4a"
        run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "warning", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(merged_path)])
        # Build chapter metadata from durations
        durations = [ffprobe_duration_ms(p) for p in chapters]
        titles = [f"Chapter {i:02d}" for i in range(1, len(chapters) + 1)]
    title = args.title or args.prefix
    album = args.album or title
    # Add common tags for audiobooks; include iTunes 'stik' atom to classify as Audiobook (2)
    global_tags = [
        ("title", title),
        ("album", album),
        ("artist", args.artist),
        ("album_artist", args.album_artist),
        ("date", args.year),
        ("genre", "Audiobook"),
        ("stik", "2"),
    ]
    parts = []
    if args.isbn:
        parts.append(f"ISBN {args.isbn}")
    if args.comment:
        parts.append(args.comment)
    comment = " • ".join(parts)
    if comment:
        global_tags.append(("comment", comment))

    # Only write ffmetadata if we constructed chapters; when using --single, preserve existing chapters
    meta_path = None
    if durations:
        meta_path = outdir / f"{args.prefix}_all_metadata.txt"
        write_ffmetadata(meta_path, durations, titles, global_tags)

    # Resolve cover
    cover = args.cover
    if cover is None:
        cover = find_cover_image(outdir)
    if cover is None:
        print("Warning: No cover image found; proceeding without cover.")

    final_path = args.output or (outdir / f"{args.prefix}_all.m4a")
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "warning", "-i", str(merged_path)]
    if cover is not None:
        cmd += ["-i", str(cover)]
    # Apply global metadata directly to avoid clobbering existing chapters when using --single
    for k, v in global_tags:
        if v:
            cmd += ["-metadata", f"{k}={v}"]
    if meta_path is not None:
        cmd += ["-f", "ffmetadata", "-i", str(meta_path), "-map_metadata", "2"]

    ext = final_path.suffix.lower()
    # Map streams and codecs based on target container
    if ext in (".m4a", ".m4b"):
        if cover is not None:
            cmd += ["-map", "0:a", "-map", "1:v", "-c:a", "copy", "-c:v", "mjpeg", "-disposition:v", "attached_pic"]
        else:
            cmd += ["-map", "0:a", "-c:a", "copy"]
        cmd += ["-movflags", "+faststart", str(final_path)]
    elif ext == ".mp3":
        # Re-encode to MP3 and embed cover as ID3 APIC
        if cover is not None:
            cmd += ["-map", "0:a", "-map", "1:v:0", "-c:a", "libmp3lame", "-b:a", "192k", "-c:v", "mjpeg", "-id3v2_version", "3", str(final_path)]
        else:
            cmd += ["-map", "0:a", "-c:a", "libmp3lame", "-b:a", "192k", "-id3v2_version", "3", str(final_path)]
    else:
        # Default fallback: copy audio and try to attach cover
        if cover is not None:
            cmd += ["-map", "0:a", "-map", "1:v", "-c:a", "copy", "-c:v", "mjpeg", str(final_path)]
        else:
            cmd += ["-map", "0:a", "-c:a", "copy", str(final_path)]
    run(cmd)

    print(f"Created: {final_path}")


if __name__ == "__main__":
    main()
