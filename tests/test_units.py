import builtins
import hashlib
import importlib.util
import os
import re
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import requests

from clearjav_aio import api, config, contact_sheet, dmm_api, logs, media as media_module, naming, pipeline, r18, tools
from clearjav_aio import torrent as torrent_module
from clearjav_aio.contact_sheet import rows_for
from clearjav_aio.media import REMUX, WEB_DL, Media
from clearjav_aio.torrent import announce_problem, bdecode, bencode, create, looks_like_torrent, piece_length


def media(width, height, vcodec="H.264", acodec="AAC", name="x.mkv", subtitles=(), interlaced=False, encoder=""):
    return Media(Path(name), "", width, height, 100.0, vcodec, acodec, None, None, subtitles, interlaced, encoder)


class TorrentTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_piece_size_matches_the_sites_own_torrents(self):
        # As in the imdl torrents of the site's own uploads: 1 MiB around 2-4 GiB, 2 MiB from 4 GiB to 16 GiB.
        self.assertEqual(piece_length(3 * 1024**3), 1024 * 1024)
        self.assertEqual(piece_length(9 * 1024**3), 2048 * 1024)
        self.assertEqual(piece_length(700 * 1024**2), 512 * 1024)
        self.assertEqual(piece_length(10), 16 * 1024)
        self.assertEqual(piece_length(2**60), 16 * 1024**2)

    def test_single_file(self):
        data = bytes(range(256)) * 300
        video = self.dir / "abc00123.mkv"
        video.write_bytes(data)
        meta = bdecode(create(video, self.dir / "out.torrent").read_bytes())
        info = meta[b"info"]
        self.assertEqual((info[b"name"], info[b"length"], info[b"private"]), (b"abc00123.mkv", len(data), 1))
        size = info[b"piece length"]
        expected = b"".join(hashlib.sha1(data[i:i + size]).digest() for i in range(0, len(data), size))
        self.assertEqual(info[b"pieces"], expected)

    def test_folder_of_parts(self):
        folder = self.dir / "abc00123"
        folder.mkdir()
        for n, size in ((2, 5000), (1, 70000)):
            (folder / f"abc00123-pt{n}.mkv").write_bytes(bytes([n]) * size)
        info = bdecode(create(folder, self.dir / "out.torrent").read_bytes())[b"info"]
        self.assertEqual([f[b"path"] for f in info[b"files"]], [[b"abc00123-pt1.mkv"], [b"abc00123-pt2.mkv"]])
        joined = bytes([1]) * 70000 + bytes([2]) * 5000
        size = info[b"piece length"]
        self.assertEqual(len(info[b"pieces"]), 20 * -(-len(joined) // size))
        self.assertEqual(info[b"pieces"][:20], hashlib.sha1(joined[:size]).digest())

    def test_announce_url(self):
        video = self.dir / "abc00123.mkv"
        video.write_bytes(b"x" * 100)
        mine = "https://clearjav.com/announce/0123456789abcdef0123456789abcdef"
        self.assertEqual(bdecode(create(video, self.dir / "a.torrent").read_bytes())[b"announce"], b"https://clearjav.com/announce")
        self.assertEqual(bdecode(create(video, self.dir / "b.torrent", announce=mine).read_bytes())[b"announce"], mine.encode())
        self.assertIsNone(announce_problem(mine, "https://clearjav.com/"))
        self.assertIsNone(announce_problem("https://clearjav.net/0123456789abcdef0123456789abcdef", "https://clearjav.com"))  # the tracker
        for bad in ("https://clearjav.com/announce/", "http://clearjav.com/announce/0123456789abcdef", "https://other.org/announce/0123456789abcdef",
                    "http://clearjav.net/0123456789abcdef", "https://clearjav.net/", "https://clearjav.net.evil.test/0123456789abcdef",
                    mine + "/x", "0123456789abcdef"):
            self.assertIsNotNone(announce_problem(bad, "https://clearjav.com"), bad)

    def test_bencode(self):
        value = {"b": [1, "x", b"\x00"], "a": {"z": 0}}
        self.assertEqual(bdecode(bencode(value)), {b"a": {b"z": 0}, b"b": [1, b"x", b"\x00"]})
        self.assertFalse(looks_like_torrent(b"<html>"))


class NamingTest(unittest.TestCase):
    def test_title_with_sd_and_tags(self):
        self.assertEqual(naming.title("NHDT-230", "2005-11-04", "SD", "H.264", "AAC"), "NHDT-230 2005-11-04 SD DMM WEB-DL H.264 AAC")
        self.assertEqual(naming.title("REAL-807", "2024-10-08", "2160p", "VP9", "AAC", "ClearJAV"), "REAL-807 2024-10-08 2160p DMM WEB-DL VP9 AAC-ClearJAV")

    def test_remux_title(self):
        self.assertEqual(naming.title("ABC-123", "2010-05-01", "1080i", "AVC", "PCM", "", "Blu-Ray", "REMUX"),
                         "ABC-123 2010-05-01 1080i Blu-Ray REMUX AVC PCM")

    def test_title(self):
        self.assertEqual(naming.title("IPZZ-020", "2023-04-07", "1080p", "H.264", "AAC"), "IPZZ-020 2023-04-07 1080p DMM WEB-DL H.264 AAC")
        self.assertEqual(naming.title("ABC-123", "2024-01-02", "2160p", "VP9", "AAC", "TAG"), "ABC-123 2024-01-02 2160p DMM WEB-DL VP9 AAC-TAG")

    def test_dvd_id_guess(self):
        cases = {"ipzz00020": "IPZZ-020", "1sdde00301": "SDDE-301", "h_1133gone00123": "GONE-123",
                 "118abp00123": "ABP-123", "1mist00432re01": "MIST-432", "abc01234": "ABC-1234",
                 "1start00561v": "START-561-V", "1sddm00972ai": "SDDM-972-AI"}  # DMM's own listings, as the site writes them
        for cid, dvd in cases.items():
            self.assertEqual(naming.guess_dvd_id(cid), dvd, cid)
        self.assertIsNone(naming.guess_dvd_id("abc_def"))

    def test_tag(self):
        self.assertTrue(naming.valid_tag("MyGroup"))
        for bad in ("", "../x", "a" * 40, "bad:tag"):
            self.assertFalse(naming.valid_tag(bad), bad)


class ResolutionTest(unittest.TestCase):
    def test_ids_match_the_site(self):
        cases = {(1920, 1080): ("1080p", 3), (3840, 2160): ("2160p", 2), (1280, 720): ("720p", 5), (720, 404): ("404p", 11),
                 (640, 360): ("SD", 15), (1920, 888): ("Other", 16), (4320, 2160): ("VR", 13), (5760, 2880): ("VR", 13),
                 (8192, 4096): ("8K-VR", 14), (720, 480): ("480p", 8)}
        for (w, h), (name, rid) in cases.items():
            m = media(w, h)
            self.assertEqual((m.resolution, m.resolution_id), (name, rid), (w, h))
        self.assertEqual(media(4320, 2160).exact_resolution, "2160p")

    def test_title_resolution_follows_the_naming_guide(self):
        cases = {(640, 360): "SD", (352, 240): "SD", (720, 404): "404p", (1920, 1080): "1080p", (3840, 2160): "2160p",
                 (1920, 888): "888p", (4320, 2160): "2160p-VR", (5760, 2880): "2880p-VR", (8192, 4096): "8K-VR",
                 (7680, 3840): "3840p-VR", (1920, 1088): "1080p", (1440, 1072): "1080p"}
        for (w, h), expected in cases.items():
            self.assertEqual(media(w, h).title_resolution, expected, (w, h))
        self.assertEqual(media(1920, 1088).resolution, "1080p")  # as the site's title checker files it

    def test_remux_resolution_and_codec_words(self):
        dvd = media(720, 480, "MPEG-2", "AC-3", interlaced=True)
        self.assertEqual((dvd.title_resolution_for(REMUX), dvd.title_resolution_for(WEB_DL), dvd.resolution_id), ("480i", "480p", 8))
        bluray = media(1920, 1080, "H.264", "PCM", interlaced=True)
        self.assertEqual((bluray.title_resolution_for(REMUX), bluray.video_word(REMUX), bluray.video_word(WEB_DL)), ("1080i", "AVC", "H.264"))
        self.assertEqual(bluray.resolution_id, 3)  # 1080i is filed as 1080p

    def test_codec_names_used_on_clearjav(self):
        for fmt, name in (("AVC", "H.264"), ("HEVC", "HEVC"), ("VP9", "VP9"), ("VC-1", "VC-1"), ("WMV1", "VC-1"), ("WMV2", "VC-1")):
            self.assertEqual(media_module._video_codec({"Format": fmt}), name, fmt)
        # The words of the site's title checker (TorrentTitle::audioWord).
        for track, name in (({"Format": "AAC"}, "AAC"), ({"Format": "WMA"}, "WMA"), ({"Format": "AC-3"}, "AC-3"), ({"Format": "E-AC-3"}, "E-AC-3"),
                            ({"Format": "PCM"}, "PCM"), ({"Format": "FLAC"}, "FLAC"), ({"Format": "MLP FBA"}, "TrueHD"),
                            ({"Format": "MPEG Audio", "Format_Profile": "Layer 2"}, "MP2"), ({"Format": "MPEG Audio", "Format_Profile": "Layer 3"}, "MP3"),
                            ({"Format": "DTS"}, "DTS"), ({"Format": "DTS", "Format_AdditionalFeatures": "XLL"}, "DTS-HD MA")):
            self.assertEqual(media_module._audio_codec(track), name, track)

    def test_web_dl_or_remux(self):
        for info in (media(1920, 1080), media(1280, 720, "VC-1", "WMA"), media(1920, 1080, name="bdsr00123.mkv")):  # ids with "bd" are fine
            self.assertEqual(media_module.kind_of(info), (WEB_DL, None), info)
        remuxes = {
            "DVD": media(720, 480, "MPEG-2", "AC-3", interlaced=True),
            "Blu-Ray": media(1920, 1080, acodec="PCM"),
            "UHD Blu-Ray": media(3840, 2160, "HEVC", "TrueHD"),
            "VHS": media(720, 480, "MPEG-2", "MP2", name="ABC-123 VHS.mkv"),
            "3D Blu-Ray": media(1920, 1080, acodec="DTS-HD MA", name="ABC-123 3D BluRay Remux.mkv"),
        }
        for source, info in remuxes.items():
            self.assertEqual(media_module.kind_of(info), (REMUX, None), source)
            self.assertEqual(media_module.remux_source(info), source)
        refused = {
            "re-encoded": media(1920, 1080, acodec="AC-3", encoder="x264"),
            "aac audio is not a disc": media(1920, 1080, name="ABC-123 BD Remux.mkv"),
            "aac audio is not a disc's": media(1920, 1080, name="ABC-123.BluRay.mkv"),
            "vp9 video is not on": media(1920, 1080, "VP9", "PCM"),
            "rips": media(1920, 1080, name="ABC-123 BDRip.mkv"),
            "iso": media(1920, 1080, name="ABC-123.iso.mkv"),
            "dmm web-dls are h.264": media(1920, 1080, "MPEG-4"),
            "opus audio": media(1920, 1080, acodec="Opus"),
        }
        for words, info in refused.items():
            kind, problem = media_module.kind_of(info)
            self.assertIsNone(kind, words)
            self.assertIn(words, problem.lower(), words)

    def test_contact_sheet_rows(self):
        # The timing of the site's own uploads: a shot every 2 minutes, 3 a row; every 4 minutes for videos over 4 hours.
        self.assertEqual(rows_for(1800), 5)
        self.assertEqual(rows_for(7200), 20)
        self.assertEqual(rows_for(20000), 28)
        self.assertEqual(rows_for(10**6), 72)
        self.assertEqual(rows_for(7200, 600), 4)  # shots further apart for a sheet over the site's limit


class PlanTest(unittest.TestCase):
    def plan(self, **kwargs):
        item = pipeline.Item(Path("abc00123.mkv"), [Path("abc00123.mkv")])
        values = dict(item=item, infos=[media(1920, 1080)], cid="abc00123", kind=WEB_DL, source="DMM")
        values.update(kwargs)
        return pipeline.Plan(**values)

    def test_questions_and_answers(self):
        opts = pipeline.Options()
        plan = self.plan(dvd_id="ABC-123", release_date="2024-01-02", dvd_from="r18.dev")
        self.assertEqual(plan.questions(opts), [])
        self.assertEqual(plan.title(opts), "ABC-123 2024-01-02 1080p DMM WEB-DL H.264 AAC")
        plan.existing = [{"name": "x"}]
        self.assertEqual(plan.questions(opts), ["duplicate"])
        self.assertEqual(plan.questions(pipeline.Options(allow_duplicates=True)), [])
        self.assertEqual(plan.questions(pipeline.Options(dry_run=True)), [])
        plan.upload_anyway = True
        self.assertEqual(plan.questions(opts), [])

        guessed = self.plan(dvd_id="ABC-123", guess="ABC-123", dvd_from="worked out", release_date="2024-01-02")
        self.assertEqual(guessed.questions(opts), ["dvd_id"])
        with self.assertRaises(ValueError):
            guessed.answer("bad id!")
        guessed.answer("abc-0123")
        self.assertEqual((guessed.questions(opts), guessed.dvd_id, guessed.dvd_from), ([], "ABC-0123", "you"))

        unknown = self.plan(known=False)
        self.assertEqual(unknown.questions(opts), ["unknown"])
        with self.assertRaises(ValueError):
            unknown.answer("ABC-123", "2024-1-2")
        unknown.answer("ABC-123", "2024-01-02")
        self.assertEqual(unknown.questions(opts), [])

    def test_remux_source_and_tags(self):
        plan = self.plan(infos=[media(1920, 1080, acodec="PCM", interlaced=True)], kind=REMUX, source="Blu-Ray", dvd_id="ABC-123",
                         release_date="2010-05-01")
        opts = pipeline.Options(personal_release=True, tag="Me")
        self.assertEqual(plan.title(opts), "ABC-123 2010-05-01 1080i Blu-Ray REMUX AVC PCM-Me")
        plan.answer(source="UHD Blu-Ray")
        self.assertTrue(plan.title(pipeline.Options(internal=True)).endswith("UHD Blu-Ray REMUX AVC PCM-ClearJAV"))
        with self.assertRaises(ValueError):
            plan.answer(source="LaserDisc")
        with self.assertRaises(ValueError):
            self.plan().answer(source="DVD")  # WEB-DLs come from DMM

    def test_exact_title_on_clearjav_blocks_until_the_title_differs(self):
        plan = self.plan(dvd_id="ABC-123", release_date="2024-01-02", dvd_from="r18.dev",
                         existing=[{"name": " abc-123 2024-01-02 1080p DMM WEB-DL H.264 AAC", "details_link": "https://x.test/torrents/9"}])
        for opts in (pipeline.Options(), pipeline.Options(dry_run=True), pipeline.Options(allow_duplicates=True)):
            self.assertIn("exactly this title (https://x.test/torrents/9)", plan.blocker(opts))
        self.assertIsNone(plan.blocker(pipeline.Options(personal_release=True, tag="Me")))  # another title: a duplicate question
        self.assertEqual(plan.questions(pipeline.Options(personal_release=True, tag="Me")), ["duplicate"])
        self.assertIsNone(plan.blocker(pipeline.Options(internal=True)))  # "-ClearJAV"
        self.assertIn("exactly this title", plan.blocker(pipeline.Options(personal_release=True)))  # no tag: the same title

    def test_unknown_video_is_refused_in_test_runs_too(self):
        unknown = self.plan(known=False, existing=[])
        self.assertTrue(unknown.unknown_everywhere())
        self.assertIn("ClearJAV has no torrent of it", unknown.blocker(pipeline.Options(dry_run=True)))
        self.assertIsNone(self.plan(known=False).blocker(pipeline.Options(dry_run=True)))  # not checked (no API key)

    def test_permissions(self):
        staff = {"allowed": True, "skips_moderation": True, "internal_options": True, "featured": True}
        member = {"allowed": True, "skips_moderation": False, "internal_options": False, "featured": False}
        self.assertIsNone(pipeline.permission_problem(staff, pipeline.Options(internal=True, featured=True)))
        self.assertIn("internal", pipeline.permission_problem(member, pipeline.Options(internal=True)))
        self.assertIn("feature", pipeline.permission_problem(member, pipeline.Options(featured=True)))
        self.assertIn("can't upload", pipeline.permission_problem({"allowed": False}, pipeline.Options()))
        self.assertIsNone(pipeline.permission_problem(None, pipeline.Options(internal=True)))

    def test_which_uploads_wait_for_staff(self):
        # From /api/user's "upload" answer and "Send to the moderation queue anyway".
        self.assertFalse(pipeline.waits_for_staff({"allowed": True, "skips_moderation": True}, False))
        self.assertTrue(pipeline.waits_for_staff({"allowed": True, "skips_moderation": True}, True))
        self.assertTrue(pipeline.waits_for_staff({"allowed": True, "skips_moderation": False}, False))
        for flags in (None, {}, {"allowed": True}):  # the site doesn't say: its own message stays
            self.assertFalse(pipeline.waits_for_staff(flags, True), flags)


class NamesAndFoldersTest(unittest.TestCase):
    def test_part_names_and_content_ids_from_names(self):
        self.assertEqual(pipeline.content_id_from_name(Path("abc00123-pt2.mkv")), ("abc00123", 2))
        self.assertEqual(pipeline.content_id_from_name(Path("ABC00123_part1.mp4")), ("abc00123", 1))
        self.assertEqual(pipeline.content_id_from_name(Path("ipt00001.mkv")), ("ipt00001", None))  # a label, not a part
        # DMM's letters after the number are listings of their own (AI upscale, extended version): parts too.
        cases = {"1sddm00972ai-pt1.mkv": ("1sddm00972ai", 1), "1start00561v-pt2.mkv": ("1start00561v", 2),
                 "1START00561V_part02.mp4": ("1start00561v", 2), "1sddm00972aipt3.mkv": ("1sddm00972ai", 3),
                 "abc00123pt1.mkv": ("abc00123", 1), "abc00123_part02.mkv": ("abc00123", 2),
                 "h_1133gone00123 pt3.mkv": ("h_1133gone00123", 3), "abc00123-pt10.mkv": ("abc00123", 10),
                 "1sddm00972ai.mkv": ("1sddm00972ai", None), "1start00561v.mp4": ("1start00561v", None)}
        for name, expected in cases.items():
            self.assertEqual(pipeline.content_id_from_name(Path(name)), expected, name)
        for cid in ("1sddm00972ai", "1start00561v"):
            self.assertTrue(media_module.CID_RE.fullmatch(cid) and pipeline.NAME_CID_RE.fullmatch(cid), cid)

    def test_a_folder_of_parts_of_a_suffixed_id(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        folder = root / "1sddm00972ai"
        folder.mkdir()
        for n in (1, 2):
            (folder / f"1sddm00972ai-pt{n}.mkv").write_bytes(b"x")
        items = pipeline.collect([folder])
        self.assertEqual([(i.target.name, [f.name for f in i.files], i.problem) for i in items],
                         [("1sddm00972ai", ["1sddm00972ai-pt1.mkv", "1sddm00972ai-pt2.mkv"], "")])
        lone = pipeline.collect([folder / "1sddm00972ai-pt1.mkv"])
        self.assertEqual(lone[0].problem, pipeline.PART_PROBLEM)  # a part on its own, as for abc00123-pt1
        for name in ("video.mkv", "movie.mp4", "My download.mkv"):
            self.assertEqual(pipeline.content_id_from_name(Path(name)), (None, None), name)

    def test_the_folder_of_one_video_says_nothing(self):
        info = media(1920, 1080, name="BD/ipzz00020.mkv")
        self.assertEqual(media_module.kind_of(info), (WEB_DL, None))  # the folder "BD" doesn't make it a Remux
        self.assertIsNone(media_module.kind_of(media(1920, 1080, name="to rip/ipzz00020.mkv"))[1])
        self.assertIsNotNone(media_module.kind_of(info, "ABC-123 BD Remux")[1])  # a folder of parts names the torrent

    def test_unreadable_subfolder_is_reported_not_fatal(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / "ok").mkdir()
        (root / "ok" / "abc00123.mkv").write_bytes(b"x")
        (root / "locked").mkdir()
        real = Path.iterdir

        def iterdir(path):
            if path.name == "locked":
                raise PermissionError(13, "Access is denied")
            return real(path)

        with mock.patch.object(Path, "iterdir", iterdir):
            items = pipeline.collect([root], recursive=True)
        self.assertEqual(sorted((i.target.name, bool(i.problem)) for i in items), [("abc00123.mkv", False), ("locked", True)])

    def test_other_videos_in_a_folder_are_reported(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        for name in ("abc00123.mkv", "mkon00148.avi", "cover.jpg", "notes.txt", "info.nfo", "sub/clip.MOV"):
            (root / name).parent.mkdir(exist_ok=True)
            (root / name).write_bytes(b"x")
        skipped = []
        items = pipeline.collect([root], skipped=skipped)
        self.assertEqual(([i.target.name for i in items], [p.name for p in skipped]), (["abc00123.mkv"], ["mkon00148.avi"]))
        self.assertEqual(pipeline.skipped_note(skipped), "1 file skipped: mkon00148.avi (only .mkv, .mp4 and .wmv are uploaded)")
        skipped = []
        pipeline.collect([root], recursive=True, skipped=skipped)
        self.assertEqual(sorted(p.name for p in skipped), ["clip.MOV", "mkon00148.avi"])
        many = [Path(f"v{n}.avi") for n in range(7)]
        self.assertEqual(pipeline.skipped_note(many), "7 files skipped: v0.avi, v1.avi, v2.avi, v3.avi, v4.avi and 2 more "
                                                      "(only .mkv, .mp4 and .wmv are uploaded)")


class DvdIdSuggestionTest(unittest.TestCase):
    """A file named by its DVD id: its content id is looked up and offered (never used by itself)."""

    def test_names_that_look_like_dvd_ids(self):
        cases = {"GVH-882": "GVH-882", "IPZZ-020": "IPZZ-020", "ABP123": "ABP-123", "gvh-882-C": "GVH-882", "IPZZ-020 4K": "IPZZ-020",
                 "ssis_001": "SSIS-001", "ABP-123A": "ABP-123", "abc00123": "ABC-00123",
                 "IPZZ-020 2023-04-07 1080p DMM WEB-DL H.264 AAC": "IPZZ-020"}  # the tool's own "Torrent Title" name
        for name, dvd_id in cases.items():
            self.assertEqual(pipeline.dvd_id_in_name(name), dvd_id, name)
        for name in ("My video", "1sdde00300", "h_1133gone00123", "video", "A-1", "ABP-1234567", "[FHD] ABP-123"):
            self.assertIsNone(pipeline.dvd_id_in_name(name), name)

    def suggest(self, name, by_dvd_id=None, digital=None, products=None, folder_files=()):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        video = root / name
        video.parent.mkdir(parents=True, exist_ok=True)
        for path in (video, *(video.parent / f for f in folder_files)):
            path.write_bytes(b"x")
        with mock.patch.object(r18, "lookup_dvd_id", side_effect=lambda d: (by_dvd_id or {}).get(d)) as r18_lookup, \
                mock.patch.object(dmm_api, "digital_id", side_effect=lambda c: (digital or {}).get(c)), \
                mock.patch.object(dmm_api, "lookup", side_effect=lambda c: (products or {}).get(c)):
            found = pipeline.suggest_content_id(pipeline.Item(video, [video]), pipeline.Ui())
        return found, [c.args[0] for c in r18_lookup.call_args_list]

    def test_r18_then_the_digital_listing(self):
        found, _ = self.suggest("GVH-882.mp4", {"GVH-882": r18.Video("gvh00882", "", "2026-10-03", "t")}, {"gvh00882": "gvh00882"})
        self.assertEqual((found.cid, found.others, found.source), ("gvh00882", [], "r18.dev"))
        found, _ = self.suggest("IPZZ-020.mkv", {"IPZZ-020": r18.Video("ipzz020", "", "2023-04-11", "t")}, {"ipzz020": "ipzz00020"})
        self.assertEqual((found.cid, found.others, found.source), ("ipzz00020", ["ipzz020"], "r18.dev and DMM"))
        self.assertIn("other listing: ipzz020", found.text())

    def test_dmm_only_with_the_same_dvd_id(self):
        products = {"gvh00882": dmm_api.Product("gvh00882", "GVH-882", "2026-10-03")}
        found, _ = self.suggest("GVH-882.mp4", digital={"gvh882": "gvh00882"}, products=products)
        self.assertEqual((found.cid, found.source), ("gvh00882", "DMM"))
        other = {"gvh00882": dmm_api.Product("gvh00882", "GVH-0882", "2026-10-03")}  # never another padding
        self.assertIsNone(self.suggest("GVH-882.mp4", digital={"gvh882": "gvh00882"}, products=other)[0])
        self.assertIsNone(self.suggest("GVH-882.mp4")[0])

    def test_the_folder_name_counts_only_for_its_one_video(self):
        by_dvd_id = {"GVH-882": r18.Video("gvh00882", "", "2026-10-03", "t")}
        found, asked = self.suggest("GVH-882/video.mp4", by_dvd_id)
        self.assertEqual((found.cid, found.where, asked), ("gvh00882", "folder name", ["GVH-882"]))
        found, asked = self.suggest("GVH-882/video.mp4", by_dvd_id, folder_files=("other.mkv",))
        self.assertEqual((found, asked), (None, []))

    def test_r18_unreachable_keeps_the_question(self):
        with mock.patch.object(r18, "lookup_dvd_id", side_effect=r18.R18Error("r18.dev is busy")), \
                mock.patch.object(dmm_api, "digital_id") as digital:
            item = pipeline.Item(Path("GVH-882.mp4"), [Path("GVH-882.mp4")])
            self.assertIsNone(pipeline.suggest_content_id(item, pipeline.Ui()))
        digital.assert_not_called()

    def test_r18_and_dmm_answers(self):
        answer = requests.Response()
        answer.status_code, answer._content = 200, b'{"content_id": "GVH00882", "release_date": "2026-10-03", "title": "t"}'
        missing = requests.Response()
        missing.status_code, missing._content = 404, b"<!doctype html><title>404 Not Found</title>"
        session = mock.Mock()
        session.get.side_effect = [answer, missing]
        self.assertEqual(r18.lookup_dvd_id("GVH-882", session), r18.Video("gvh00882", "", "2026-10-03", "t"))
        self.assertIsNone(r18.lookup_dvd_id("ZZZZ-999", session))
        self.assertEqual(session.get.call_args_list[0].args[0], "https://r18.dev/videos/vod/movies/detail/-/dvd_id=GVH-882/json")
        self.assertEqual(dmm_api.parse_digital({"data": {"ppvContentSummaryByExternalId": {"id": "ipzz00020"}}}), "ipzz00020")
        self.assertIsNone(dmm_api.parse_digital({"data": {"ppvContentSummaryByExternalId": None}}))

    def test_maker_and_length_from_both_r18_answers(self):
        # dvd_id= (as answered live for KIL-033) and combined= name them differently.
        by_dvd_id = {"content_id": "118kil033", "release_date": "2014-03-01", "title": "Another sample title", "maker": {"name": "Other Maker"},
                     "runtime_minutes": 115, "label": {"name": "Sample Label"}}
        self.assertEqual(r18._video(by_dvd_id), r18.Video("118kil033", "", "2014-03-01", "Another sample title", "Other Maker", 115))
        combined = {"content_id": "gvh00882", "dvd_id": None, "release_date": "2026-10-03", "title_en": "Sample title", "maker_name_en": "Sample Maker",
                    "runtime_mins": 121}
        self.assertEqual(r18._video(combined), r18.Video("gvh00882", "", "2026-10-03", "Sample title", "Sample Maker", 121))
        payload = {"data": {"ppvContent": {"id": "gvh00882", "makerContentId": "GVH-882", "deliveryStartDate": "2026-10-02T15:00:00Z",
                                           "title": "見本の題名", "duration": 7260, "maker": {"name": "見本メーカー"}}}}
        self.assertEqual(dmm_api.parse(payload), dmm_api.Product("gvh00882", "GVH-882", "2026-10-03", "見本の題名", "見本メーカー", 121))

    def test_cover_from_both_r18_answers(self):
        # IPZZ-020 as r18.dev answered on 2026-11-18 (the fields that matter): combined= has the small vertical cover
        # (jacket_thumb_url) and the large one (jacket_full_url); dvd_id= only the large one (images.jacket_image.large2).
        combined = {"content_id": "ipzz00020", "dvd_id": None, "release_date": "2023-04-11", "title_en": "t",
                    "jacket_full_url": "https://pics.dmm.co.jp/digital/video/ipzz00020/ipzz00020pl.jpg",
                    "jacket_thumb_url": "https://pics.dmm.co.jp/digital/video/ipzz00020/ipzz00020ps.jpg"}
        self.assertEqual(r18._video(combined).cover, "https://pics.dmm.co.jp/digital/video/ipzz00020/ipzz00020ps.jpg")
        by_dvd_id = {"content_id": "ipzz020", "release_date": "2023-04-11", "title": "t",
                     "images": {"jacket_image": {"large": " ", "large2": "https://pics.dmm.co.jp/mono/movie/adult/ipzz020/ipzz020pl.jpg"}}}
        self.assertEqual(r18._video(by_dvd_id).cover, "https://pics.dmm.co.jp/mono/movie/adult/ipzz020/ipzz020ps.jpg")
        # Only the large one, or none.
        self.assertEqual(r18._video({**combined, "jacket_thumb_url": None}).cover, "https://pics.dmm.co.jp/digital/video/ipzz00020/ipzz00020ps.jpg")
        self.assertEqual(r18._video({"content_id": "abc00123", "images": {"jacket_image": {"large": " "}}}).cover, "")
        self.assertEqual(r18._video({"content_id": "abc00123", "images": None}).cover, "")

    def test_suggestion_shows_which_video_it_is(self):
        # KIL-033 on r18.dev is another maker's 118kil033: the member sees that before using it.
        found, _ = self.suggest("KIL-033 [1080p].mp4", {"KIL-033": r18.Video("118kil033", "", "2014-03-01", "Another sample title " * 6,
                                                                              "Other Maker", 115)})
        self.assertEqual((found.cid, found.maker, found.release_date, found.runtime), ("118kil033", "Other Maker", "2014-03-01", 115))
        text = found.text()
        self.assertTrue(text.startswith('the file name looks like the DVD id KIL-033, and r18.dev gives the content id 118kil033, '
                                        'which is "Another sample title Another'), text)
        self.assertTrue(text.endswith('…" (Other Maker, released 2014-03-01, 115 min): check it is your video'), text)
        self.assertLess(len(found.about()), 140)  # long titles are cut
        # DMM's own answer (title in Japanese) when r18.dev doesn't know the DVD id.
        products = {"gvh00882": dmm_api.Product("gvh00882", "GVH-882", "2026-10-03", "見本の題名", "見本メーカー", 121)}
        found, _ = self.suggest("GVH-882.mp4", digital={"gvh882": "gvh00882"}, products=products)
        self.assertEqual(found.about(), '"見本の題名" (見本メーカー, released 2026-10-03, 121 min)')
        # Nothing known about it: as before.
        found, _ = self.suggest("GVH-882.mp4", {"GVH-882": r18.Video("gvh00882", "", "", "")})
        self.assertTrue(found.text().endswith("the content id gvh00882 (check it)"), found.text())


class Answers(pipeline.Ui):
    """A member in the terminal who answers the questions in turn."""

    interactive = True

    def __init__(self, *answers):
        self.answers, self.asked, self.steps = list(answers), [], []

    def step(self, text):
        self.steps.append(text)

    def confirm(self, question):
        self.asked.append(question)
        return self.answers.pop(0) == "y"

    def ask(self, question):
        self.asked.append(question)
        return self.answers.pop(0)


class OtherContentIdTest(unittest.TestCase):
    """r18.dev's combined= also matches DVD ids and other paddings: an answer for another content id is offered as a
    suggestion, its DVD id and date never used by themselves."""

    ABP = r18.Video("118abp00123", "ABP-123", "2010-01-01", "Sample title", "Sample Maker", 120)

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.videos = {"abp123": self.ABP, "118abp00123": self.ABP, "xyz00012": r18.Video("xyz012", "XYZ-012", "2001-02-03", "t")}
        self.products = {}
        for patcher in (mock.patch.object(r18, "lookup", side_effect=lambda cid, *a, **k: self.videos.get(cid)),
                        mock.patch.object(dmm_api, "lookup", side_effect=lambda cid: self.products.get(cid)),
                        mock.patch.object(r18, "lookup_dvd_id", return_value=None), mock.patch.object(dmm_api, "digital_id", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = mock.Mock()
        self.client.torrents_of.return_value = []

    def item(self, name, file_id=None):
        path = self.dir / name
        path.write_bytes(b"x")
        info = Media(path, "", 1920, 1080, 6000.0, "H.264", "AAC", file_id, None)
        return pipeline.collect([path])[0], info

    def test_another_content_id_is_only_suggested(self):
        item, info = self.item("abp123.mkv")
        with self.assertRaises(pipeline.NeedsContentId) as caught:
            pipeline.prepare(item, self.client, Path("mediainfo"), pipeline.Ui(), infos=[info])
        s = caught.exception.suggestion
        self.assertEqual((s.cid, s.dvd_id, s.source, s.where, s.others, s.title, s.maker, s.release_date, s.runtime),
                         ("118abp00123", "ABP-123", "r18.dev", "content id abp123", [], "Sample title", "Sample Maker", "2010-01-01", 120))
        self.assertEqual(str(caught.exception), "r18.dev and DMM don't know abp123: the content id abp123 looks like the DVD id ABP-123, and "
                                                'r18.dev gives the content id 118abp00123, which is "Sample title" (Sample Maker, released '
                                                "2010-01-01, 120 min): check it is your video")
        self.assertEqual(caught.exception.infos, [info])
        self.client.torrents_of.assert_not_called()  # nothing is checked under an id that may be wrong
        # Confirmed by the member: that video, with its DVD id and date.
        plan = pipeline.prepare(item, self.client, Path("mediainfo"), pipeline.Ui(), cid="118abp00123", infos=[info])
        self.assertEqual((plan.cid, plan.known, plan.dvd_id, plan.release_date, plan.warnings), ("118abp00123", True, "ABP-123", "2010-01-01", []))
        self.assertEqual(plan.title(pipeline.Options()), "ABP-123 2010-01-01 1080p DMM WEB-DL H.264 AAC")

    def test_in_the_terminal_the_suggestion_is_the_default_answer(self):
        item, info = self.item("abp123.mkv")
        done = lambda plan, *args: pipeline.Result(plan.item.target, True, "ready", plan.title(pipeline.Options()))
        for answer, title in (("", "ABP-123 2010-01-01 1080p DMM WEB-DL H.264 AAC"), ("abp123", None)):
            ui = Answers(answer)
            with mock.patch.object(media_module, "read", return_value=info), mock.patch.object(pipeline, "execute", side_effect=done):
                result = pipeline.process(item, None, None, Path("mediainfo"), pipeline.Options(dry_run=True), ui)
            self.assertTrue(ui.asked[0].endswith("check it is your video (your file: 100 min). Content id [118abp00123]: "), ui.asked[0])
            self.assertEqual(result.title if result.ok else None, title)  # typed again: still not taken as that video

    def test_an_id_dmm_has_itself_is_unknown_on_r18(self):
        # DMM's own id (written in the file): r18.dev answered for its DVD listing xyz012, which may be another video.
        item, info = self.item("download.mkv", file_id="xyz00012")
        self.products["xyz00012"] = dmm_api.Product("xyz00012", "XYZ-012", "2001-02-04")
        plan = pipeline.prepare(item, self.client, Path("mediainfo"), pipeline.Ui(), infos=[info])
        self.assertEqual((plan.cid, plan.known, plan.r18_id, plan.cover, plan.dvd_id, plan.release_date, plan.dvd_from),
                         ("xyz00012", False, "", "", "XYZ-012", "2001-02-04", "DMM"))
        self.assertEqual(plan.questions(pipeline.Options()), ["unknown"])  # confirmed by the member, as for any id r18.dev lacks
        self.client.torrents_of.assert_called_once_with("xyz00012")


class ReleaseDateTest(unittest.TestCase):
    """r18.dev knows the video but has no release date: DMM's date, else the member's, else a note on the card."""

    def prepare(self, cid, video, product=None):
        info = media(1920, 1080, name=f"{cid}.mkv")
        item = pipeline.Item(info.path, [info.path])
        with mock.patch.object(r18, "lookup", return_value=video), mock.patch.object(dmm_api, "lookup", return_value=product) as dmm:
            plan = pipeline.prepare(item, None, Path("mediainfo"), pipeline.Ui(), infos=[info])
        return plan, dmm

    def test_dmm_gives_the_date(self):
        plan, dmm = self.prepare("abc00123", r18.Video("abc00123", "ABC-123", "", "t"), dmm_api.Product("abc00123", "ABC-123", "2024-01-03"))
        dmm.assert_called_once_with("abc00123")  # asked although r18.dev has the DVD id
        self.assertEqual((plan.release_date, plan.dvd_id, plan.dvd_from, plan.notes()), ("2024-01-03", "ABC-123", "r18.dev", []))
        self.assertEqual(plan.title(pipeline.Options()), "ABC-123 2024-01-03 1080p DMM WEB-DL H.264 AAC")
        plan, dmm = self.prepare("abc00123", r18.Video("abc00123", "ABC-123", "2024-01-02", "t"))
        dmm.assert_not_called()  # r18.dev has both: DMM isn't asked, as before

    def test_no_date_anywhere_is_asked_in_the_terminal(self):
        note = "r18.dev and DMM have no release date for abc00123: the title goes without one"
        plan, _ = self.prepare("abc00123", r18.Video("abc00123", "ABC-123", "", "t"))
        self.assertEqual((plan.release_date, plan.questions(pipeline.Options()), plan.notes()), ("", [], [note]))
        ui = Answers("2024-01-02")
        pipeline.resolve_in_terminal(plan, pipeline.Options(), ui)
        self.assertEqual(ui.asked, ["No release date for abc00123 on r18.dev or DMM. Release date [YYYY-MM-DD, empty: none]: "])
        self.assertEqual((plan.title(pipeline.Options()), plan.notes()), ("ABC-123 2024-01-02 1080p DMM WEB-DL H.264 AAC", []))
        # Nothing typed, or nobody to ask: without a date, with the note (the browser page shows it on the card).
        for ui in (Answers(""), Steps()):
            plan, _ = self.prepare("abc00123", r18.Video("abc00123", "ABC-123", "", "t"))
            pipeline.resolve_in_terminal(plan, pipeline.Options(), ui)
            self.assertEqual((plan.title(pipeline.Options()), plan.notes()), ("ABC-123 1080p DMM WEB-DL H.264 AAC", [note]))
        plan, _ = self.prepare("abc00123", r18.Video("abc00123", "ABC-123", "", "t"))
        with self.assertRaises(pipeline.Skip) as caught:
            pipeline.resolve_in_terminal(plan, pipeline.Options(), Answers("2024-1-2"))
        self.assertEqual(str(caught.exception), "the release date is written YYYY-MM-DD")

    def test_fc2_titles_have_no_date(self):
        plan, _ = self.prepare("fc2ppv1234567", r18.Video("fc2ppv1234567", "FC2-1234567", "", "t"), dmm_api.Product("fc2ppv1234567", "", "2024-01-03"))
        ui = Answers()
        pipeline.resolve_in_terminal(plan, pipeline.Options(), ui)
        self.assertEqual((plan.release_date, plan.notes(), ui.asked), ("", [], []))
        # Told by the DVD id alone (FC2-...), also when r18.dev gives it.
        plan, _ = self.prepare("ppv01234567", r18.Video("ppv01234567", "FC2-PPV-1234567", "", "t"), dmm_api.Product("ppv01234567", "", "2024-01-03"))
        self.assertEqual((plan.release_date, plan.notes()), ("", []))


class PartsTest(unittest.TestCase):
    """A folder of parts is one video cut in pieces: numbered 1, 2, 3 ... and alike."""

    @staticmethod
    def part(number, width=3840, height=2160, codec="VP9", fps=30.0, part=None):
        return Media(Path(f"kil00032-pt{number}.mp4"), "", width, height, 100.0, codec, "AAC", "kil00032", part, frame_rate=fps)

    def test_missing_and_doubled_parts(self):
        cases = {
            (1, 3): "part 2 is missing (the folder has parts 1 and 3)",
            (2, 3): "part 1 is missing (the folder has parts 2 and 3)",
            (1, 4): "parts 2 and 3 are missing (the folder has parts 1 and 4)",
            (1, 2, 5): "parts 3 and 4 are missing (the folder has parts 1, 2 and 5)",
        }
        for numbers, message in cases.items():
            self.assertEqual(pipeline.numbering_problem([self.part(n) for n in numbers]), message, numbers)
        self.assertIsNone(pipeline.numbering_problem([self.part(n) for n in (2, 1, 3)]))
        # DMM's part number in the file wins over the name: two files are then part 2.
        doubled = [self.part(1), self.part(2), self.part(3, part=2)]
        self.assertEqual(pipeline.numbering_problem(doubled),
                         "two files are part 2 (kil00032-pt2.mp4 and kil00032-pt3.mp4): keep only the right one")

    def test_parts_that_dont_match(self):
        # A 4K part 1 and a 1080p part 2 are not one 2160p upload.
        mixed = [self.part(1), self.part(2, 1920, 1080)]
        self.assertEqual(pipeline.parts_mismatch(mixed, [WEB_DL, WEB_DL]), "the parts don't match: part 2 is 1920×1080, part 1 is 3840×2160")
        self.assertEqual(pipeline.parts_mismatch(mixed[::-1], [WEB_DL, WEB_DL]), "the parts don't match: part 2 is 1920×1080, part 1 is 3840×2160")
        # Most parts agree: the odd one is named, even when it is part 1.
        odd_first = [self.part(1, 1920, 1080), self.part(2), self.part(3)]
        self.assertEqual(pipeline.parts_mismatch(odd_first, [WEB_DL] * 3), "the parts don't match: part 1 is 1920×1080 (the others: 3840×2160)")
        self.assertEqual(pipeline.parts_mismatch([self.part(1), self.part(2, codec="H.264")], [WEB_DL] * 2),
                         "the parts don't match: part 2 is H.264 video, part 1 is VP9 video")
        self.assertEqual(pipeline.parts_mismatch([self.part(1), self.part(2)], [WEB_DL, REMUX]),
                         "the parts don't match: part 2 is a Remux, part 1 is a WEB-DL")
        self.assertEqual(pipeline.parts_mismatch([self.part(1, fps=29.97), self.part(2, fps=30.0)], [WEB_DL] * 2),
                         "the parts don't match: part 2 is 30 fps, part 1 is 29.97 fps")

    def test_the_same_parts_written_differently_match(self):
        same = [self.part(1, fps=29.97), self.part(2, fps=30000 / 1001), self.part(3, fps=0.0)]  # 0: variable or unknown
        self.assertIsNone(pipeline.parts_mismatch(same, [WEB_DL] * 3))

    def test_a_folder_mixing_parts_of_different_videos(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        folder = root / "kil00032"
        folder.mkdir()
        for name in ("kil00032-pt1.mp4", "brg00011-pt2.mp4"):
            (folder / name).write_bytes(b"x")
        items = pipeline.collect([folder])
        self.assertEqual(sorted(i.target.name for i in items), ["brg00011-pt2.mp4", "kil00032-pt1.mp4"])
        for item in items:
            self.assertEqual(item.problem, "this folder mixes parts of different videos (brg00011, kil00032): give each video its own folder")
        # Parts of one video next to another whole video: the usual "put its parts in a folder".
        (folder / "brg00011-pt2.mp4").rename(folder / "brg00011.mp4")
        (folder / "kil00032-pt2.mp4").write_bytes(b"x")
        problems = {i.target.name: i.problem for i in pipeline.collect([folder])}
        self.assertEqual(problems, {"brg00011.mp4": "", "kil00032-pt1.mp4": pipeline.PART_PROBLEM, "kil00032-pt2.mp4": pipeline.PART_PROBLEM})


class FinalRefusalTest(unittest.TestCase):
    """Refusals that checking the same files again can't change (the page offers no "Check again" for them), and the
    folder read again for the ones the member can fix."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="aio final "))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def refusal(self, item, infos=None):
        """(message, final) of prepare's refusal; `infos` stand for MediaInfo's reading."""
        with self.assertRaises(pipeline.Skip) as caught:
            pipeline.prepare(item, None, Path("mediainfo"), pipeline.Ui(), infos=infos)
        return str(caught.exception), caught.exception.final

    def parts(self, name, numbers):
        folder = self.dir / name
        folder.mkdir()
        for n in numbers:
            (folder / f"{name}-pt{n}.mkv").write_bytes(b"x")
        items = pipeline.collect([folder])
        self.assertEqual([i.target for i in items], [folder])
        return items[0]

    def test_what_the_files_are_is_final(self):
        # Their names decide: an ISO, another kind of video, a part on its own (alone, or found in a folder).
        loose = self.dir / "loose"
        loose.mkdir()
        for name in ("ABP-123.iso", "kil00032-pt1.mp4"):
            (loose / name).write_bytes(b"x")
        singles = ("ABP-124.iso", "mkon00148.avi", "abc00123-pt2.mkv")
        for name in singles:
            (self.dir / name).write_bytes(b"x")
        items = pipeline.collect([loose]) + [pipeline.collect([self.dir / name])[0] for name in singles]
        self.assertEqual(len(items), 5)
        for item in items:
            self.assertTrue(item.problem and item.final, item.target.name)
            self.assertEqual(self.refusal(item), (item.problem, True))
        # What MediaInfo reads: codecs the site doesn't take, a rip's name, a re-encoded "Remux".
        video = self.dir / "abc00124.mkv"
        video.write_bytes(b"x")
        one = pipeline.collect([video])[0]
        self.assertEqual((one.problem, one.final), ("", False))
        for info in (media(1920, 1080, vcodec="MPEG-4 Visual"), media(1920, 1080, acodec="MP3"),
                     media(1920, 1080, name="abc00124 webrip.mkv"), media(720, 480, "MPEG-2", "AC-3", encoder="x264 core 164")):
            message, final = self.refusal(one, [info])
            self.assertTrue(final, message)
        # DMM's own part number in a single file, a Remux or H.264 video in parts: another upload is the way out.
        tagged = Media(video, "", 1920, 1080, 60.0, "H.264", "AAC", "abc00124", 1)
        self.assertTrue(self.refusal(one, [tagged])[1])
        item = self.parts("abc00125", (1, 2))
        remux = [Media(p, "", 720, 480, 60.0, "MPEG-2", "AC-3", None, None) for p in item.files]
        self.assertEqual(self.refusal(item, remux), ("a Remux in several files: upload Remuxes one file at a time", True))
        h264 = [Media(p, "", 1920, 1080, 60.0, "H.264", "AAC", None, None) for p in item.files]
        message, final = self.refusal(item, h264)
        self.assertTrue(final and message.startswith("several H.264 parts"), message)

    def test_what_the_member_can_fix_is_not_final(self):
        # A missing part, an odd part, parts of different videos, another file in the folder, a place that isn't there.
        item = self.parts("abc00123", (1, 3))
        vp9 = [Media(p, "", 3840, 2160, 60.0, "VP9", "AAC", None, None) for p in item.files]
        self.assertEqual(self.refusal(item, vp9), ("part 2 is missing (the folder has parts 1 and 3)", False))
        pair = self.parts("abc00126", (1, 2))
        odd = [Media(pair.files[0], "", 3840, 2160, 60.0, "VP9", "AAC", None, None),
               Media(pair.files[1], "", 1920, 1080, 60.0, "VP9", "AAC", None, None)]
        self.assertEqual(self.refusal(pair, odd), ("the parts don't match: part 2 is 1920×1080, part 1 is 3840×2160", False))
        others = [Media(p, "", 3840, 2160, 60.0, "VP9", "AAC", cid, None) for p, cid in zip(pair.files, ("abc00126", "xyz00001"))]
        self.assertEqual(self.refusal(pair, others), ("the files belong to different videos (abc00126, xyz00001)", False))
        (item.target / "notes.doc").write_bytes(b"x")
        crowded = pipeline.collect([item.target])[0]
        self.assertIn("also holds notes.doc", crowded.problem)
        self.assertEqual(self.refusal(crowded), (crowded.problem, False))
        gone = pipeline.collect([self.dir / "missing.mkv"])[0]
        self.assertEqual(self.refusal(gone), ("not found", False))

    def test_check_again_reads_the_folder_again(self):
        item = self.parts("abc00123", (1, 2))
        (item.target / "notes.doc").write_bytes(b"x")
        crowded = pipeline.collect([item.target])[0]
        self.assertIn("also holds notes.doc", crowded.problem)
        (item.target / "notes.doc").unlink()
        fixed = pipeline.reread(crowded)
        self.assertEqual((fixed.target, fixed.files, fixed.problem), (item.target, item.files, ""))
        (item.target / "abc00123-pt3.mkv").write_bytes(b"x")  # a part added: the files are read again too
        self.assertEqual([p.name for p in pipeline.reread(fixed).files], ["abc00123-pt1.mkv", "abc00123-pt2.mkv", "abc00123-pt3.mkv"])
        # The place no longer holds this one upload (the parts were joined): the old one stays, with its reason.
        for path in list(item.target.iterdir()):
            path.unlink()
        (item.target / "abc00123.mkv").write_bytes(b"x")
        self.assertIs(pipeline.reread(crowded), crowded)
        later = pipeline.collect([self.dir / "later.mkv"])[0]  # not there yet, then there
        self.assertEqual(later.problem, "not found")
        (self.dir / "later.mkv").write_bytes(b"x")
        again = pipeline.reread(later)
        self.assertEqual((again.problem, again.files), ("", [later.target]))


SITE_TORRENT = bencode({"announce": "https://clearjav.example/announce/PASSKEY",
                        "info": {"name": "x", "length": 1, "piece length": 16384, "pieces": b"0" * 20, "private": 1}})


class StandInSite:
    """ClearJAV's API for execute(): `refuse` is raised by the upload, `broken` by the torrent download."""

    site = "https://clearjav.example"

    def __init__(self, refuse=None, broken=None):
        self.refuse, self.broken = refuse, broken
        self.sent = []  # (fields, contact sheet names) of each upload

    def upload(self, fields, torrent, sheets):
        self.sent.append((fields, [sheet.name for sheet in sheets]))
        if self.refuse:
            raise self.refuse
        return f"{self.site}/torrent/download/77.RSSKEY", "Torrent uploaded successfully."

    def download_torrent(self, link):
        if self.broken:
            raise self.broken
        return SITE_TORRENT


class Steps(pipeline.Ui):
    def __init__(self):
        self.steps = []

    def step(self, text):
        self.steps.append(text)


class ExecuteTest(unittest.TestCase):
    """execute() with FFmpeg and the site stood in for: which file each contact sheet is made from, and the names
    the files have afterwards."""

    TITLE = "ABC-123 2024-01-02 2160p DMM WEB-DL VP9 AAC"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="aio execute "))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.sheets = []  # (sheet, video) in the order they were made
        self.on_sheet = None  # called with the video before its sheet is made (may raise)

        def make_sheet(video, output, *args, **kwargs):
            if self.on_sheet:
                self.on_sheet(video)
            self.sheets.append((output.name, video.name))
            output.write_bytes(b"RIFF")
            return output

        def make_torrent(target, output, **kwargs):
            output.write_bytes(bencode({"info": {"name": target.name}}))
            return output

        for patcher in (mock.patch.object(contact_sheet, "make", side_effect=make_sheet),
                        mock.patch.object(torrent_module, "create", side_effect=make_torrent)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def plan(self, item, tagged=False):
        """A checked upload of the files: 4K VP9 parts of abc00123 (DMM's part number inside when `tagged`)."""
        infos = [Media(path, f"General\nComplete name : {path.name}\n", 3840, 2160, 60.0, "VP9", "AAC",
                       "abc00123" if tagged else None, pipeline.content_id_from_name(path)[1] if tagged else None)
                 for path in item.files]
        return pipeline.Plan(item, infos, "abc00123", WEB_DL, "DMM", dvd_id="ABC-123", dvd_from="r18.dev", release_date="2024-01-02")

    def parts(self, count):
        folder = self.dir / "abc00123"
        folder.mkdir()
        for n in range(1, count + 1):
            (folder / f"abc00123-pt{n}.mkv").write_bytes(b"x")
        items = pipeline.collect([folder])
        self.assertEqual(len(items), 1)
        return items[0]

    def test_ten_parts_in_their_order(self):
        # By name the folder lists pt1, pt10, pt2 ...: sheet N and the MediaInfo's part N come from part N, also once
        # the parts are renamed to the title (those names don't tell the part any more).
        for tagged in (False, True):
            with self.subTest(tagged=tagged):
                self.sheets = []
                item = self.parts(10)
                self.assertEqual(item.files[1].name, "abc00123-pt10.mkv")
                site, ui = StandInSite(), Steps()
                result = pipeline.execute(self.plan(item, tagged), site, None, pipeline.Options(rename="title"), ui)
                self.assertTrue(result.ok, result.message)
                names = [f"{self.TITLE}-pt{n}.mkv" for n in range(1, 11)]
                self.assertEqual(self.sheets, [(f"abc00123-pt{n}_s.webp", name) for n, name in enumerate(names, 1)])
                fields, sheets = site.sent[0]
                self.assertEqual(sheets, [f"abc00123-pt{n}_s.webp" for n in range(1, 11)])
                self.assertEqual(re.findall(r"^Complete name : (.+)$", fields["mediainfo"], re.MULTILINE), names)
                self.assertEqual([s for s in ui.steps if s.startswith("making the contact sheet")],
                                 [f"making the contact sheet (part {n}/10)" for n in range(1, 11)])
                shutil.rmtree(self.dir / self.TITLE)
                shutil.rmtree(self.dir / "clearjav-uploads")

    def test_names_are_put_back_when_nothing_was_uploaded(self):
        # Renamed to the DVD id, then the contact sheet or the upload fails: the folder and the parts get their names
        # back (ABC-123-pt1.mkv can't be read as a part), and the plan is as it was before.
        def broken_sheet(video):
            raise contact_sheet.SheetError("no picture")

        for on_sheet, site in ((broken_sheet, StandInSite()), (None, StandInSite(refuse=api.ApiError("upload refused: Validation Error."))),
                               (None, StandInSite(refuse=KeyboardInterrupt()))):
            with self.subTest(site=site.refuse):
                self.on_sheet = on_sheet
                item = self.parts(2)
                plan = self.plan(item)
                texts = [info.text for info in plan.infos]
                ui = Steps()
                with self.assertRaises((contact_sheet.SheetError, api.ApiError, KeyboardInterrupt)):
                    pipeline.execute(plan, site, None, pipeline.Options(rename="dvd_id"), ui)
                self.assertIn("renamed abc00123 -> ABC-123", ui.steps)
                self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["abc00123", "clearjav-uploads"])
                self.assertEqual(sorted(p.name for p in (self.dir / "abc00123").iterdir()), ["abc00123-pt1.mkv", "abc00123-pt2.mkv"])
                self.assertEqual(ui.steps[-3:], ["renamed back ABC-123 -> abc00123", "renamed back ABC-123-pt2.mkv -> abc00123-pt2.mkv",
                                                 "renamed back ABC-123-pt1.mkv -> abc00123-pt1.mkv"])
                self.assertEqual((plan.item, [info.path for info in plan.infos], [info.text for info in plan.infos]),
                                 (item, item.files, texts))
                again = pipeline.collect([self.dir / "abc00123"])  # one upload again, as before
                self.assertEqual([(i.target, i.files, i.problem) for i in again], [(item.target, item.files, "")])
                shutil.rmtree(self.dir / "abc00123")

    def test_uploaded_without_the_torrent_to_seed(self):
        # The upload went through, then the site's .torrent can't be fetched or saved: still uploaded (not "NOT
        # UPLOADED"), with the page and where to get the .torrent; the video keeps its new name (the torrent has it).
        page = "https://clearjav.example/torrents/77"
        for broken, why in ((api.ApiError("can't download the torrent: ConnectionError"), r"fetched \(can't download the torrent: ConnectionError\)"),
                            (None, r"saved \(.+\)")):
            with self.subTest(why=why):
                video = self.dir / "My video.mkv"
                video.write_bytes(b"video")
                out = self.dir / "clearjav-uploads"
                if broken is None:  # a folder where the .torrent should go: it can't be written
                    (out / f"{self.TITLE}.torrent").mkdir(parents=True)
                item = pipeline.collect([video])[0]
                plan = self.plan(item)
                opts = pipeline.Options(clean_up=True, seed="qbittorrent", qbittorrent=("http://127.0.0.1:1", "", "", ""))
                with mock.patch.object(pipeline, "prepare", return_value=plan), mock.patch("clearjav_aio.qbittorrent.add") as add, \
                        self.assertLogs("clearjav_aio.pipeline", "WARNING"):
                    result = pipeline.process(item, StandInSite(broken=broken), None, Path("mediainfo"), opts, Steps())
                self.assertEqual((result.ok, result.message, result.page, result.seed_torrent),
                                 (True, "Torrent uploaded successfully.", page, None))
                self.assertEqual(len(result.warnings), 1)
                self.assertRegex(result.warnings[0], rf"^uploaded, but the \.torrent to seed couldn't be {why}: download it from {re.escape(page)}$")
                add.assert_not_called()
                self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.is_file()), ["abc00123.mkv"])
                # The sheets and MediaInfo deleted as set (and the tool's own folder once empty), no .torrent.
                left = sorted(p.name for p in out.iterdir()) if out.exists() else []
                self.assertEqual(left, [] if broken else [f"{self.TITLE}.torrent"])
                shutil.rmtree(out, ignore_errors=True)
                (self.dir / "abc00123.mkv").unlink()
        self.assertEqual(pipeline.seed_torrent_problem(api.ApiError("x"), None),
                         "uploaded, but the .torrent to seed couldn't be fetched (x): download it from your uploads on ClearJAV")

    def test_relative_paths(self):
        # "upload ." inside a folder of parts, "upload abc00123.mkv" next to the video: absolute from the start, so
        # the folder isn't "the top of a drive" and qBittorrent gets a real folder, never ".".
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        item = self.parts(2)
        os.chdir(item.target)
        self.assertEqual([(i.target, i.files, i.problem) for i in pipeline.collect([Path(".")])], [(item.target, item.files, "")])
        self.assertEqual(pipeline.collect([Path("..") / "abc00123"])[0].target, item.target)
        os.chdir(self.dir)
        (self.dir / "xyz00001.mkv").write_bytes(b"x")
        video = pipeline.collect([Path("xyz00001.mkv")])[0]
        self.assertEqual((video.target, video.files), (self.dir / "xyz00001.mkv", [self.dir / "xyz00001.mkv"]))
        opts = pipeline.Options(seed="qbittorrent", qbittorrent=("http://127.0.0.1:1", "", "", ""))
        with mock.patch("clearjav_aio.qbittorrent.add") as add:
            result = pipeline.execute(self.plan(video), StandInSite(), None, opts, Steps())
        self.assertTrue(result.ok, result.message)
        self.assertEqual(add.call_args.args[4], self.dir)
        self.assertTrue(add.call_args.args[4].is_absolute())

    def test_a_missing_release_date_is_noted(self):
        video = self.dir / "abc00123.mkv"
        video.write_bytes(b"x")
        plan = self.plan(pipeline.collect([video])[0])
        plan.release_date = ""
        result = pipeline.execute(plan, None, None, pipeline.Options(dry_run=True), Steps())
        self.assertEqual(result.title, "ABC-123 2160p DMM WEB-DL VP9 AAC")
        self.assertIn("r18.dev and DMM have no release date for abc00123: the title goes without one", result.warnings)

    def test_an_upload_that_waits_for_staff_says_so(self):
        # The site answers "Torrent uploaded successfully." also when the upload goes to the moderation queue.
        video = self.dir / "abc00123.mkv"
        for waits, message in ((False, "Torrent uploaded successfully."), (True, "Uploaded: waits for staff approval")):
            with self.subTest(waits=waits):
                video.write_bytes(b"x")
                plan = self.plan(pipeline.collect([video])[0])
                result = pipeline.execute(plan, StandInSite(), None, pipeline.Options(waits_for_staff=waits), Steps())
                self.assertEqual((result.ok, result.message, result.waits_for_staff, result.page),
                                 (True, message, waits, "https://clearjav.example/torrents/77"))
                shutil.rmtree(self.dir / "clearjav-uploads")
        # A test run uploads nothing, so nothing waits.
        result = pipeline.execute(plan, None, None, pipeline.Options(dry_run=True, waits_for_staff=True), Steps())
        self.assertEqual((result.ok, result.waits_for_staff), (True, False))
        self.assertTrue(result.message.startswith("ready (not uploaded)"), result.message)

    def test_only_what_is_still_there_is_put_back(self):
        video = self.dir / "My video.mkv"
        video.write_bytes(b"video")
        item = pipeline.collect([video])[0]

        def meanwhile(_):  # another file now has the old name: never overwritten
            video.write_bytes(b"another file")
            raise contact_sheet.SheetError("no picture")

        self.on_sheet = meanwhile
        plan, ui = self.plan(item), Steps()
        with self.assertRaises(contact_sheet.SheetError), self.assertLogs("clearjav_aio.pipeline", "WARNING"):
            pipeline.execute(plan, StandInSite(), None, pipeline.Options(), ui)
        self.assertEqual(((self.dir / "abc00123.mkv").read_bytes(), video.read_bytes()), (b"video", b"another file"))
        self.assertEqual(ui.steps[-1], "could not rename back (rename them yourself): abc00123.mkv (was My video.mkv)")
        self.assertEqual(plan.item.files, [self.dir / "abc00123.mkv"])  # the names the files have now

        def gone(_):  # the member moved the renamed file away: nothing to put back
            (self.dir / "abc00123.mkv").rename(self.dir / "moved.mkv")
            raise contact_sheet.SheetError("no picture")

        video.unlink()
        (self.dir / "moved.mkv").unlink(missing_ok=True)
        (self.dir / "abc00123.mkv").rename(video)
        self.on_sheet = gone
        plan, ui = self.plan(pipeline.collect([video])[0]), Steps()
        with self.assertRaises(contact_sheet.SheetError), self.assertLogs("clearjav_aio.pipeline", "WARNING"):
            pipeline.execute(plan, StandInSite(), None, pipeline.Options(), ui)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.is_file()), ["moved.mkv"])
        self.assertEqual(ui.steps[-1], "could not rename back (rename them yourself): abc00123.mkv (was My video.mkv)")


class UnreadableFileTest(unittest.TestCase):
    """What MediaInfo makes of files that aren't videos (any more): said plainly, not "has no video track"."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "bkd00370.mp4"
        self.path.write_bytes(b"\x00" * 100)

    def read(self, general, *tracks):
        answer = {"media": {"track": [{"@type": "General", **general}, *tracks]}}
        with mock.patch.object(media_module, "_run", side_effect=[__import__("json").dumps(answer), "Complete name : x\n"]) as run:
            try:
                return media_module.read(self.path, Path("mediainfo")), run
            except media_module.MediaError as exc:
                return str(exc), run

    def test_empty_file(self):
        self.path.write_bytes(b"")
        with mock.patch.object(media_module, "_run") as run:
            with self.assertRaises(media_module.MediaError) as caught:
                media_module.read(self.path, Path("mediainfo"))
        self.assertEqual(str(caught.exception), "bkd00370.mp4 is empty (0 bytes)")
        run.assert_not_called()

    def test_not_a_video_or_cut_off(self):
        unreadable = "bkd00370.mp4 can't be read as a video: damaged, cut off while copying, or not a video file"
        self.assertEqual(self.read({"FileSize": "100"})[0], unreadable)  # random bytes: MediaInfo knows no format
        self.assertEqual(self.read({"Format": "MPEG Audio"}, {"@type": "Audio", "Format": "MPEG Audio"})[0], unreadable)  # an .mp3 renamed
        # An .mp4 cut off before its index (moov at the end): MediaInfo knows the container but finds no track.
        cut = {"Format": "MPEG-4", "extra": {"IsTruncated": "Yes"}}
        self.assertEqual(self.read(cut)[0], unreadable + " (MediaInfo reports IsTruncated=Yes)")

    def test_real_container_without_video_and_damaged_video(self):
        self.assertEqual(self.read({"Format": "MPEG-4"}, {"@type": "Audio", "Format": "AAC"})[0], "bkd00370.mp4 has no video track")
        video = {"@type": "Video", "Format": "AVC", "Width": "1920", "Height": "1080"}
        self.assertEqual(self.read({"Format": "MPEG-4", "extra": {"IsTruncated": "Yes"}}, video)[0],
                         "bkd00370.mp4 looks damaged: MediaInfo reports IsTruncated=Yes")

    def test_frame_rate(self):
        for video, fps in (({"FrameRate": "29.970", "FrameRate_Num": "30000", "FrameRate_Den": "1001"}, 30000 / 1001),
                           ({"FrameRate": "30.000"}, 30.0), ({"FrameRate": "29.970", "FrameRate_Mode": "VFR"}, 0.0), ({}, 0.0)):
            info, _ = self.read({"Format": "MPEG-4"}, {"@type": "Video", "Format": "VP9", "Width": "3840", "Height": "2160", **video})
            self.assertAlmostEqual(info.frame_rate, fps, msg=video)


REAL_CONFIG_DIR = config.config_dir


@unittest.skipIf(config.secrets_in_file() or importlib.util.find_spec("keyring") is None, "keyring is used only on Windows and macOS")
class SecretStoreTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(config, "_store", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stores_that_keep_nothing_or_plain_files_are_not_used(self):
        from keyring.backends import fail, null

        for backend in (null.Keyring(), fail.Keyring()):
            config._store = None
            with mock.patch("keyring.get_keyring", return_value=backend):
                self.assertFalse(config.store_available(), backend)
                self.assertFalse(config.set_secret("api-key", "x"))
                self.assertTrue(config.store_problem())

    def test_switched_off(self):
        with mock.patch.dict(os.environ, {"CLEARJAV_AIO_NO_KEYRING": "1", "CLEARJAV_API_KEY": "from-env"}):
            self.assertFalse(config.store_available())
            self.assertEqual(config.get_secret("api-key"), "from-env")

    def test_a_store_that_hangs_or_forgets(self):
        import threading
        import time as time_module

        hang = threading.Event()

        class Slow:
            priority = 5

            def get_password(self, service, name):
                hang.wait(5)

        config._store = None
        with mock.patch("keyring.get_keyring", return_value=Slow()), mock.patch.object(config, "STORE_TIMEOUT", 0.2):
            started = time_module.monotonic()
            self.assertEqual(config.get_secret("api-key"), "")
            self.assertLess(time_module.monotonic() - started, 2)
            self.assertFalse(config.store_available())  # not asked again this run
            self.assertIn("didn't answer", config.store_problem())
        hang.set()

        class Forgetful:
            priority = 5

            def set_password(self, service, name, value):
                pass

            def get_password(self, service, name):
                return None

        config._store = None
        with mock.patch("keyring.get_keyring", return_value=Forgetful()):
            self.assertFalse(config.set_secret("api-key", "x"))
            self.assertIn("didn't keep", config.store_problem())


class SecretsFileTest(unittest.TestCase):
    """Linux (and other systems): the secrets in secrets.ini, readable only by this account; keyring is never used."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.folder = self.dir / "clearjav-aio"
        self.path = self.folder / "secrets.ini"
        self.imported = []
        real_import = builtins.__import__

        def watched_import(name, *args, **kwargs):
            if name == "keyring" or name.startswith("keyring."):
                self.imported.append(name)
            return real_import(name, *args, **kwargs)

        for patcher in (mock.patch.object(sys, "platform", "linux"), mock.patch.object(config, "_store", None),
                        mock.patch.object(config, "config_dir", return_value=self.folder), mock.patch.dict(os.environ),
                        mock.patch.object(builtins, "__import__", watched_import)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in (*config.SECRET_ENV.values(), config.NO_STORE_ENV):
            os.environ.pop(name, None)

    def tearDown(self):
        self.assertEqual(self.imported, [], "keyring must not be imported on Linux")

    def test_saved_read_and_deleted(self):
        self.assertTrue(config.secrets_in_file())
        self.assertTrue(config.store_available())
        self.assertEqual(config.store_problem(), "")
        self.assertEqual(config.secrets_path(), self.path)
        self.assertEqual(config.get_secret(config.API_KEY), "")  # no file yet
        self.assertTrue(config.set_secret(config.API_KEY, "key-1"))
        self.assertTrue(config.set_secret(config.QBIT_PASSWORD, "p%w ;#x"))  # no interpolation, no inline comments
        self.assertTrue(config.set_secret(config.API_KEY, "key-2"))
        self.assertEqual((config.get_secret(config.API_KEY), config.get_secret(config.QBIT_PASSWORD)), ("key-2", "p%w ;#x"))
        self.assertEqual(config.secret_source(config.API_KEY), "store")
        self.assertTrue(config.delete_secret(config.API_KEY))
        self.assertFalse(config.delete_secret(config.API_KEY))  # not saved any more
        self.assertFalse(config.delete_secret(config.ANNOUNCE_URL))
        self.assertTrue(self.path.is_file())  # the file stays, with the other secret
        self.assertEqual((config.get_secret(config.API_KEY), config.secret_source(config.API_KEY)), ("", ""))
        self.assertEqual(config.get_secret(config.QBIT_PASSWORD), "p%w ;#x")
        self.assertEqual([p.name for p in self.folder.iterdir()], ["secrets.ini"])  # no temporary files left

    def test_environment_wins(self):
        self.assertTrue(config.set_secret(config.API_KEY, "from-file"))
        with mock.patch.dict(os.environ, {"CLEARJAV_API_KEY": "from-env"}):
            self.assertEqual(config.get_secret(config.API_KEY), "from-env")
            self.assertEqual(config.secret_source(config.API_KEY), "environment")
        self.assertEqual(config.get_secret(config.API_KEY), "from-file")

    def test_one_line_per_secret(self):
        self.assertTrue(config.set_secret(config.ANNOUNCE_URL, "https://clearjav.net/abc\r\n[other]\napi-key = x\n"))
        self.assertEqual(config.get_secret(config.ANNOUNCE_URL), "https://clearjav.net/abc[other]api-key = x")
        self.assertEqual(config.get_secret(config.API_KEY), "")
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("\r", text)
        self.assertEqual([line for line in text.splitlines() if line and not line.startswith("#")],
                         ["[secrets]", "announce-url = https://clearjav.net/abc[other]api-key = x"])

    @unittest.skipIf(os.name == "nt", "Windows has no file modes")
    def test_only_this_account_can_read_it(self):
        self.assertTrue(config.set_secret(config.API_KEY, "k"))
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.path.chmod(0o644)  # e.g. copied by hand
        self.assertTrue(config.set_secret(config.QBIT_PASSWORD, "p"))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_switched_off(self):
        with mock.patch.dict(os.environ, {"CLEARJAV_AIO_NO_KEYRING": "1", "CLEARJAV_API_KEY": "from-env"}):
            self.assertFalse(config.store_available())
            self.assertIn("CLEARJAV_AIO_NO_KEYRING", config.store_problem())
            self.assertFalse(config.set_secret(config.ANNOUNCE_URL, "x"))
            self.assertEqual(config.get_secret(config.API_KEY), "from-env")
        self.assertFalse(self.folder.exists())

    def test_a_broken_file_is_never_quoted(self):
        self.folder.mkdir()
        self.path.write_text("[secrets]\napi-key = abc\nthis line is broken SECRET123\n", encoding="utf-8")
        with self.assertLogs("clearjav_aio.config", "WARNING") as logged:
            self.assertEqual(config.get_secret(config.API_KEY), "")
            self.assertFalse(config.set_secret(config.API_KEY, "new"))
        self.assertIn("secrets.ini can't be read", config.store_problem())
        self.assertNotIn("SECRET123", " ".join(logged.output) + config.store_problem())
        self.assertIn("SECRET123", self.path.read_text(encoding="utf-8"))  # left as it was

    def test_store_name_shows_the_file(self):
        with mock.patch.object(Path, "home", return_value=self.dir):
            self.assertEqual(config.store_name(), "the file ~/clearjav-aio/secrets.ini (only you can read it)")

    @unittest.skipIf(os.name == "nt", "Windows keeps the settings in %APPDATA%")
    def test_folder_follows_xdg_config_home(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.dir / "xdg")}), mock.patch.object(config, "config_dir", REAL_CONFIG_DIR):
            self.assertEqual(config.secrets_path(), self.dir / "xdg" / "clearjav-aio" / "secrets.ini")


class ToolsTest(unittest.TestCase):
    def test_missing_programs_say_where_to_download_them(self):
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, empty, True)
        with mock.patch.dict(os.environ, {"PATH": ""}), mock.patch.object(tools, "program_dir", return_value=empty):
            for name, url in (("mediainfo", tools.MEDIAINFO_URL), ("ffmpeg", tools.FFMPEG_WINDOWS_URL), ("ffprobe", tools.FFMPEG_URL)):
                with self.assertRaises(tools.ToolError) as caught:
                    tools.find_tool(name)
                self.assertIn(url, str(caught.exception))
                self.assertNotIn("http", caught.exception.short)  # the page shows its own links


class SettingsTest(unittest.TestCase):
    def test_hand_edited_files(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "config.ini"
        path.write_bytes(b"\xef\xbb\xbf[settings]\nanonymous = yes\nanonymous = no\n")  # BOM, a key twice
        self.assertFalse(config.load(path).anonymous)
        path.write_text("[settings]\nthis line has no equals sign\n", encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.load(path)

    def test_save_keeps_comments(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "config.ini"
        path.write_text(config.TEMPLATE, encoding="utf-8")
        settings = config.load(path)
        self.assertEqual(settings.seeding, "save")
        settings.anonymous, settings.seed_mode, settings.watch_dir, settings.tag = True, "watch", "D:\\Torrents\\watch", "Me"
        config.save(settings, path)
        again = config.load(path)
        self.assertEqual((again.anonymous, again.seeding, again.watch_dir, again.tag), (True, "watch", "D:\\Torrents\\watch", "Me"))
        text = path.read_text(encoding="utf-8")
        self.assertIn("# Upload options: yes or no.", text)
        self.assertEqual(text.count("anonymous ="), 1)
        self.assertEqual(config.Settings(qbittorrent_url="http://127.0.0.1:8080").seeding, "qbittorrent")

    def test_theme_and_covers(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / "config.ini"
        path.write_text("[settings]\nanonymous = no\n", encoding="utf-8")  # a settings file from before the theme and covers settings
        settings = config.load(path)
        self.assertEqual((settings.theme, settings.covers), ("system", True))  # follow the computer, covers on
        settings.theme, settings.covers = "dark", False
        config.save(settings, path)
        self.assertEqual((config.load(path).theme, config.load(path).covers), ("dark", False))
        self.assertIn("theme = dark\ncovers = no", path.read_text(encoding="utf-8"))
        path.write_text("[settings]\ntheme = Light\n", encoding="utf-8")
        self.assertEqual(config.load(path).theme, "light")
        path.write_text("[settings]\ntheme = blue\ncovers = yes\n", encoding="utf-8")  # typed by hand
        self.assertEqual((config.load(path).theme, config.load(path).covers), ("system", True))
        path.write_text(config.TEMPLATE, encoding="utf-8")
        self.assertEqual((config.load(path).theme, config.load(path).covers), ("system", True))


class DmmApiTest(unittest.TestCase):
    def test_answer_and_japan_date(self):
        payload = {"data": {"ppvContent": {"id": "ipzz00020", "makerContentId": "IPZZ-020", "deliveryStartDate": "2023-04-07T01:00:00Z"}}}
        self.assertEqual(dmm_api.parse(payload), dmm_api.Product("ipzz00020", "IPZZ-020", "2023-04-07"))
        self.assertEqual(dmm_api.japan_date("2026-01-24T15:00:00Z"), "2026-01-25")  # midnight in Japan
        self.assertIsNone(dmm_api.parse({"data": {"ppvContent": None}}))
        self.assertIsNone(dmm_api.parse({"errors": [{"message": "x"}]}))


class RedactTest(unittest.TestCase):
    def test_secrets_hidden(self):
        text = logs.redact("Authorization: Bearer abc123 https://clearjav.com/torrent/download/77.RSSKEY99 api_key=zz http://u:pw@h "
                           "https://clearjav.net/0123456789abcdef0123456789abcdef https://clearjav.com/announce/fedcba9876543210 "
                           "'https://clearjav.net/aaaabbbbccccdddd1111'")
        for secret in ("abc123", "RSSKEY99", "zz", "pw@", "0123456789abcdef", "fedcba9876543210", "aaaabbbbccccdddd1111"):
            self.assertNotIn(secret, text)
        kept = "https://r18.dev/videos/vod/movies/detail/-/id=abc00123/"
        self.assertEqual(logs.redact(kept), kept)


if __name__ == "__main__":
    unittest.main()


class LostAnswerTest(unittest.TestCase):
    """A connection that breaks off while the answer is read is sent again, like a dropped one."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.torrent = Path(folder.name) / "x.torrent"
        self.torrent.write_bytes(b"d4:infod4:name1:xee")
        self.client = api.Client("https://clearjav.example", "key")
        self.addCleanup(self.client.close)
        sleep = mock.patch.object(api.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        no_wait = mock.patch.object(api, "MIN_INTERVAL", 0)
        no_wait.start()
        self.addCleanup(no_wait.stop)

    @staticmethod
    def answer(status, payload=None, content=b""):
        response = requests.Response()
        response.status_code = status
        response._content = content or (b"" if payload is None else __import__("json").dumps(payload).encode())
        return response

    def test_upload(self):
        broken = requests.exceptions.ChunkedEncodingError("Connection broken: ConnectionResetError(104)")
        ok = self.answer(200, {"success": True, "data": "https://clearjav.example/torrent/download/5.key", "message": "Torrent already uploaded."})
        with mock.patch.object(self.client.http, "post", side_effect=[broken, ok]) as post:
            link, message = self.client.upload({"name": "x"}, self.torrent, [])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(message, "Torrent already uploaded.")
        self.assertTrue(link.endswith("/5.key"))

    def test_torrent_download(self):
        broken = requests.exceptions.ChunkedEncodingError("Connection broken")
        ok = self.answer(200, content=b"d4:infod4:name1:xee")
        with mock.patch.object(self.client.http, "get", side_effect=[broken, ok]) as get:
            self.assertEqual(self.client.download_torrent("https://clearjav.example/torrent/download/5.key"), b"d4:infod4:name1:xee")
        self.assertEqual(get.call_count, 2)

    def test_cloudflare_errors_are_sent_again(self):
        # Cloudflare's 520, 522 and 524: the site may have made the torrent; a resend gets it back.
        ok = self.answer(200, {"success": True, "data": "https://clearjav.example/torrent/download/5.key", "message": "Torrent already uploaded."})
        for status in (520, 522, 524):
            timeout = self.answer(status, content=b"<html>A timeout occurred</html>")
            with mock.patch.object(self.client.http, "post", side_effect=[timeout, ok]) as post, self.assertLogs("clearjav_aio.api", "WARNING"):
                self.assertEqual(self.client.upload({"name": "x"}, self.torrent, [])[1], "Torrent already uploaded.")
            self.assertEqual(post.call_count, 2, status)
        torrent = self.answer(200, content=b"d4:infod4:name1:xee")
        with mock.patch.object(self.client.http, "get", side_effect=[self.answer(524), torrent]) as get:
            self.assertEqual(self.client.download_torrent("https://clearjav.example/torrent/download/5.key"), b"d4:infod4:name1:xee")
        self.assertEqual(get.call_count, 2)

    def test_a_resend_that_meets_its_first_try_waits_for_its_link(self):
        # The first try's answer was lost while the site was still storing it: the resend meets its title or files.
        # That is this upload, so the tool waits and sends again until the site answers with the link.
        taken = self.answer(404, {"success": False, "message": "Validation Error.", "data": {"name": ["The name has already been taken."]}})
        same = self.answer(404, {"success": False, "message": "Validation Error.",
                                 "data": "A torrent with the same files has already been uploaded and is pending moderation."})
        ok = self.answer(200, {"success": True, "data": "https://clearjav.example/torrent/download/5.key", "message": "Torrent already uploaded."})
        with mock.patch.object(self.client.http, "post", side_effect=[requests.ConnectionError("reset"), taken, same, ok]) as post,                 self.assertLogs("clearjav_aio.api", "WARNING") as logged:
            link, message = self.client.upload({"name": "x"}, self.torrent, [])
        self.assertEqual((post.call_count, link, message), (4, "https://clearjav.example/torrent/download/5.key", "Torrent already uploaded."))
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], list(api.RESEND_DELAYS))
        self.assertIn("upload try 2 meets the first try (Validation Error. name: The name has already been taken.); sending again in 60 s",
                      logged.output[1])
        # The link never comes: the upload may have gone through; never "someone else has this title".
        with mock.patch.object(self.client.http, "post", side_effect=[self.answer(524), taken, same, taken]) as post,                 self.assertLogs("clearjav_aio.api", "WARNING"):
            with self.assertRaises(api.UnconfirmedUpload) as caught:
                self.client.upload({"name": "x"}, self.torrent, [])
        self.assertEqual(post.call_count, 4)
        self.assertTrue(str(caught.exception).startswith("the upload may have gone through: an earlier try reached ClearJAV"), caught.exception)
        with self.assertRaises(api.UnconfirmedUpload):
            pipeline.send(StandInSite(refuse=caught.exception), {}, self.torrent, [])
        # A first try that meets the title: someone's torrent the duplicate check can't see, as before.
        with mock.patch.object(self.client.http, "post", side_effect=[taken]) as post:
            with self.assertRaises(api.ApiError) as caught:
                self.client.upload({"name": "x"}, self.torrent, [])
        self.assertEqual((post.call_count, type(caught.exception), str(caught.exception)),
                         (1, api.ApiError, "upload refused: Validation Error. name: The name has already been taken."))
        with self.assertRaises(pipeline.Skip) as skipped:
            pipeline.send(StandInSite(refuse=caught.exception), {}, self.torrent, [])
        self.assertEqual(str(skipped.exception), pipeline.same_title_problem())
