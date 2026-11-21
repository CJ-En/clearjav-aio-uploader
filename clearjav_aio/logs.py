"""A log file per user (rotating), without API keys, passkeys or passwords."""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config

PATTERNS = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+"), r"\1[hidden]"),
    (re.compile(r"(?i)((?:api_token|apikey|api_key|password|passkey|rsskey)\s*[:=]\s*)\S+"), r"\1[hidden]"),
    (re.compile(r"(/torrent/download/\d+\.)[A-Za-z0-9]+"), r"\1[hidden]"),  # the link holds your RSS key
    (re.compile(r"(/announce/)[A-Za-z0-9]+"), r"\1[hidden]"),
    # The tracker's announce URLs carry the passkey right after the host: https://clearjav.net/<passkey>.
    (re.compile(r"(https?://[^/\s\"']+/)[A-Za-z0-9]{16,64}(?=$|[\s\"'<>?#/),;])"), r"\1[hidden]"),
    (re.compile(r"(https?://)([^:/@\s]+):([^/@\s]+)@"), r"\1[hidden]@"),
)


def redact(text) -> str:
    text = str(text or "")
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup(verbose: bool = False) -> Path | None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for old in [h for h in root.handlers if getattr(h, "clearjav_aio", False)]:
        root.removeHandler(old)
        old.close()
    for noisy in ("urllib3", "charset_normalizer", "keyring"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if verbose:
        console = logging.StreamHandler()
        console.setFormatter(RedactingFormatter("  [log] %(message)s"))
        console.clearjav_aio = True
        root.addHandler(console)
    path = config.log_dir() / "clearjav-aio.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
    handler.clearjav_aio = True
    root.addHandler(handler)
    return path
