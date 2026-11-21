"""Adding the torrent to qBittorrent through its Web UI, pointing at the video so it can seed."""

from __future__ import annotations

from pathlib import Path

import requests


class QbitError(Exception):
    """qBittorrent refused or could not be reached."""


def _login(http: requests.Session, base: str, user: str, password: str) -> None:
    if user or password:
        login = http.post(f"{base}/api/v2/auth/login", data={"username": user, "password": password}, timeout=15)
        if login.status_code != 200 or login.text.strip() != "Ok.":
            raise QbitError("qBittorrent refused the login (check user and password, or the Web UI's IP ban)")


def version(url: str, user: str, password: str) -> str:
    """qBittorrent's version, to test the Web UI address and login."""
    base = url.rstrip("/")
    with requests.Session() as http:
        http.headers["Referer"] = base
        try:
            _login(http, base, user, password)
            answer = http.get(f"{base}/api/v2/app/version", timeout=15)
        except requests.RequestException as exc:
            raise QbitError(f"qBittorrent's Web UI can't be reached at {base} ({type(exc).__name__})") from exc
    if answer.status_code == 403:
        raise QbitError("qBittorrent wants a login: fill in the user name and password")
    if answer.status_code != 200:
        raise QbitError(f"qBittorrent answered HTTP {answer.status_code}")
    return answer.text.strip()


def add(url: str, user: str, password: str, torrent: bytes, save_path: Path, category: str = "") -> None:
    """Add the torrent with its data folder = where the video is. qBittorrent checks the files
    first and then seeds them (nothing is downloaded)."""
    base = url.rstrip("/")
    with requests.Session() as http:
        http.headers["Referer"] = base  # qBittorrent's CSRF check
        try:
            _login(http, base, user, password)
            data = {"savepath": str(save_path), "paused": "false", "stopped": "false", "autoTMM": "false",
                    "useDownloadPath": "false"}
            if category:
                data["category"] = category
            answer = http.post(f"{base}/api/v2/torrents/add", data=data,
                               files={"torrents": ("upload.torrent", torrent, "application/x-bittorrent")}, timeout=30)
        except requests.RequestException as exc:
            raise QbitError(f"qBittorrent's Web UI can't be reached at {base} ({type(exc).__name__})") from exc
        if answer.status_code == 403:
            raise QbitError("qBittorrent wants a login: set qbittorrent_user and the password (clearjav-aio qbittorrent)")
        if answer.status_code != 200 or answer.text.strip().lower().startswith("fails"):
            raise QbitError(f"qBittorrent did not add the torrent (HTTP {answer.status_code}: {answer.text.strip()[:100]})")
