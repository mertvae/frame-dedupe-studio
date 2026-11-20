"""Plan and render stepped animation on a 24 fps timeline."""
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import subprocess
import tempfile

from dedupe_video_frames import frame_hashes, frame_ranges, run, select_expression


MODES = {"twos", "adaptive"}
FPS = 24
WIDTH, HEIGHT = 96, 64
NORMALIZE = f"fps={FPS}:start_time=0"
EXPOSURE_COST = (0, 0.64, 0.24, 0.30, 0.36)


@dataclass(frozen=True)
class TimingPlan:
    frame_count: int
    starts: tuple
    cuts: frozenset
    pinned: frozenset
    accents: frozenset
    pans: frozenset

    def exposures(self):
        for start, end in zip(self.starts, (*self.starts[1:], self.frame_count)):
            yield start, end - start

    def exposure_counts(self):
        return Counter(length for _, length in self.exposures())


def analysis_frames(path):
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("Для адаптивного тайминга нужен NumPy. "
                           "Запустите программу через Start_Frame_Dedupe.bat.") from exc

    size = WIDTH * HEIGHT * 3
    filters = f"{NORMALIZE},scale={WIDTH}:{HEIGHT}:flags=area,format=yuv444p"
    command = ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-an",
               "-vf", filters, "-c:v", "rawvideo", "-threads:v", "1", "-f", "rawvideo", "-"]
    with tempfile.TemporaryFile() as stderr:
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr) as process:
            try:
                while data := process.stdout.read(size):
                    if len(data) != size:
                        raise RuntimeError("FFmpeg вернул неполный кадр для анализа.")
                    yield np.frombuffer(data, dtype=np.uint8).reshape(3, HEIGHT, WIDTH).astype(np.int16)
                if process.wait():
                    stderr.seek(0)
                    raise RuntimeError(stderr.read().decode("utf-8", errors="replace").strip())
            finally:
                if process.poll() is None:
                    process.kill()


def measure_motion(frames):
    import numpy as np

    # Estimate only small camera translations. A candidate must explain motion
    # near several borders, where a moving character is less likely to dominate.
    window = np.outer(np.hanning(HEIGHT), np.hanning(WIDTH))
    border = np.ones((HEIGHT - 8, WIDTH - 8), dtype=bool)
    border[12:-12, 16:-16] = False
    previous = None
    motion, local_peaks, cuts, shifts = [], [], set(), []

    for index, current in enumerate(frames):
        luma = current[0]
        spectrum = np.fft.fft2((luma - luma.mean()) * window)
        histogram = np.stack([np.bincount((plane // 16).ravel(), minlength=16)
                              for plane in current])
        if previous is None:
            motion.append(0.0)
            local_peaks.append(0.0)
            shifts.append((0, 0))
        else:
            delta = np.abs(current - previous)
            hist_distance = np.abs(histogram - previous_histogram).sum() / (6 * WIDTH * HEIGHT)
            luma_delta = delta[0].mean()
            chroma_delta = delta[1:].mean()
            hard_cut = (
                (luma_delta > 47 and (delta[0] > 28).mean() > 0.58)
                or ((luma_delta + 0.65 * chroma_delta) > 28 and hist_distance > 0.27)
            )
            dx = dy = 0
            if luma_delta >= 2.0:
                phase = spectrum * previous_spectrum.conj()
                correlation = np.fft.ifft2(phase / np.maximum(np.abs(phase), 1e-6)).real
                peaks = np.roll(correlation, (4, 4), axis=(0, 1))[:9, :9]
                y, x = np.unravel_index(peaks.argmax(), peaks.shape)
                guess_x, guess_y = int(x - 4), int(y - 4)
                if guess_x or guess_y:
                    original = delta[0, 4:-4, 4:-4][border].mean()
                    moved = np.abs(current[:, 4:-4, 4:-4] -
                                   previous[:, 4-guess_y:HEIGHT-4-guess_y,
                                            4-guess_x:WIDTH-4-guess_x])
                    improved = moved[0][border].mean()
                    confidence = 0.15 if hard_cut else 0.08
                    if (peaks[y, x] >= confidence and original >= 2.2
                            and improved < original * 0.70):
                        dx, dy = guess_x, guess_y
                        delta = np.pad(moved, ((0, 0), (4, 4), (4, 4)), mode="edge")
            if hard_cut and not (dx or dy):
                cuts.add(index)
            difference = 0.75 * delta[0] + 0.125 * (delta[1] + delta[2])
            tiles = difference.reshape(HEIGHT // 8, 8, WIDTH // 8, 8).mean(axis=(1, 3))
            upper, peak = np.percentile(tiles, (85, 98))
            motion.append(float((0.55 * upper + 0.45 * peak) / 255))
            local_peaks.append(float(tiles.max() / 255))
            shifts.append((dx, dy))
        previous, previous_spectrum, previous_histogram = current, spectrum, histogram

    return motion, local_peaks, cuts, shifts


def smooth_pan_frames(shifts, cuts):
    """Protect sustained camera movement, leaving isolated estimates alone."""
    protected = set()
    for start in range(1, len(shifts) - 2):
        a, b, c = shifts[start:start + 3]
        if (not cuts.intersection(range(start - 1, start + 3))
                and all(x or y for x, y in (a, b, c))
                and a[0] * b[0] + a[1] * b[1] > 0
                and b[0] * c[0] + b[1] * c[1] > 0):
            protected.update(range(start - 1, start + 3))
    return protected


def plan_shot(motion, first, end, required, scale):
    """Minimize pose error and the cost of holding each drawing."""
    length = end - first
    costs = [0.0] * (length + 1)
    choices = [1] * length
    for position in range(length - 1, -1, -1):
        best = EXPOSURE_COST[1] + costs[position + 1]
        pose_error = 0.0
        drift = 0.0
        for exposure in range(2, min(4, length - position) + 1):
            frame = first + position + exposure - 1
            if frame in required:
                break
            drift += max(0, motion[frame] - 0.007)
            pose_error += (drift / scale) ** 1.35
            candidate = EXPOSURE_COST[exposure] + 0.90 * pose_error + costs[position + exposure]
            if candidate < best:
                best, choices[position] = candidate, exposure
        costs[position] = best

    position = 0
    while position < length:
        yield first + position
        position += choices[position]


def analyze(path, mode="adaptive", pinned=()):
    import statistics

    motion, local_peaks, cuts, shifts = measure_motion(analysis_frames(path))
    count = len(motion)
    if count == 0:
        raise ValueError("В видео нет декодированных кадров.")
    pinned = frozenset(pinned)
    if any(index < 0 or index >= count for index in pinned):
        raise ValueError(f"Номера закреплённых кадров должны быть от 1 до {count}.")

    pans = smooth_pan_frames(shifts, cuts) if mode == "adaptive" else set()
    accents, starts = set(), []
    boundaries = sorted({0, count, *cuts})
    for first, end in zip(boundaries, boundaries[1:]):
        if mode == "twos":
            starts.extend(range(first, end, 2))
            continue

        values = motion[first + 1:end]
        median = statistics.median(values) if values else 0.0
        scale = min(0.11, max(0.040, median * 2.3))
        rapid = max(0.095, median * 1.95)
        shot_accents = {frame for i in range(first + 1, end) if motion[i] > rapid
                        for frame in (i - 1, i)}
        # A brief, localized change can disappear from aggregate motion scores.
        for frame in range(first + 2, end - 2):
            before, after = local_peaks[frame:frame + 2]
            surroundings = max(local_peaks[frame - 1], local_peaks[frame + 2], 0.14)
            if min(before, after) > 1.5 * surroundings:
                shot_accents.add(frame)
        # Keep the tail pose if the action is still changing at the boundary.
        if values and motion[end - 1] > max(0.018, median * 0.70):
            shot_accents.add(end - 1)
        accents.update(shot_accents)
        required = pinned | pans | shot_accents
        starts.extend(plan_shot(motion, first, end, required, scale))

    return TimingPlan(count, tuple(starts), frozenset(cuts), pinned,
                      frozenset(accents), frozenset(pans))


def render(source, destination, plan):
    if destination.exists():
        raise FileExistsError(f"Файл уже существует: {destination}")
    starts = set(plan.starts)
    selection = select_expression(list(frame_ranges(
        (index for index in range(plan.frame_count) if index not in starts), plan.frame_count
    )))
    # Keep the selected frames at their source timestamps. CFR regeneration
    # maintains the holds, including the final pose through the end of the clip.
    filters = (f"{NORMALIZE},select='{selection}',"
               f"tpad=stop_mode=clone:stop=4,{NORMALIZE},trim=end_frame={plan.frame_count}")
    with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
        directory = Path(directory)
        graph = directory / "timing.txt"
        graph.write_text(filters, encoding="ascii")
        output = directory / destination.name
        run("ffmpeg", "-v", "error", "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
            "-/filter:v", str(graph), "-c:v", "libx264", "-crf", "0", "-preset", "fast",
            "-c:a", "copy", str(output))
        actual = len(frame_hashes(output))
        if actual != plan.frame_count:
            raise RuntimeError(f"Ожидалось {plan.frame_count} кадров, записано {actual}.")
        output.rename(destination)
