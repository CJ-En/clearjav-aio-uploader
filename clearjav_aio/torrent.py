"""Making .torrent files (private, BitTorrent v1) without any extra program.

The piece size follows intermodal (imdl), which the site's own uploads use: 2^(ceil(log2(size)) / 2 + 4)
bytes, between 16 KiB and 16 MiB. With the same file and piece size the pieces are the same, so
ClearJAV recognises a copy of a torrent that is already there (it compares piece hashes).
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

ANNOUNCE = "https://clearjav.com/announce"  # without your passkey; ClearJAV puts your own in on upload
READ_SIZE = 4 * 1024 * 1024
# ClearJAV's tracker runs apart from the site: the Upload page shows announce URLs as https://clearjav.net/<passkey>.
# The site's own https://clearjav.com/announce/<passkey> route is accepted too.
TRACKER_HOSTS = ("clearjav.net",)
PASSKEY = r"[A-Za-z0-9]{16,64}"


def announce_problem(url: str, site: str) -> str | None:
    """Why `url` is not a member's announce URL (as ClearJAV's Upload page shows it), or None."""
    parsed = urlparse(site.rstrip("/"))
    prefixes = [re.escape(f"{parsed.scheme}://{parsed.netloc}") + r"/announce/"]
    prefixes += [re.escape(f"https://{host}") + r"/(?:announce/)?" for host in TRACKER_HOSTS]
    if any(re.fullmatch(prefix + PASSKEY, url.strip()) for prefix in prefixes):
        return None
    return f"paste the announce URL as ClearJAV's Upload page shows it: https://{TRACKER_HOSTS[0]}/ and your passkey"


def bencode(value) -> bytes:
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(bencode(v) for v in value) + b"e"
    if isinstance(value, dict):
        items = sorted((k.encode("utf-8") if isinstance(k, str) else k, v) for k, v in value.items())
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in items) + b"e"
    raise TypeError(f"can't bencode {type(value).__name__}")


def bdecode(data: bytes):
    def parse(i: int):
        c = data[i:i + 1]
        if c == b"i":
            end = data.index(b"e", i)
            return int(data[i + 1:end]), end + 1
        if c in (b"l", b"d"):
            i += 1
            items = []
            while data[i:i + 1] != b"e":
                item, i = parse(i)
                items.append(item)
            if c == b"l":
                return items, i + 1
            return dict(zip(items[::2], items[1::2])), i + 1
        colon = data.index(b":", i)
        length = int(data[i:colon])
        return data[colon + 1:colon + 1 + length], colon + 1 + length

    value, end = parse(0)
    if end != len(data):
        raise ValueError("trailing data after the torrent")
    return value


def piece_length(total: int) -> int:
    exponent = math.ceil(math.log2(max(total, 1)))
    return min(max(1 << (exponent // 2 + 4), 16 * 1024), 16 * 1024 * 1024)


def files_of(target: Path, only: list[Path] | None = None) -> list[tuple[Path, list[str]]]:
    """(file, path inside the torrent) for a file, or the files of a folder in name order: only the
    given ones (the video parts: no .nfo, pictures or other extras), or else all of them."""
    if target.is_file():
        return [(target, [target.name])]
    candidates = only if only is not None else [p for p in target.rglob("*") if p.is_file() and not p.name.startswith(".")]
    found = sorted(candidates, key=lambda p: [part.lower() for part in p.relative_to(target).parts])
    return [(p, list(p.relative_to(target).parts)) for p in found]


def create(target: Path, output: Path, progress: Callable[[float], None] | None = None, only: list[Path] | None = None,
           announce: str = "") -> Path:
    """Hash `target` (a video, or a folder of parts) into a private .torrent at `output`, with your own
    announce URL when given (then it is a normal ClearJAV torrent from the start)."""
    entries = files_of(target, only)
    if not entries:
        raise ValueError(f"nothing to put in a torrent: {target}")
    total = sum(path.stat().st_size for path, _ in entries)
    size = piece_length(total)
    pieces = bytearray()
    buffer = bytearray()
    done = 0
    for path, _ in entries:
        with open(path, "rb") as handle:
            while chunk := handle.read(READ_SIZE):
                buffer += chunk
                done += len(chunk)
                while len(buffer) >= size:
                    pieces += hashlib.sha1(buffer[:size]).digest()
                    del buffer[:size]
                if progress:
                    progress(done / total)
    if buffer:
        pieces += hashlib.sha1(buffer).digest()

    info: dict = {"name": target.name, "piece length": size, "pieces": bytes(pieces), "private": 1}
    if target.is_file():
        info["length"] = total
    else:
        info["files"] = [{"length": path.stat().st_size, "path": parts} for path, parts in entries]
    data = bencode({"announce": announce or ANNOUNCE, "created by": "clearjav-aio", "info": info})
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(output)
    return output


def looks_like_torrent(data: bytes) -> bool:
    try:
        value = bdecode(data)
    except (ValueError, IndexError):
        return False
    return isinstance(value, dict) and isinstance(value.get(b"info"), dict)
