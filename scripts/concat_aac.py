#!/usr/bin/env python3
#
# Concatenate many .aac (ADTS) parts into fewer outputs using ffmpeg.
#
# Key implementation choices:
# - We prefer ffmpeg concat demuxer when possible, but for huge sets or noisy
#   inputs we expose a "rawcat" method (byte‑append ADTS frames) that is far
#   more tolerant. Rawcat can then remux to m4a or re‑encode to stabilize.
# - File ordering uses natural sort to handle numbered parts correctly.
# - List files are written with absolute, shell‑quoted paths to avoid cwd issues.
#
import argparse
import math
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import List


def natural_key(s: str):
    import re

    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", s)]


def find_aac_files(input_dir: Path) -> List[Path]:
    # Search recursively; providers may nest parts in subfolders
    files = sorted((p for p in input_dir.rglob("*.aac") if p.is_file()), key=lambda p: natural_key(p.name))
    return files


def chunk(items: List[Path], parts: int) -> List[List[Path]]:
    if parts <= 1:
        return [items]
    n = len(items)
    if n == 0:
        return [[]]
    base = n // parts
    rem = n % parts
    chunks = []
    start = 0
    for i in range(parts):
        size = base + (1 if i < rem else 0)
        end = start + size
        chunks.append(items[start:end])
        start = end
    return chunks


def write_concat_list(list_path: Path, files: List[Path]):
    with list_path.open("w", encoding="utf-8") as f:
        for p in files:
            # Use absolute paths to avoid cwd issues; quote path via shlex
            ap = p.resolve()
            f.write(f"file {shlex.quote(str(ap))}\n")


def run_ffmpeg_concat(
    list_path: Path,
    out_path: Path,
    container: str,
    ffmpeg: str,
    loglevel: str,
    dry_run: bool,
    reencode: bool = False,
    bitrate: str = "128k",
    progress: bool = False,
    total_ms: int = 0,
):
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-loglevel",
        loglevel,
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
    ]
    if reencode:
        cmd += ["-c:a", "aac", "-b:a", bitrate]
    else:
        cmd += ["-c", "copy"]
        if container == "m4a":
            # Convert ADTS bitstream to MP4 container without re-encoding
            cmd += ["-bsf:a", "aac_adtstoasc"]
    if container == "m4a":
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(out_path))
    print("$", " ".join(shlex.quote(c) for c in cmd))
    if dry_run:
        return 0
    if not progress:
        return subprocess.call(cmd)
    # With progress: run ffmpeg with -progress pipe:1 and parse out_time_ms
    # Rebuild the command to include progress reporting
    cmd_progress = cmd[:]
    # insert after binary
    insert_at = 1
    cmd_progress[insert_at:insert_at] = ["-progress", "pipe:1", "-nostats"]
    start = time.time()
    try:
        proc = subprocess.Popen(cmd_progress, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except Exception:
        return subprocess.call(cmd)
    last_ms = 0
    while True:
        line = proc.stdout.readline() if proc.stdout else ''
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
            continue
        line = line.strip()
        if line.startswith('out_time_ms='):
            try:
                out_ms = int(line.split('=',1)[1])
            except Exception:
                out_ms = 0
            last_ms = max(last_ms, out_ms)
            pct = 0
            if total_ms > 0:
                pct = int(min(100, max(0, (out_ms * 100) // total_ms)))
            elapsed = time.time() - start
            eta = 0
            if pct > 0 and elapsed > 0:
                eta = int(elapsed * (100 - pct) / pct)
            print(f"PROGRESS phase=ffmpeg out_time_ms={out_ms} total_ms={total_ms} pct={pct} elapsed_s={int(elapsed)} eta_s={eta}")
        elif line == 'progress=end':
            break
    return proc.wait()


def run_ffmpeg_transcode(
    in_path: Path,
    out_path: Path,
    ffmpeg: str,
    loglevel: str,
    audio_codec: str = "aac",
    bitrate: str = "128k",
    container: str = "m4a",
    progress: bool = False,
    total_ms: int = 0,
):
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-loglevel",
        loglevel,
        "-i",
        str(in_path),
        "-c:a",
        audio_codec,
        "-b:a",
        bitrate,
    ]
    if container == "m4a":
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(out_path))
    print("$", " ".join(shlex.quote(c) for c in cmd))
    if not progress:
        return subprocess.call(cmd)
    # With progress, use -progress pipe:1 and parse out_time_ms
    cmd_progress = cmd[:]
    insert_at = 1
    cmd_progress[insert_at:insert_at] = ["-progress", "pipe:1", "-nostats"]
    start = time.time()
    try:
        proc = subprocess.Popen(cmd_progress, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except Exception:
        return subprocess.call(cmd)
    last_ms = 0
    while True:
        line = proc.stdout.readline() if proc.stdout else ''
        if not line:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
            continue
        line = line.strip()
        if line.startswith('out_time_ms='):
            try:
                out_ms = int(line.split('=',1)[1])
            except Exception:
                out_ms = 0
            last_ms = max(last_ms, out_ms)
            pct = 0
            if total_ms > 0:
                pct = int(min(100, max(0, (out_ms * 100) // total_ms)))
            elapsed = time.time() - start
            eta = 0
            if pct > 0 and elapsed > 0:
                eta = int(elapsed * (100 - pct) / pct)
            print(f"PROGRESS phase=reencode out_time_ms={out_ms} total_ms={total_ms} pct={pct} elapsed_s={int(elapsed)} eta_s={eta}")
        elif line == 'progress=end':
            break
    return proc.wait()
 
def ffprobe_duration_ms(path: Path) -> int:
    try:
        out = subprocess.check_output([
            'ffprobe','-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',str(path)
        ], text=True)
        return int(float(out.strip())*1000)
    except Exception:
        return 0


def ffprobe_total_ms_from_list(list_path: Path) -> int:
    try:
        out = subprocess.check_output([
            'ffprobe','-v','error','-f','concat','-safe','0','-i',str(list_path),
            '-show_entries','format=duration','-of','default=nw=1:nk=1'
        ], text=True)
        return int(float(out.strip())*1000)
    except Exception:
        return 0


def _copy_adts_frames_only(src: Path, outfh) -> int:
    """Copy only valid ADTS frames from src into outfh.

    Why frames-only? Some sources sprinkle ID3 tags or junk bytes at
    boundaries; blindly concatenating bytes can place those mid‑stream and
    break decoders. This scanner resynchronizes on the ADTS sync word and
    writes only well‑formed frames, trading a tiny amount of CPU for stability.
    """
    data = src.read_bytes()
    n = len(data)
    i = 0
    wrote = 0
    while i + 7 <= n:
        if data[i] == 0xFF and (data[i + 1] & 0xF0) == 0xF0 and (data[i + 1] & 0x06) == 0x00:
            # ADTS header detected; compute frame length (13 bits)
            if i + 6 >= n:
                break
            frame_length = ((data[i + 3] & 0x03) << 11) | (data[i + 4] << 3) | ((data[i + 5] & 0xE0) >> 5)
            if frame_length < 7 or i + frame_length > n:
                # Invalid length; step forward to resync
                i += 1
                continue
            outfh.write(data[i : i + frame_length])
            wrote += frame_length
            i += frame_length
        else:
            # Not at sync word; advance
            i += 1
    # If nothing was recognized, fall back to raw copy
    if wrote == 0:
        outfh.write(data)
        wrote = len(data)
    return wrote


def main():
    ap = argparse.ArgumentParser(description="Concatenate many .aac parts using ffmpeg concat demuxer, optionally in chunks.")
    ap.add_argument("--input-dir", required=True, type=Path, help="Directory containing .aac parts")
    ap.add_argument("--output-dir", type=Path, default=Path.cwd(), help="Where to write outputs and list files")
    ap.add_argument("--chunks", type=int, default=1, help="Number of chunked outputs to produce (e.g., 12)")
    ap.add_argument("--prefix", type=str, default="concat", help="Base name for outputs and lists")
    ap.add_argument("--container", choices=["aac", "m4a"], default="aac", help="Output container format")
    ap.add_argument("--method", choices=["demux", "rawcat"], default="demux", help="Concat via ffmpeg demuxer or raw file concatenation")
    ap.add_argument("--reencode", action="store_true", help="Re-encode to AAC instead of stream copy")
    ap.add_argument("--bitrate", type=str, default="128k", help="Bitrate when re-encoding (e.g., 128k, 192k)")
    ap.add_argument("--merge-output", type=Path, default=None, help="If set, merge the chunk outputs into this final file")
    ap.add_argument("--verify", action="store_true", help="Verify merged duration ~= sum of parts; exit nonzero if short")
    ap.add_argument("--progress", action="store_true", help="Emit machine-readable progress lines for UI consumption")
    ap.add_argument("--list-only", action="store_true", help="Only generate list files; do not run ffmpeg")
    ap.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    ap.add_argument("--ffmpeg", type=str, default="ffmpeg", help="Path to ffmpeg binary")
    ap.add_argument("--loglevel", type=str, default="info", help="ffmpeg loglevel (quiet, error, warning, info)")

    args = ap.parse_args()

    if not shutil.which(args.ffmpeg):
        print(f"Error: ffmpeg not found at '{args.ffmpeg}'. Install ffmpeg and try again.")
        raise SystemExit(1)

    input_dir = args.input_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    files = find_aac_files(input_dir)
    if not files:
        print(f"No .aac files found in {input_dir}")
        raise SystemExit(1)

    chunks = chunk(files, max(1, args.chunks))

    width = max(2, int(math.ceil(math.log10(max(1, len(chunks)) + 1))))
    list_paths = []
    out_paths = []

    for idx, group in enumerate(chunks, start=1):
        list_name = f"{args.prefix}_list_{idx:0{width}d}.txt"
        list_path = output_dir / list_name
        write_concat_list(list_path, group)
        list_paths.append(list_path)

        ext = args.container
        out_name = f"{args.prefix}_{idx:0{width}d}.{ext}"
        out_path = output_dir / out_name
        out_paths.append(out_path)
        print(f"Wrote list: {list_path} ({len(group)} files)")

        if args.list_only:
            continue

        if args.method == "demux":
            tot_ms = 0
            if args.progress:
                tot_ms = ffprobe_total_ms_from_list(list_path)
            rc = run_ffmpeg_concat(
                list_path,
                out_path,
                args.container,
                args.ffmpeg,
                args.loglevel,
                args.dry_run,
                reencode=args.reencode,
                bitrate=args.bitrate,
                progress=args.progress,
                total_ms=tot_ms,
            )
            if rc != 0:
                print(f"ffmpeg failed for {list_path} -> {out_path} (code {rc})")
                raise SystemExit(rc)
            # Optional duration verification for this chunk
            if args.verify and not args.dry_run:
                try:
                    parts_ms = sum(ffprobe_duration_ms(p) for p in group)
                    out_ms = ffprobe_duration_ms(out_path)
                    if parts_ms > 0 and out_ms < int(0.99 * parts_ms):
                        print(f"Verification failed: output shorter than sum ({out_ms/1000:.1f}s < {parts_ms/1000:.1f}s)")
                        raise SystemExit(2)
                except Exception:
                    pass
        else:
            # rawcat: concatenate ADTS bitstreams, then (optionally) remux or re-encode
            tmp_aac = output_dir / f"{args.prefix}_{idx:0{width}d}.adts.aac"
            print(f"Concatenating {len(group)} files into {tmp_aac}")
            if args.dry_run:
                continue
            start = time.time()
            parts_total = max(1, len(group))
            with tmp_aac.open("wb") as outfh:
                for i, p in enumerate(group, start=1):
                    _ = _copy_adts_frames_only(p, outfh)
                    if args.progress:
                        pct = int((i * 100) // parts_total)
                        elapsed = int(time.time() - start)
                        eta = int(elapsed * (parts_total - i) / max(1, i)) if i > 0 else 0
                        print(f"PROGRESS phase=rawcat parts_done={i}/{parts_total} pct={pct} elapsed_s={elapsed} eta_s={eta}")
            if args.container == "aac":
                if args.reencode:
                    tot = ffprobe_duration_ms(tmp_aac) if args.progress else 0
                    rc = 0 if args.dry_run else run_ffmpeg_transcode(tmp_aac, out_path, args.ffmpeg, args.loglevel, audio_codec="aac", bitrate=args.bitrate, container="aac", progress=args.progress, total_ms=tot)
                    if rc != 0:
                        print(f"ffmpeg failed (rawcat aac) -> {out_path} (code {rc})")
                        raise SystemExit(rc)
                    tmp_aac.unlink(missing_ok=True)
                else:
                    tmp_aac.replace(out_path)
            else:
                if args.reencode:
                    tot = ffprobe_duration_ms(tmp_aac) if args.progress else 0
                    rc = 0 if args.dry_run else run_ffmpeg_transcode(tmp_aac, out_path, args.ffmpeg, args.loglevel, audio_codec="aac", bitrate=args.bitrate, container="m4a", progress=args.progress, total_ms=tot)
                else:
                    cmd = [args.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", args.loglevel, "-i", str(tmp_aac), "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out_path)]
                    print("$", " ".join(shlex.quote(c) for c in cmd))
                    rc = 0 if args.dry_run else subprocess.call(cmd)
                if rc != 0:
                    print(f"ffmpeg failed (rawcat m4a) -> {out_path} (code {rc})")
                    raise SystemExit(rc)
                tmp_aac.unlink(missing_ok=True)

    if args.merge_output:
        # Build a list from the chunk outputs and merge once more
        merged_list = output_dir / f"{args.prefix}_merge_list.txt"
        write_concat_list(merged_list, out_paths)
        print(f"Wrote merge list: {merged_list} ({len(out_paths)} parts)")

        if not args.list_only:
            tot_ms = 0
            if args.progress:
                tot_ms = ffprobe_total_ms_from_list(merged_list)
            rc = run_ffmpeg_concat(
                merged_list,
                args.merge_output.resolve(),
                args.container,
                args.ffmpeg,
                args.loglevel,
                args.dry_run,
                reencode=args.reencode,
                bitrate=args.bitrate,
                progress=args.progress,
                total_ms=tot_ms,
            )
            if rc != 0:
                print(f"ffmpeg failed for merge -> {args.merge_output} (code {rc})")
                raise SystemExit(rc)
            if args.verify and not args.dry_run:
                try:
                    parts_ms = sum(ffprobe_duration_ms(p) for p in out_paths)
                    out_ms = ffprobe_duration_ms(args.merge_output.resolve())
                    if parts_ms > 0 and out_ms < int(0.99 * parts_ms):
                        print(f"Verification failed: merged output shorter than sum ({out_ms/1000:.1f}s < {parts_ms/1000:.1f}s)")
                        raise SystemExit(2)
                except Exception:
                    pass

    print("Done.")


if __name__ == "__main__":
    main()
