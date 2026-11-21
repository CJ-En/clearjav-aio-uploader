"""DVD id and release date of a video from r18.dev (the source ClearJAV itself uses).

Content ids are used exactly: no padded or unpadded variants ("sone096" and "sone00096" can be
different videos).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests

from . import __version__

log = logging.getLogger(__name__)

URL = "https://r18.dev/videos/vod/movies/detail/-/combined={cid}/json"
# The video of a DVD id (checked 2026-10-15): GVH-882 -> gvh00882, IPZZ-020 -> ipzz020. One listing only,
# DMM's DVD or digital one; unknown DVD ids get 404.
DVD_URL = "https://r18.dev/videos/vod/movies/detail/-/dvd_id={dvd_id}/json"


class R18Error(Exception):
    """r18.dev could not be reached."""


@dataclass
class Video:
    content_id: str
    dvd_id: str
    release_date: str  # YYYY-MM-DD
    title: str
    maker: str = ""
    runtime: int = 0  # minutes
    cover: str = ""  # DMM's small vertical cover (147 x 200), shown in the page's list


def _cover(data: dict) -> str:
    """The small vertical cover. combined= gives jacket_thumb_url (...ps.jpg) and jacket_full_url (...pl.jpg, the large
    horizontal one); dvd_id= only images.jacket_image.large2 (...pl.jpg; "large" was a blank there), checked 2026-11-18.
    DMM names the small one like the large one with ps instead of pl."""
    thumb = str(data.get("jacket_thumb_url") or "").strip()
    if thumb:
        return thumb
    images = data.get("images") if isinstance(data.get("images"), dict) else {}
    jacket = images.get("jacket_image") if isinstance(images.get("jacket_image"), dict) else {}
    for large in (data.get("jacket_full_url"), jacket.get("large2"), jacket.get("large")):
        large = str(large or "").strip()
        if large.endswith("pl.jpg"):
            return large[: -len("pl.jpg")] + "ps.jpg"
    return ""


def _get(url: str, session: requests.Session | None, attempts: int) -> dict | None:
    """r18.dev's JSON answer, or None when it doesn't know the video."""
    http = session or requests.Session()
    try:
        for attempt in range(1, attempts + 1):
            try:
                response = http.get(url, timeout=(10, 30), headers={"User-Agent": f"clearjav-aio/{__version__}", "Accept": "application/json"})
            except requests.RequestException as exc:
                if attempt == attempts:
                    raise R18Error(f"r18.dev can't be reached ({type(exc).__name__})") from exc
                time.sleep(3)
                continue
            if response.status_code == 429 or response.status_code >= 500:  # busy: wait as asked, never "not found"
                wait = response.headers.get("Retry-After", "")
                if attempt == attempts:
                    raise R18Error(f"r18.dev is busy (HTTP {response.status_code}); try again later")
                time.sleep(min(30.0, float(wait)) if wait.isdigit() else 3 * attempt)
                continue
            if response.status_code == 404:
                return None
            try:
                data = response.json()
            except ValueError:
                return None  # r18.dev answers unknown ids with an HTML page
            return data if isinstance(data, dict) and data.get("content_id") else None
        return None
    finally:
        if session is None:
            http.close()


def _video(data: dict | None) -> Video | None:
    if data is None:
        return None
    date = str(data.get("release_date") or "").split("T")[0].split(" ")[0].replace("/", "-")
    title = str(data.get("title_en") or data.get("title") or data.get("title_ja") or "")
    # The two answers name things differently (checked 2026-10-15): combined= has maker_name_en and runtime_mins,
    # dvd_id= has maker {"name"} and runtime_minutes.
    maker = data.get("maker") if isinstance(data.get("maker"), dict) else {}
    maker_name = str(data.get("maker_name_en") or data.get("maker_name_ja") or maker.get("name") or "")
    try:
        runtime = int(data.get("runtime_mins") or data.get("runtime_minutes") or 0)
    except (TypeError, ValueError):
        runtime = 0
    return Video(str(data["content_id"]).lower(), str(data.get("dvd_id") or ""), date, title, maker_name, runtime, _cover(data))


def lookup(cid: str, session: requests.Session | None = None, attempts: int = 4) -> Video | None:
    """The video, or None when r18.dev doesn't know it."""
    return _video(_get(URL.format(cid=quote(cid, safe="")), session, attempts))


def lookup_dvd_id(dvd_id: str, session: requests.Session | None = None, attempts: int = 4) -> Video | None:
    """The video r18.dev gives for a DVD id (its content id; this answer has no dvd_id), or None."""
    return _video(_get(DVD_URL.format(dvd_id=quote(dvd_id, safe="")), session, attempts))
