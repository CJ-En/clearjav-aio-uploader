"""DMM's covers for the page's list, fetched by the uploader itself: the page loads them from 127.0.0.1 only.

The links come from r18.dev (jacket_thumb_url, the small vertical cover). Only https links to DMM's image servers
are fetched, without following redirects (DMM sends a missing picture to a "now printing" one elsewhere), at most
2 MB, and only JPEG, PNG, GIF or WebP. They are kept in memory for this run; nothing is written to disk.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from urllib.parse import urlsplit

import requests

from . import __version__

log = logging.getLogger(__name__)

HOSTS = frozenset({"pics.dmm.co.jp", "awsimgsrc.dmm.co.jp"})
MAX_BYTES = 2 * 1024 * 1024
KEEP = 300  # covers kept in memory (about 10 KB each)
RETRY_AFTER = 300.0  # seconds before a cover that failed is asked for again
TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")


class CoverError(Exception):
    """The cover can't be used; the message says why (for the log)."""


def allowed(url: str) -> bool:
    """Only https links to DMM's image servers (no other host, port or login)."""
    try:
        parts = urlsplit(str(url or ""))
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and (parts.hostname or "") in HOSTS and port in (None, 443)
            and parts.username is None and parts.password is None)


def _is_picture(kind: str, data: bytes) -> bool:
    """The first bytes match the type the server named."""
    if kind == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if kind == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if kind == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def fetch(session: requests.Session, url: str) -> tuple[bytes, str]:
    """The picture and its type; CoverError when it isn't one we take."""
    if not allowed(url):
        raise CoverError("not a link to DMM's image server")
    try:
        with session.get(url, timeout=(5, 15), stream=True, allow_redirects=False, headers={"Accept": "image/*"}) as response:
            if response.status_code != 200:
                raise CoverError(f"HTTP {response.status_code}")  # 302: DMM has no such picture ("now printing")
            kind = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if kind not in TYPES:
                raise CoverError(f"not a picture ({kind or 'no type'})")
            length = str(response.headers.get("Content-Length") or "")
            if length.isdigit() and int(length) > MAX_BYTES:
                raise CoverError("too big")
            data = bytearray()
            for chunk in response.iter_content(64 * 1024):
                data += chunk
                if len(data) > MAX_BYTES:
                    raise CoverError("too big")
    except requests.RequestException as exc:
        raise CoverError(type(exc).__name__) from None
    if not _is_picture(kind, bytes(data[:16])):
        raise CoverError(f"not a picture (doesn't look like {kind})")
    return bytes(data), kind


class Covers:
    """The covers asked for in this run, by link (the newest KEEP), and the ones that failed (asked again later)."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = f"clearjav-aio/{__version__}"
        self._lock = threading.Lock()
        self._kept: collections.OrderedDict[str, tuple[bytes, str] | float] = collections.OrderedDict()
        self._busy: dict[str, threading.Lock] = {}  # one fetch per link at a time

    def get(self, url: str) -> tuple[bytes, str] | None:
        """The cover (picture, type), or None: not a DMM link, or it can't be had (one line in the log file)."""
        if not allowed(url):
            return None
        with self._lock:
            busy = self._busy.setdefault(url, threading.Lock())
        with busy:
            with self._lock:
                kept = self._kept.get(url)
                if isinstance(kept, tuple):
                    self._kept.move_to_end(url)
                    return kept
                if kept is not None and time.monotonic() < kept:
                    return None  # failed a moment ago
            try:
                image: tuple[bytes, str] | None = fetch(self.session, url)
            except CoverError as exc:
                log.warning("cover not loaded (%s): %s", exc, url)
                image = None
            with self._lock:
                self._kept[url] = image if image is not None else time.monotonic() + RETRY_AFTER
                self._kept.move_to_end(url)
                while len(self._kept) > KEEP:
                    old, _ = self._kept.popitem(last=False)
                    self._busy.pop(old, None)
            return image

    def close(self) -> None:
        self.session.close()
