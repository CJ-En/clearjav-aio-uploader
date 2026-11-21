"""Torrent titles in the format of the site's rules (Torrent Naming Guide), and safe file names."""

from __future__ import annotations

import re

SAFE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,30}$")


def title(dvd_id: str, release_date: str, resolution: str, video_codec: str, audio_codec: str, tag: str = "",
          source: str = "DMM", kind: str = "WEB-DL") -> str:
    """e.g. "SSIS-123 2023-04-11 1080p DMM WEB-DL H.264 AAC" or "ABC-123 2010-05-01 1080i Blu-Ray REMUX AVC PCM",
    "-ClearJAV" for internal releases, "-TAG" for a personal release with a tag."""
    parts = [dvd_id, release_date, resolution, source, kind, video_codec, audio_codec]
    name = " ".join(part for part in parts if part)
    return f"{name}-{tag}" if tag else name


DVD_GUESS_RE = re.compile(r"^(?:h_)?\d*([a-z]+)0*(\d+)(?:re\d{1,2}|(?P<suffix>[a-z]{1,3}))?$")


def guess_dvd_id(cid: str) -> str | None:
    """DMM's digital ids often have no DVD id on r18.dev; most follow LABEL + number:
    ipzz00020 -> IPZZ-020, 1sdde00301 -> SDDE-301, h_1133gone00123 -> GONE-123, and DMM's letters after the number
    as the site writes them (1start00561v -> START-561-V, 1sddm00972ai -> SDDM-972-AI). Only a guess."""
    match = DVD_GUESS_RE.match(cid)
    if not match:
        return None
    suffix = f"-{match.group('suffix').upper()}" if match.group("suffix") else ""
    return f"{match.group(1).upper()}-{int(match.group(2)):03d}{suffix}"


def valid_tag(tag: str) -> bool:
    return bool(TAG_RE.fullmatch(tag))


def safe_name(text: str) -> str:
    return SAFE_RE.sub("_", text).strip(" .") or "upload"
