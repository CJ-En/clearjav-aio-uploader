"""Terminal interface: clearjav-aio [gui] | [upload] PATH... | login | announce | logout | qbittorrent | doctor | config."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
import threading
import time
from pathlib import Path

from . import __version__, config, logs, media, pipeline, qbittorrent, tools, torrent
from .api import ApiError, Client

log = logging.getLogger(__name__)

EXIT_OK, EXIT_FAILED, EXIT_SETUP, EXIT_INTERRUPTED = 0, 1, 2, 130
COMMANDS = {"gui", "upload", "login", "announce", "logout", "qbittorrent", "doctor", "config"}
API_KEY, ANNOUNCE_URL, QBIT_PASSWORD = config.API_KEY, config.ANNOUNCE_URL, config.QBIT_PASSWORD


class SetupError(Exception):
    """Something must be installed or configured first."""


class ConsoleUi(pipeline.Ui):
    def __init__(self, interactive: bool) -> None:
        self.interactive = interactive
        self.tty = sys.stdout.isatty()
        self.lock = threading.Lock()
        self.drawn = False
        self.last = 0.0
        self.step_no = -1

    def step(self, text: str) -> None:
        print(f"    {text}", flush=True)

    def progress(self, fraction: float) -> None:
        with self.lock:
            if self.tty:
                now = time.monotonic()
                if now - self.last < 0.3 and fraction < 1:
                    return
                self.last, self.drawn = now, True
                print(f"\r    [{'#' * int(fraction * 30):.<30}] {fraction * 100:5.1f}%", end="", flush=True)
            elif int(fraction * 10) > self.step_no:
                self.step_no = int(fraction * 10)

    def end_progress(self) -> None:
        if self.drawn:
            print()
        self.drawn, self.step_no = False, -1

    def confirm(self, question: str) -> bool:
        if not self.interactive:
            return False
        try:
            return input(f"    {question} [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False

    def ask(self, question: str) -> str:
        try:
            return input(f"    {question}") if self.interactive else ""
        except EOFError:
            return ""


def find_mediainfo(settings: config.Settings) -> Path:
    try:
        return tools.find_tool("mediainfo", settings.mediainfo or None)
    except tools.ToolError as exc:  # says where to download it (tools.MEDIAINFO_HINT)
        raise SetupError(str(exc)) from None


def find_ffmpeg(settings: config.Settings) -> tools.Tools:
    try:
        return tools.find_tools(settings.ffmpeg or None, settings.ffprobe or None)
    except tools.ToolError as exc:
        raise SetupError(str(exc)) from None


def api_client(settings: config.Settings) -> Client:
    key = config.get_secret(API_KEY, "CLEARJAV_API_KEY")
    if not key:
        raise SetupError("No API key yet. Run `clearjav-aio login` (create a key on ClearJAV: your profile > Settings > API Key).")
    return Client(settings.site, key)


# -- commands --------------------------------------------------------------------------------


def cmd_upload(args: argparse.Namespace, settings: config.Settings, interactive: bool) -> int:
    mediainfo_path = find_mediainfo(settings)
    ffmpeg = find_ffmpeg(settings)
    if args.dry_run:  # still shows what ClearJAV already has, when a key is saved
        client = api_client(settings) if config.get_secret(API_KEY) else None
    else:
        client = api_client(settings)
    skipped: list[Path] = []
    items = pipeline.collect(args.paths, args.recursive, skipped)
    if skipped:
        print(pipeline.skipped_note(skipped) + ".")
    if not items:
        print("No videos found (.mkv, .mp4, .wmv).")
        return EXIT_FAILED

    personal = args.personal or settings.personal_release
    rename = args.rename or settings.rename
    if rename not in pipeline.RENAME_MODES:
        raise SetupError(f"rename must be one of {', '.join(pipeline.RENAME_MODES)} (settings file: {rename!r})")
    opts = pipeline.Options(
        rename=rename,
        freeleech=args.freeleech,
        freeleech_days=args.freeleech_days,
        double_upload=args.double_upload,
        double_upload_days=args.double_upload_days,
        sticky=args.sticky,
        refundable=args.refundable,
        featured=args.featured,
        output_dir=args.out.expanduser().resolve() if args.out else (Path(settings.output_dir).expanduser() if settings.output_dir else None),
        anonymous=args.anonymous or settings.anonymous,
        personal_release=personal,
        tag=args.tag or settings.tag,
        mod_queue=args.mod_queue or settings.mod_queue,
        internal=args.internal or settings.internal,
        dry_run=args.dry_run,
        allow_duplicates=args.allow_duplicates,
        remux_source=args.source or "",
        announce=config.get_secret(ANNOUNCE_URL),
        seed=settings.seeding,
        watch_dir=Path(settings.watch_dir).expanduser() if settings.watch_dir else None,
        clean_up=settings.clean_up,
    )
    if opts.announce and torrent.announce_problem(opts.announce, settings.site):
        raise SetupError("The saved announce URL doesn't look right: run `clearjav-aio announce` again.")
    if opts.seed == "watch" and not opts.watch_dir:
        raise SetupError(f"seed_mode is watch but watch_dir is empty ({config.config_path()})")
    if args.no_qbittorrent and opts.seed != "save":
        opts.seed = "save"
    if client:
        user = client.user()
        check_permissions(user.get("upload"), opts)
    if client and not args.dry_run:
        flags = user.get("upload") or {}
        queue = ("" if "skips_moderation" not in flags else
                 ", uploads go live at once" if flags["skips_moderation"] and not opts.mod_queue else ", uploads wait for moderation")
        print(f"Uploading as {user.get('username')} ({user.get('group')}){queue}" + (", anonymous" if opts.anonymous else ""))
        if opts.seed == "qbittorrent" and settings.qbittorrent_url:
            opts.qbittorrent = (settings.qbittorrent_url, settings.qbittorrent_user,
                                config.get_secret(QBIT_PASSWORD), settings.qbittorrent_category)

    ui = ConsoleUi(interactive)
    results = []
    try:
        for index, item in enumerate(items, 1):
            print(f"\n[{index}/{len(items)}] {item.target}")
            result = pipeline.process(item, client, ffmpeg, mediainfo_path, opts, ui)
            ui.end_progress()
            results.append(result)
            if result.ok:
                print(f"    {'Ready' if args.dry_run else 'Uploaded'}: {result.title}")
                if result.page:
                    print(f"    {result.page}  ({result.message})")
                if result.seed_torrent:
                    print(f"    Seed this torrent: {result.seed_torrent}")
            else:
                print(f"    NOT UPLOADED: {result.message}")
            for warning in result.warnings:
                print(f"    note: {warning}")
    finally:
        if client:
            client.close()
    done = sum(r.ok for r in results)
    print(f"\n{'Ready' if args.dry_run else 'Uploaded'}: {done}  Not uploaded: {len(results) - done}")
    return EXIT_OK if done == len(results) else EXIT_FAILED


def check_permissions(flags: dict | None, opts: pipeline.Options) -> None:
    """Refuse options the account can't use (the site would silently ignore them)."""
    if flags is None:  # a site without the "upload" answer yet
        if any(getattr(opts, name) for name in pipeline.STAFF_OPTIONS) or opts.featured:
            print("Note: the site doesn't say whether your account may use the staff options; it ignores them if not.")
        return
    problem = pipeline.permission_problem(flags, opts)
    if problem:
        raise SetupError(problem)
    if opts.mod_queue and not flags.get("skips_moderation"):
        print("Note: your uploads always wait for moderation, so --mod-queue changes nothing.")
        opts.mod_queue = False


def cmd_login(args: argparse.Namespace, settings: config.Settings, interactive: bool) -> int:
    print(f"Create an API key on {settings.site}: your profile > Settings > API Key. Paste it here (it isn't shown).")
    key = getpass.getpass("API key: ").strip()
    if not key:
        return EXIT_FAILED
    client = Client(settings.site, key)
    try:
        user = client.user()
    finally:
        client.close()
    print(f"The key works: {user.get('username')} ({user.get('group')}).")
    return save_secret(API_KEY, key)


def save_secret(name: str, value: str) -> int:
    """Save a secret (password store, or secrets.ini on Linux) and say where it went (or why not)."""
    env = config.SECRET_ENV[name]
    if os.environ.get(env, "").strip():
        print(f"Not saved: the environment variable {env} is set and always wins. Change or remove it there.")
        return EXIT_FAILED
    if config.set_secret(name, value):
        print(f"Saved in {config.store_name()}.")
        return EXIT_OK
    print(f"Not saved: {config.store_problem() or 'unknown reason'}. Set {env} in the environment instead.")
    return EXIT_FAILED


def cmd_announce(args: argparse.Namespace, settings: config.Settings, interactive: bool) -> int:
    print(f"Copy your announce URL from {settings.site}/upload (Announce URL, copy button). Paste it here (it isn't shown).")
    print("It goes into the .torrent files the tool makes; like the API key it holds your passkey.")
    url = getpass.getpass("Announce URL: ").strip()
    if not url:
        return EXIT_FAILED
    problem = torrent.announce_problem(url, settings.site)
    if problem:
        print(problem[0].upper() + problem[1:] + ".")
        return EXIT_FAILED
    return save_secret(ANNOUNCE_URL, url)


def cmd_logout(args: argparse.Namespace, settings: config.Settings, interactive: bool) -> int:
    removed = [config.delete_secret(name) for name in (API_KEY, ANNOUNCE_URL, QBIT_PASSWORD)]
    print("API key, announce URL and qBittorrent password removed." if any(removed) else "Nothing was saved.")
    return EXIT_OK


def cmd_qbittorrent(args: argparse.Namespace, settings: config.Settings, interactive: bool) -> int:
    if not settings.qbittorrent_url:
        print(f"First set qbittorrent_url (and qbittorrent_user) in {config.ensure_config_file()}, e.g. http://127.0.0.1:8080")
        return EXIT_SETUP
    password = getpass.getpass(f"qBittorrent Web UI password for '{settings.qbittorrent_user}' (empty if none): ")
    try:
        version = qbittorrent.version(settings.qbittorrent_url, settings.qbittorrent_user, password)
    except qbittorrent.QbitError as exc:
        print(f"Can't use qBittorrent: {exc}")
        return EXIT_FAILED
    print(f"qBittorrent {version} works.")
    return save_secret(QBIT_PASSWORD, password) if password else EXIT_OK


def cmd_doctor(args: argparse.Namespace, settings: config.Settings, interactive: bool) -> int:
    print(f"clearjav-aio {__version__}, Python {sys.version.split()[0]} on {sys.platform}")
    print(f"Settings: {config.config_path()}")
    print(f"Logs:     {config.log_dir()}")
    print(f"Secrets:  {config.store_name()}" if config.store_available() else
          f"Secrets:  can't be saved ({config.store_problem()}); set them in the environment")
    ok = True
    for name, finder in (("mediainfo", find_mediainfo), ("ffmpeg/ffprobe", find_ffmpeg)):
        try:
            found = finder(settings)
            print(f"{name}: {found if isinstance(found, Path) else f'{found.ffmpeg}, {found.ffprobe}'}")
        except SetupError as exc:
            print(f"{name}: {exc}")
            ok = False
    try:
        client = api_client(settings)
        try:
            user = client.user()
            print(f"API key: works ({user.get('username')}, {user.get('group')})")
        finally:
            client.close()
    except (SetupError, ApiError) as exc:
        print(f"API key: {exc}")
        ok = False
    announce = config.get_secret(ANNOUNCE_URL)
    print("Announce URL: " + ("saved" if announce and not torrent.announce_problem(announce, settings.site) else
                              "doesn't look right, run `clearjav-aio announce`" if announce else
                              "not saved (torrents are made without your passkey; the site adds it)"))
    seeding = {"save": "only saves the site's .torrent",
               "watch": f"copies the .torrent into {settings.watch_dir or '(watch_dir not set)'}",
               "qbittorrent": f"adds it to qBittorrent at {settings.qbittorrent_url or '(qbittorrent_url not set)'}"}
    print(f"Seeding: {seeding[settings.seeding]}")
    print("Everything needed was found." if ok else "Something is missing, see above.")
    return EXIT_OK if ok else EXIT_SETUP


def cmd_config(args: argparse.Namespace, settings: config.Settings, interactive: bool) -> int:
    path = config.ensure_config_file()
    print(f"Settings file: {path}\nOpen it in a text editor to change the settings. Current values:")
    for key, value in vars(config.load(path)).items():
        print(f"    {key} = {value}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clearjav-aio", description="Upload DMM WEB-DLs and disc Remuxes to ClearJAV. Without arguments: the browser page.")
    parser.add_argument("--version", action="version", version=f"clearjav-aio {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="show the log while running")
    sub = parser.add_subparsers(dest="command")
    u = sub.add_parser("upload", help="upload videos (default command)")
    u.add_argument("paths", nargs="+", type=Path, help="video files, or folders (a folder holding the parts of one video is one upload)")
    u.add_argument("-r", "--recursive", action="store_true", help="also look in subfolders")
    u.add_argument("-n", "--dry-run", action="store_true", help="make everything but don't upload")
    u.add_argument("-o", "--out", type=Path, help="folder for the .torrent files, contact sheets and MediaInfo texts")
    u.add_argument("--anonymous", action="store_true", help="upload anonymously")
    u.add_argument("--personal", action="store_true", help="a personal release: you ripped it yourself (optional --tag)")
    u.add_argument("--tag", help="your release tag")
    u.add_argument("--rename", choices=pipeline.RENAME_MODES, help="rename the video to its content id (default), DVD id or title")
    u.add_argument("--mod-queue", action="store_true", help="trusted members: send to the moderation queue anyway")
    u.add_argument("--allow-duplicates", action="store_true", help="upload even when the video already has torrents")
    u.add_argument("--source", choices=media.REMUX_SOURCES, help="Remuxes: the disc they come from (default: worked out from the name or size)")
    staff = u.add_argument_group("staff and internal groups only")
    staff.add_argument("--internal", action="store_true", help="an internal release (title tagged -ClearJAV)")
    staff.add_argument("--freeleech", type=int, choices=(0, 25, 50, 75, 100), default=0, help="freeleech percent")
    staff.add_argument("--freeleech-days", type=int, help="freeleech for this many days (default: no end)")
    staff.add_argument("--double-upload", action="store_true", help="double upload")
    staff.add_argument("--double-upload-days", type=int, help="double upload for this many days (default: no end)")
    staff.add_argument("--sticky", action="store_true", help="pin to the top of the torrent list")
    staff.add_argument("--refundable", action="store_true", help="refundable download")
    staff.add_argument("--featured", action="store_true", help="add to the featured slider")
    u.add_argument("--no-qbittorrent", action="store_true", help="only save the torrent to seed (no qBittorrent, no watch folder)")
    g = sub.add_parser("gui", help="the browser page (default when started without arguments)")
    g.add_argument("--port", type=port_number, default=0, help="port on 127.0.0.1 (default: 8765, else a free one)")
    g.add_argument("--no-browser", action="store_true", help="don't open the browser")
    sub.add_parser("login", help="save your ClearJAV API key")
    sub.add_parser("announce", help="save your announce URL (it goes into the torrents the tool makes)")
    sub.add_parser("logout", help="remove the saved API key, announce URL and qBittorrent password")
    sub.add_parser("qbittorrent", help="test qBittorrent's Web UI and save its password")
    sub.add_parser("doctor", help="check that everything needed is found")
    sub.add_parser("config", help="show (and create) the settings file")
    return parser


def owns_console() -> bool:
    if os.name != "nt" or not sys.stdin.isatty():
        return False
    try:
        import ctypes

        processes = (ctypes.c_uint * 4)()
        # The window is ours when only we use it: the .exe alone, or run.bat's cmd + Python. Started from a
        # terminal there is one more process (the shell), and the window must not ask for Enter.
        alone = 1 if getattr(sys, "frozen", False) else 2
        return ctypes.windll.kernel32.GetConsoleProcessList(processes, 4) <= alone
    except Exception:
        return False


def cmd_gui(args: argparse.Namespace, settings: config.Settings, interactive: bool) -> int:
    from . import web

    try:
        return web.serve(settings, port=args.port, open_browser=not args.no_browser)
    except web.PortError as exc:  # --port is taken (another copy of the uploader?)
        raise SetupError(str(exc)) from None


def port_number(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        port = -1
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("a port is a number from 0 to 65535")
    return port


FUNCTIONS = {"gui": cmd_gui, "upload": cmd_upload, "login": cmd_login, "announce": cmd_announce, "logout": cmd_logout,
             "qbittorrent": cmd_qbittorrent, "doctor": cmd_doctor, "config": cmd_config}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    pause = owns_console()
    interactive = sys.stdin.isatty()
    if not argv:
        argv = ["gui"]  # the browser page
    start = 0
    while start < len(argv) and argv[start] in ("-v", "--verbose"):
        start += 1
    if start < len(argv) and argv[start] not in COMMANDS and argv[start] not in ("-h", "--help", "--version"):
        argv.insert(start, "upload")
    args = build_parser().parse_args(argv)
    if not args.command:
        build_parser().print_help()
        return EXIT_SETUP
    log_path = logs.setup(args.verbose)
    log.info("clearjav-aio %s: %s", __version__, args.command)
    try:
        code = FUNCTIONS[args.command](args, config.load(), interactive)
    except (SetupError, config.ConfigError) as exc:
        print(exc)
        code = EXIT_SETUP
    except ApiError as exc:
        print(f"ClearJAV: {exc}")
        code = EXIT_FAILED
    except KeyboardInterrupt:
        print("\nStopped.")
        code = EXIT_INTERRUPTED
    if log_path and code != EXIT_OK:
        print(f"Log: {log_path}")
    if pause and not (args.command == "gui" and code == EXIT_OK):  # the page ended normally: nothing to read
        try:
            input("\nPress Enter to close.")
        except (EOFError, KeyboardInterrupt):
            pass
    return code
