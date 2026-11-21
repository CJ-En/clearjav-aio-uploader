import io
import re
import shutil
import socket
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from clearjav_aio import api, cli, config, contact_sheet, dmm_api, pipeline, r18
from clearjav_aio.torrent import bdecode

from .helpers import MEDIAINFO, SITE_TORRENT, TOOLS, FakeSite, make_clip

KNOWN = {"ipzz00020": r18.Video("ipzz00020", "", "2023-04-07", "t"), "abc00123": r18.Video("abc00123", "ABC-123", "2024-01-02", "t"),
         "xyz00001": r18.Video("xyz00001", "", "2025-01-01", "t"), "gvh00882": r18.Video("gvh00882", "GVH-882", "2026-10-03", "t"),
         "118abp00123": r18.Video("118abp00123", "ABP-123", "2010-01-01", "t")}
DMM = {"ipzz00020": dmm_api.Product("ipzz00020", "IPZZ-020", "2023-04-07"), "zzz00001": dmm_api.Product("zzz00001", "ZZZ-001", "2025-05-05")}
# r18.dev's answer for a DVD id (one listing, as checked live) and DMM's digital listing of an id.
BY_DVD_ID = {"GVH-882": r18.Video("gvh00882", "", "2026-10-03", "Sample title", "Sample Maker", 121), "IPZZ-020": r18.Video("ipzz020", "", "2023-04-11", "t"),
             "ABP-123": r18.Video("118abp123", "", "2010-01-01", "t")}
DIGITAL = {"gvh00882": "gvh00882", "ipzz020": "ipzz00020", "118abp123": "118abp00123"}


class Quiet(pipeline.Ui):
    def __init__(self, answers=()):
        self.interactive = bool(answers)
        self.answers = list(answers)
        self.steps = []
        self.asked = []

    def step(self, text):
        self.steps.append(text)

    def confirm(self, question):
        self.asked.append(question)
        return self.answers.pop(0) == "y"

    def ask(self, question):
        self.asked.append(question)
        return self.answers.pop(0)


@unittest.skipUnless(TOOLS and MEDIAINFO, "ffmpeg/mediainfo not installed")
class FlowTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="aio test "))
        patcher = mock.patch.object(r18, "lookup", side_effect=lambda cid, *a, **k: KNOWN.get(cid))
        patcher.start()
        self.addCleanup(patcher.stop)
        for patcher in (mock.patch.object(dmm_api, "lookup", side_effect=lambda cid: DMM.get(cid)),
                        mock.patch.object(r18, "lookup_dvd_id", side_effect=lambda dvd_id, *a, **k: BY_DVD_ID.get(dvd_id)),
                        mock.patch.object(dmm_api, "digital_id", side_effect=lambda cid: DIGITAL.get(cid))):
            patcher.start()
            self.addCleanup(patcher.stop)
        no_sleep = mock.patch.object(api.time, "sleep")
        no_sleep.start()
        self.addCleanup(no_sleep.stop)
        api.MIN_INTERVAL = 0

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def site(self, **kwargs):
        site = FakeSite(**kwargs)
        self.addCleanup(site.close)
        return site, api.Client(site.url, "good-key")

    def run_item(self, path, client, ui=None, **options):
        items = pipeline.collect([path])
        self.assertEqual(len(items), 1)
        return pipeline.process(items[0], client, TOOLS, MEDIAINFO, pipeline.Options(**options), ui or Quiet())

    def test_upload_sends_everything_and_saves_the_torrent_to_seed(self):
        video = make_clip(self.dir / "abc00123.mkv", size="1920x1080")
        site, client = self.site()
        result = self.run_item(video, client, anonymous=True)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.title, "ABC-123 2024-01-02 1080p DMM WEB-DL H.264 AAC")
        self.assertEqual(result.page, f"{site.url}/torrents/77")
        self.assertEqual(result.seed_torrent.read_bytes(), SITE_TORRENT)
        body = site.uploads[0]["body"].decode("latin1")
        for field, value in (("jav_id", "abc00123"), ("type_id", "4"), ("resolution_id", "3"), ("anonymous", "1"), ("category_id", "1")):
            self.assertRegex(body, rf'name="{field}"\r\n\r\n{value}\r\n')
        self.assertIn('name="thumb_sheets[]"; filename="abc00123_s.webp"', body)
        self.assertIn("Complete name                            : abc00123.mkv", body)
        self.assertNotIn(str(self.dir), body)  # no folders of the member's computer
        self.assertNotIn('name="dvd_id"', body)  # r18.dev knows it: the site takes its own
        outputs = sorted(p.name for p in (self.dir / "clearjav-uploads").iterdir())
        self.assertEqual(outputs, ["ABC-123 2024-01-02 1080p DMM WEB-DL H.264 AAC.torrent", "abc00123.mediainfo.txt", "abc00123_s.webp"])

    def test_renamed_to_the_content_id(self):
        video = make_clip(self.dir / "My download.mkv", size="1280x720", comment={"file_id": "abc00123"})
        site, client = self.site()
        result = self.run_item(video, client)
        self.assertTrue(result.ok, result.message)
        self.assertFalse(video.exists())
        self.assertTrue((self.dir / "abc00123.mkv").exists())
        body = site.uploads[0]["body"].decode("latin1")
        self.assertIn("Complete name                            : abc00123.mkv", body)

    def test_rename_options_and_dry_run(self):
        video = make_clip(self.dir / "abc00123.mkv", comment={"file_id": "abc00123"})
        ui = Quiet()
        result = self.run_item(video, None, ui, dry_run=True, rename="dvd_id")
        self.assertTrue(result.ok, result.message)
        self.assertIn("would rename abc00123.mkv -> ABC-123.mkv", ui.steps)
        self.assertTrue(video.exists())  # a dry run changes nothing
        site, client = self.site()
        result = self.run_item(video, client, rename="title")
        self.assertTrue(result.ok, result.message)
        self.assertTrue((self.dir / "ABC-123 2024-01-02 SD DMM WEB-DL H.264 AAC.mkv").exists())

    def test_internal_release_and_staff_fields(self):
        make_clip(self.dir / "abc00123.mkv", size="1920x1080")
        site, client = self.site()
        result = self.run_item(self.dir / "abc00123.mkv", client, personal_release=True, tag="Me", internal=True,
                               freeleech=50, freeleech_days=7, double_upload=True, sticky=True)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.title.endswith("H.264 AAC-ClearJAV"))  # internal always wins over a personal tag
        body = site.uploads[0]["body"].decode("latin1")
        for field, value in (("internal", "1"), ("free", "50"), ("fl_until", "7"), ("doubleup", "1"), ("sticky", "1")):
            self.assertRegex(body, rf'name="{field}"\r\n\r\n{value}\r\n')
        self.assertNotIn('name="du_until"', body)

    def test_personal_release_tag_is_optional(self):
        make_clip(self.dir / "abc00123.mkv", size="1920x1080")
        result = self.run_item(self.dir / "abc00123.mkv", None, dry_run=True, personal_release=True)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.title.endswith("H.264 AAC"))
        result = self.run_item(self.dir / "abc00123.mkv", None, dry_run=True, personal_release=True, tag="Me")
        self.assertTrue(result.title.endswith("H.264 AAC-Me"))

    def test_reencoded_disc_audio_is_refused(self):
        make_clip(self.dir / "abc00123.mkv", audio="pcm_s16le")  # x264 video: an encode, not a Remux
        result = self.run_item(self.dir / "abc00123.mkv", None, dry_run=True)
        self.assertFalse(result.ok)
        self.assertIn("re-encoded", result.message)

    def test_dvd_remux(self):
        make_clip(self.dir / "abc00123.mkv", seconds=4, size="720x480", codec="mpeg2video", audio="ac3", rate=30,
                  extra=["-flags", "+ildct+ilme", "-top", "1"])
        site, client = self.site()
        result = self.run_item(self.dir / "abc00123.mkv", client)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.title, "ABC-123 2024-01-02 480i DVD REMUX MPEG-2 AC-3")
        self.assertIn("Remux source DVD was worked out", " ".join(result.warnings))
        body = site.uploads[0]["body"].decode("latin1")
        for field, value in (("type_id", "2"), ("resolution_id", "8")):
            self.assertRegex(body, rf'name="{field}"\r\n\r\n{value}\r\n')
        result = self.run_item(self.dir / "abc00123.mkv", None, dry_run=True, remux_source="VHS")
        self.assertEqual(result.title, "ABC-123 2024-01-02 480i VHS REMUX MPEG-2 AC-3")
        self.assertNotIn("worked out", " ".join(result.warnings))

    def test_announce_url_and_watch_folder(self):
        make_clip(self.dir / "abc00123.mkv", seconds=4)
        site, client = self.site()
        mine = "https://clearjav.com/announce/0123456789abcdef0123456789abcdef"
        watch = self.dir / "watch"
        result = self.run_item(self.dir / "abc00123.mkv", client, announce=mine, seed="watch", watch_dir=watch)
        self.assertTrue(result.ok, result.message)
        body = site.uploads[0]["body"]
        start = body.index(b"d8:announce")
        self.assertEqual(bdecode(body[start:body.index(b"\r\n--", start)])[b"announce"], mine.encode())
        self.assertEqual([p.read_bytes() for p in watch.iterdir()], [SITE_TORRENT])

    def test_clean_up_after_upload(self):
        make_clip(self.dir / "abc00123.mkv", seconds=4)
        site, client = self.site()
        result = self.run_item(self.dir / "abc00123.mkv", client, clean_up=True)  # only saved: the .torrent stays
        self.assertTrue(result.ok, result.message)
        self.assertEqual([p.name for p in (self.dir / "clearjav-uploads").iterdir()], [result.seed_torrent.name])
        self.run_item(self.dir / "abc00123.mkv", None, dry_run=True, clean_up=True)  # a test run keeps everything
        self.assertEqual(len(list((self.dir / "clearjav-uploads").iterdir())), 4)
        shutil.rmtree(self.dir / "clearjav-uploads")
        with mock.patch("clearjav_aio.qbittorrent.add") as add:  # qBittorrent took it: nothing is left
            result = self.run_item(self.dir / "abc00123.mkv", client, clean_up=True, seed="qbittorrent",
                                   qbittorrent=("http://127.0.0.1:1", "", "", ""))
        self.assertTrue(result.ok, result.message)
        add.assert_called_once()
        self.assertIsNone(result.seed_torrent)
        self.assertFalse((self.dir / "clearjav-uploads").exists())
        self.assertTrue((self.dir / "abc00123.mkv").exists())

    def test_content_id_typed_by_the_member(self):
        make_clip(self.dir / "My video.mkv", seconds=4)
        result = self.run_item(self.dir / "My video.mkv", None, dry_run=True)
        self.assertIn("can't tell the content id", result.message)
        result = self.run_item(self.dir / "My video.mkv", None, Quiet(answers=["ABC00123"]), dry_run=True)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.title.startswith("ABC-123 2024-01-02"))

    def test_lost_answer_is_sent_again(self):
        make_clip(self.dir / "abc00123.mkv")
        site, client = self.site(upload_status=[503, 502])
        result = self.run_item(self.dir / "abc00123.mkv", client)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(site.requests.count("/api/torrents/upload"), 3)

    def test_site_refusal_is_shown(self):
        make_clip(self.dir / "abc00123.mkv")
        _, client = self.site(refuse={"description": ["The description field is required."]})
        result = self.run_item(self.dir / "abc00123.mkv", client)
        self.assertFalse(result.ok)
        self.assertIn("The description field is required.", result.message)

    def test_title_taken_on_the_site_is_explained(self):
        # A torrent the duplicate check can't see (waiting for moderation, deleted) has the title already.
        make_clip(self.dir / "abc00123.mkv")
        _, client = self.site(refuse={"name": ["The name has already been taken."]})
        result = self.run_item(self.dir / "abc00123.mkv", client)
        self.assertFalse(result.ok)
        self.assertIn("ClearJAV already has a torrent with exactly this title (it may be waiting for moderation", result.message)
        self.assertIn("Only a different version can go up", result.message)

    def test_exact_title_on_clearjav_is_refused_before_uploading(self):
        make_clip(self.dir / "abc00123.mkv", seconds=4)
        existing = [{"name": "abc-123 2024-01-02 SD DMM WEB-DL H.264 AAC ", "type": "WEB-DL", "resolution": "SD", "seeders": 2,
                     "details_link": "https://clearjav.example/torrents/5"}]
        site, client = self.site(existing=existing)
        for options in ({}, {"dry_run": True}, {"allow_duplicates": True}):  # a test run says the same; the site refuses it anyway
            ui = Quiet(answers=["y"])
            result = self.run_item(self.dir / "abc00123.mkv", client, ui, **options)
            self.assertFalse(result.ok, options)
            self.assertIn("exactly this title (https://clearjav.example/torrents/5): the site refuses a second one", result.message)
            self.assertEqual(ui.asked, [])  # no "Upload anyway?"
        self.assertEqual(site.uploads, [])
        self.assertFalse((self.dir / "clearjav-uploads").exists())
        # A personal release with a tag has another title: then it is an ordinary duplicate question.
        ui = Quiet(answers=["y"])
        result = self.run_item(self.dir / "abc00123.mkv", client, ui, personal_release=True, tag="Me")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.title, "ABC-123 2024-01-02 SD DMM WEB-DL H.264 AAC-Me")
        self.assertEqual(len(ui.asked), 1)

    def test_duplicates_warn_and_skip_unless_confirmed(self):
        make_clip(self.dir / "abc00123.mkv")
        existing = [{"name": "ABC-123 2024-01-02 2160p DMM WEB-DL VP9 AAC", "type": "WEB-DL", "resolution": "2160p", "seeders": 4}]
        site, client = self.site(existing=existing)
        result = self.run_item(self.dir / "abc00123.mkv", client)
        self.assertFalse(result.ok)
        self.assertIn("already on ClearJAV", result.message)
        self.assertEqual(site.uploads, [])
        ui = Quiet(answers=["y"])
        result = self.run_item(self.dir / "abc00123.mkv", client, ui)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(any("2160p" in s for s in ui.steps))
        self.assertIn("uploaded although 1 torrent(s) were already there", result.warnings)
        uploads = len(site.uploads)
        result = self.run_item(self.dir / "abc00123.mkv", client, dry_run=True)  # a dry run only reports it
        self.assertTrue(result.ok, result.message)
        self.assertIn("ClearJAV already has 1 torrent(s) of this video: a real upload would ask whether to upload yours anyway",
                      result.warnings)
        self.assertNotIn("uploaded although", " ".join(result.warnings))
        self.assertEqual(len(site.uploads), uploads)

    def test_upload_anyway_that_fails_says_nothing_was_uploaded(self):
        make_clip(self.dir / "abc00123.mkv", seconds=4)
        existing = [{"name": "ABC-123 2024-01-02 2160p DMM WEB-DL VP9 AAC", "type": "WEB-DL", "resolution": "2160p", "seeders": 4}]
        _, client = self.site(existing=existing, refuse={"mediainfo": ["The mediainfo field is required."]})
        items = pipeline.collect([self.dir / "abc00123.mkv"])
        plan = pipeline.prepare(items[0], client, MEDIAINFO, Quiet())
        pipeline.resolve_in_terminal(plan, pipeline.Options(), Quiet(answers=["y"]))
        self.assertTrue(plan.upload_anyway)
        with self.assertRaises(api.ApiError):
            pipeline.execute(plan, client, TOOLS, pipeline.Options(), Quiet())
        self.assertNotIn("uploaded although", " ".join(plan.warnings))  # the page shows these on the card

    def test_dvd_id_as_the_name_suggests_the_content_id(self):
        video = make_clip(self.dir / "GVH-882.mp4", seconds=4)
        # Without a member it is never used: the message says what was found.
        result = self.run_item(video, None, dry_run=True)
        self.assertFalse(result.ok)
        self.assertIn("the file name looks like the DVD id GVH-882, and r18.dev gives the content id gvh00882", result.message)
        # In the terminal it is the default answer, shown with what r18.dev says about that video and the file's length.
        ui = Quiet(answers=[""])
        result = self.run_item(video, None, ui, dry_run=True)
        self.assertTrue(result.ok, result.message)
        self.assertIn('which is "Sample title" (Sample Maker, released 2026-10-03, 121 min): check it is your video (your file: 0 min). '
                      "Content id [gvh00882]: ", ui.asked[0])
        self.assertEqual(result.title, "GVH-882 2026-10-03 SD DMM WEB-DL H.264 AAC")
        # Typed over: the member's id wins.
        result = self.run_item(video, None, Quiet(answers=["abc00123"]), dry_run=True)
        self.assertTrue(result.title.startswith("ABC-123 "), result.title)

    def test_dvd_id_suggestion_prefers_the_digital_listing(self):
        make_clip(self.dir / "IPZZ-020 4K.mkv", seconds=2)
        with self.assertRaises(pipeline.NeedsContentId) as caught:
            pipeline.prepare(pipeline.collect([self.dir / "IPZZ-020 4K.mkv"])[0], None, MEDIAINFO, Quiet())
        s = caught.exception.suggestion
        self.assertEqual((s.dvd_id, s.cid, s.others, s.source, s.where), ("IPZZ-020", "ipzz00020", ["ipzz020"], "r18.dev and DMM", "file name"))
        self.assertIsNotNone(caught.exception.infos)  # the answer doesn't read the video again
        # "ABP123" looks like a content id too, but r18.dev and DMM don't know abp123: its DVD id is looked up.
        make_clip(self.dir / "ABP123.mkv", seconds=2)
        with self.assertRaises(pipeline.NeedsContentId) as caught:
            pipeline.prepare(pipeline.collect([self.dir / "ABP123.mkv"])[0], None, MEDIAINFO, Quiet())
        self.assertIn("r18.dev and DMM don't know abp123", str(caught.exception))
        self.assertEqual((caught.exception.suggestion.cid, caught.exception.suggestion.others), ("118abp00123", ["118abp123"]))
        # Nothing found: the question as before.
        make_clip(self.dir / "ZZZZ-999.mkv", seconds=2)
        with self.assertRaises(pipeline.NeedsContentId) as caught:
            pipeline.prepare(pipeline.collect([self.dir / "ZZZZ-999.mkv"])[0], None, MEDIAINFO, Quiet())
        self.assertIsNone(caught.exception.suggestion)
        self.assertIn("name the file after it", str(caught.exception))

    def test_dvd_id_from_dmm_for_digital_ids(self):
        make_clip(self.dir / "ipzz00020.mkv")
        result = self.run_item(self.dir / "ipzz00020.mkv", None, dry_run=True)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.title.startswith("IPZZ-020 2023-04-07 SD DMM"))
        self.assertEqual(result.warnings, ["not checked against ClearJAV (no API key saved)"])

    def test_dvd_id_worked_out_when_nobody_has_one(self):
        make_clip(self.dir / "xyz00001.mkv")
        result = self.run_item(self.dir / "xyz00001.mkv", None, dry_run=True)
        self.assertTrue(result.title.startswith("XYZ-001 2025-01-01"))
        self.assertIn("worked out", " ".join(result.warnings))

    def test_unknown_on_r18_needs_the_member(self):
        make_clip(self.dir / "zzz00001.mkv")
        self.assertIn("doesn't know", self.run_item(self.dir / "zzz00001.mkv", None, dry_run=True).message)
        # Nowhere at all: the site would need a title and a cover, so nothing is changed or sent.
        site, client = self.site()
        result = self.run_item(self.dir / "zzz00001.mkv", client, Quiet(answers=["y", "", ""]))
        self.assertFalse(result.ok)
        self.assertIn("ClearJAV has no torrent of it", result.message)
        self.assertEqual((site.uploads, sorted(p.name for p in self.dir.iterdir())), ([], ["zzz00001.mkv"]))
        ui = Quiet(answers=["y", "", ""])  # a test run says what a real upload would meet, before asking anything
        result = self.run_item(self.dir / "zzz00001.mkv", client, ui, dry_run=True)
        self.assertFalse(result.ok)
        self.assertIn("ClearJAV has no torrent of it", result.message)
        self.assertEqual(ui.asked, [])
        # On ClearJAV already: DMM's DVD id and date are offered and accepted, then "upload anyway".
        existing = [{"name": "ZZZ-001 2025-05-05 1080p DMM WEB-DL H.264 AAC", "type": "WEB-DL", "resolution": "1080p", "seeders": 1}]
        site, client = self.site(existing=existing)
        result = self.run_item(self.dir / "zzz00001.mkv", client, Quiet(answers=["y", "", "", "y"]))
        self.assertTrue(result.ok, result.message)
        body = site.uploads[0]["body"].decode("latin1")
        self.assertRegex(body, r'name="dvd_id"\r\n\r\nZZZ-001\r\n')
        self.assertRegex(body, r'name="release_date"\r\n\r\n2025-05-05\r\n')

    def test_nothing_happens_without_a_key(self):
        make_clip(self.dir / "abc00123x.mkv", seconds=4, comment={"file_id": "abc00123"})
        items = pipeline.collect([self.dir / "abc00123x.mkv"])
        plan = pipeline.prepare(items[0], None, MEDIAINFO, Quiet())
        with self.assertRaises(pipeline.Skip):
            pipeline.execute(plan, None, TOOLS, pipeline.Options(), Quiet())
        self.assertTrue((self.dir / "abc00123x.mkv").exists())  # not renamed

    def test_percent_in_the_name_and_one_kind_of_line_end(self):
        video = make_clip(self.dir / "100% done.mkv", seconds=4, comment={"file_id": "abc00123"})
        result = self.run_item(video, None, dry_run=True)
        self.assertTrue(result.ok, result.message)
        text = (self.dir / "clearjav-uploads" / "abc00123.mediainfo.txt").read_bytes()
        self.assertNotIn(b"\r", text)

    def test_dmm_file_id_wins_over_the_name(self):
        make_clip(self.dir / "abc00123hhb.mkv", comment={"file_id": "abc00123"})
        result = self.run_item(self.dir / "abc00123hhb.mkv", None, dry_run=True)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.title.startswith("ABC-123"))

    def test_vp9_parts_folder_is_one_upload(self):
        encoders = subprocess.run([str(TOOLS.ffmpeg), "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        if "libvpx-vp9" not in encoders:
            self.skipTest("no VP9 encoder")
        folder = self.dir / "My parts"
        folder.mkdir()
        for n in (1, 2):
            make_clip(folder / f"abc00123_pt{n}.mkv", seconds=4, codec="libvpx-vp9", comment={"file_id": "abc00123", "part": str(n)})
        (folder / "release.nfo").write_text("not a video")
        site, client = self.site()
        result = self.run_item(folder, client)
        self.assertTrue(result.ok, result.message)
        renamed = self.dir / "abc00123"
        self.assertEqual(sorted(p.name for p in renamed.iterdir()), ["abc00123-pt1.mkv", "abc00123-pt2.mkv", "release.nfo"])
        torrent_part = site.uploads[0]["body"]
        start = torrent_part.index(b"d8:announce")
        info = bdecode(torrent_part[start:torrent_part.index(b"\r\n--", start)])[b"info"]
        self.assertEqual(info[b"name"], b"abc00123")
        self.assertEqual([f[b"path"] for f in info[b"files"]], [[b"abc00123-pt1.mkv"], [b"abc00123-pt2.mkv"]])  # no .nfo
        self.assertEqual(sorted(p.name for p in (self.dir / "clearjav-uploads").glob("*.webp")), ["abc00123-pt1_s.webp", "abc00123-pt2_s.webp"])
        # The MediaInfo of every part, in order, with the new names (Upload Rules; the site's separator).
        sent = torrent_part.decode("latin1")
        names = re.findall(r"^Complete name\s*:\s*(\S+)", sent, re.MULTILINE)
        self.assertEqual(names, ["abc00123-pt1.mkv", "abc00123-pt2.mkv"])
        self.assertIn("\n\n-------------------------\n\n", sent)
        saved = (self.dir / "clearjav-uploads" / "abc00123.mediainfo.txt").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"^Complete name\s*:\s*(\S+)", saved, re.MULTILINE), names)

    def test_parts_folder_with_other_things_is_not_renamed(self):
        folder = self.dir / "Downloads"
        folder.mkdir()
        for n in (1, 2):
            make_clip(folder / f"abc00123-pt{n}.mkv", seconds=2)
        (folder / "taxes.pdf").write_text("x")
        (folder / "other torrent").mkdir()
        items = pipeline.collect([folder])
        self.assertEqual(len(items), 1)
        self.assertIn("also holds", items[0].problem)
        self.assertIn("taxes.pdf", items[0].problem)
        result = pipeline.process(items[0], None, TOOLS, MEDIAINFO, pipeline.Options(), Quiet())
        self.assertFalse(result.ok)
        self.assertTrue(folder.exists())

    def test_h264_parts_and_lone_parts_are_refused(self):
        folder = self.dir / "abc00123"
        folder.mkdir()
        for n in (1, 2):
            make_clip(folder / f"abc00123-pt{n}.mkv", seconds=4)
        self.assertIn("join them", self.run_item(folder, None, dry_run=True).message)
        lone = make_clip(self.dir / "single.mkv", seconds=4, comment={"file_id": "abc00123", "part": "2"})
        self.assertIn("part 2", self.run_item(lone, None, dry_run=True).message)
        named = self.dir / "abc00123-pt1.mkv"
        (folder / "abc00123-pt1.mkv").rename(named)
        self.assertIn("a part of a video", self.run_item(named, None, dry_run=True).message)  # by its name alone

    def test_parts_that_dont_belong_together(self):
        # A 4K part 1 with a 1080p part 2 is not one 2160p upload.
        folder = self.dir / "abc00123"
        folder.mkdir()
        make_clip(folder / "abc00123-pt1.mkv", seconds=2, size="640x360")
        make_clip(folder / "abc00123-pt2.mkv", seconds=2, size="320x180")
        self.assertEqual(self.run_item(folder, None, dry_run=True).message, "the parts don't match: part 2 is 320×180, part 1 is 640×360")
        (folder / "abc00123-pt2.mkv").rename(folder / "abc00123-pt3.mkv")
        self.assertEqual(self.run_item(folder, None, dry_run=True).message, "part 2 is missing (the folder has parts 1 and 3)")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["abc00123"])  # nothing made or renamed

    def test_unreadable_files_are_explained(self):
        (self.dir / "brg00011.mp4").write_bytes(b"")
        (self.dir / "gvh00882.mp4").write_bytes(bytes(range(256)) * 4000)  # not a video at all
        # An .mp4 copied only halfway: FFmpeg writes its index (moov) at the end, so the copy has none ...
        whole = make_clip(self.dir / "whole.mp4", seconds=6, size="640x360").read_bytes()
        (self.dir / "bkd00370.mp4").write_bytes(whole[:len(whole) // 2])
        # ... unless it is written first ("faststart"): then MediaInfo sees the video and that it is cut short.
        whole = make_clip(self.dir / "whole.mp4", seconds=6, size="640x360", extra=["-movflags", "+faststart"]).read_bytes()
        (self.dir / "mkon00148.mp4").write_bytes(whole[:len(whole) // 2])
        unreadable = "can't be read as a video: damaged, cut off while copying, or not a video file"
        expected = {"brg00011.mp4": "brg00011.mp4 is empty (0 bytes)", "gvh00882.mp4": f"gvh00882.mp4 {unreadable}",
                    "bkd00370.mp4": f"bkd00370.mp4 {unreadable} (MediaInfo reports IsTruncated=Yes)",
                    "mkon00148.mp4": "mkon00148.mp4 looks damaged: MediaInfo reports IsTruncated=Yes"}
        for name, message in expected.items():
            self.assertEqual(self.run_item(self.dir / name, None, dry_run=True).message, message, name)

    def test_contact_sheet_made_smaller_for_the_site(self):
        video = make_clip(self.dir / "abc00123.mkv", seconds=60, size="1280x720")
        full = contact_sheet.make(video, self.dir / "full.webp", TOOLS).stat().st_size  # quality 90
        notes = []
        with mock.patch.object(contact_sheet, "AIM_BYTES", full - 1):
            small = contact_sheet.make(video, self.dir / "small.webp", TOOLS, fit=True, note=notes.append)
        self.assertLess(small.stat().st_size, full)
        self.assertEqual(notes, [f"contact sheet made smaller for ClearJAV's 4 MB limit: {contact_sheet.megabytes(full)} -> "
                                 f"{contact_sheet.megabytes(small.stat().st_size)} (quality 80)"])
        size_of = lambda path: subprocess.run([str(TOOLS.ffprobe), "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0",
                                               str(path)], capture_output=True, text=True).stdout.strip()
        self.assertEqual(size_of(small), size_of(self.dir / "full.webp"))  # the same shots and layout
        # Too large even at the lowest quality: refused before anything is uploaded, in the steps of the item.
        site, client = self.site()
        with mock.patch.object(contact_sheet, "AIM_BYTES", 10), mock.patch.object(contact_sheet, "SITE_LIMIT", 10):
            ui = Quiet()
            result = self.run_item(video, client, ui)
        self.assertFalse(result.ok)
        self.assertRegex(result.message, r"^the contact sheet of abc00123\.mkv is \d+\.\d MB, over ClearJAV's 4 MB limit, even at the lowest quality$")
        self.assertEqual(site.uploads, [])
        with mock.patch.object(contact_sheet, "AIM_BYTES", full - 1):
            ui = Quiet()
            result = self.run_item(video, None, ui, dry_run=True)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(any(s.startswith("contact sheet made smaller for ClearJAV's 4 MB limit: ") for s in ui.steps), ui.steps)

    def test_contact_sheet_over_the_limit_gets_its_shots_further_apart(self):
        # One hour: 10 rows, a shot every 2 minutes. Over the limit, the shots go 10 minutes apart (2 rows), same quality.
        video = make_clip(self.dir / "abc00123.mkv", seconds=3600, size="160x90", rate=1)
        full = contact_sheet.make(video, self.dir / "full.webp", TOOLS).stat().st_size
        notes = []
        with mock.patch.object(contact_sheet, "AIM_BYTES", full - 1):
            small = contact_sheet.make(video, self.dir / "small.webp", TOOLS, fit=True, note=notes.append)
        self.assertLess(small.stat().st_size, full)
        self.assertEqual(notes, [f"contact sheet made smaller for ClearJAV's 4 MB limit: {contact_sheet.megabytes(full)} -> "
                                 f"{contact_sheet.megabytes(small.stat().st_size)} (a shot every 10 minutes instead of 2)"])
        height = lambda path: int(subprocess.run([str(TOOLS.ffprobe), "-v", "error", "-show_entries", "stream=height", "-of", "csv=p=0",
                                                  str(path)], capture_output=True, text=True).stdout.strip())
        self.assertLess(height(small), height(self.dir / "full.webp"))  # fewer rows

    def test_site_refusing_a_large_contact_sheet_is_explained(self):
        make_clip(self.dir / "abc00123.mkv", seconds=4)
        _, client = self.site(refuse={"thumb_sheets.0": ["The image must be between 1 and 4096 kilobytes."]})
        result = self.run_item(self.dir / "abc00123.mkv", client)
        self.assertFalse(result.ok)
        self.assertRegex(result.message, r"^ClearJAV refused a contact sheet over 4 MB \(the largest made here is \d+\.\d MB\): nothing was uploaded$")

    def test_contact_sheet_size_like_mtn(self):
        video = make_clip(self.dir / "abc00123.mkv", seconds=1800, size="1280x720", rate=1)
        sheet = contact_sheet.make(video, self.dir / "s.png", TOOLS)
        out = subprocess.run([str(TOOLS.ffprobe), "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(sheet)],
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "1024,1003")  # mtn: 4 info lines (53 px) + 5 rows of 340x190
        tall = make_clip(self.dir / "tall.mkv", seconds=60, size="640x480")
        out = subprocess.run([str(TOOLS.ffprobe), "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0",
                              str(contact_sheet.make(tall, self.dir / "t.webp", TOOLS))], capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "1024,307")  # one row of 4:3 shots (340x254)

    def test_contact_sheet_with_either_way_of_reading_the_filters(self):
        """Newer FFmpeg releases no longer know -filter_script; FFmpeg before 7 doesn't know -/filter."""
        video = make_clip(self.dir / "abc00123.mkv", seconds=60, size="640x480")
        real_run = subprocess.run
        for unknown in contact_sheet.FILTER_OPTIONS:
            with self.subTest(unknown=unknown):
                contact_sheet._FILTER_OPTION.clear()
                calls = []

                def run(cmd, *args, **kwargs):
                    if "canvas.rgb" in cmd:
                        calls.append(next(c for c in cmd if c in contact_sheet.FILTER_OPTIONS))
                        if unknown in cmd:  # as an FFmpeg answers an option it doesn't know
                            return subprocess.CompletedProcess(cmd, 1, b"", f"Unrecognized option '{unknown[1:]}'.\r\n"
                                                               "Error splitting the argument list: Option not found\r\n".encode())
                    return real_run(cmd, *args, **kwargs)

                with mock.patch.object(contact_sheet.subprocess, "run", side_effect=run):
                    sheet = contact_sheet.make(video, self.dir / f"s{len(unknown)}.webp", TOOLS)
                    contact_sheet.make(video, self.dir / f"again{len(unknown)}.webp", TOOLS)
                self.assertGreater(sheet.stat().st_size, 0)
                other = next(o for o in contact_sheet.FILTER_OPTIONS if o != unknown)
                # The first sheet finds out which one this FFmpeg knows; the next ones use it at once.
                self.assertEqual(calls, ([unknown] if calls[0] == unknown else []) + [other, other])
        contact_sheet._FILTER_OPTION.clear()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        for name in ("config_dir", "log_dir"):
            patcher = mock.patch.object(config, name, return_value=self.dir / name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(args))
        return code, out.getvalue()

    def test_upload_is_the_default_and_needs_a_key(self):
        with mock.patch.object(config, "get_secret", return_value=""), \
                mock.patch.object(cli, "find_mediainfo", return_value=Path("m")), \
                mock.patch.object(cli, "find_ffmpeg", return_value=None):
            code, output = self.run_cli(str(self.dir))
        self.assertEqual(code, 2)
        self.assertIn("No API key yet", output)

    def test_other_videos_in_a_folder_are_named(self):
        folder = self.dir / "videos"
        folder.mkdir()
        for name in ("mkon00148.avi", "cover.jpg", "notes.txt"):
            (folder / name).write_bytes(b"x")
        with mock.patch.object(config, "get_secret", return_value=""), \
                mock.patch.object(cli, "find_mediainfo", return_value=Path("m")), \
                mock.patch.object(cli, "find_ffmpeg", return_value=None):
            code, output = self.run_cli("--dry-run", str(folder))
        self.assertEqual(code, 1)
        self.assertIn("1 file skipped: mkon00148.avi (only .mkv, .mp4 and .wmv are uploaded).", output)

    def test_busy_port_is_explained(self):
        busy = socket.socket()
        self.addCleanup(busy.close)
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        code, output = self.run_cli("gui", "--port", str(port), "--no-browser")
        self.assertEqual(code, 2)
        self.assertIn(f"Port {port} is in use, maybe by another copy of the uploader: close it, or start without --port", output)
        with self.assertRaises(SystemExit):  # argparse: not a port
            with redirect_stderr(io.StringIO()):
                cli.main(["gui", "--port", "70000"])

    def test_staff_options_need_permission(self):
        opts = pipeline.Options(internal=True)
        with self.assertRaises(cli.SetupError):
            cli.check_permissions({"allowed": True, "skips_moderation": True, "internal_options": False, "featured": False}, opts)
        cli.check_permissions({"allowed": True, "skips_moderation": True, "internal_options": True, "featured": False}, opts)
        with self.assertRaises(cli.SetupError):
            cli.check_permissions({"allowed": True, "internal_options": True, "featured": False}, pipeline.Options(featured=True))
        with self.assertRaises(cli.SetupError):
            cli.check_permissions({"allowed": False}, pipeline.Options())
        queued = pipeline.Options(mod_queue=True)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.check_permissions({"allowed": True, "skips_moderation": False}, queued)
        self.assertFalse(queued.mod_queue)
        self.assertIn("always wait for moderation", out.getvalue())

    def test_config_file(self):
        code, output = self.run_cli("config")
        self.assertEqual(code, 0)
        path = config.config_path()
        path.write_text(path.read_text().replace("anonymous = no", "anonymous = yes").replace("site = https://clearjav.com", "site = https://example.test/"))
        settings = config.load()
        self.assertEqual((settings.anonymous, settings.site, settings.personal_release), (True, "https://example.test", False))


if __name__ == "__main__":
    unittest.main()
