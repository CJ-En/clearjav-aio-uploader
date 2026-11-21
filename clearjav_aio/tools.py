"""Finding and running ffmpeg, ffprobe and MediaInfo. Members install them themselves: the tool never includes them."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

WINDOWS = os.name == "nt"
# Keeps a console window from flashing up for every tool run when started from the GUI.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Download pages (always the current version, never a fixed file). The browser page links the same ones (web/app.js).
FFMPEG_WINDOWS_URL = "https://www.gyan.dev/ffmpeg/builds/"
FFMPEG_URL = "https://ffmpeg.org/download.html"
MEDIAINFO_URL = "https://mediaarea.net/en/MediaInfo/Download"
WHERE_TO_PUT = ("Then put {what} in the uploader's folder (next to ClearJAV-Uploader.exe, run.bat or run.sh) or on PATH, "
                "or give {where} location in Settings > Programs (or the settings file).")

INSTALL_HINT = (
    f"Download FFmpeg. Windows: the \"release essentials\" build from {FFMPEG_WINDOWS_URL} (ffmpeg.exe and ffprobe.exe "
    f"are in its bin folder). Other systems: {FFMPEG_URL} (Debian/Ubuntu: sudo apt install ffmpeg). "
    + WHERE_TO_PUT.format(what="ffmpeg and ffprobe", where="their")
)
MEDIAINFO_HINT = (
    f"Download MediaInfo, the command-line version (CLI): {MEDIAINFO_URL} (Debian/Ubuntu: sudo apt install mediainfo). "
    + WHERE_TO_PUT.format(what="MediaInfo", where="its")
)
HINTS = {"ffmpeg": INSTALL_HINT, "ffprobe": INSTALL_HINT, "mediainfo": MEDIAINFO_HINT}
NAMES = {"mediainfo": "MediaInfo (command-line version)"}


class ToolError(Exception):
    """A required program is missing or unusable. The message says how to get it; `short` only what is wrong."""

    def __init__(self, short: str, hint: str = "") -> None:
        super().__init__(f"{short} {hint}".strip())
        self.short = short


@dataclass(frozen=True)
class Tools:
    ffmpeg: Path
    ffprobe: Path


def program_dir() -> Path:
    """The folder of the .exe (packaged) or of the source checkout."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _file_names(name: str) -> list[str]:
    if WINDOWS and not name.lower().endswith(".exe"):
        return [f"{name}.exe"]
    return [name]


def _usable(path: Path) -> bool:
    return path.is_file() and (WINDOWS or os.access(path, os.X_OK))


def find_tool(name: str, override: str | os.PathLike | None = None) -> Path:
    """Find a program: the given path, next to this program (or in its bin folder), then PATH.

    The current folder is never searched (unlike Windows' own lookup), so a stray ffmpeg.exe
    sitting next to the videos is not run.
    """
    shown = NAMES.get(name, name)
    if override:
        given = Path(override).expanduser()
        candidates = [given / n for n in _file_names(name)] if given.is_dir() else [given]
        for candidate in candidates:
            if _usable(candidate):
                return candidate.resolve()
        raise ToolError(f"{shown} was not found at {given}.")

    folders = [program_dir(), program_dir() / "bin"]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if entry and Path(entry).is_absolute():
            folders.append(Path(entry))
    for folder in folders:
        for file_name in _file_names(name):
            candidate = folder / file_name
            if _usable(candidate):
                return candidate.resolve()
    raise ToolError(f"{shown} was not found.", HINTS.get(name, ""))


def find_tools(ffmpeg: str | None = None, ffprobe: str | None = None) -> Tools:
    return Tools(ffmpeg=find_tool("ffmpeg", ffmpeg), ffprobe=find_tool("ffprobe", ffprobe))


def run(cmd: list, timeout: float) -> subprocess.CompletedProcess:
    """Run a short command and capture its output (bytes)."""
    return subprocess.run(
        [str(part) for part in cmd],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout,
        creationflags=NO_WINDOW,
    )


def version(path: Path) -> str:
    try:
        result = run([path, "-version"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"not working ({exc})"
    first_line = result.stdout.decode("utf-8", "replace").splitlines()[:1]
    return first_line[0].strip() if first_line else "unknown version"
