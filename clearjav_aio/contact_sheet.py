"""Contact sheets that look like mtn's (Movie Thumbnailer), made with FFmpeg only.

Same layout as the sheets of the site's own uploads (mtn -c 3 -w 1024 -h 80): white background, four
grey lines of file information at the top, 3 columns of shots 340 px wide with no gaps, each shot
with its time in white with a black shadow at the bottom left, DejaVu Sans (mtn's font).
Shots are taken at length / (shots + 1) apart, like mtn, about one every 2 minutes (4 minutes for videos over 4 hours);
mostly blank frames are skipped.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable

from . import tools

FONT = Path(__file__).resolve().parent / "fonts" / "DejaVuSans.ttf"
WIDTH = 1024
COLUMNS = 3
MARGIN = 2  # white edge left and right
TILE_WIDTH = (WIDTH - 2 * MARGIN) // COLUMNS  # 340
INFO_SIZE = 12  # mtn: 9 pt
INFO_COLOR = "0x555555"
INFO_LINE = 13
TIME_SIZE = 11  # mtn: 8 pt
MAX_ROWS = 72  # as on the site's own uploads
MAX_HEIGHT = 16000  # ClearJAV converts images up to 16384 px per side
BLANK_SHARE = 0.80  # mtn -b 0.80: skip a shot when this much of it is one colour
# The timing of the site's own uploads: one shot every 2 minutes, every 4 minutes for videos over 4 hours.
SHOT_INTERVAL = 120
LONG_VIDEO = 14400
LONG_INTERVAL = 240
# ClearJAV refuses a contact sheet over 4096 KiB ("The image must be between 1 and 4096 kilobytes"). Real videos stay far
# below (the largest of 58,929 sheets on the site: 2.92 MB), but a very long, detailed video can pass it. Such a sheet is
# made again with the shots further apart (fewer shots rather than worse pictures) until it is under AIM_BYTES, a
# little below the limit; lower WebP quality only when even 30 minutes apart is too much.
SITE_LIMIT = 4096 * 1024
LIMIT_TEXT = "4 MB"
AIM_BYTES = 4_000_000
SPARSER_INTERVALS = (600, 900, 1200, 1800)
WEBP_QUALITIES = (90, 80, 70, 60, 50)
# Reading the filters from a file: `-/filter:v FILE` since FFmpeg 7 (newer releases no longer know -filter_script),
# `-filter_script:v FILE` before. The one an FFmpeg answered is remembered per program.
FILTER_OPTIONS = ("-/filter:v", "-filter_script:v")
_FILTER_OPTION: dict[str, str] = {}


def _filter_options(ffmpeg: str) -> tuple[str, ...]:
    known = _FILTER_OPTION.get(ffmpeg)
    return (known,) if known else FILTER_OPTIONS


def megabytes(size: int) -> str:
    """6236678 -> "5.9 MB" (MB as the site counts them: 4096 KiB = 4 MB)."""
    return f"{size / 1024**2:.1f} MB"


class SheetError(Exception):
    """The contact sheet could not be made."""


@dataclass
class VideoInfo:
    path: Path
    size: int
    duration: float
    bitrate: int
    width: int
    height: int
    aspect: Fraction  # display aspect ratio
    lines: list[str]  # mtn's information lines


def interval_for(duration: float) -> int:
    """Seconds between two shots: 2 minutes, 4 for videos over 4 hours (as the site's own uploads)."""
    return LONG_INTERVAL if duration > LONG_VIDEO else SHOT_INTERVAL


def rows_for(duration: float, interval: float | None = None) -> int:
    """Rows of 3 shots, one shot per `interval` seconds (default interval_for()): one row per 6 or 12 minutes."""
    interval = interval or interval_for(duration)
    return max(1, min(MAX_ROWS, int((duration / interval + 2) // 3)))


def minutes(seconds: float) -> str:
    return f"{seconds / 60:g} minutes"


def hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _fraction(text: str | None, default: Fraction) -> Fraction:
    try:
        value = Fraction(str(text).replace(":", "/"))
        return value if value > 0 else default
    except (ValueError, ZeroDivisionError):
        return default


def describe(path: Path, ffprobe: Path) -> VideoInfo:
    """Read the video with ffprobe and write mtn's four information lines."""
    result = tools.run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path], timeout=120)
    if result.returncode != 0:
        raise SheetError(f"ffprobe can't read {path.name}")
    data = json.loads(result.stdout.decode("utf-8", "replace"))
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    if not video:
        raise SheetError(f"{path.name} has no video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    size = path.stat().st_size
    duration = float(fmt.get("duration") or video.get("duration") or 0)
    bitrate = int(fmt.get("bit_rate") or 0)
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    sar = _fraction(video.get("sample_aspect_ratio"), Fraction(1))
    aspect = Fraction(width, height) * sar if width and height else Fraction(16, 9)

    def codec(stream: dict) -> str:
        profile = stream.get("profile")
        return f"{stream.get('codec_name', '?')}" + (f" ({profile})" if profile and profile != "unknown" else "")

    mib = size / 1024**2
    human = f"{mib / 1024:.2f} GiB" if mib >= 1024 else f"{mib:.0f} MiB"
    lines = [
        f"File: {path.name}",
        f"Size: {size} bytes ({human}), duration: {hms(duration)}, bitrate: {bitrate // 1000} kb/s",
    ]
    if audio:
        layout = audio.get("channel_layout") or f"{audio.get('channels', '?')} channels"
        lines.append(f"Audio: {codec(audio)}, {audio.get('sample_rate', '?')} Hz, {layout}, {audio.get('sample_fmt', '?')}")
    colour = ", ".join(x for x in (video.get("color_range"), video.get("field_order")) if x and x != "unknown")
    dar = _fraction(video.get("display_aspect_ratio"), aspect)
    fps = _fraction(video.get("r_frame_rate"), Fraction(0))
    lines.append(
        f"Video: {codec(video)}, {video.get('pix_fmt', '?')}{f'({colour})' if colour else ''}, {width}x{height} "
        f"[SAR {sar.numerator}:{sar.denominator} DAR {dar.numerator}:{dar.denominator}], {float(fps):.2f} fps(r)"
    )
    return VideoInfo(path, size, duration, bitrate, width, height, aspect, lines)


def _grab(ffmpeg: Path, video: Path, at: float, tile_h: int) -> bytes:
    """One frame at `at` seconds, scaled to a tile, as raw RGB."""
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-ss", f"{at:.3f}", "-i", video, "-frames:v", "1", "-an", "-sn",
           "-vf", f"scale={TILE_WIDTH}:{tile_h}:flags=bicubic,setsar=1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    result = tools.run(cmd, timeout=120)
    expected = TILE_WIDTH * tile_h * 3
    return result.stdout if len(result.stdout) == expected else b""


def _blank(frame: bytes) -> bool:
    """mtn skips shots where one colour fills most of the picture (black or white screens)."""
    buckets: dict[int, int] = {}
    pixels = len(frame) // 3
    for i in range(0, len(frame), 3 * 7):  # every 7th pixel is plenty
        key = (frame[i] >> 4, frame[i + 1] >> 4, frame[i + 2] >> 4)
        packed = key[0] << 8 | key[1] << 4 | key[2]
        buckets[packed] = buckets.get(packed, 0) + 1
    return max(buckets.values(), default=0) >= BLANK_SHARE * (pixels / 7)


def _text_filter(text_file: str, size: int, color: str, x: str, y: str, shadow: bool = False) -> str:
    extra = ":shadowcolor=black:shadowx=1:shadowy=1" if shadow else ""
    return (f"drawtext=fontfile=DejaVuSans.ttf:textfile={text_file}:expansion=none:fontsize={size}:fontcolor={color}"
            f":x={x}:y={y}{extra}")


def make(video: Path, output: Path, tool_paths: tools.Tools, progress: Callable[[float], None] | None = None,
         fit: bool = False, note: Callable[[str], None] | None = None) -> Path:
    """Write the contact sheet of `video` to `output` (.webp, .jpg, .png or .avif).

    With `fit` (sheets for ClearJAV), a WebP over AIM_BYTES is made again with the shots further apart (10, 15, 20, 30
    minutes, as long as that means fewer rows), at the same quality; only when even that is too big is the sparsest
    one written at lower quality. At the lowest quality it only has to be under the site's limit, else SheetError.
    `note` hears about the shrinking ("... 5.9 MB -> 3.4 MB (a shot every 10 minutes instead of 5)")."""
    info = describe(video, tool_paths.ffprobe)
    if info.duration <= 0:
        raise SheetError(f"{video.name}: unknown length")
    suffix = output.suffix.lower()
    codecs = {".webp": ["-c:v", "libwebp", "-quality", "{quality}"], ".jpg": ["-q:v", "2"], ".png": [],
              ".avif": ["-c:v", "libaom-av1", "-still-picture", "1", "-crf", "20", "-pix_fmt", "yuv444p"]}
    if suffix not in codecs:
        raise SheetError(f"unsupported contact sheet format: {suffix}")
    tile_h = max(2, int(TILE_WIDTH / info.aspect) // 2 * 2)
    header = len(info.lines) * INFO_LINE + 1
    max_rows = (MAX_HEIGHT - header) // tile_h
    # (seconds between shots, rows): the normal layout first, then sparser ones for a sheet over the limit.
    layouts = [(interval_for(info.duration), min(rows_for(info.duration), max_rows))]
    if fit and suffix == ".webp":
        for interval in SPARSER_INTERVALS:
            rows = min(rows_for(info.duration, interval), max_rows)
            if interval > layouts[-1][0] and rows < layouts[-1][1]:
                layouts.append((interval, rows))

    work = Path(tempfile.mkdtemp(prefix="clearjav-sheet-"))
    target = work / f"sheet{suffix}"
    height = 0

    def render(rows: int) -> None:
        """Grab the shots of a layout and write the picture (raw canvas) and its texts into the work folder."""
        nonlocal height
        count = rows * COLUMNS
        step = info.duration / (count + 1)
        height = header + rows * tile_h
        canvas = bytearray(b"\xff" * (WIDTH * height * 3))
        times = []
        for index in range(count):
            at = step * (index + 1)
            frame = b""
            for nudge in (0.0, 0.25, 0.5, -0.25):  # try a little later or earlier when blank
                candidate_at = min(max(0.0, at + nudge * step), max(0.0, info.duration - 1))
                candidate = _grab(tool_paths.ffmpeg, video, candidate_at, tile_h)
                if candidate:
                    frame, used = candidate, candidate_at
                    if not _blank(candidate):
                        break
            if not frame:
                raise SheetError(f"{video.name}: no picture at {hms(at)}")
            times.append(used)
            x0 = MARGIN + (index % COLUMNS) * TILE_WIDTH
            y0 = header + (index // COLUMNS) * tile_h
            row_bytes = TILE_WIDTH * 3
            for line in range(tile_h):
                start = ((y0 + line) * WIDTH + x0) * 3
                canvas[start:start + row_bytes] = frame[line * row_bytes:(line + 1) * row_bytes]
            if progress:
                progress((index + 1) / count)
        (work / "canvas.rgb").write_bytes(canvas)
        filters = []
        for number, line in enumerate(info.lines):
            (work / f"h{number}.txt").write_text(line, encoding="utf-8")
            filters.append(_text_filter(f"h{number}.txt", INFO_SIZE, INFO_COLOR, str(MARGIN + 1), str(1 + number * INFO_LINE)))
        for index, at in enumerate(times):
            (work / f"t{index}.txt").write_text(hms(at), encoding="utf-8")
            x0 = MARGIN + (index % COLUMNS) * TILE_WIDTH
            bottom = header + (index // COLUMNS + 1) * tile_h
            filters.append(_text_filter(f"t{index}.txt", TIME_SIZE, "white", str(x0 + 3), f"{bottom - 3}-th", shadow=True))
        (work / "filters.txt").write_text(",".join(filters), encoding="utf-8")

    def write(quality: int) -> int:
        """Encode the picture (the raw canvas and the texts, never a smaller copy) and return the file's size."""
        codec = [str(c).format(quality=quality) for c in codecs[suffix]]
        ffmpeg = str(tool_paths.ffmpeg)
        for filter_option in _filter_options(ffmpeg):
            cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                   "-s", f"{WIDTH}x{height}", "-i", "canvas.rgb", filter_option, "filters.txt", "-frames:v", "1", *codec,
                   target.name]
            result = subprocess.run([str(c) for c in cmd], cwd=work, capture_output=True, timeout=600,
                                    creationflags=tools.NO_WINDOW)
            message = result.stderr.decode("utf-8", "replace").strip().splitlines()
            if result.returncode != 0 and any(f"Unrecognized option '{filter_option[1:]}'" in line for line in message):
                continue  # this FFmpeg doesn't know that way of reading the filters from a file: try the other one
            _FILTER_OPTION[ffmpeg] = filter_option
            break
        if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            raise SheetError(f"FFmpeg could not write the contact sheet: {message[-1] if message else 'no output'}")
        return target.stat().st_size

    try:
        # Relative names in the work folder: no escaping of drive letters or text for FFmpeg.
        shutil.copyfile(FONT, work / "DejaVuSans.ttf")
        sizes = []
        quality = WEBP_QUALITIES[0]
        for interval, rows in layouts:
            render(rows)
            sizes.append(write(quality))
            if not fit or sizes[-1] <= AIM_BYTES:
                break
        if fit and sizes[-1] > AIM_BYTES and suffix == ".webp":
            for quality in WEBP_QUALITIES[1:]:
                sizes.append(write(quality))
                if sizes[-1] <= AIM_BYTES:
                    break
        if fit and sizes[-1] > SITE_LIMIT:
            how = "with the shots further apart and " if len(layouts) > 1 else ""
            raise SheetError(f"the contact sheet of {video.name} is {megabytes(sizes[-1])}, over ClearJAV's {LIMIT_TEXT} "
                             f"limit, even {how}at the lowest quality")
        if len(sizes) > 1 and note:
            how = []
            if interval != layouts[0][0]:
                how.append(f"a shot every {minutes(interval)} instead of {minutes(layouts[0][0]).split()[0]}")
            if quality != WEBP_QUALITIES[0]:
                how.append(f"quality {quality}")
            note(f"contact sheet made smaller for ClearJAV's {LIMIT_TEXT} limit: {megabytes(sizes[0])} -> "
                 f"{megabytes(sizes[-1])} ({', '.join(how)})")
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(output))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return output
