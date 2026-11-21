"""The browser page: a small web server on 127.0.0.1 that only this computer's browser can use.

The page (web/index.html, app.css, app.js) asks this server for the state of the upload queue and sends
what the member does. Only the browser opened by the tool gets in: the first address carries a random
key, which becomes a cookie for this port; other web pages can't send requests that pass (cookie with
SameSite=Strict, a header of our own, the Host checked against DNS rebinding).
"""

from __future__ import annotations

import collections
import errno
import json
import re
import logging
import os
import queue
import secrets
import string
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, config, covers, logs, media, pipeline, qbittorrent, tools, torrent
from .api import ApiError, Client

log = logging.getLogger(__name__)

PORT = 8765
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.css": ("app.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
HEADER = "X-ClearJAV-AIO"
COVER_PATH = re.compile(r"/cover/(\d{1,9})")
MAX_BODY = 64 * 1024
FINISHED = {"done", "test", "skipped", "bad", "failed"}
# Options kept in the settings file; the others last for this run.
SAVED_OPTIONS = ("rename", "anonymous", "personal_release", "tag", "mod_queue", "internal", "output_dir")


def web_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", tools.program_dir())) / "clearjav_aio" / "web"
    return Path(__file__).resolve().parent / "web"


def store_where() -> str:
    """Where saved secrets are, as the page says it after "Saved in"."""
    if config.secrets_in_file():
        return config.store_name()
    shared = ", shared by run.bat and ClearJAV-Uploader.exe" if sys.platform == "win32" else ""
    return f"{config.store_name()} (entries named {config.SERVICE}){shared}"


class Refused(Exception):
    """The request can't be done; the message is shown on the page."""


@dataclass
class Job:
    id: int
    item: pipeline.Item
    state: str = "checking"  # checking, prepared, bad, waiting, working, done, test, skipped, failed
    plan: pipeline.Plan | None = None
    needs_cid: bool = False
    suggestion: pipeline.Suggestion | None = None  # a content id found for the DVD id in the name (to confirm)
    infos: list[media.Media] | None = None  # MediaInfo's reading while the content id is asked
    cid_problem: bool = False  # refused because of the content id: the page offers to try another
    final: bool = False  # refused for what the files are (pipeline.Skip.final): the page offers no "Check again"
    reread: bool = False  # "Check again" of a video refused before it was checked: read it again from its folder
    message: str = ""
    steps: list[str] = field(default_factory=list)
    progress: float | None = None
    result: pipeline.Result | None = None


class JobUi(pipeline.Ui):
    def __init__(self, app: "App", job: Job) -> None:
        self.app, self.job = app, job

    def step(self, text: str) -> None:
        with self.app.lock:
            self.job.steps.append(text)
            self.job.progress = None
        self.app.note(f"{self.job.item.target.name}: {text}")

    def progress(self, fraction: float) -> None:
        self.job.progress = max(0.0, min(1.0, fraction))

    def end_progress(self) -> None:
        self.job.progress = None


class App:
    def __init__(self, settings: config.Settings) -> None:
        self.lock = threading.RLock()
        self.settings = settings
        self.jobs: list[Job] = []
        self.next_id = 1
        self.lines: collections.deque = collections.deque(maxlen=2000)
        self.line_no = 0
        self.run_options = {"dry_run": False, "allow_duplicates": False, "freeleech": 0, "freeleech_days": None,
                            "double_upload": False, "double_upload_days": None, "sticky": False, "refundable": False,
                            "featured": False}
        self.running = False
        self.stop_after = False
        self.last_folder = ""
        self.store = config.store_available()
        self.client: Client | None = None
        self.user: dict | None = None
        self.user_error = ""
        self.mediainfo: Path | None = None
        self.ffmpeg: tools.Tools | None = None  # ffmpeg and ffprobe, once both are found
        self.tool_paths: dict[str, Path] = {}  # each program found: mediainfo, ffmpeg, ffprobe
        self.tool_errors: dict[str, str] = {}
        self.checks: queue.Queue[int] = queue.Queue()
        self.quit = threading.Event()
        self.ready = threading.Event()  # tools found and the key checked
        self._secrets: dict[str, str] = {}
        self.covers = covers.Covers()
        threading.Thread(target=self._checker, name="checker", daemon=True).start()

    def secret(self, name: str) -> str:
        """A saved secret, read once from the password store or secrets.ini (the page asks for the state every second)."""
        if name not in self._secrets:
            self._secrets[name] = config.get_secret(name)
        return self._secrets[name]

    def set_secret(self, name: str, value: str) -> bool:
        self._secrets.pop(name, None)
        return config.set_secret(name, value)

    def delete_secret(self, name: str) -> None:
        self._secrets.pop(name, None)
        config.delete_secret(name)

    def startup(self) -> None:
        try:
            self.find_tools()
            self.sign_in()
        finally:
            self.ready.set()

    # -- log -------------------------------------------------------------------------------

    def note(self, text: str) -> None:
        text = logs.redact(text)
        log.info("%s", text)
        with self.lock:
            self.line_no += 1
            self.lines.append((self.line_no, time.strftime("%H:%M:%S"), text))

    # -- setup -----------------------------------------------------------------------------

    def find_tools(self) -> None:
        """Each program on its own: Settings > Programs shows each one's state (ffprobe may be missing while ffmpeg
        is there, or the other way round)."""
        s = self.settings
        found, errors = {}, {}
        for name, override in (("mediainfo", s.mediainfo), ("ffmpeg", s.ffmpeg), ("ffprobe", s.ffprobe)):
            try:
                found[name] = tools.find_tool(name, override or None)
            except tools.ToolError as exc:  # the page shows the download links itself
                errors[name] = exc.short
        self.mediainfo = found.get("mediainfo")
        self.ffmpeg = tools.Tools(found["ffmpeg"], found["ffprobe"]) if "ffmpeg" in found and "ffprobe" in found else None
        self.tool_paths, self.tool_errors = found, errors

    def sign_in(self, key: str | None = None) -> None:
        """Check the saved (or given) API key with the site."""
        key = key if key is not None else self.secret(config.API_KEY)
        new_client, user, error = None, None, ""
        if not key:
            error = "No API key saved yet (Settings)."
        else:
            client = Client(self.settings.site, key)
            try:
                user, new_client = client.user(), client
            except ApiError as exc:
                client.close()
                error = str(exc)
        with self.lock:
            old = self.client
            self.client, self.user, self.user_error = new_client, user, error
        if old is not None and old is not new_client and not self.running:
            old.close()

    def flags(self) -> dict | None:
        return (self.user or {}).get("upload") if self.user else None

    def options(self) -> pipeline.Options:
        s = self.settings
        run = self.run_options
        qb = None
        if s.seeding == "qbittorrent" and s.qbittorrent_url:
            qb = (s.qbittorrent_url, s.qbittorrent_user, self.secret(config.QBIT_PASSWORD), s.qbittorrent_category)
        return pipeline.Options(
            output_dir=Path(s.output_dir).expanduser() if s.output_dir else None,
            anonymous=s.anonymous, personal_release=s.personal_release, tag=s.tag, mod_queue=s.mod_queue,
            internal=s.internal, rename=s.rename if s.rename in pipeline.RENAME_MODES else "content_id",
            announce=self.secret(config.ANNOUNCE_URL), seed=s.seeding,
            watch_dir=Path(s.watch_dir).expanduser() if s.watch_dir else None, qbittorrent=qb, clean_up=s.clean_up,
            waits_for_staff=pipeline.waits_for_staff(self.flags(), s.mod_queue),
            dry_run=run["dry_run"], allow_duplicates=run["allow_duplicates"], freeleech=int(run["freeleech"] or 0),
            freeleech_days=run["freeleech_days"], double_upload=run["double_upload"],
            double_upload_days=run["double_upload_days"], sticky=run["sticky"], refundable=run["refundable"],
            featured=run["featured"],
        )

    # -- queue -----------------------------------------------------------------------------

    def add(self, paths: list[str], recursive: bool) -> dict:
        skipped: list[Path] = []
        found = pipeline.collect([Path(p).expanduser() for p in paths if p.strip()], recursive, skipped)
        added = 0
        with self.lock:
            busy = {j.item.target for j in self.jobs if j.state not in FINISHED}
            for item in found:
                if item.target in busy:
                    continue
                job = Job(self.next_id, item)
                self.next_id += 1
                self.jobs.append(job)
                self.checks.put(job.id)
                added += 1
        if found:
            self.last_folder = str(found[0].target.parent)
        note = pipeline.skipped_note(skipped) if skipped else ""
        self.note(f"added {added} video(s)" + (f" ({len(found) - added} already in the list)" if len(found) > added else "")
                  + (f"; {note}" if note else ""))
        return {"added": added, "skipped": note}

    def job(self, job_id) -> Job:
        with self.lock:
            for job in self.jobs:
                if job.id == job_id:
                    return job
        raise Refused("That video is no longer in the list.")

    def _checker(self) -> None:
        self.ready.wait()
        while not self.quit.is_set():
            try:
                job_id = self.checks.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                job = self.job(job_id)
            except Refused:
                continue
            self.check(job)

    def check(self, job: Job, cid: str | None = None) -> None:
        ui = JobUi(self, job)
        infos = (job.plan.infos if job.plan else job.infos) if cid else None
        job.state, job.message, job.needs_cid, job.steps, job.result = "checking", "", False, [], None
        job.suggestion, job.infos, job.cid_problem, job.final = None, None, False, False
        if job.reread:
            job.reread = False
            job.item = pipeline.reread(job.item)
        if self.mediainfo is None and not job.item.problem:  # an ISO is refused as one, MediaInfo or not
            job.state, job.message = "bad", "MediaInfo was not found: download it (links in Settings > Programs)."
            return
        try:
            job.plan = pipeline.prepare(job.item, self.client, self.mediainfo, ui, cid=cid, infos=infos)
            job.state = "prepared"
        except pipeline.NeedsContentId as exc:
            job.state, job.needs_cid, job.message = "prepared", True, str(exc)
            job.suggestion, job.infos = exc.suggestion, exc.infos
        except pipeline.WrongId as exc:
            job.state, job.message, job.cid_problem = "bad", str(exc), True
        except pipeline.Skip as exc:
            job.state, job.message, job.final = "bad", str(exc), exc.final
        except pipeline.ERRORS as exc:
            log.warning("%s: %s", job.item.target, exc)
            job.state, job.message = "bad", str(exc)
        except Exception as exc:  # never let one video stop the checker
            log.exception("check failed")
            job.state, job.message = "bad", f"unexpected error: {exc}"
        if job.state == "bad":
            self.note(f"{job.item.target.name}: not accepted: {job.message}")

    def questions(self, job: Job, opts: pipeline.Options) -> list[str]:
        if job.needs_cid:
            return ["content_id"]
        return job.plan.questions(opts) if job.plan else []

    def answer(self, data: dict) -> None:
        job = self.job(data.get("id"))
        action = data.get("action")
        with self.lock:
            if job.state in ("waiting", "working", "checking"):
                raise Refused("Wait until this video is checked or uploaded.")
            if action == "skip":
                if job.state not in ("prepared", "bad", "failed"):
                    raise Refused("This video can't be skipped now.")
                job.state, job.message = "skipped", "skipped by you"
                self.note(f"{job.item.target.name}: skipped")
                return
            if action == "retry":
                # Refused before it could be checked: its folder is read again (in the checker, not under the lock).
                job.reread = job.plan is None
                if job.plan:
                    job.item = job.plan.item  # renamed already: check the new names
                job.plan, job.state = None, "checking"
                self.checks.put(job.id)
                return
            if action == "content_id":
                if job.state not in ("prepared", "bad"):
                    raise Refused("This video can't be changed now.")
                cid = str(data.get("content_id") or "").strip().lower()
                if not media.CID_RE.fullmatch(cid):
                    raise Refused("A content id has small letters, digits and _ only (e.g. abc00123).")
                job.state = "checking"
                self.note(f"{job.item.target.name}: content id {cid} given")
                threading.Thread(target=self.check, args=(job, cid), daemon=True).start()
                return
            if job.state != "prepared":
                raise Refused("This video can't be changed now.")
            self._answer_plan(job, action, data)

    def _answer_plan(self, job: Job, action, data: dict) -> None:
        plan = job.plan
        if plan is None:
            raise Refused("This video isn't checked yet.")
        try:
            if action == "source":
                plan.answer(source=str(data.get("source") or ""))
            elif action == "confirm":
                plan.answer(str(data.get("dvd_id") or ""), str(data.get("date") or "") or None)
                self.note(f"{job.item.target.name}: DVD id {plan.dvd_id} confirmed")
            elif action == "upload_anyway":
                plan.upload_anyway = True  # "uploaded although ..." is said once the upload went through
                self.note(f"{job.item.target.name}: upload anyway")
            else:
                raise Refused("Unknown answer.")
        except ValueError as exc:
            raise Refused(str(exc)[0].upper() + str(exc)[1:] + ".") from None

    def remove(self, job_id) -> None:
        job = self.job(job_id)
        with self.lock:
            if job.state in ("waiting", "working", "checking"):
                raise Refused("Wait until this video is done.")
            self.jobs.remove(job)

    def clear(self) -> None:
        opts = self.options()
        with self.lock:  # also videos shown as refused because they can't be uploaded at all (Plan.blocker)
            self.jobs = [j for j in self.jobs if j.state not in FINISHED
                         and not (j.state == "prepared" and j.plan and not j.needs_cid and j.plan.blocker(opts))]

    # -- running ---------------------------------------------------------------------------

    def start(self) -> int:
        opts = self.options()
        with self.lock:
            if self.running:
                raise Refused("Already working.")
            if self.ffmpeg is None or self.mediainfo is None:
                raise Refused("FFmpeg or MediaInfo is missing: download it (links in Settings > Programs).")
            if not opts.dry_run and self.client is None:
                raise Refused("Save your API key first (Settings), or tick Test run.")
            problem = pipeline.permission_problem(self.flags(), opts) if self.user else None
            if problem:
                raise Refused(problem)
            tag_problem = opts.tag_problem()
            if tag_problem:
                raise Refused(tag_problem[0].upper() + tag_problem[1:] + ".")
            if opts.seed == "watch" and not opts.watch_dir:
                raise Refused("Choose the watch folder first (Settings > Seeding).")
            todo = [j for j in self.jobs if j.state == "prepared" and j.plan is not None and not self.questions(j, opts)
                    and not j.plan.blocker(opts)]
            if not todo:
                raise Refused("No video is ready.")
            for job in todo:
                job.state, job.steps, job.message = "waiting", [], ""
            self.running, self.stop_after = True, False
            run = (opts, self.client, self.ffmpeg)
        threading.Thread(target=self._work, args=run, name="uploads", daemon=True).start()
        self.note(f"{'test run' if opts.dry_run else 'upload'} of {len(todo)} video(s) started")
        return len(todo)

    def _work(self, opts: pipeline.Options, client: Client | None, ffmpeg: tools.Tools) -> None:
        try:
            while not self.stop_after and not self.quit.is_set():
                with self.lock:
                    job = next((j for j in self.jobs if j.state == "waiting"), None)
                    if job is None:
                        break
                    job.state = "working"
                self.run_job(job, opts, client, ffmpeg)
        finally:
            with self.lock:
                for job in self.jobs:
                    if job.state == "waiting":
                        job.state = "prepared"
                self.running = False
            self.note("stopped" if self.stop_after else "queue finished")

    def run_job(self, job: Job, opts: pipeline.Options, client: Client | None, ffmpeg: tools.Tools) -> None:
        ui = JobUi(self, job)
        plan = job.plan
        if plan is None:
            job.state, job.message = "failed", "not checked yet"
            return
        try:
            if plan.existing is None and client is not None:
                pipeline.check_existing(plan, client, ui)  # the key was saved after the check
                if plan.questions(opts) or plan.blocker(opts):
                    job.state = "prepared"
                    return
            job.result = pipeline.execute(plan, client, ffmpeg, opts, ui)
            job.state = "test" if opts.dry_run else "done"
            job.message = job.result.message
            waits = " (waits for staff approval)" if job.result.waits_for_staff else ""
            self.note(f"{plan.item.target.name}: " + (f"uploaded {job.result.page or ''}{waits}" if not opts.dry_run else "test run done"))
        except pipeline.Skip as exc:
            job.state, job.message = "failed", str(exc)
        except pipeline.ERRORS as exc:
            log.warning("%s: %s", job.item.target, exc)
            job.state, job.message = "failed", str(exc)
        except Exception as exc:
            log.exception("upload failed")
            job.state, job.message = "failed", f"unexpected error: {exc}"
        finally:
            job.progress = None
            job.item = plan.item  # renamed: the list shows the new names
        if job.state == "failed":
            self.note(f"{job.item.target.name}: failed: {job.message}")

    # -- what the page shows ----------------------------------------------------------------

    def job_view(self, job: Job, opts: pipeline.Options) -> dict:
        plan = job.plan
        questions = self.questions(job, opts) if job.state == "prepared" else []
        state = ("ask" if questions else "ready") if job.state == "prepared" else job.state
        message = job.message
        blocker = plan.blocker(opts) if plan and job.state == "prepared" and not job.needs_cid else None
        if blocker:
            state, message, questions = "bad", blocker, []
        # "Wrong CID? Check with this one" only where the content id can be what's wrong.
        cid_problem = job.cid_problem or bool(blocker and plan.unknown_everywhere())
        size = 0
        for path in job.item.files:
            try:
                size += path.stat().st_size
            except OSError:
                pass
        view = {
            "id": job.id, "state": state, "target": str(job.item.target), "name": job.item.target.name,
            "files": [p.name for p in job.item.files], "size": size, "message": message, "questions": questions,
            "steps": job.steps[-12:], "progress": job.progress, "cid_problem": cid_problem,
            "final": job.final and job.state == "bad",  # no "Check again": it can't change this refusal
        }
        if job.needs_cid:
            # The file's name may start with the content id ("kobp00007 DVD.mkv"): offered, never taken by itself.
            word = re.split(r"[\s.\[\]()]+", job.item.target.stem.lower().strip())[0]
            view["cid_guess"] = word if media.CID_RE.fullmatch(word) and re.search(r"[a-z]\d", word) else ""
            if job.suggestion:  # the content id of the DVD id in the name: filled in, the member confirms it
                s = job.suggestion
                view.update(cid_suggestion=s.cid, cid_others=s.others, cid_dvd_id=s.dvd_id, cid_source=s.source, cid_where=s.where,
                            # What the lookup said about that video (another maker may use the same DVD id), and the
                            # length of the member's file to compare with.
                            cid_title=s.title, cid_maker=s.maker, cid_date=s.release_date, cid_runtime=s.runtime,
                            file_duration=sum(i.duration for i in job.infos or []))
        if plan:
            first = plan.first
            view.update(
                cid=plan.cid, kind=plan.kind, source=plan.source, source_chosen=plan.source_chosen,
                title=plan.title(opts), resolution=first.title_resolution_for(plan.kind),
                video=first.video_word(plan.kind), audio=first.audio_codec, duration=sum(i.duration for i in plan.infos),
                width=first.width, height=first.height, interlaced=first.interlaced,
                known=plan.known, dvd_id=plan.dvd_id, dvd_from=plan.dvd_from, guess=plan.guess, date=plan.release_date,
                dmm_dvd_id=plan.dmm.dvd_id if plan.dmm else "", dmm_date=plan.dmm.release_date if plan.dmm else "",
                existing=[{k: t.get(k) for k in ("name", "type", "resolution", "seeders", "details_link")} for t in plan.existing or []],
                checked=plan.existing is not None, upload_anyway=plan.upload_anyway, warnings=plan.notes(),
                renames=[[str(old.name), str(new.name)] for old, new in plan.renames(opts)],
            )
        if job.result:
            view.update(page=job.result.page, seed=str(job.result.seed_torrent or ""), warnings=job.result.warnings,
                        waits_for_staff=job.result.waits_for_staff)
        # A cover to show (the page loads it from /cover/<id>): covers on, r18.dev gave one, and the video isn't refused.
        view["cover"] = bool(self.settings.covers and plan and covers.allowed(plan.cover) and state != "bad")
        return view

    def state(self, after: int) -> dict:
        opts = self.options()
        s = self.settings
        with self.lock:
            jobs = []
            for job in self.jobs:
                try:
                    jobs.append(self.job_view(job, opts))
                except Exception as exc:
                    log.exception("showing %s failed", job.item.target)
                    jobs.append({"id": job.id, "state": "bad", "target": str(job.item.target), "name": job.item.target.name,
                                 "files": [], "size": 0, "message": f"can't be shown ({exc})", "questions": [],
                                 "steps": [], "progress": None})
            lines = [list(line) for line in self.lines if line[0] > after]
        return {
            "version": __version__, "site": s.site, "running": self.running, "stopping": self.stop_after,
            "jobs": jobs, "log": lines,
            "user": {"name": self.user.get("username"), "group": self.user.get("group")} if self.user else None,
            "user_error": self.user_error, "flags": self.flags(),
            "options": {**{k: getattr(s, k) for k in SAVED_OPTIONS}, **self.run_options},
            "settings": {
                "seed_mode": s.seeding, "watch_dir": s.watch_dir, "clean_up": s.clean_up, "qbittorrent_url": s.qbittorrent_url,
                "qbittorrent_user": s.qbittorrent_user, "qbittorrent_category": s.qbittorrent_category,
                "mediainfo": s.mediainfo, "ffmpeg": s.ffmpeg, "ffprobe": s.ffprobe,
                "has_key": bool(self.secret(config.API_KEY)), "starting": not self.ready.is_set(),
                "has_announce": bool(opts.announce), "announce_ok": not opts.announce or not torrent.announce_problem(opts.announce, s.site),
                "has_qb_password": bool(self.secret(config.QBIT_PASSWORD)), "store": self.store,
                "store_name": config.store_name(), "store_where": store_where(),
                "store_problem": "" if self.store else config.store_problem(),
                "key_from_env": self.from_env(config.API_KEY), "announce_from_env": self.from_env(config.ANNOUNCE_URL),
                "config_path": str(config.config_path()), "log_dir": str(config.log_dir()),
                "theme": s.theme, "covers": s.covers,
            },
            "tools": {
                **{name: str(self.tool_paths[name]) if name in self.tool_paths else "" for name in ("mediainfo", "ffmpeg", "ffprobe")},
                "errors": self.tool_errors,
            },
            "last_folder": self.last_folder,
        }

    # -- settings --------------------------------------------------------------------------

    def set_options(self, data: dict) -> None:
        s = self.settings
        saved, run = {}, {}
        for name in SAVED_OPTIONS:
            if name in data:
                value = data[name]
                value = bool(value) if isinstance(getattr(s, name), bool) else str(value or "").strip()
                if name == "rename" and value not in pipeline.RENAME_MODES:
                    raise Refused("Unknown rename choice.")
                if name == "tag" and value:
                    problem = pipeline.Options(personal_release=True, tag=value).tag_problem()
                    if problem:
                        raise Refused(problem[0].upper() + problem[1:] + ".")
                saved[name] = value
        for name in self.run_options:
            if name in data:
                value = data[name]
                try:
                    if name == "freeleech":
                        value = int(value or 0)
                        if value not in (0, 25, 50, 75, 100):
                            raise Refused("Freeleech is 0, 25, 50, 75 or 100 %.")
                    elif name.endswith("_days"):
                        value = int(value) if str(value or "").strip() else None
                        if value is not None and not 1 <= value <= 365:
                            raise Refused("Days are 1 to 365 (empty: no end).")
                    else:
                        value = bool(value)
                except (TypeError, ValueError):
                    raise Refused("Days and freeleech are whole numbers.") from None
                run[name] = value
        with self.lock:
            changed = {name: value for name, value in saved.items() if value != getattr(s, name)}
            for name, value in changed.items():
                setattr(s, name, value)
            self.run_options.update(run)
        if changed:
            config.save(s)

    def save_page(self, data: dict) -> None:
        """The page's theme (system, dark, light) and whether the list shows covers."""
        s = self.settings
        theme, shown = s.theme, s.covers
        if "theme" in data:
            theme = data["theme"]
            if theme not in config.THEMES:
                raise Refused("The theme is system, dark or light.")
        if "covers" in data:
            shown = data["covers"]
            if not isinstance(shown, bool):
                raise Refused("Covers are on (true) or off (false).")
        with self.lock:
            changed = (theme, shown) != (s.theme, s.covers)
            s.theme, s.covers = theme, shown
        if changed:
            config.save(s)

    def cover(self, job_id: int) -> tuple[bytes, str] | None:
        """The cover of a video in the list (fetched once from DMM's image server), or None. Nothing is fetched
        while covers are off."""
        if not self.settings.covers:
            return None
        with self.lock:
            job = next((j for j in self.jobs if j.id == job_id), None)
            url = job.plan.cover if job is not None and job.plan is not None else ""
        return self.covers.get(url) if url else None

    def save_key(self, key: str) -> None:
        key = key.strip()
        if not key:
            raise Refused("Paste the API key first.")
        if self.running:
            raise Refused("Wait until the uploads are done.")
        self.env_wins(config.API_KEY)
        self.sign_in(key)
        if not self.user:
            error = self.user_error
            self.sign_in()  # back to the saved key
            raise Refused(f"ClearJAV refused that key: {error}")
        self.store_secret(config.API_KEY, key)
        self.note(f"API key checked and saved: signed in as {self.user.get('username')}")

    def from_env(self, name: str) -> bool:
        return bool(os.environ.get(config.SECRET_ENV[name], "").strip())

    def env_wins(self, name: str) -> None:
        if self.from_env(name):
            raise Refused(f"The environment variable {config.SECRET_ENV[name]} is set and always wins: change or remove it there.")

    def store_secret(self, name: str, value: str) -> None:
        if not self.set_secret(name, value):
            self.store = config.store_available()
            raise Refused(f"Not saved: {config.store_problem() or 'unknown reason'}. "
                          f"Set {config.SECRET_ENV[name]} in the environment instead.")

    def forget_key(self) -> None:
        if self.running:
            raise Refused("Wait until the uploads are done.")
        if self.from_env(config.API_KEY):
            raise Refused("The key comes from the environment variable CLEARJAV_API_KEY: remove it there.")
        self.delete_secret(config.API_KEY)
        self.sign_in()
        self.note(f"API key removed from {config.store_name()}")

    def save_announce(self, url: str) -> None:
        url = url.strip()
        problem = torrent.announce_problem(url, self.settings.site)
        if problem:
            raise Refused(problem[0].upper() + problem[1:] + ".")
        self.env_wins(config.ANNOUNCE_URL)
        self.store_secret(config.ANNOUNCE_URL, url)
        self.note("announce URL saved")

    def forget_announce(self) -> None:
        if self.from_env(config.ANNOUNCE_URL):
            raise Refused("The announce URL comes from the environment variable CLEARJAV_ANNOUNCE_URL: remove it there.")
        self.delete_secret(config.ANNOUNCE_URL)
        self.note("announce URL removed")

    def save_seeding(self, data: dict) -> None:
        s = self.settings
        mode = str(data.get("seed_mode") or "save")
        if mode not in pipeline.SEED_MODES:
            raise Refused("Unknown seeding choice.")
        watch = str(data.get("watch_dir") or "").strip()
        url = str(data.get("qbittorrent_url") or "").strip().rstrip("/")
        if mode == "watch" and not watch:
            raise Refused("Choose the watch folder.")
        if mode == "qbittorrent" and not url.startswith(("http://", "https://")):
            raise Refused("qBittorrent's Web UI address starts with http:// (e.g. http://127.0.0.1:8080).")
        s.seed_mode, s.watch_dir, s.qbittorrent_url = mode, watch, url
        s.clean_up = bool(data.get("clean_up"))
        s.qbittorrent_user = str(data.get("qbittorrent_user") or "").strip()
        s.qbittorrent_category = str(data.get("qbittorrent_category") or "").strip()
        password = str(data.get("qbittorrent_password") or "")
        if password:
            self.env_wins(config.QBIT_PASSWORD)
            self.store_secret(config.QBIT_PASSWORD, password)
        config.save(s)
        self.note(f"seeding: {mode}")

    def test_qbittorrent(self, data: dict) -> str:
        url = str(data.get("qbittorrent_url") or self.settings.qbittorrent_url).strip()
        password = str(data.get("qbittorrent_password") or "") or self.secret(config.QBIT_PASSWORD)
        try:
            version = qbittorrent.version(url, str(data.get("qbittorrent_user") or "").strip(), password)
        except qbittorrent.QbitError as exc:
            raise Refused(str(exc)) from None
        self.note(f"qBittorrent {version} answered")
        return version

    def save_tools(self, data: dict) -> None:
        if self.running:
            raise Refused("Wait until the uploads are done.")
        for name in ("mediainfo", "ffmpeg", "ffprobe"):
            if name in data:
                setattr(self.settings, name, str(data[name] or "").strip().strip('"'))
        config.save(self.settings)
        self.find_tools()

    def doctor(self) -> None:
        if self.running:
            raise Refused("Wait until the uploads are done.")
        self.find_tools()
        self.sign_in()
        names = {"mediainfo": "MediaInfo", "ffmpeg": "FFmpeg", "ffprobe": "ffprobe"}
        found = ", ".join(names[name] for name in self.tool_paths) or "nothing"
        self.note(f"setup checked: {found} found; " + (f"signed in as {self.user.get('username')}" if self.user else self.user_error))


# -- folders ------------------------------------------------------------------------------------


def drives() -> list[str]:
    """Windows' drive letters, without touching the drives (an empty card reader would make us wait)."""
    try:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        mask = (1 << 26) - 1
    return [f"{letter}:\\" for bit, letter in enumerate(string.ascii_uppercase) if mask >> bit & 1]


def browse(path: str) -> dict:
    """Folders and videos in `path` (the drives on Windows, or the home folder, when empty)."""
    if not path:
        if os.name == "nt":
            return {"path": "", "parent": None, "entries": [{"name": d, "path": d, "dir": True} for d in drives()]}
        path = str(Path.home())
    folder = Path(path).expanduser()
    if not folder.is_dir():
        raise Refused(f"Not a folder: {folder}")
    entries = []
    try:
        children = list(folder.iterdir())
    except OSError as exc:
        raise Refused(f"Can't open {folder} ({exc.strerror or exc})") from None
    for child in children:
        if child.name.startswith(".") or child.name.startswith("$"):
            continue
        try:
            if getattr(child.stat(), "st_file_attributes", 0) & 6:  # Windows' hidden and system files and folders
                continue
            is_dir = child.is_dir()
            if not is_dir and child.suffix.lower() not in media.VIDEO_EXTENSIONS | {".iso"}:
                continue
            entries.append({"name": child.name, "path": str(child), "dir": is_dir,
                            "size": None if is_dir else child.stat().st_size})
        except OSError:
            continue
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    parent = str(folder.parent) if folder.parent != folder else ("" if os.name == "nt" else None)
    return {"path": str(folder), "parent": parent, "entries": entries}


# -- HTTP ---------------------------------------------------------------------------------------


def with_theme(page: bytes, theme: str) -> bytes:
    """The page with the saved theme already set, so it shows in the right colors at once (no script needed)."""
    if theme not in ("dark", "light"):
        return page  # system: the page follows the computer's setting (prefers-color-scheme)
    return page.replace(b'<html lang="en">', f'<html lang="en" data-theme="{theme}">'.encode(), 1)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Linux/macOS: a restart within a minute gets the same port again (TIME_WAIT); Windows would let two share it.
    allow_reuse_address = os.name != "nt"

    app: App

    def __init__(self, address, token: str) -> None:
        super().__init__(address, Handler)
        self.token = token
        self.port = self.server_address[1]
        self.cookie = f"clearjav_aio_{self.port}"


class Handler(BaseHTTPRequestHandler):
    server: Server
    server_version = "clearjav-aio"
    sys_version = ""

    def log_message(self, format, *args) -> None:  # noqa: A002 (the base class's name)
        pass

    def _headers(self, status: int, content_type: str, length: int, extra: dict | None = None) -> None:
        extra = dict(extra or {})
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", extra.pop("Cache-Control", "no-store"))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                                    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        for name, value in extra.items():
            self.send_header(name, value)
        self.end_headers()

    def _send(self, status: int, payload, content_type: str = "application/json", extra: dict | None = None) -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self._headers(status, content_type, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _host_ok(self) -> bool:
        # Only this computer's names (another site's name = DNS rebinding); any port, for SSH tunnels (-L 9000:...:8765).
        host = (self.headers.get("Host") or "").lower()
        return re.fullmatch(r"(127\.0\.0\.1|localhost|\[::1\])(:\d{1,5})?", host) is not None

    def _cookie_ok(self) -> bool:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == self.server.cookie and secrets.compare_digest(value, self.server.token):
                return True
        return False

    def _refuse(self, text: str) -> None:
        self._send(HTTPStatus.FORBIDDEN, f"<!doctype html><meta charset=utf-8><title>ClearJAV Uploader</title>"
                   f"<p>{text}</p>".encode(), "text/html; charset=utf-8")

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._refuse("Wrong address.")
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/" and "key" in query:
            if not secrets.compare_digest(query["key"][0], self.server.token):
                return self._refuse("This link is from an earlier start. Use the link in the uploader's window.")
            cookie = f"{self.server.cookie}={self.server.token}; Path=/; HttpOnly; SameSite=Strict"
            return self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": "/", "Set-Cookie": cookie})
        if not self._cookie_ok():
            return self._refuse("Open the uploader with the link shown in its window.")
        app = self.server.app
        if url.path in STATIC:
            name, content_type = STATIC[url.path]
            try:
                body = (web_dir() / name).read_bytes()
            except OSError:
                return self._send(HTTPStatus.NOT_FOUND, {"error": "missing file"})
            if url.path == "/":
                body = with_theme(body, app.settings.theme)
            return self._send(HTTPStatus.OK, body, content_type)
        cover = COVER_PATH.fullmatch(url.path)
        if cover:
            try:
                image = app.cover(int(cover.group(1)))
            except Exception:  # never a broken answer: the page shows its stand-in tile
                log.exception("cover failed")
                image = None
            if image is None:
                return self._send(HTTPStatus.NOT_FOUND, {"error": "no cover"})
            # The browser keeps it: the list is redrawn often, and a cover never changes for a link (?cid=).
            return self._send(HTTPStatus.OK, image[0], image[1], {"Cache-Control": "private, max-age=86400"})
        try:
            if url.path == "/api/state":
                return self._send(HTTPStatus.OK, app.state(int((query.get("after") or ["0"])[0] or 0)))
            if url.path == "/api/browse":
                return self._send(HTTPStatus.OK, browse((query.get("path") or [""])[0]))
        except Refused as exc:
            return self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
        except ValueError:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "bad request"})
        self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._host_ok() or not self._cookie_ok() or self.headers.get(HEADER) != "1":
            return self._send(HTTPStatus.FORBIDDEN, {"error": "refused"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JSON only"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "bad length"})
        if length > MAX_BODY:
            return self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too large"})
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return self._send(HTTPStatus.BAD_REQUEST, {"error": "bad JSON"})
        app = self.server.app
        path = urlparse(self.path).path
        actions = {
            "/api/add": lambda: app.add([str(p) for p in data.get("paths") or []], bool(data.get("recursive"))),
            "/api/answer": lambda: app.answer(data),
            "/api/remove": lambda: app.remove(data.get("id")),
            "/api/clear": app.clear,
            "/api/options": lambda: app.set_options(data),
            "/api/start": lambda: {"started": app.start()},
            "/api/stop": lambda: setattr(app, "stop_after", True),
            "/api/key": lambda: app.save_key(str(data.get("key") or "")),
            "/api/key/forget": app.forget_key,
            "/api/announce": lambda: app.save_announce(str(data.get("url") or "")),
            "/api/announce/forget": app.forget_announce,
            "/api/seeding": lambda: app.save_seeding(data),
            "/api/qbittorrent/test": lambda: {"version": app.test_qbittorrent(data)},
            "/api/tools": lambda: app.save_tools(data),
            "/api/page": lambda: app.save_page(data),
            "/api/doctor": app.doctor,
            "/api/quit": lambda: self._quit(),
        }
        action = actions.get(path)
        if action is None:
            return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
        try:
            answer = action()
        except Refused as exc:
            return self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
        except Exception as exc:
            log.exception("request %s failed", path)
            return self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"unexpected error: {logs.redact(exc)}"})
        self._send(HTTPStatus.OK, answer if isinstance(answer, dict) else {"ok": True})

    do_HEAD = do_GET

    def _quit(self) -> None:
        if self.server.app.running:
            raise Refused("Uploads are running: stop them first (or close the window).")
        self.server.app.quit.set()
        threading.Thread(target=self.server.shutdown, daemon=True).start()


class PortError(Exception):
    """The port asked for (--port) can't be used; the message says what to do."""


def make_server(settings: config.Settings, port: int = 0) -> Server:
    token = secrets.token_urlsafe(32)
    server = None
    for candidate in ([port] if port else [PORT, 0]):
        try:
            server = Server(("127.0.0.1", candidate), token)
            break
        except OSError as exc:
            if not port:
                continue  # 8765 is taken (another copy?): any free port
            if exc.errno == errno.EADDRINUSE:
                raise PortError(f"Port {port} is in use, maybe by another copy of the uploader: close it, "
                                "or start without --port to use a free port.") from None
            raise PortError(f"Port {port} can't be used ({exc.strerror or exc}): start without --port to use a free port.") from None
    if server is None:
        raise OSError("no free port")
    server.app = App(settings)
    return server


def serve(settings: config.Settings, port: int = 0, open_browser: bool = True) -> int:
    server = make_server(settings, port)
    app = server.app
    threading.Thread(target=app.startup, name="startup", daemon=True).start()
    url = f"http://127.0.0.1:{server.port}/?key={server.token}"
    app.note(f"ClearJAV Uploader {__version__} started")
    print(f"ClearJAV Uploader {__version__} is running in your browser:\n\n    {url}\n")
    print("Keep this window open while you upload. Closing it (or Ctrl+C) stops the uploader.", flush=True)
    has_screen = os.name == "nt" or sys.platform == "darwin" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if open_browser and has_screen:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    elif open_browser:
        print(f"No screen here: open the address above in a browser on your computer. Over SSH, first run there: "
              f"ssh -L {server.port}:127.0.0.1:{server.port} <you>@<this machine>", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        app.quit.set()
        server.server_close()
        if app.client:
            app.client.close()
        app.covers.close()
    return 0
