"""Uploads from start to finish: what to upload, checks, files for the site, upload, seeding.

Each upload is prepared first (`prepare`: reads the video, looks it up, checks ClearJAV; changes nothing),
then the open questions are answered (`Plan.questions`: by the terminal at once, by the browser page
whenever the member answers), then it is carried out (`execute`: renames, contact sheets, torrent,
upload, seeding).
"""

from __future__ import annotations

import collections
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import contact_sheet, dmm_api, media, naming, qbittorrent, r18, torrent, tools
from .api import ApiError, Client, UnconfirmedUpload, torrent_page

log = logging.getLogger(__name__)

# A part: the content id, maybe with DMM's letters after the number (1sddm00972ai is the AI upscale, 1start00561v the
# extended version: listings of their own), then pt or part and the number (abc00123-pt1, abc00123_part02, -pt10).
PART_RE = re.compile(r"^(?P<cid>[a-z0-9_]*[a-z][a-z0-9_]*\d[a-z]{0,3})[-_ .]?(?:pt|part)0*(?P<n>[1-9]\d?)$", re.IGNORECASE)
# A file name counts as a content id only with letters and digits (abc00123), not "video" or "movie".
NAME_CID_RE = re.compile(r"^(?=[a-z0-9_]*[a-z])(?=[a-z0-9_]*\d)[a-z0-9_]{3,80}$")
# Small files that may sit next to the parts in their folder (the folder is renamed with them).
PART_FOLDER_EXTRAS = {".nfo", ".txt", ".sfv", ".md5", ".jpg", ".jpeg", ".png", ".webp"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DVD_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,40}$")
# A file or folder name that starts with a DVD id: GVH-882, IPZZ-020, ABP123, maybe with a suffix (GVH-882-C, ABP-123A,
# "IPZZ-020 2023-04-07 1080p DMM WEB-DL H.264 AAC").
DVD_NAME_RE = re.compile(r"^(?P<label>[a-z]{2,10})[-_ ]?(?P<number>\d{2,6})(?:[a-z]{1,3})?(?:[-_ .\[(].*)?$", re.IGNORECASE)
WEB_DL_TYPE = media.TYPE_IDS[media.WEB_DL]
CATEGORY = 1
SEED_MODES = ("save", "watch", "qbittorrent")
ERRORS = (media.MediaError, contact_sheet.SheetError, r18.R18Error, ApiError, OSError, ValueError)


class Skip(Exception):
    """This video is not uploaded (the reason is the message). `final`: what the files are decides it (an ISO, a part
    on its own, codecs the site doesn't take ...), so checking the same files again can't change it."""

    def __init__(self, message: str = "", final: bool = False) -> None:
        super().__init__(message)
        self.final = final


class WrongId(Skip):
    """Not uploaded because of the content id (unknown, or files that name different ones): another can be tried."""


@dataclass
class Suggestion:
    """A content id found for the DVD id in a name, or the one r18.dev answered another content id with: only
    offered, the member confirms it. Another maker may use the same DVD id (KIL-033 is Prestige's 118kil033 on
    r18.dev, not kil00033), so what the lookup said about the video is shown with it."""

    dvd_id: str
    cid: str
    others: list[str]  # other listings of the DVD id (DMM's DVD listing when the digital one is suggested)
    source: str  # r18.dev, DMM, or both
    where: str  # file name, folder name, or "content id abp123" (r18.dev answered it with another one)
    title: str = ""
    maker: str = ""
    release_date: str = ""
    runtime: int = 0  # minutes

    def about(self) -> str:
        """'"Title" (Prestige, released 2014-03-01, 115 min)', or "" when the lookup said nothing about the video."""
        title = self.title if len(self.title) <= 90 else self.title[:88].rstrip() + "…"
        facts = ", ".join(fact for fact in (self.maker, f"released {self.release_date}" if self.release_date else "",
                                            f"{self.runtime} min" if self.runtime else "") if fact)
        return " ".join(part for part in (f'"{title}"' if title else "", f"({facts})" if facts else "") if part)

    def text(self) -> str:
        give = "give" if " and " in self.source else "gives"
        about = self.about()
        check = f", which is {about}: check it is your video" if about else " (check it)"
        others = f"; other listing: {', '.join(self.others)}" if self.others else ""
        return f"the {self.where} looks like the DVD id {self.dvd_id}, and {self.source} {give} the content id {self.cid}{check}{others}"


class NeedsContentId(Skip):
    """Neither DMM's id in the file nor the name tells the content id: the member must type it (or confirm the
    content id suggested from a DVD id in the name)."""

    def __init__(self, message: str, suggestion: Suggestion | None = None, infos: list | None = None) -> None:
        super().__init__(message)
        self.suggestion = suggestion
        self.infos = infos  # what MediaInfo read, so the answer doesn't read the files again


@dataclass
class Item:
    """One upload: a video file, or a folder holding the parts of one video."""

    target: Path
    files: list[Path]
    problem: str = ""
    final: bool = False  # the problem can't change while the video keeps its place and names (Skip.final)


@dataclass
class Options:
    output_dir: Path | None = None
    anonymous: bool = False
    personal_release: bool = False
    tag: str = ""
    mod_queue: bool = False
    internal: bool = False
    dry_run: bool = False
    allow_duplicates: bool = False
    rename: str = "content_id"  # content_id, dvd_id or title: the Upload Rules want one of these as file name
    remux_source: str = ""  # the terminal's --source: the source of every Remux (empty: guessed per video)
    announce: str = ""  # your announce URL for the .torrent the tool makes (empty: without passkey)
    seed: str = "save"  # save, watch (copy the site's .torrent into watch_dir) or qbittorrent
    watch_dir: Path | None = None
    qbittorrent: tuple[str, str, str, str] | None = None  # url, user, password, category
    clean_up: bool = False  # after a successful upload, delete the sheets and MediaInfo (and the .torrent qBittorrent took)
    waits_for_staff: bool = False  # the upload goes to the moderation queue (waits_for_staff())
    # Staff and internal groups only (the site ignores them for everyone else):
    freeleech: int = 0  # percent
    freeleech_days: int | None = None
    double_upload: bool = False
    double_upload_days: int | None = None
    sticky: bool = False
    refundable: bool = False
    featured: bool = False

    def tag_problem(self) -> str | None:
        if self.internal or not self.personal_release or not self.tag.strip():
            return None
        if not naming.valid_tag(self.tag.strip()):
            return "the tag may only have letters, digits, . _ - and spaces (at most 31)"
        if "clearjav" in self.tag.lower():
            return "the tag ClearJAV is for internal releases"
        return None

    def release_tag(self) -> str:
        """Internal releases are always "-ClearJAV"; a personal release may carry your tag."""
        if self.internal:
            return "ClearJAV"
        return self.tag.strip() if self.personal_release else ""


RENAME_MODES = ("content_id", "dvd_id", "title")


@dataclass
class Result:
    target: Path
    ok: bool
    message: str
    title: str = ""
    page: str | None = None
    seed_torrent: Path | None = None
    warnings: list[str] = field(default_factory=list)
    waits_for_staff: bool = False  # uploaded, and waits in the moderation queue until staff approve it


class Ui:
    """Questions and progress; the terminal and the browser page answer them their own way."""

    interactive = False

    def step(self, text: str) -> None: ...

    def progress(self, fraction: float) -> None: ...

    def end_progress(self) -> None: ...

    def confirm(self, question: str) -> bool:
        return False

    def ask(self, question: str) -> str:
        return ""


@dataclass
class Plan:
    """What `prepare` found out about one upload, completed by the member's answers."""

    item: Item
    infos: list[media.Media]
    cid: str
    kind: str  # media.WEB_DL or media.REMUX
    source: str  # DMM for WEB-DLs; DVD, VHS, Blu-Ray, 3D Blu-Ray or UHD Blu-Ray for Remuxes
    source_chosen: bool = False  # the member chose the Remux source (else guessed from the name or size)
    known: bool = True  # r18.dev knows the content id (if not, DVD id and date go with the upload)
    r18_id: str = ""  # the id r18.dev knows it as
    cover: str = ""  # r18.dev's link to DMM's small cover (the page shows it when covers are on)
    dvd_id: str = ""
    release_date: str = ""
    dvd_from: str = ""  # where the DVD id came from: r18.dev, DMM, worked out, you
    guess: str = ""  # DVD id worked out from the content id (only a guess)
    dmm: dmm_api.Product | None = None
    existing: list[dict] | None = None  # torrents already on ClearJAV (None: not checked)
    confirmed: bool = False  # the member confirmed the DVD id / date / upload of an unknown id
    upload_anyway: bool = False  # the member wants it although ClearJAV has the video
    warnings: list[str] = field(default_factory=list)

    @property
    def first(self) -> media.Media:
        return self.infos[0]

    def blocker(self, opts: Options) -> str | None:
        """Why this can't be uploaded at all (not a question the member can answer), or None. The same in a test
        run: it reports what a real upload would meet."""
        same = self.same_title(opts)
        if same is not None:
            return same_title_problem(str(same.get("details_link") or ""))
        if self.unknown_everywhere():
            # The site would have to create the video, which needs its title and cover (website upload form only).
            return (f"r18.dev doesn't know {self.cid} and ClearJAV has no torrent of it, so ClearJAV would have to create "
                    "the video, which needs its title and cover: check the content id, or upload it on the website")
        return None

    def unknown_everywhere(self) -> bool:
        """r18.dev doesn't know the id and ClearJAV has no torrent of it: a wrong content id, or a new video."""
        return not self.known and self.existing == []

    def same_title(self, opts: Options) -> dict | None:
        """The torrent on ClearJAV with exactly this upload's title: the site refuses a second one ("The name has
        already been taken"). The title changes with the options (internal, personal release tag)."""
        mine = self.title(opts).strip().casefold()
        return next((t for t in self.existing or [] if str(t.get("name") or "").strip().casefold() == mine), None)

    def questions(self, opts: Options) -> list[str]:
        """What the member must still answer: "unknown" (r18.dev doesn't know the id: DVD id and date),
        "dvd_id" (nobody has a DVD id, or only a worked-out one), "duplicate" (ClearJAV has the video)."""
        open_ = []
        if not self.known and not self.confirmed:
            open_.append("unknown")
        elif not self.dvd_id or (self.dvd_from == "worked out" and not self.confirmed):
            open_.append("dvd_id")
        if self.existing and not self.upload_anyway and not opts.allow_duplicates and not opts.dry_run:
            open_.append("duplicate")
        return open_

    def answer(self, dvd_id: str | None = None, date: str | None = None, source: str | None = None) -> None:
        """The member's DVD id (and date for an id r18.dev doesn't know); ValueError when not valid."""
        if source is not None:
            if self.kind != media.REMUX or source not in media.REMUX_SOURCES:
                raise ValueError(f"the source is one of {', '.join(media.REMUX_SOURCES)} (Remuxes only)")
            self.source, self.source_chosen = source, True
        if dvd_id is None:
            return
        dvd_id = dvd_id.strip().upper()
        if not DVD_ID_RE.fullmatch(dvd_id):
            raise ValueError("the DVD id has letters, digits and dashes only (e.g. ABC-123)")
        if not self.known:
            date = (date or "").strip()
            if not DATE_RE.fullmatch(date):
                raise ValueError("the release date is written YYYY-MM-DD")
            self.release_date = date
        self.dvd_id, self.dvd_from, self.confirmed = dvd_id, "you", True

    def is_fc2(self) -> bool:
        """FC2 videos (fc2... ids): their titles have no date, by the site's rules."""
        return self.cid.startswith("fc2") or self.dvd_id.lower().startswith("fc2")

    def date_missing(self) -> bool:
        """r18.dev knows the video, but neither it nor DMM has a release date for the title."""
        return self.known and not self.release_date and not self.is_fc2()

    def answer_date(self, date: str) -> None:
        """The member's release date for a video r18.dev and DMM know without one; ValueError when not valid."""
        date = date.strip()
        if not DATE_RE.fullmatch(date):
            raise ValueError("the release date is written YYYY-MM-DD")
        self.release_date = date

    def notes(self) -> list[str]:
        """The warnings, and a missing release date (the browser page has no date field for a known video)."""
        missing = [f"r18.dev and DMM have no release date for {self.cid}: the title goes without one"] if self.date_missing() else []
        return [*self.warnings, *missing]

    def title(self, opts: Options) -> str:
        return naming.title(self.dvd_id or self.guess or self.cid.upper(), self.release_date,
                            self.first.title_resolution_for(self.kind), self.first.video_word(self.kind),
                            self.first.audio_codec, opts.release_tag(), self.source, self.kind)

    def base_name(self, opts: Options) -> str:
        names = {"content_id": self.cid, "dvd_id": self.dvd_id or self.guess or self.cid, "title": self.title(opts)}
        return naming.safe_name(names.get(opts.rename, self.cid))

    def renames(self, opts: Options) -> list[tuple[Path, Path]]:
        return rename_plan(self.item, self.infos, self.base_name(opts))


def same_title_problem(link: str = "") -> str:
    where = link or "it may be waiting for moderation or deleted, which the duplicate check can't see"
    return (f"ClearJAV already has a torrent with exactly this title ({where}): the site refuses a second one. Only a "
            "different version can go up (another resolution or type, or a personal release with your tag)")


NAME_TAKEN = "name has already been taken"  # the site's answer to an upload with a title it has
# The site's answer to an image over its limit: "The image must be between 1 and 4096 kilobytes."
IMAGE_SIZE = re.compile(r"must be between \d+ and \d+ kilobytes", re.IGNORECASE)


def content_id_from_name(path: Path) -> tuple[str | None, int | None]:
    stem = path.stem.lower()
    match = PART_RE.match(stem)
    if match:
        return match.group("cid"), int(match.group("n"))
    return (stem, None) if NAME_CID_RE.fullmatch(stem) else (None, None)


def dvd_id_in_name(name: str) -> str | None:
    """The DVD id a name looks like (GVH-882, abp123, IPZZ-020 4K -> GVH-882, ABP-123, IPZZ-020), the digits exactly
    as written; None when it doesn't look like one."""
    match = DVD_NAME_RE.fullmatch(name.strip())
    return f"{match.group('label').upper()}-{match.group('number')}" if match else None


def _bare(dvd_id: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", dvd_id.upper())


def suggest_content_id(item: Item, ui: Ui) -> Suggestion | None:
    """The content id of a DVD id in the file's name (or its folder's), looked up on r18.dev and DMM; None when
    nothing is found. Only a suggestion: the member confirms it."""
    names = [(item.target.name, "folder name")] if item.target not in item.files else [(item.files[0].stem, "file name")]
    if item.target in item.files:
        folder = item.target.parent
        try:  # the folder names the video only when it holds no other video
            alone = folder.parent != folder and sum(p.suffix.lower() in media.VIDEO_EXTENSIONS for p in folder.iterdir()) == 1
        except OSError:
            alone = False
        if alone:
            names.append((folder.name, "folder name"))
    for name, where in names:
        dvd_id = dvd_id_in_name(name)
        if not dvd_id:
            continue
        ui.step(f"{dvd_id}: looking up the DVD id on r18.dev")
        try:
            video = r18.lookup_dvd_id(dvd_id)
        except r18.R18Error as exc:
            ui.step(f"  {exc}")
            return None
        if video is not None:
            # r18.dev gives one listing; ClearJAV prefers DMM's digital one (ipzz00020 over ipzz020).
            about = dict(title=video.title, maker=video.maker, release_date=video.release_date, runtime=video.runtime)
            digital = dmm_api.digital_id(video.content_id)
            if digital and digital != video.content_id:
                return Suggestion(dvd_id, digital, [video.content_id], "r18.dev and DMM", where, **about)
            return Suggestion(dvd_id, video.content_id, [], "r18.dev", where, **about)
        # DMM can't search by DVD id, but a DVD listing is often named like it (gvh882): taken only when DMM's
        # product has exactly this DVD id.
        digital = dmm_api.digital_id(_bare(dvd_id).lower())
        product = dmm_api.lookup(digital) if digital else None
        if product is not None and _bare(product.dvd_id) == _bare(dvd_id):
            return Suggestion(dvd_id, product.content_id, [], "DMM", where, title=product.title, maker=product.maker,
                              release_date=product.release_date, runtime=product.runtime)
        ui.step(f"  {dvd_id}: not found")
    return None


def other_content_id(cid: str, video: r18.Video) -> Suggestion:
    """r18.dev's answer for `cid` when it is another video's content id (it matched the DVD id, or another padding),
    as a suggestion the member confirms."""
    dvd_id = video.dvd_id or dvd_id_in_name(cid) or cid.upper()
    return Suggestion(dvd_id, video.content_id, [], "r18.dev", f"content id {cid}", title=video.title, maker=video.maker,
                      release_date=video.release_date, runtime=video.runtime)


PART_PROBLEM = "a part of a video: put all its parts (and nothing else) in a folder and upload the folder"


def mixed_parts_problem(ids: list[str]) -> str:
    return f"this folder mixes parts of different videos ({', '.join(ids)}): give each video its own folder"


def _and(words) -> str:
    """[1, 3] -> "1 and 3", [1, 3, 5] -> "1, 3 and 5"."""
    words = [str(w) for w in words]
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} and {words[-1]}"


def part_number(info: media.Media) -> int:
    """The part DMM wrote into the file, else the one in its name (abc00123-pt2), else 0."""
    return info.part or content_id_from_name(info.path)[1] or 0


def numbering_problem(infos: list[media.Media]) -> str | None:
    """A part twice or one missing ("part 2 is missing (the folder has parts 1 and 3)"); None when the parts are
    1, 2, 3 ... exactly."""
    numbers: dict[int, list[str]] = {}
    for info in infos:
        numbers.setdefault(part_number(info), []).append(info.path.name)
    if 0 in numbers:
        return f"can't tell which part {numbers[0][0]} is: name the parts abc00123-pt1.mkv, -pt2 ..."
    for number, names in sorted(numbers.items()):
        if len(names) > 1:
            count = {2: "two", 3: "three"}.get(len(names), str(len(names)))
            return f"{count} files are part {number} ({_and(names)}): keep only the right one"
    have = sorted(numbers)
    missing = [n for n in range(1, have[-1] + 1) if n not in numbers]
    if not missing:
        return None
    which = f"part {missing[0]} is" if len(missing) == 1 else f"parts {_and(missing)} are"
    return f"{which} missing (the folder has parts {_and(have)})"


def parts_mismatch(infos: list[media.Media], kinds: list[str]) -> str | None:
    """The parts of one video are alike: the same kind (WEB-DL or Remux), size, video codec and frame rate. Says
    which part is the odd one, e.g. "the parts don't match: part 2 is 1920×1080, part 1 is 3840×2160"."""
    numbered = sorted(zip(infos, kinds), key=lambda pair: part_number(pair[0]))

    def rate(info: media.Media):
        # 29.97 and 30000/1001 are the same rate written differently (MediaInfo rounds to 3 decimals).
        return (round(info.frame_rate, 2), f"{round(info.frame_rate, 3):g} fps") if info.frame_rate else None

    looks = (
        lambda info, kind: (kind, "a Remux" if kind == media.REMUX else "a WEB-DL"),
        lambda info, kind: ((info.width, info.height), f"{info.width}×{info.height}"),
        lambda info, kind: (info.video_codec, f"{info.video_codec} video"),
        lambda info, kind: rate(info),
    )
    for look in looks:
        seen = [(part_number(info), *found) for info, kind in numbered if (found := look(info, kind)) is not None]
        counts = collections.Counter(key for _, key, _ in seen)
        if len(counts) < 2:
            continue
        (common, most), (_, second) = counts.most_common(2)
        if most > second and most > 1:  # most parts agree: name the odd ones
            odd = [f"part {number} is {words}" for number, key, words in seen if key != common]
            usual = next(words for _, key, words in seen if key == common)
            return f"the parts don't match: {_and(odd)} (the others: {usual})"
        first = seen[0]
        number, _, words = next(entry for entry in seen if entry[1] != first[1])
        return f"the parts don't match: part {number} is {words}, part {first[0]} is {first[2]}"
    return None


def _skipped_folder(path: Path) -> bool:
    """Hidden, system and linked folders (junctions, symlinks: they can loop) and the tool's own output."""
    if path.name.startswith((".", "$")) or path.name == "clearjav-uploads":
        return True
    try:
        if path.is_symlink():
            return True
        attributes = getattr(os.stat(path, follow_symlinks=False), "st_file_attributes", 0)
        return bool(attributes & (0x2 | 0x4 | 0x400))  # Windows: hidden, system, reparse point (junction)
    except OSError:
        return True


def file_problem(path: Path) -> str:
    if path.suffix.lower() == ".iso":
        return "ISO disc images are uploaded on the website, after staff approve them (Upload Rules)"
    return "" if path.suffix.lower() in media.VIDEO_EXTENSIONS else "not a .mkv, .mp4 or .wmv video"


def skipped_note(skipped: list[Path], shown: int = 5) -> str:
    """What the member is told, e.g. "1 file skipped: mkon00148.avi (only .mkv, .mp4 and .wmv are uploaded)"."""
    names = [p.name for p in skipped[:shown]]
    more = f" and {len(skipped) - shown} more" if len(skipped) > shown else ""
    return (f"{len(skipped)} file{'' if len(skipped) == 1 else 's'} skipped: {', '.join(names)}{more} "
            "(only .mkv, .mp4 and .wmv are uploaded)")


def collect(paths: list[Path], recursive: bool = False, skipped: list[Path] | None = None) -> list[Item]:
    """What to upload: each video file, or a folder that holds the parts of one video. Other videos found in the
    folders (.avi, .mov, ...) go into `skipped`, so the member can be told."""
    items: list[Item] = []

    def from_folder(folder: Path) -> None:
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            items.append(Item(folder, [], f"can't open this folder ({exc.strerror or exc})"))
            return
        files = [p for p in entries if p.suffix.lower() in media.VIDEO_EXTENSIONS and not p.name.startswith(".") and p.is_file()]
        if skipped is not None:
            skipped.extend(p for p in entries if p.suffix.lower() in media.OTHER_VIDEO_EXTENSIONS
                           and not p.name.startswith(".") and p.is_file())
        for iso in (p for p in entries if p.suffix.lower() == ".iso" and p.is_file()):
            items.append(Item(iso, [iso], file_problem(iso), final=True))  # shown with the reason, not left out silently
        ids = {content_id_from_name(f)[0] for f in files}
        parts = [f for f in files if content_id_from_name(f)[1]]
        part_ids = list(dict.fromkeys(content_id_from_name(f)[0] for f in parts))
        if len(files) > 1 and len(ids) == 1 and None not in ids and len(parts) == len(files):
            # The parts of one video: one torrent named after the folder, and the folder is renamed with the parts,
            # so it must hold nothing else (a Downloads folder with other things in it is never renamed).
            others = [p.name for p in entries if p not in files and not p.name.startswith(".") and p.name != "clearjav-uploads"
                      and not (p.is_file() and p.suffix.lower() in PART_FOLDER_EXTRAS)]
            if folder.parent == folder:
                items.append(Item(folder, files, "the parts are at the top of a drive: put them in a folder of their own",
                                  final=True))
            elif others:  # not final: once the other things are out, "Check again" reads the folder again
                items.append(Item(folder, files, f"the folder of the parts also holds {', '.join(others[:3])}"
                                  f"{' ...' if len(others) > 3 else ''}: put the parts (and nothing else) in a folder of their own"))
            else:
                items.append(Item(folder, files))
        else:
            # Parts of several videos together (kil00032-pt1 next to brg00011-pt2): said plainly on each part.
            part_problem = mixed_parts_problem(part_ids) if len(part_ids) > 1 else PART_PROBLEM
            for f in files:
                part = bool(content_id_from_name(f)[1])
                items.append(Item(f, [f], part_problem if part else "", final=part))
        if recursive:
            for sub in entries:
                if sub.is_dir() and not _skipped_folder(sub):
                    from_folder(sub)

    for path in paths:
        # Absolute from here on: "." in a folder of parts is that folder (not the top of a drive), and qBittorrent gets
        # a real folder to seed from. Only made absolute, not resolved: a mapped network drive keeps its letter.
        path = Path(os.path.abspath(path.expanduser()))
        if path.is_file():
            # The file's name decides these (an ISO, another kind of video, a part on its own): final.
            problem = file_problem(path) or (PART_PROBLEM if content_id_from_name(path)[1] else "")
            items.append(Item(path, [path], problem, final=bool(problem)))
        elif path.is_dir():
            from_folder(path)
        else:
            items.append(Item(path, [], "not found"))
    return items


def reread(item: Item) -> Item:
    """The upload read again from its place, as "Check again" does for a video refused before it could be checked:
    the member may have fixed it (taken another file out of the folder of the parts, added a missing part, connected
    the drive). The old one when that place no longer holds one upload."""
    again = [i for i in collect([item.target]) if i.target == item.target]
    return again[0] if len(again) == 1 else item


def rename_plan(item: Item, infos: list[media.Media], base: str) -> list[tuple[Path, Path]]:
    """(old, new) names: "<base>.mkv", or for parts "<base>-pt1.mkv" ... in a folder "<base>".
    Empty when the names are already right."""
    plan: list[tuple[Path, Path]] = []
    if len(item.files) == 1 and item.target.is_file():
        source = item.files[0]
        target = source.with_name(base + source.suffix.lower())
        if source.name != target.name:
            plan.append((source, target))
        return plan
    numbered = sorted(zip(infos, item.files), key=lambda pair: part_number(pair[0]))
    for number, (_, source) in enumerate(numbered, 1):
        target = source.with_name(f"{base}-pt{number}{source.suffix.lower()}")
        if source.name != target.name:
            plan.append((source, target))
    folder_target = item.target.with_name(base)
    if item.target.name != folder_target.name:
        plan.append((item.target, folder_target))
    return plan


def apply_renames(plan: list[tuple[Path, Path]], ui: "Ui") -> list[tuple[Path, Path]]:
    """Rename as planned and return what was renamed (for undo_renames); when one fails, the others are put back."""
    for old, new in plan:
        if new.exists() and not (old.exists() and old.samefile(new)):  # samefile: only the letter case changes
            raise Skip(f"can't rename {old.name}: {new.name} already exists")
    done = []
    failed = None
    try:
        for failed in plan:
            old, new = failed
            old.rename(new)
            done.append((old, new))
            ui.step(f"renamed {old.name} -> {new.name}")
    except OSError as exc:
        stuck = undo_renames(done, ui)
        message = f"can't rename {failed[0].name} ({exc.strerror or exc}); is it open in another program?"
        if stuck:
            message += f" These could not be renamed back: {', '.join(stuck)}"
        raise Skip(message) from exc
    return done


def undo_renames(done: list[tuple[Path, Path]], ui: "Ui") -> list[str]:
    """Give back the old names of what apply_renames renamed (newest first: a folder before its parts), each only while
    the new name is still there and the old one is free. Returns the ones that couldn't be put back."""
    stuck = []
    for old, new in reversed(done):
        try:
            if new.exists() and (not old.exists() or old.samefile(new)):  # samefile: only the letter case changed
                new.rename(old)
                ui.step(f"renamed back {new.name} -> {old.name}")
                continue
        except OSError:
            pass
        stuck.append(f"{new.name} (was {old.name})")
    return stuck


def renamed_item(item: Item, infos: list[media.Media], plan: list[tuple[Path, Path]]) -> tuple[Item, list[media.Media]]:
    mapping = dict(plan)
    target = mapping.get(item.target, item.target)
    files = []
    for path, info in zip(item.files, infos):
        new = mapping.get(path, path)
        if item.target not in item.files and target != item.target:  # a folder of parts, renamed too
            new = target / new.name
        info.path = new
        files.append(new)
    return Item(target, files, item.problem), infos


MEDIAINFO_SEPARATOR = "\n\n-------------------------\n\n"  # as the site's own uploads write it


def parts_in_order(item: Item, infos: list[media.Media]) -> tuple[Item, list[media.Media]]:
    """The files and their MediaInfo readings sorted by part (pt2 before pt10), as the renames number them."""
    pairs = sorted(zip(infos, item.files), key=lambda pair: part_number(pair[0]))
    return Item(item.target, [path for _, path in pairs], item.problem), [info for info, _ in pairs]


def combined_mediainfo(infos: list[media.Media]) -> str:
    """The Upload Rules want the MediaInfo of every video in the torrent: the parts one after another, in the order
    given (parts_in_order: once renamed to the DVD id or title, the names may not tell the part)."""
    return MEDIAINFO_SEPARATOR.join(info.text.strip() for info in infos) + "\n"


def describe_existing(found: list[dict]) -> list[str]:
    lines = []
    for t in found:
        lines.append(f"{t.get('name', '?')}  [{t.get('type', '?')}, {t.get('resolution', '?')}, "
                     f"{t.get('seeders', 0)} seeder(s)]  {t.get('details_link', '')}")
    return lines


STAFF_OPTIONS = ("internal", "freeleech", "double_upload", "sticky", "refundable")


def permission_problem(flags: dict | None, opts: Options) -> str | None:
    """Why the account can't use these options (the site would silently ignore them), from /api/user's
    "upload" answer; None when fine or when the site doesn't say."""
    if flags is None:
        return None
    if not flags.get("allowed", True):
        return "Your account can't upload right now (see your profile)."
    wanted = [name for name in STAFF_OPTIONS if getattr(opts, name)]
    if wanted and not flags.get("internal_options"):
        return f"Only staff and internal groups can use: {', '.join(w.replace('_', ' ') for w in wanted)}."
    if opts.featured and not flags.get("featured"):
        return "Only staff and internal groups can feature an upload."
    return None


# Said instead of the site's "Torrent uploaded successfully.", which it also answers when the upload waits for staff.
WAITS_FOR_STAFF = "Uploaded: waits for staff approval"


def waits_for_staff(flags: dict | None, mod_queue: bool) -> bool:
    """Whether an upload goes to the moderation queue and waits for staff to approve it: the account's uploads don't
    skip it (/api/user's "upload" answer), or the member sends it there anyway. False when the site doesn't say."""
    if not flags or "skips_moderation" not in flags:
        return False
    return not flags["skips_moderation"] or mod_queue


# -- 1. prepare: read, look up, check (changes nothing) -----------------------------------------


def prepare(item: Item, client: Client | None, mediainfo_path: Path, ui: Ui, cid: str | None = None,
            infos: list[media.Media] | None = None) -> Plan:
    if item.problem:
        raise Skip(item.problem, final=item.final)
    warnings: list[str] = []

    if infos is None:
        ui.step("reading the video" + ("s" if len(item.files) > 1 else ""))
        infos = [media.read(f, mediainfo_path) for f in item.files]
    first = infos[0]
    folder_name = item.target.name if item.target not in item.files else ""
    kinds = []
    for info in infos:
        kind, problem = media.kind_of(info, folder_name)
        if problem:  # what the file is (an ISO, a rip, its codecs): final
            raise Skip(problem, final=True)
        kinds.append(kind)
    kind = kinds[0]  # the parts are all of one kind (checked below, with the content id known)

    # The content id: what DMM wrote into the file, else the file name, else what the member typed.
    from_name = False
    if cid is None:
        ids = {i.file_id for i in infos if i.file_id}
        if not ids:
            ids, from_name = {content_id_from_name(f)[0] for f in item.files} - {None}, True
        if len(ids) > 1:
            raise WrongId(f"the files belong to different videos ({', '.join(sorted(ids))})")
        if not ids:
            # "GVH-882.mp4": the content id of the DVD id is offered, never used by itself.
            suggestion = suggest_content_id(item, ui)
            raise NeedsContentId("can't tell the content id: " + (suggestion.text() if suggestion else
                                 "name the file after it (abc00123.mkv)"), suggestion, infos)
        cid = ids.pop()
    cid = cid.strip().lower()
    if not media.CID_RE.fullmatch(cid):
        raise NeedsContentId(f"{cid!r} is not a content id (small letters, digits and _ only, e.g. abc00123)", None, infos)

    # A part on its own, a Remux or H.264 video in parts: final (the way out is another upload: the folder of all the
    # parts, each file on its own, the joined file). A missing or odd part is not: the folder can be fixed.
    if len(item.files) == 1 and not first.part and content_id_from_name(first.path)[1]:
        raise Skip(PART_PROBLEM, final=True)
    if len(item.files) == 1 and first.part:
        raise Skip(f"DMM marked this file as part {first.part} of a video in several parts: put all its parts in a "
                   "folder (named abc00123-pt1.mkv, -pt2, ...) and upload the folder, or join H.264 parts first", final=True)
    if len(item.files) > 1:
        # One video cut in pieces: parts 1, 2, 3 ..., alike (a 1080p file next to 4K parts is another video).
        problem = numbering_problem(infos) or parts_mismatch(infos, kinds)
        if problem:
            raise Skip(problem)
        if kind == media.REMUX:
            raise Skip("a Remux in several files: upload Remuxes one file at a time", final=True)
        if any(i.video_codec != "VP9" for i in infos):
            raise Skip("several H.264 parts: join them into one file first (VideoMerger), only 4K VP9 parts are uploaded as parts",
                       final=True)

    source = media.remux_source(first, folder_name) if kind == media.REMUX else "DMM"
    plan = Plan(item, infos, cid, kind, source, warnings=warnings)

    ui.step(f"{cid}: looking it up on r18.dev")
    video = r18.lookup(cid)
    dmm = None
    if video is not None and video.content_id != cid:
        # r18.dev also finds videos by DVD id (abp123 -> 118abp00123), and the zeros tell listings apart (sone096 and
        # sone00096 can be different videos): another content id is only offered, its DVD id and date never used by
        # themselves. When DMM has this very id, r18.dev just doesn't know it.
        dmm = dmm_api.lookup(cid)
        if dmm is None or dmm.content_id != cid:
            other = other_content_id(cid, video)
            raise NeedsContentId(f"r18.dev and DMM don't know {cid}: {other.text()}", other, infos)
        video = None
    plan.known = video is not None
    # DMM's digital listings usually have no DVD id on r18.dev, and some videos no date: ask DMM's own API.
    plan.dmm = dmm or (dmm_api.lookup(cid) if video is None or not video.dvd_id or not video.release_date else None)
    if video is None and plan.dmm is None and from_name:
        # "ABP123.mkv": a DVD id rather than a content id; its content id is offered, never used by itself.
        suggestion = suggest_content_id(item, ui)
        if suggestion is not None and suggestion.cid != cid:
            raise NeedsContentId(f"r18.dev and DMM don't know {cid}: {suggestion.text()}", suggestion, infos)
    if video is None:
        plan.dvd_id = plan.dmm.dvd_id if plan.dmm else ""
        plan.release_date = plan.dmm.release_date if plan.dmm else ""
        plan.dvd_from = "DMM" if plan.dvd_id else ""
    else:
        plan.r18_id, plan.release_date, plan.cover = video.content_id, video.release_date, video.cover
        if video.dvd_id:
            plan.dvd_id, plan.dvd_from = video.dvd_id, "r18.dev"
        elif plan.dmm and plan.dmm.dvd_id:
            plan.dvd_id, plan.dvd_from = plan.dmm.dvd_id, "DMM"
        else:
            # Neither r18.dev nor DMM: work it out from the content id, to be confirmed.
            plan.guess = naming.guess_dvd_id(cid) or ""
            if plan.guess:
                plan.dvd_id, plan.dvd_from = plan.guess, "worked out"
        if not plan.release_date and plan.dmm and not plan.is_fc2():
            plan.release_date = plan.dmm.release_date  # DMM's delivery date: a few days off r18.dev's at most

    if client is not None:
        check_existing(plan, client, ui)
    return plan


def check_existing(plan: Plan, client: Client, ui: Ui) -> None:
    ui.step("checking what's already on ClearJAV")
    plan.existing = client.torrents_of(plan.cid)
    for line in describe_existing(plan.existing):
        ui.step(f"  already there: {line}")
    if not plan.known and not plan.confirmed:
        # The video is on ClearJAV although r18.dev doesn't know it: its torrents' titles give the DVD id and date.
        for found in plan.existing:
            match = re.match(r"^(\S+) (\d{4}-\d{2}-\d{2}) ", str(found.get("name") or ""))
            if match:
                plan.dvd_id = plan.dvd_id or match.group(1)
                plan.release_date = plan.release_date or match.group(2)
                plan.dvd_from = plan.dvd_from or "ClearJAV's torrent"
                break


# -- 2. questions, answered in the terminal ---------------------------------------------------


def resolve_in_terminal(plan: Plan, opts: Options, ui: Ui) -> None:
    """Ask the open questions in the terminal (or decide them without a member, as before the browser page)."""
    blocker = plan.blocker(opts)
    if blocker:
        raise Skip(blocker)
    if not plan.known:
        if not ui.interactive:
            raise WrongId("r18.dev doesn't know this content id (check it; ClearJAV may not be able to create the video)")
        if not ui.confirm(f"r18.dev doesn't know {plan.cid}. Upload it anyway (ClearJAV may still know it)?"):
            raise Skip("not uploaded (unknown on r18.dev)")
        dvd_id = ui.ask(f"DVD id [{plan.dvd_id or 'e.g. ABC-123'}]: ").strip().upper() or plan.dvd_id
        date = ui.ask(f"Release date [{plan.release_date or 'YYYY-MM-DD'}]: ").strip() or plan.release_date
        try:
            plan.answer(dvd_id, date)
        except ValueError:
            raise Skip("no valid DVD id / release date given") from None
        plan.warnings.append("r18.dev doesn't know this id: DVD id and date were confirmed by you")
    elif "dvd_id" in plan.questions(opts):
        if ui.interactive:
            typed = ui.ask(f"No DVD id for {plan.cid} on r18.dev or DMM. DVD id [{plan.guess or 'none'}]: ").strip().upper()
            if typed or plan.guess:
                try:
                    plan.answer(typed or plan.guess)
                except ValueError as exc:
                    raise Skip(str(exc)) from None
        elif plan.guess:
            plan.warnings.append(f"DVD id {plan.guess} was worked out from the content id (r18.dev and DMM have none): check the title")
            plan.confirmed = True
        if not plan.dvd_id:
            raise Skip("no DVD id for the title (r18.dev and DMM have none and none was given)")
    if plan.date_missing() and ui.interactive:
        typed = ui.ask(f"No release date for {plan.cid} on r18.dev or DMM. Release date [YYYY-MM-DD, empty: none]: ").strip()
        if typed:
            try:
                plan.answer_date(typed)
            except ValueError as exc:
                raise Skip(str(exc)) from None

    if plan.existing and not opts.dry_run:  # a test run only reports them (execute)
        count = len(plan.existing)
        if not opts.allow_duplicates and not (ui.interactive and ui.confirm(
                f"{plan.cid} already has {count} torrent(s) on ClearJAV. Upload yours anyway (e.g. better quality)?")):
            raise Skip(f"already on ClearJAV ({count} torrent(s)); --allow-duplicates to upload anyway")
        plan.upload_anyway = True


# -- 3. execute: files, upload, seeding --------------------------------------------------------


def execute(plan: Plan, client: Client | None, tool_paths: tools.Tools, opts: Options, ui: Ui) -> Result:
    open_ = plan.questions(opts)
    if open_:
        raise Skip(f"not answered yet: {', '.join(open_)}")
    blocker = plan.blocker(opts)
    if blocker:
        raise Skip(blocker)
    if client is None and not opts.dry_run:
        raise Skip("not signed in to ClearJAV (no API key): nothing was changed or uploaded")
    tag_problem = opts.tag_problem()
    if tag_problem:
        raise Skip(tag_problem)
    warnings = plan.notes()
    if plan.kind == media.REMUX and not plan.source_chosen:
        warnings.append(f"Remux source {plan.source} was worked out from the name or the size: check the title")
    title = plan.title(opts)
    # The parts in their order while the names still tell it (renamed to the DVD id or title they may not): contact
    # sheet N and the MediaInfo's part N are made from part N, also with 10 parts or more (pt1, pt10, pt2 by name).
    item, infos = parts_in_order(plan.item, plan.infos)
    before = (plan.item, plan.infos, [(info, info.path, info.text) for info in infos])

    # File names: the Upload Rules want the content id, the DVD id or the title.
    renames = rename_plan(item, infos, plan.base_name(opts))
    renamed: list[tuple[Path, Path]] = []
    if renames:
        if opts.dry_run:
            for old, new in renames:
                ui.step(f"would rename {old.name} -> {new.name}")
        else:
            renamed = apply_renames(renames, ui)
            item, infos = renamed_item(item, infos, renames)
            plan.item, plan.infos = item, infos
            for info in infos:
                info.text = re.sub(r"^(Complete name\s*:\s*).*$", lambda m, name=info.path.name: m.group(1) + name,
                                   info.text, count=1, flags=re.MULTILINE)
    out = opts.output_dir or item.target.parent / "clearjav-uploads"
    base = naming.safe_name(plan.cid)
    try:
        mediainfo = combined_mediainfo(infos)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{base}.mediainfo.txt").write_text(mediainfo, encoding="utf-8", newline="\n")

        sheets = []
        for number, info in enumerate(infos, 1):
            label = f" (part {number}/{len(infos)})" if len(infos) > 1 else ""
            ui.step(f"making the contact sheet{label}")
            sheet = out / (f"{base}-pt{number}_s.webp" if len(infos) > 1 else f"{base}_s.webp")
            contact_sheet.make(info.path, sheet, tool_paths, progress=ui.progress, fit=True, note=ui.step)
            ui.end_progress()
            sheets.append(sheet)

        ui.step("making the torrent (reading the whole video)")
        local_torrent = torrent.create(item.target, out / f"{base}.torrent", progress=ui.progress, only=item.files,
                                       announce=opts.announce)
        ui.end_progress()

        if opts.dry_run or client is None:
            if client is None:
                warnings.append("not checked against ClearJAV (no API key saved)")
            if plan.existing:
                would = "go ahead, as you chose" if plan.upload_anyway or opts.allow_duplicates else "ask whether to upload yours anyway"
                warnings.append(f"ClearJAV already has {len(plan.existing)} torrent(s) of this video: a real upload would {would}")
            return Result(item.target, True, f"ready (not uploaded): {title}", title, warnings=warnings)

        ui.step("uploading")
        link, message = send(client, upload_fields(plan, title, mediainfo, opts), local_torrent, sheets)
    except BaseException:
        # Not uploaded: the files get their names back, so that they can be checked and uploaded again as they were
        # (a name written in dvd_id or title mode can't be read as a content id or a part).
        put_back(plan, renamed, before, ui)
        raise
    page = torrent_page(client.site, link)
    if plan.existing:  # only now: the upload went through
        warnings.append(f"uploaded although {len(plan.existing)} torrent(s) were already there")

    # The upload went through: from here on nothing makes it "not uploaded". Without the .torrent to seed the member
    # is told to fetch it from the torrent's page.
    ui.step("downloading the torrent to seed")
    seed: Path | None = out / f"{naming.safe_name(title)}.torrent"
    try:
        data = save_seed_torrent(client, link, seed)
    except (ApiError, OSError) as exc:
        log.warning("%s: uploaded, but no torrent to seed: %s", item.target, exc)
        warnings.append(seed_torrent_problem(exc, page))
        seed, seed_warnings = None, []
    else:
        seed_warnings = start_seeding(seed, data, item.target.parent, opts, ui)
        warnings += seed_warnings
    try:
        local_torrent.unlink(missing_ok=True)  # the site's torrent replaces it (different info hash)
    except OSError as exc:
        log.warning("could not delete %s: %s", local_torrent, exc)

    if opts.clean_up:
        # The .torrent stays unless qBittorrent has it (with "save" and "watch" it is the member's copy to seed).
        given_to_qbittorrent = (seed is not None and opts.seed == "qbittorrent" and opts.qbittorrent is not None
                                and not seed_warnings)
        made = [out / f"{base}.mediainfo.txt", *sheets] + ([seed] if given_to_qbittorrent else [])
        clean_up(made, out if opts.output_dir is None else None, ui)
        if given_to_qbittorrent:
            seed = None
    if opts.waits_for_staff:
        return Result(item.target, True, WAITS_FOR_STAFF, title, page, seed, warnings, waits_for_staff=True)
    return Result(item.target, True, message or "uploaded", title, page, seed, warnings)


def upload_fields(plan: Plan, title: str, mediainfo: str, opts: Options) -> dict:
    fields = {
        "jav_id": plan.cid,
        "name": title,
        "description": f"R18: https://r18.dev/videos/vod/movies/detail/-/id={plan.cid}/",
        "mediainfo": mediainfo,
        "category_id": CATEGORY,
        "type_id": media.TYPE_IDS[plan.kind],
        "resolution_id": plan.first.resolution_id,
        "anonymous": int(opts.anonymous),
        "personal_release": int(opts.personal_release),
        "mod_queue_opt_in": int(opts.mod_queue),
    }
    if opts.internal:
        fields["internal"] = 1
    if opts.freeleech:
        fields["free"] = opts.freeleech
        if opts.freeleech_days:
            fields["fl_until"] = opts.freeleech_days
    if opts.double_upload:
        fields["doubleup"] = 1
        if opts.double_upload_days:
            fields["du_until"] = opts.double_upload_days
    for flag in ("sticky", "refundable", "featured"):
        if getattr(opts, flag):
            fields[flag] = 1
    if plan.dvd_id and plan.dvd_from != "r18.dev":
        fields["dvd_id"] = plan.dvd_id  # the site keeps it for a video r18.dev knows without one
    if not plan.known:
        fields["release_date"] = plan.release_date
    return fields


def send(client: Client, fields: dict, local_torrent: Path, sheets: list[Path]) -> tuple[str, str]:
    """The upload: (link of the torrent to seed, the site's message); the site's refusals said in the tool's words."""
    try:
        return client.upload(fields, local_torrent, sheets)
    except UnconfirmedUpload:
        raise  # the first try of this upload (its answer was lost), not someone else's torrent
    except ApiError as exc:
        if NAME_TAKEN in str(exc).lower():  # a torrent the duplicate check can't see (pending or deleted)
            raise Skip(same_title_problem()) from None
        if IMAGE_SIZE.search(str(exc)):  # the only images this tool sends are the contact sheets
            largest = max((s.stat().st_size for s in sheets if s.exists()), default=0)
            raise Skip(f"ClearJAV refused a contact sheet over {contact_sheet.LIMIT_TEXT} (the largest made here is "
                       f"{contact_sheet.megabytes(largest)}): nothing was uploaded") from None
        raise


def save_seed_torrent(client: Client, link: str, seed: Path) -> bytes:
    """Fetch the site's .torrent (with the member's passkey) and save it as `seed`: ApiError when it can't be
    fetched, OSError when it can't be saved (nothing half-written is left)."""
    data = client.download_torrent(link)
    try:
        seed.write_bytes(data)
    except OSError:
        try:
            seed.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return data


def seed_torrent_problem(exc: Exception, page: str | None) -> str:
    """The warning for an upload whose .torrent to seed is missing: where to get it."""
    if isinstance(exc, OSError):
        doing, reason = "saved", exc.strerror or str(exc)
    else:
        doing, reason = "fetched", str(exc)
    where = page or "your uploads on ClearJAV"
    return f"uploaded, but the .torrent to seed couldn't be {doing} ({reason}): download it from {where}"


def put_back(plan: Plan, renamed: list[tuple[Path, Path]], before: tuple, ui: Ui) -> None:
    """After a failed upload: the old names of what this run renamed, and the plan as it was before the renames
    (`before`: item, infos, and each info's path and MediaInfo text). When a name can't be put back, the plan keeps
    the new names and the member is told which files to rename by hand."""
    if not renamed:
        return
    stuck = undo_renames(renamed, ui)
    if stuck:
        log.warning("could not rename back: %s", ", ".join(stuck))
        ui.step(f"could not rename back (rename them yourself): {', '.join(stuck)}")
        return
    plan.item, plan.infos, readings = before
    for info, path, text in readings:
        info.path, info.text = path, text


def clean_up(files: list[Path], own_folder: Path | None, ui: Ui) -> None:
    """Delete what the tool made for an upload, and its own clearjav-uploads folder once empty (never a folder
    the member chose)."""
    removed = 0
    for path in files:
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not delete %s: %s", path, exc)
    if own_folder is not None:
        try:
            own_folder.rmdir()  # only when empty
        except OSError:
            pass
    ui.step(f"deleted {removed} file(s) the tool made (setting: delete after upload)")


def start_seeding(seed: Path, data: bytes, data_folder: Path, opts: Options, ui: Ui) -> list[str]:
    """Hand the site's .torrent to the torrent client as set; warnings when that didn't work."""
    if opts.seed == "watch" and opts.watch_dir:
        try:
            opts.watch_dir.mkdir(parents=True, exist_ok=True)
            target = opts.watch_dir / seed.name
            if not (target.exists() and target.samefile(seed)):  # the output folder may be the watch folder
                shutil.copyfile(seed, target)
            ui.step(f"put {seed.name} in the watch folder")
        except OSError as exc:
            return [f"not put in the watch folder ({exc.strerror or exc}); add {seed.name} yourself"]
    elif opts.seed == "qbittorrent" and opts.qbittorrent:
        url, user, password, category = opts.qbittorrent
        try:
            qbittorrent.add(url, user, password, data, data_folder, category)
            ui.step("added to qBittorrent (it checks the files, then seeds)")
        except (qbittorrent.QbitError, OSError) as exc:
            return [f"not added to qBittorrent: {exc}; add {seed.name} yourself"]
    return []


# -- all three, for the terminal ---------------------------------------------------------------


def process(item: Item, client: Client | None, tool_paths: tools.Tools, mediainfo_path: Path, opts: Options, ui: Ui) -> Result:
    try:
        try:
            plan = prepare(item, client, mediainfo_path, ui)
        except NeedsContentId as exc:
            if not ui.interactive:  # a suggested content id is never used without the member
                raise
            suggested = exc.suggestion.cid if exc.suggestion else ""
            # The suggested video's length next to the file's, to tell whether it is the member's video.
            mine = sum(i.duration for i in exc.infos or [])
            length = f" (your file: {round(mine / 60)} min)" if exc.suggestion and exc.suggestion.runtime and mine else ""
            typed = ui.ask(f"{exc}{length}. Content id{f' [{suggested}]' if suggested else ''}: ").strip() or suggested
            if not typed:
                raise
            plan = prepare(item, client, mediainfo_path, ui, cid=typed, infos=exc.infos)
        if opts.remux_source and plan.kind == media.REMUX:
            plan.answer(source=opts.remux_source)
        resolve_in_terminal(plan, opts, ui)
        return execute(plan, client, tool_paths, opts, ui)
    except Skip as exc:
        return Result(item.target, False, str(exc))
    except ERRORS as exc:
        log.warning("%s: %s", item.target, exc)
        return Result(item.target, False, str(exc))
