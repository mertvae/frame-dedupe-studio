"""Remove identical decoded frames from a folder of videos."""
import argparse
from fractions import Fraction
import json
import re
from pathlib import Path
import shutil
import subprocess
import tempfile

EXTENSIONS = {".mp4", ".mov", ".mkv"}


def run(*command):
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                            errors="replace")
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"{command[0]} failed")
    return result.stdout


def media_properties(path):
    result = run("ffprobe", "-v", "error", "-show_entries",
                 "stream=codec_type,avg_frame_rate,r_frame_rate,start_time,sample_rate",
                 "-of", "json", str(path))
    streams = json.loads(result)["streams"]
    video = next((stream for stream in streams if stream["codec_type"] == "video"), None)
    if video is None:
        raise ValueError("No video stream")
    fps = next((Fraction(video[key]) for key in ("avg_frame_rate", "r_frame_rate")
                if video.get(key) not in (None, "0/0", "0/1")), None)
    if fps is None or fps <= 0:
        raise ValueError("Cannot determine frame rate")
    audio_rates = [int(stream["sample_rate"]) for stream in streams
                   if stream["codec_type"] == "audio"]
    return fps, float(video.get("start_time", 0)), audio_rates


def inspect(path):
    fps, _, audio_rates = media_properties(path)
    return fps, bool(audio_rates)


def frame_hashes(path):
    result = run("ffmpeg", "-v", "error", "-noautorotate", "-i", str(path),
                 "-map", "0:v:0", "-an", "-fps_mode", "passthrough",
                 "-f", "framehash", "-hash", "sha256", "-")
    return [line.rsplit(",", 1)[-1].strip() for line in result.splitlines()
            if line and not line.startswith("#")]


def duplicate_indices(hashes, mode):
    if mode == "adjacent":
        return [i for i in range(1, len(hashes)) if hashes[i] == hashes[i - 1]]
    seen = set()
    duplicates = []
    for index, digest in enumerate(hashes):
        if digest in seen:
            duplicates.append(index)
        seen.add(digest)
    return duplicates


def find_duplicates(path, mode):
    if mode in {"adjacent", "all"}:
        hashes = frame_hashes(path)
        return len(hashes), duplicate_indices(hashes, mode)

    # Keep frame indices as timestamps so the filter reports the originals.
    threshold = "hi=512:lo=160:frac=0.18" if mode == "visual" else "hi=1024:lo=320:frac=0.35"
    graph = f"settb=1/1000,setpts=N,split[all][candidate];" \
            f"[all]showinfo@original[original];" \
            f"[candidate]mpdecimate={threshold},showinfo@kept[kept]"
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-noautorotate",
         "-i", str(path), "-filter_complex", graph, "-map", "[original]", "-map",
         "[kept]", "-an", "-fps_mode", "passthrough", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    original = re.findall(r"showinfo@original[^\n]*?\bn:\s*\d+\s+pts:\s*(\d+)", result.stderr)
    kept = {int(index) for index in re.findall(
        r"showinfo@kept[^\n]*?\bn:\s*\d+\s+pts:\s*(\d+)", result.stderr)}
    if not original or not kept:
        raise ValueError("Could not read frame indices from FFmpeg.")
    return len(original), [int(index) for index in original if int(index) not in kept]


def frame_ranges(duplicates, total):
    """Return half-open ranges of frames to keep."""
    first = 0
    for index in duplicates:
        if index > first:
            yield first, index
        first = index + 1
    if first < total:
        yield first, total


def select_expression(ranges):
    """Build a balanced decision tree for FFmpeg's expression evaluator."""
    if len(ranges) == 1:
        first, end = ranges[0]
        return f"eq(n,{first})" if end == first + 1 else f"between(n,{first},{end - 1})"
    middle = len(ranges) // 2
    return (f"if(lt(n,{ranges[middle][0]}),"
            f"{select_expression(ranges[:middle])},{select_expression(ranges[middle:])})")


def audio_filters(track, rate, fps, start, ranges, kept):
    # Align decoded audio to video time zero, preserving any initial delay.
    # Cutting by sample index avoids rounding each join to a video timebase.
    prefix = f"a{track}"
    segments = [f"{prefix}s{i}" for i in range(len(ranges))]
    statements = [
        f"[0:a:{track}]asetpts=PTS-({start:.6f})/TB,"
        f"aresample={rate}:async=1:first_pts=0,apad"
    ]
    if len(ranges) > 1:
        statements[0] += f",asplit={len(ranges)}"
    statements[0] += "".join(f"[{name}]" for name in segments)

    joined = []
    for index, ((first, end), segment) in enumerate(zip(ranges, segments)):
        output = f"[{prefix}r{index}]"
        low, high = round(first * rate / fps), round(end * rate / fps)
        statements.append(f"[{segment}]atrim=start_sample={low}:end_sample={high},"
                          f"asetpts=PTS-STARTPTS{output}")
        joined.append(output)

    final = f"apad,atrim=end_sample={round(kept * rate / fps)}"
    if len(joined) > 1:
        final = f"concat=n={len(joined)}:v=0:a=1,{final}"
    statements.append("".join(joined) + final + f"[{prefix}out]")
    return statements


def process(source, destination, args):
    fps, start, audio_rates = media_properties(source)
    total, duplicates = find_duplicates(source, args.mode)
    if not total:
        raise ValueError("No decoded frames")
    kept = total - len(duplicates)
    print(f"{source.name}: {total} -> {kept} frames", flush=True)
    if args.dry_run:
        return
    if destination.exists():
        raise FileExistsError(f"Output already exists: {destination}")

    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        temp = Path(temp)
        output = temp / destination.name
        if not duplicates:
            shutil.copyfile(source, output)
        else:
            keep_ranges = list(frame_ranges(duplicates, total))
            statements = [f"[0:v:0]select='{select_expression(keep_ranges)}',"
                          f"settb=expr={fps.denominator}/{fps.numerator},setpts=N[vout]"]
            maps = ["-map", "[vout]"]
            audio_options = ["-an"]
            if audio_rates and not args.drop_audio:
                for track, rate in enumerate(audio_rates):
                    statements.extend(audio_filters(track, rate, fps, start, keep_ranges, kept))
                    maps.extend(("-map", f"[a{track}out]"))
                audio_options = ["-c:a", "aac", "-b:a", "192k"]
            graph = temp / "filter.txt"
            graph.write_text(";\n".join(statements), encoding="ascii")
            run("ffmpeg", "-v", "error", "-copyts", "-noautorotate", "-i", str(source),
                "-filter_complex_threads", "2", "-/filter_complex", str(graph),
                *maps, *audio_options,
                "-r", str(fps), "-fps_mode", "cfr", "-c:v", "libx264",
                "-crf", "0", "-preset", "fast", str(output))
            if len(frame_hashes(output)) != kept:
                raise RuntimeError("Encoded frame count differs from expected")
        output.rename(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--mode", choices=("visual", "strong", "adjacent", "all"), default="visual")
    parser.add_argument("--dry-run", action="store_true", help="Count duplicates only")
    parser.add_argument("--drop-audio", action="store_true",
                        help="Remove audio from clips shortened by deduplication")
    args = parser.parse_args()
    for executable in ("ffmpeg", "ffprobe"):
        if not shutil.which(executable):
            parser.error(f"{executable} is required on PATH")
    source, output = args.input.resolve(), args.output.resolve()
    if not source.is_dir() or source == output:
        parser.error("Use an existing input folder and a different output folder")
    videos = sorted(p for p in source.iterdir() if p.is_file()
                    and p.suffix.lower() in EXTENSIONS)
    if not videos:
        parser.error("No supported videos found")
    if not args.dry_run:
        output.mkdir(parents=True, exist_ok=True)
    failed = 0
    for video in videos:
        try:
            process(video, output / video.name, args)
        except (OSError, RuntimeError, ValueError) as exc:
            failed += 1
            print(f"ERROR {video.name}: {exc}", flush=True)
    print(f"Processed {len(videos) - failed}/{len(videos)}; failed: {failed}")
    return bool(failed)


if __name__ == "__main__":
    raise SystemExit(main())

