"""The settings file, where things are kept, and the secrets (API key, announce URL, qBittorrent password).

Secrets never go into the settings file. Windows keeps them in Windows Credential Manager, macOS in the
Keychain. Linux (and any other system) keeps them in secrets.ini next to the settings file, which only the
member's account can read. The environment (CLEARJAV_API_KEY, CLEARJAV_ANNOUNCE_URL, QBITTORRENT_PASSWORD)
always wins over saved values.
"""

from __future__ import annotations

import configparser
import contextlib
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass, fields
from pathlib import Path

log = logging.getLogger(__name__)

APP = "ClearJAV-AIO"
SLUG = "clearjav-aio"
SECTION = "settings"
SERVICE = "clearjav-aio"
# Secrets: name in the password store (and in secrets.ini), environment variable.
API_KEY, ANNOUNCE_URL, QBIT_PASSWORD = "api-key", "announce-url", "qbittorrent-password"
SECRET_ENV = {API_KEY: "CLEARJAV_API_KEY", ANNOUNCE_URL: "CLEARJAV_ANNOUNCE_URL", QBIT_PASSWORD: "QBITTORRENT_PASSWORD"}
THEMES = ("system", "dark", "light")  # the page's colors: ClearJAV Dark or Light, or as the computer is set

TEMPLATE = """\
# clearjav-aio settings. Empty values use the default. Command-line options win over these.

[settings]
# The site.
site = https://clearjav.com

# Folder for the .torrent files to seed, contact sheets and MediaInfo texts
# (default: "clearjav-uploads" next to each video).
output_dir =

# Rename the video to: content_id (default), dvd_id or title (the Upload Rules ask for one of
# these). Parts become <name>-pt1, -pt2 in a folder <name>.
rename = content_id

# Upload options: yes or no.
anonymous = no
personal_release = no
# Your tag for personal releases (added as "-TAG" to the title).
tag =
# Trusted members: send the upload to the moderation queue anyway (others always go there).
mod_queue = no
# Staff and internal groups only: an internal release (tagged -ClearJAV).
internal = no

# Programs (default: next to clearjav-aio, then PATH).
mediainfo =
ffmpeg =
ffprobe =

# After the upload: save (only save the site's .torrent), watch (also copy it into watch_dir, for a
# torrent client that watches a folder) or qbittorrent (add it through qBittorrent's Web UI).
# Empty: qbittorrent when qbittorrent_url is set, else save.
seed_mode =
watch_dir =
# After a successful upload, delete the contact sheets and the MediaInfo text the tool made: yes or no.
# The .torrent to seed is kept with save and watch, and deleted once qBittorrent has it.
clean_up = no

# qBittorrent Web UI, to start seeding right away, e.g. http://127.0.0.1:8080
# The password is not kept in this file: save it on the page (Settings) or with "clearjav-aio qbittorrent".
# It goes to Windows Credential Manager, the macOS Keychain, or on Linux to secrets.ini next to this file.
qbittorrent_url =
qbittorrent_user =
qbittorrent_category =

# The page: theme = system (as the computer is set: light or dark), dark or light.
theme = system
# Show the videos' covers in the page's list: yes or no. They are DMM's pictures, which the uploader
# loads from DMM's image server (pics.dmm.co.jp) for the videos r18.dev knows.
covers = yes
"""


def config_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / SLUG


def log_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP / "Logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / APP
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / SLUG


def config_path() -> Path:
    return config_dir() / "config.ini"


@dataclass
class Settings:
    site: str = "https://clearjav.com"
    output_dir: str = ""
    rename: str = "content_id"
    anonymous: bool = False
    personal_release: bool = False
    tag: str = ""
    mod_queue: bool = False
    internal: bool = False
    mediainfo: str = ""
    ffmpeg: str = ""
    ffprobe: str = ""
    seed_mode: str = ""
    watch_dir: str = ""
    clean_up: bool = False
    qbittorrent_url: str = ""
    qbittorrent_user: str = ""
    qbittorrent_category: str = ""
    theme: str = "system"  # THEMES
    covers: bool = True

    @property
    def seeding(self) -> str:
        """save, watch or qbittorrent (an empty or unknown seed_mode follows qbittorrent_url)."""
        if self.seed_mode in ("save", "watch", "qbittorrent"):
            return self.seed_mode
        return "qbittorrent" if self.qbittorrent_url else "save"


def _make_dir(folder: Path) -> None:
    """Makes the settings folder so that only this account may open it (Windows ignores the mode). An existing
    folder is left as it is: secrets.ini itself is always only readable by this account."""
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)


def ensure_config_file() -> Path:
    path = config_path()
    if not path.exists():
        _make_dir(path.parent)
        path.write_text(TEMPLATE, encoding="utf-8")
    return path


class ConfigError(Exception):
    """The settings file can't be read."""


def load(path: Path | None = None) -> Settings:
    path = path or config_path()
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    if path.exists():
        try:
            parser.read(path, encoding="utf-8-sig")  # utf-8-sig: Notepad and PowerShell 5 may write a BOM
        except (configparser.Error, UnicodeDecodeError) as exc:
            first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            raise ConfigError(f"The settings file {path} can't be read ({first}). Fix that line, or delete the file "
                              "to start again with the defaults.") from None
    values = parser[SECTION] if parser.has_section(SECTION) else {}
    settings = Settings()
    for field in fields(Settings):
        raw = str(values.get(field.name, "") or "").strip().strip('"')
        if not raw:
            continue
        if field.type in (bool, "bool"):
            setattr(settings, field.name, raw.lower() in ("yes", "true", "1", "on"))
        else:
            setattr(settings, field.name, raw)
    settings.site = settings.site.rstrip("/")
    settings.theme = settings.theme.lower()
    if settings.theme not in THEMES:
        settings.theme = "system"
    return settings


def save(settings: Settings, path: Path | None = None) -> Path:
    """Write the values into the settings file, keeping its comments (the browser page's Settings)."""
    path = path or ensure_config_file()
    with _save_lock:
        return _save(settings, path)


_save_lock = threading.Lock()


def _save(settings: Settings, path: Path) -> Path:
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else TEMPLATE.splitlines()
    values = {}
    for item in fields(Settings):
        value = getattr(settings, item.name)
        values[item.name] = ("yes" if value else "no") if isinstance(value, bool) else str(value).replace("\r", " ").replace("\n", " ").strip()
    written = set()
    for index, line in enumerate(lines):
        match = re.match(r"^\s*([a-z_]+)\s*=", line)
        if match and match.group(1) in values and match.group(1) not in written:
            lines[index] = f"{match.group(1)} = {values[match.group(1)]}".rstrip()
            written.add(match.group(1))
    if not any(line.strip() == f"[{SECTION}]" for line in lines):
        lines.insert(0, f"[{SECTION}]")
    lines += [f"{name} = {value}".rstrip() for name, value in values.items() if name not in written]
    _make_dir(path.parent)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


# -- secrets ---------------------------------------------------------------------------------
#
# Windows and macOS: the system's password store, through keyring (Windows Credential Manager, the macOS Keychain).
# Linux and other systems: secrets.ini in the settings folder, which only this account may read. keyring isn't used
# there: its Secret Service needs an unlocked desktop keyring, which a seedbox reached over SSH doesn't have.
# The environment variables (SECRET_ENV) always win over saved values.

STORE_TIMEOUT = 10  # seconds: a locked password store may wait for an unlock prompt nobody sees
NO_STORE_ENV = "CLEARJAV_AIO_NO_KEYRING"  # 1 = never save secrets anywhere (only the environment variables)
SECRETS_SECTION = "secrets"
_store_lock = threading.Lock()
_file_lock = threading.Lock()  # one change of secrets.ini at a time (the page's server has several threads)
_store: tuple | None = None  # (keyring backend, SecretsFile or None; why there is none), looked up once


class StoreError(Exception):
    """The password store or the secrets file refused, failed or didn't answer."""


def secrets_in_file() -> bool:
    """True on Linux and other systems (secrets.ini); Windows and macOS use the system's password store."""
    return sys.platform not in ("win32", "darwin")


def secrets_path() -> Path:
    return config_dir() / "secrets.ini"


def _shown(path: Path) -> str:
    """A path as members see it: ~ for their home folder."""
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except (ValueError, RuntimeError):  # not inside it, or no home folder
        return str(path)


def _what() -> str:
    return "the secrets file" if secrets_in_file() else "the password store"


def _one_line(value: str) -> str:
    return value.replace("\r", "").replace("\n", "")


class SecretsFile:
    """secrets.ini (Linux and other systems), used like a keyring backend. Section [secrets], one line per secret.
    It is always written whole as a new file that only this account can read (0600), then put in place."""

    @property
    def path(self) -> Path:
        return secrets_path()

    def problem(self) -> str:
        """Why the file can't be used ("" when it can)."""
        folder = self.path.parent
        try:
            _make_dir(folder)
        except OSError as exc:
            return f"{_shown(folder)} can't be made ({exc.strerror or type(exc).__name__})"
        if not os.access(folder, os.W_OK | os.X_OK):
            return f"{_shown(folder)} can't be written"
        if self.path.exists() and not os.access(self.path, os.R_OK):
            return f"{_shown(self.path)} can't be read (does it belong to another account?)"
        return ""

    def _read(self) -> configparser.ConfigParser:
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            with open(self.path, encoding="utf-8-sig") as file:
                parser.read_file(file)
        except FileNotFoundError:
            pass
        except (configparser.Error, UnicodeDecodeError):
            # Not the parser's own message: it quotes the broken line, which may hold a secret.
            raise ValueError(f"{_shown(self.path)} can't be read: delete it and save the secrets again") from None
        return parser

    def _write(self, parser: configparser.ConfigParser) -> None:
        folder = self.path.parent
        _make_dir(folder)
        tmp = folder / f".{self.path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
        # Created only readable by this account, so the secrets are never in a file others can read.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
                file.write("# clearjav-aio: API key, announce URL and qBittorrent password. Only your account may read this file.\n")
                parser.write(file)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        os.chmod(self.path, 0o600)  # in case an older copy had looser rights

    def get_password(self, service: str, name: str) -> str | None:
        return self._read().get(SECRETS_SECTION, name, fallback=None)

    def set_password(self, service: str, name: str, value: str) -> None:
        with _file_lock:
            parser = self._read()
            if not parser.has_section(SECRETS_SECTION):
                parser.add_section(SECRETS_SECTION)
            parser.set(SECRETS_SECTION, name, _one_line(value))
            self._write(parser)

    def delete_password(self, service: str, name: str) -> None:
        with _file_lock:
            parser = self._read()
            if not (parser.has_section(SECRETS_SECTION) and parser.remove_option(SECRETS_SECTION, name)):
                raise KeyError(name)  # wasn't saved
            self._write(parser)


def _priority(backend) -> float:
    try:
        return float(backend.priority)
    except Exception:
        return 0.0


def _backend():
    """Where secrets are saved, or None: secrets.ini (Linux and other systems) or the system's password store
    (Windows, macOS). Only real stores count (priority 1 and up: Windows Credential Manager, macOS Keychain), not
    keyring's null/fail backends, which keep nothing, nor stores in plain files (keyrings.alt)."""
    global _store
    with _store_lock:
        if _store is None:
            backend, reason = None, ""
            if os.environ.get(NO_STORE_ENV, "").strip().lower() in ("1", "yes", "true", "on"):
                reason = f"saving is switched off with {NO_STORE_ENV}"
            elif secrets_in_file():
                file_store = SecretsFile()
                reason = file_store.problem()
                backend = None if reason else file_store
            else:
                try:
                    import keyring  # only here: requirements.txt installs it only on Windows and macOS

                    chosen = keyring.get_keyring()
                    candidates = list(getattr(chosen, "backends", None) or [chosen])
                    backend = next((b for b in candidates if _priority(b) >= 1), None)
                    reason = "" if backend else f"no password store found ({type(chosen).__name__})"
                except Exception as exc:
                    reason = f"the password store failed to start ({type(exc).__name__})"
            _store = (backend, reason)
        return _store[0]


def _disable(reason: str) -> None:
    global _store
    log.warning("%s (not used again this run)", reason)
    with _store_lock:
        _store = (None, reason)


def _call(function, *args):
    """A store call that gives up after STORE_TIMEOUT seconds (then the store isn't used again this run)."""
    answer: dict = {}

    def run() -> None:
        try:
            answer["value"] = function(*args)
        except Exception as exc:  # the backends raise their own kinds
            answer["error"] = exc

    thread = threading.Thread(target=run, name="password-store", daemon=True)
    thread.start()
    thread.join(STORE_TIMEOUT)
    if thread.is_alive():
        _disable(f"{_what()} didn't answer within {STORE_TIMEOUT} s (locked?)")
        raise StoreError(f"{_what()} didn't answer within {STORE_TIMEOUT} s (locked?)")
    if "error" in answer:
        raise StoreError(f"{type(answer['error']).__name__}: {answer['error']}")
    return answer.get("value")


def store_available() -> bool:
    return _backend() is not None


def store_problem() -> str:
    """Why secrets can't be saved ("" when they can)."""
    _backend()
    return "" if _store is None else _store[1]


def get_secret(name: str, env: str | None = None) -> str:
    value = os.environ.get(env or SECRET_ENV.get(name, ""), "").strip()
    if value:
        return value
    backend = _backend()
    if backend is None:
        return ""
    try:
        return _call(backend.get_password, SERVICE, name) or ""
    except StoreError as exc:
        log.warning("could not read %s: %s", name, exc)
        return ""


def secret_source(name: str) -> str:
    """Where a saved secret comes from: "environment", "store" (password store or secrets file) or "" (none)."""
    if os.environ.get(SECRET_ENV.get(name, ""), "").strip():
        return "environment"
    return "store" if get_secret(name) else ""


def store_name() -> str:
    """Where secrets are saved, for "Saved in ..."."""
    if sys.platform == "win32":
        return "Windows Credential Manager"
    if sys.platform == "darwin":
        return "the macOS Keychain"
    return f"the file {_shown(secrets_path())} (only you can read it)"


def set_secret(name: str, value: str) -> bool:
    """Save a secret; True only when it reads back unchanged. secrets.ini keeps each secret on one line (CR and LF
    are removed first)."""
    global _store
    backend = _backend()
    if backend is None:
        return False
    if isinstance(backend, SecretsFile):
        value = _one_line(value)
    try:
        _call(backend.set_password, SERVICE, name, value)
        if _call(backend.get_password, SERVICE, name) == value:
            return True
        reason = f"{_what()} didn't keep the value"
    except StoreError as exc:
        reason = str(exc)
    log.warning("could not save %s: %s", name, reason)
    with _store_lock:
        if _store is not None and _store[0] is not None:
            _store = (_store[0], reason)  # still usable for reading; the reason is shown
    return False


def delete_secret(name: str) -> bool:
    """Remove a saved secret; True when it was there (secrets.ini itself stays)."""
    backend = _backend()
    if backend is None:
        return False
    try:
        _call(backend.delete_password, SERVICE, name)
        return True
    except StoreError:
        return False  # not saved, or the store refused
