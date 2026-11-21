"""What a video is: MediaInfo's text for the upload, resolution, codecs, DMM's id, WEB-DL or Remux."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import tools

CID_RE = re.compile(r"^[a-z0-9_]{1,80}$")
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".wmv"}
# Other videos: not uploaded, but a folder that holds some says so (not .txt, .jpg, .nfo ...).
OTHER_VIDEO_EXTENSIONS = {".avi", ".mov", ".m4v", ".ts", ".m2ts", ".mts", ".flv", ".f4v", ".mpg", ".mpeg", ".m2v", ".vob",
                          ".webm", ".rm", ".rmvb", ".divx", ".asf", ".3gp", ".ogv", ".mxf"}
ERROR_WORDS = ("error", "compliance", "truncated", "invalid", "corrupt")
# MediaInfo's names of the containers .mp4, .mkv and .wmv files are: one of these without a video track is a real
# video file that holds no picture (sound only); anything else can't be read as a video at all.
VIDEO_CONTAINERS = {"MPEG-4", "QuickTime", "Matroska", "WebM", "Windows Media"}

# ClearJAV's resolution ids (live site, 2026-10-12). Interlaced videos are filed under the same height
# (1080i as 1080p), as the site's title checker does.
RESOLUTION_IDS = {"8K-VR": 14, "VR": 13, "2160p": 2, "1080p": 3, "720p": 5, "576p": 6, "480p": 8, "404p": 11, "SD": 15, "Other": 16}
STANDARD_HEIGHTS = {2160: "2160p", 1080: "1080p", 720: "720p", 576: "576p", 480: "480p", 404: "404p"}

# Torrent types (ids never change on the site).
WEB_DL, REMUX = "WEB-DL", "REMUX"
TYPE_IDS = {WEB_DL: 4, REMUX: 2}
REMUX_SOURCES = ("DVD", "VHS", "Blu-Ray", "3D Blu-Ray", "UHD Blu-Ray")


class MediaError(Exception):
    """MediaInfo could not read a file, or it looks damaged."""


@dataclass
class Media:
    path: Path
    text: str  # MediaInfo's normal output, with the folder removed from "Complete name"
    width: int
    height: int
    duration: float
    video_codec: str  # H.264, HEVC, VP9, VC-1, MPEG-2, ... (Remux titles say AVC for H.264: video_word())
    audio_codec: str  # AAC, WMA, AC-3, E-AC-3, PCM, MP2, TrueHD, DTS, ...
    file_id: str | None  # DMM's content id written in the file
    part: int | None
    subtitle_formats: tuple[str, ...] = ()
    interlaced: bool = False  # MediaInfo's scan type Interlaced or MBAFF
    encoder: str = ""  # the video's writing library (x264 ... = re-encoded, not a Remux)
    frame_rate: float = 0.0  # frames per second; 0 when variable or unknown

    @property
    def is_vr(self) -> bool:
        # DMM's VR videos are side by side, exactly twice as wide as high (4320x2160, 5760x2880,
        # 8192x4096): true for every VR torrent on ClearJAV (2026-10-13). Cinema-wide films
        # (1920x888) are not VR.
        return self.height > 0 and self.width == 2 * self.height

    @property
    def exact_resolution(self) -> str:
        return f"{self.height}p"

    @property
    def standard_height(self) -> int:
        """A standard height a few lines off counts as the standard (1088 -> 1080), like the site."""
        return next((h for h in STANDARD_HEIGHTS if abs(h - self.height) <= 10), self.height)

    @property
    def title_resolution(self) -> str:
        return self.title_resolution_for(WEB_DL)

    def title_resolution_for(self, kind: str) -> str:
        """The Torrent Naming Guide (as the site's title checker): SD below 404p, VR as the height of one
        eye + "-VR" (2160p-VR for DMM's 4320x2160 side by side), 8K-VR for 8192 px wide VR, "i" for
        interlaced Remuxes (1080i, 480i)."""
        if self.is_vr:
            return "8K-VR" if self.width >= 8192 else f"{self.height}p-VR"
        if self.height < 404:
            return "SD"
        return f"{self.standard_height}{'i' if kind == REMUX and self.interlaced else 'p'}"

    def video_word(self, kind: str) -> str:
        return "AVC" if kind == REMUX and self.video_codec == "H.264" else self.video_codec

    @property
    def resolution(self) -> str:
        if self.is_vr:
            return "8K-VR" if self.width >= 8192 else "VR"
        if self.height < 404:
            return "SD"
        return STANDARD_HEIGHTS.get(self.standard_height, "Other")

    @property
    def resolution_id(self) -> int:
        return RESOLUTION_IDS[self.resolution]


def _run(mediainfo: Path, *args) -> str:
    try:
        result = tools.run([mediainfo, *args], timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise MediaError("MediaInfo took longer than 5 minutes") from exc
    except OSError as exc:
        raise MediaError(f"MediaInfo could not be started: {exc}") from exc
    if result.returncode != 0:
        raise MediaError(f"MediaInfo failed (exit code {result.returncode})")
    return result.stdout.decode("utf-8", "replace").replace("\r\n", "\n")


def _video_codec(track: dict) -> str:
    fmt = str(track.get("Format") or "").upper()
    # Names used on ClearJAV: HEVC (not H.265), VC-1 for every WMV video (MediaInfo says VC-1 for both
    # codec ids WVC1 and WMV3; 186 titles say VC-1, the 30 "WMV3" ones came from the old tool).
    for key, name in (("HEVC", "HEVC"), ("AVC", "H.264"), ("VP9", "VP9"), ("AV1", "AV1"), ("VC-1", "VC-1"),
                      ("WMV", "VC-1"), ("MPEG-4", "MPEG-4"), ("MPEG VIDEO", "MPEG-2")):
        if key in fmt:
            return name
    return fmt or "unknown"


def _audio_codec(track: dict) -> str:
    """The audio word of the site's title checker (TorrentTitle::audioWord)."""
    fmt = str(track.get("Format") or "").upper()
    if fmt == "MPEG AUDIO":
        return "MP2" if "LAYER 2" in str(track.get("Format_Profile") or "").upper() else "MP3"
    if fmt.startswith("DTS"):
        features = f"{track.get('Format_AdditionalFeatures') or ''} {track.get('Format_Commercial_IfAny') or ''}".upper()
        return "DTS-HD MA" if "XLL" in features or "MASTER AUDIO" in features else "DTS"
    for key, name in (("AAC", "AAC"), ("HE-AAC", "AAC"), ("WMA", "WMA"), ("E-AC-3", "E-AC-3"), ("AC-3", "AC-3"),
                      ("PCM", "PCM"), ("FLAC", "FLAC"), ("MLP FBA", "TrueHD"), ("OPUS", "Opus")):
        if fmt.startswith(key):
            return name
    return fmt or "unknown"


def damage(tracks: list[dict]) -> str | None:
    """The integrity scan: an error/truncated/corrupt field with a value."""
    general = tracks[0] if tracks else {}
    if str(general.get("Error_Count") or "0").isdigit() and int(general.get("Error_Count") or 0) > 0:
        return f"MediaInfo found {general['Error_Count']} error(s)"
    if str(general.get("IsTruncated", "")).lower() == "yes":
        return "the file is cut short (truncated)"
    for track in tracks:
        fields = dict(track)
        fields.update(track.get("extra") or {})
        for key, value in fields.items():
            name = str(key).lower()
            if "errordetectiontype" in name or not any(word in name for word in ERROR_WORDS) or value in (None, "", 0, "0"):
                continue
            text = str(value).strip()
            if text == "Yes" or (text.isdigit() and int(text) > 0) or (not text.isdigit() and len(text) > 3):
                return f"MediaInfo reports {key}={value}"
    return None


def _frame_rate(video: dict) -> float:
    """The constant frame rate (30000/1001 exactly when MediaInfo gives the fraction), 0 when variable or unknown."""
    if str(video.get("FrameRate_Mode") or "").upper() == "VFR":
        return 0.0
    try:
        num, den = video.get("FrameRate_Num"), video.get("FrameRate_Den")
        return int(num) / int(den) if num and den else float(video.get("FrameRate") or 0)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def read(path: Path, mediainfo: Path) -> Media:
    try:
        empty = path.stat().st_size == 0
    except OSError as exc:
        raise MediaError(f"{path.name} can't be opened ({exc.strerror or exc})") from exc
    if empty:
        raise MediaError(f"{path.name} is empty (0 bytes)")
    data = json.loads(_run(mediainfo, "--Output=JSON", path) or "{}")
    tracks = [t for t in ((data.get("media") or {}).get("track") or []) if isinstance(t, dict)]
    general = next((t for t in tracks if t.get("@type") == "General"), {})
    video = next((t for t in tracks if t.get("@type") == "Video"), None)
    audio = next((t for t in tracks if t.get("@type") == "Audio"), {})
    problem = damage(tracks)
    if video is None:
        # Random bytes, another kind of file renamed, or an .mp4 cut off before its index (MediaInfo then knows the
        # container but finds no track): "no video track" only for a real video file that holds sound only.
        if problem or str(general.get("Format") or "") not in VIDEO_CONTAINERS:
            raise MediaError(f"{path.name} can't be read as a video: damaged, cut off while copying, or not a video file"
                             + (f" ({problem})" if problem else ""))
        raise MediaError(f"{path.name} has no video track")
    if problem:
        raise MediaError(f"{path.name} looks damaged: {problem}")

    text = _run(mediainfo, path)
    # Only the file name in "Complete name" (no folders of the member's computer).
    text = re.sub(r"^(Complete name\s*:\s*).*$", lambda m: m.group(1) + path.name, text, count=1, flags=re.MULTILINE)
    comment = general.get("Comment") or (general.get("extra") or {}).get("Comment")
    file_id, part = None, None
    try:
        info = json.loads(comment) if comment else {}
        candidate = str(info.get("file_id") or "").strip().lower()
        file_id = candidate if CID_RE.fullmatch(candidate) else None
        part = int(info["part"]) if str(info.get("part", "")).strip().isdigit() else None
    except (ValueError, TypeError, AttributeError):
        pass

    def number(value) -> int:
        try:
            return int(float(str(value)))
        except ValueError:
            return 0

    scan = f"{video.get('ScanType') or ''} {video.get('ScanType_StoreMethod') or ''}"
    return Media(
        path=path,
        text=text.strip() + "\n",
        width=number(video.get("Width")),
        height=number(video.get("Height")),
        duration=float(general.get("Duration") or 0),
        video_codec=_video_codec(video),
        audio_codec=_audio_codec(audio) if audio else "",
        file_id=file_id,
        part=part,
        subtitle_formats=tuple(str(t.get("Format") or "") for t in tracks if t.get("@type") == "Text"),
        interlaced=bool(re.search(r"interlaced|mbaff", scan, re.IGNORECASE)),
        encoder=str(video.get("Encoded_Library_Name") or video.get("Encoded_Library") or ""),
        frame_rate=_frame_rate(video),
    )


# What DMM's WEB-DLs are made of (checked on every WEB-DL on ClearJAV, 2026-10-13).
WEB_DL_VIDEO = {"H.264", "HEVC", "VP9", "VC-1"}
WEB_DL_AUDIO = {"AAC", "WMA"}
# What DVDs and Blu-rays hold (the disc's own streams, copied unchanged into a Remux).
REMUX_VIDEO = {"H.264", "HEVC", "VC-1", "MPEG-2"}
REMUX_AUDIO = {"PCM", "AC-3", "E-AC-3", "DTS", "DTS-HD MA", "TrueHD", "MP2", "FLAC"}
REENCODERS = re.compile(r"x264|x265|xvid|divx|nvenc|qsv|amf", re.IGNORECASE)
DISC_SUBTITLES = {"PGS", "VOBSUB"}
DISC_WORDS = re.compile(
    r"(?<![a-z0-9])(remux|bdremux|blu-?ray|bdmv|bd(?:25|50|66|100)?|dvd(?:remux|5|9)?|uhd|vhs)(?![a-z0-9])",
    re.IGNORECASE,
)
RIP_WORDS = re.compile(r"(?<![a-z0-9])(bdrip|brrip|dvdrip|hdrip|webrip|rip)(?![a-z0-9])", re.IGNORECASE)
ISO_WORDS = re.compile(r"(?<![a-z0-9])(iso|bdmv|video_ts)(?![a-z0-9])", re.IGNORECASE)


def _names(info: Media, folder_name: str = "") -> tuple[str, ...]:
    """The file name, and the folder's name only for a folder of parts (it names the torrent): the folder a
    single video happens to sit in ("D:/JAV/BD", "D:/to rip") says nothing about the video."""
    return (info.path.stem, folder_name) if folder_name else (info.path.stem,)


def kind_of(info: Media, folder_name: str = "") -> tuple[str | None, str | None]:
    """("WEB-DL" or "REMUX", None), or (None, why this tool doesn't upload the file).

    A DMM WEB-DL has H.264/HEVC/VP9/VC-1 video with AAC or WMA audio. Disc audio (PCM, AC-3, DTS,
    TrueHD, MP2 ...), disc subtitles or a disc word in the name ("Remux", "Blu-ray", "DVD") make it a
    Remux, which must then hold the disc's own streams (not re-encoded)."""
    for name in _names(info, folder_name):
        if ISO_WORDS.search(name):
            return None, "ISO disc images are uploaded on the website, after staff approve them (Upload Rules)"
        match = RIP_WORDS.search(name)
        if match:
            return None, f'"{match.group(0)}" in the name: rips (re-encoded) are not uploaded, only WEB-DLs and Remuxes'
    disc_word = next((m.group(0) for m in map(DISC_WORDS.search, _names(info, folder_name)) if m), None)
    disc_subs = next((f for f in info.subtitle_formats if f.upper() in DISC_SUBTITLES), None)
    disc_audio = info.audio_codec in REMUX_AUDIO
    if not (disc_word or disc_subs or disc_audio):
        if info.video_codec not in WEB_DL_VIDEO:
            return None, f"{info.video_codec} video: DMM WEB-DLs are H.264, HEVC, VP9 or VC-1"
        if info.audio_codec not in WEB_DL_AUDIO:
            return None, (f"{info.audio_codec or 'no'} audio: DMM WEB-DLs have AAC or WMA, "
                          "disc Remuxes the disc's audio (PCM, AC-3, DTS, TrueHD, MP2)")
        return WEB_DL, None
    why = f'"{disc_word}" in the name' if disc_word else f"{disc_subs} subtitles" if disc_subs else f"{info.audio_codec} audio"
    if info.video_codec not in REMUX_VIDEO:
        return None, f"{why} looks like a disc Remux, but {info.video_codec} video is not on DVDs or Blu-rays"
    if info.audio_codec not in REMUX_AUDIO:
        return None, (f"{why} looks like a disc Remux, but {info.audio_codec or 'no'} audio is not a disc's: "
                      "a Remux keeps the disc's audio (PCM, AC-3, DTS, TrueHD, MP2)")
    if REENCODERS.search(info.encoder):
        return None, f"{why} looks like a disc Remux, but the video was re-encoded ({info.encoder}): only Remuxes and WEB-DLs are uploaded"
    return REMUX, None


def remux_source(info: Media, folder_name: str = "") -> str:
    """The source word of a Remux title, from the name, else the size: DVD up to 576 lines, UHD Blu-Ray
    for 2160, else Blu-Ray (the site's title checker guesses the same). The member can change it."""
    name = " ".join(_names(info, folder_name)).lower()
    if re.search(r"(?<![a-z0-9])vhs(?![a-z0-9])", name):
        return "VHS"
    if re.search(r"(?<![a-z0-9])3d(?![a-z0-9])", name):
        return "3D Blu-Ray"
    if re.search(r"(?<![a-z0-9])uhd(?![a-z0-9])", name):
        return "UHD Blu-Ray"
    if re.search(r"(?<![a-z0-9])dvd(?:remux|5|9)?(?![a-z0-9])", name):
        return "DVD"
    if re.search(r"(?<![a-z0-9])(blu-?ray|bd(?:25|50|66|100)?|bdremux)(?![a-z0-9])", name):
        return "Blu-Ray"
    if 0 < info.height <= 576:
        return "DVD"
    return "UHD Blu-Ray" if info.height >= 2160 else "Blu-Ray"
