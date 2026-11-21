"""ClearJAV's API: who am I, what's already there, upload, and fetch the torrent to seed."""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import __version__
from .torrent import looks_like_torrent

log = logging.getLogger(__name__)

MIN_INTERVAL = 2.1  # the API allows 30 requests a minute per account
RESEND_DELAYS = (15, 60, 180)  # a lost answer: send again (the site answers a resend with the first upload)
# Gateway errors, also Cloudflare's 520 (unknown error), 522 (connection timed out) and 524 (a timeout occurred): the
# site may have made the torrent all the same. Sending again is safe: the site answers a resend from the same uploader
# with the same name and size with the torrent it has, also while it waits for moderation.
RETRY_STATUS = {502, 503, 504, 520, 522, 524}
# What a resend can meet while the first try is still being stored: the first try's title or files. Once it is
# stored, the site answers the resend with its link.
FIRST_TRY = ("name has already been taken", "a torrent with the same files has already been uploaded")
# A reset while the answer is being read arrives as ChunkedEncodingError ("Connection broken"), not as
# ConnectionError: the site has made the torrent, so it must be sent again to get it.
LOST_ANSWER = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)


class ApiError(Exception):
    """The site refused something or could not be reached."""


class UnconfirmedUpload(ApiError):
    """A resend kept meeting the first try (its title or files), and the site never sent its link: the upload may
    have gone through. Never a torrent of someone else."""


def _message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if not isinstance(payload, dict):
        return f"HTTP {response.status_code}"
    message = str(payload.get("message") or f"HTTP {response.status_code}")
    data = payload.get("data")
    if isinstance(data, dict):  # validation errors: {"field": ["text", ...]}
        details = "; ".join(f"{k}: {' '.join(map(str, v)) if isinstance(v, list) else v}" for k, v in data.items())
        return f"{message} {details}".strip()
    if isinstance(data, str) and data and data != message:
        return f"{message} {data}"
    return message


class Client:
    def __init__(self, site: str, api_key: str) -> None:
        self.site = site.rstrip("/")
        self.http = requests.Session()
        self.http.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": f"clearjav-aio/{__version__}",
        })
        self._last = 0.0
        self._lock = threading.Lock()

    def close(self) -> None:
        self.http.close()

    def _wait_turn(self) -> None:
        with self._lock:
            pause = MIN_INTERVAL - (time.monotonic() - self._last)
            if pause > 0:
                time.sleep(pause)
            self._last = time.monotonic()

    def _get(self, path: str, **kwargs) -> requests.Response:
        self._wait_turn()
        try:
            response = self.http.get(f"{self.site}{path}", timeout=(15, 60), **kwargs)
        except requests.RequestException as exc:
            raise ApiError(f"can't reach {self.site}: {type(exc).__name__}") from exc
        if response.status_code == 401:
            raise ApiError("the site refused the API key (wrong, or reset?). Run `clearjav-aio login` again.")
        if response.status_code == 429:
            raise ApiError("too many requests to the site; wait a minute")
        return response

    def user(self) -> dict:
        response = self._get("/api/user")
        if response.status_code != 200:
            raise ApiError(_message(response))
        return response.json()

    def torrents_of(self, jav_id: str) -> list[dict]:
        """Torrents of a video (the site also finds it under its other ids)."""
        response = self._get("/api/torrents/filter", params={"jav_id": jav_id, "perPage": 100})
        if response.status_code != 200:
            raise ApiError(f"duplicate check failed: {_message(response)}")
        data = response.json().get("data") or []
        return [item.get("attributes") or {} for item in data if isinstance(item, dict)]

    def upload(self, fields: dict, torrent: Path, sheets: list[Path]) -> tuple[str, str]:
        """Upload; returns (link of the torrent to seed, the site's message)."""
        url = f"{self.site}/api/torrents/upload"
        for attempt, delay in enumerate((*RESEND_DELAYS, None), start=1):
            self._wait_turn()
            handles = [open(torrent, "rb"), *(open(sheet, "rb") for sheet in sheets)]
            try:
                files = [("torrent", (torrent.name, handles[0], "application/x-bittorrent"))]
                files += [("thumb_sheets[]", (sheet.name, handle, "image/webp")) for sheet, handle in zip(sheets, handles[1:])]
                try:
                    response = self.http.post(url, data=fields, files=files, timeout=(15, 600))
                except LOST_ANSWER as exc:
                    if delay is None:
                        raise ApiError(f"upload failed: {type(exc).__name__}") from exc
                    log.warning("upload try %d lost (%s); sending again in %d s", attempt, type(exc).__name__, delay)
                    time.sleep(delay)
                    continue
            finally:
                for handle in handles:
                    handle.close()
            if response.status_code in RETRY_STATUS and delay is not None:
                log.warning("upload try %d: HTTP %d; sending again in %d s", attempt, response.status_code, delay)
                time.sleep(delay)
                continue
            if response.status_code == 401:
                raise ApiError("the site refused the API key (wrong, or reset?). Run `clearjav-aio login` again.")
            try:
                payload = response.json()
            except ValueError:
                raise ApiError(f"upload failed: HTTP {response.status_code}") from None
            if response.status_code == 200 and payload.get("success") and isinstance(payload.get("data"), str):
                return payload["data"], str(payload.get("message") or "")
            refused = _message(response)
            if attempt > 1 and any(words in refused.lower() for words in FIRST_TRY):
                # This tool's own first try, whose answer was lost: wait until the site has stored it and answers
                # the resend with its link.
                if delay is None:
                    raise UnconfirmedUpload(f"the upload may have gone through: an earlier try reached ClearJAV, but its "
                                            f"answer was lost and the site still says \"{refused}\". Look for it in your "
                                            "uploads on ClearJAV before uploading it again")
                log.warning("upload try %d meets the first try (%s); sending again in %d s", attempt, refused, delay)
                time.sleep(delay)
                continue
            raise ApiError(f"upload refused: {refused}")
        raise ApiError("upload failed")

    def download_torrent(self, link: str) -> bytes:
        """The .torrent the site made (with your passkey): this is the one to seed."""
        if urlparse(link).netloc.lower() != urlparse(self.site).netloc.lower():  # the API key goes only to the site
            raise ApiError(f"the site's download link points to another host ({urlparse(link).netloc})")
        for attempt, delay in enumerate((*RESEND_DELAYS, None), start=1):
            self._wait_turn()
            try:
                response = self.http.get(link, timeout=(15, 120), headers={"Accept": "application/x-bittorrent, */*"})
            except LOST_ANSWER as exc:
                if delay is None:
                    raise ApiError(f"can't download the torrent: {type(exc).__name__}") from exc
                time.sleep(delay)
                continue
            if response.status_code in RETRY_STATUS and delay is not None:
                time.sleep(delay)
                continue
            if response.status_code == 200 and looks_like_torrent(response.content):
                return response.content
            raise ApiError(f"the site didn't send a torrent file (HTTP {response.status_code})")
        raise ApiError("can't download the torrent")


def torrent_page(site: str, link: str) -> str | None:
    match = re.search(r"/torrent/download/(\d+)", link)
    return f"{site.rstrip('/')}/torrents/{match.group(1)}" if match else None
