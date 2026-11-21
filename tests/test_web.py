import http.client
import io
import re
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import requests

from clearjav_aio import api, config, covers, dmm_api, media, pipeline, r18, tools, web

from .helpers import MEDIAINFO, TOOLS, FakeSite, make_clip

KNOWN = {"abc00123": r18.Video("abc00123", "ABC-123", "2024-01-02", "t"), "gvh00882": r18.Video("gvh00882", "GVH-882", "2026-10-03", "t")}
DMM = {"zzz00001": dmm_api.Product("zzz00001", "ZZZ-001", "2025-05-05")}
BY_DVD_ID = {"GVH-882": r18.Video("gvh00882", "", "2026-10-03", "Sample title", "Sample Maker", 121)}
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + bytes(200)
COVER = "https://pics.dmm.co.jp/digital/video/abc00123/abc00123ps.jpg"


def picture(body=JPEG, kind="image/jpeg", status=200, length=None):
    """An answer of DMM's image server."""
    answer = requests.Response()
    answer.status_code = status
    answer.headers["Content-Type"] = kind
    if length is not None:
        answer.headers["Content-Length"] = str(length)
    answer.raw = io.BytesIO(body)
    return answer


class WebTest(unittest.TestCase):
    """The browser page's server: who may use it, and a queue from adding to uploading."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="aio web "))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.secrets = {"api-key": "good-key"}
        patches = [
            mock.patch.object(r18, "lookup", side_effect=lambda cid, *a, **k: KNOWN.get(cid)),
            mock.patch.object(dmm_api, "lookup", side_effect=lambda cid: DMM.get(cid)),
            mock.patch.object(r18, "lookup_dvd_id", side_effect=lambda dvd_id, *a, **k: BY_DVD_ID.get(dvd_id)),
            mock.patch.object(dmm_api, "digital_id", side_effect=lambda cid: cid if cid in KNOWN else None),
            mock.patch.object(config, "config_dir", return_value=self.dir / "config"),
            mock.patch.object(config, "log_dir", return_value=self.dir / "logs"),
            mock.patch.object(config, "get_secret", side_effect=lambda name, env=None: self.secrets.get(name, "")),
            mock.patch.object(config, "set_secret", side_effect=lambda name, value: self.secrets.__setitem__(name, value) or True),
            mock.patch.object(config, "delete_secret", side_effect=lambda name: self.secrets.pop(name, None) is not None),
            mock.patch.object(config, "store_available", return_value=True),
            mock.patch.object(api.time, "sleep"),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        api.MIN_INTERVAL = 0
        self.site = FakeSite(flags={"allowed": True, "skips_moderation": True, "internal_options": False, "featured": False})
        self.addCleanup(self.site.close)
        settings = config.Settings(site=self.site.url)
        self.server = web.make_server(settings, port=0)
        self.server.app.startup()
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        self.addCleanup(self.stop_server)
        self.base = f"http://127.0.0.1:{self.server.port}"
        self.http = requests.Session()
        self.addCleanup(self.http.close)

    def stop_server(self):
        self.server.app.quit.set()
        self.server.shutdown()
        self.server.server_close()

    def sign_in(self):
        answer = self.http.get(f"{self.base}/?key={self.server.token}", allow_redirects=False)
        self.assertEqual(answer.status_code, 303)
        self.assertIn("HttpOnly; SameSite=Strict", answer.headers["Set-Cookie"])

    def post(self, path, body=None, expect=200):
        answer = self.http.post(f"{self.base}{path}", json=body or {}, headers={"X-ClearJAV-AIO": "1"})
        self.assertEqual(answer.status_code, expect, answer.text)
        return answer.json()

    def state(self):
        return self.http.get(f"{self.base}/api/state").json()

    def wait_for(self, check, seconds=60):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            state = self.state()
            if check(state):
                return state
            time.sleep(0.2)
        self.fail(f"timed out: {[(j['name'], j['state'], j['message']) for j in state['jobs']]}")

    def test_only_the_opened_browser_gets_in(self):
        self.assertEqual(self.http.get(f"{self.base}/").status_code, 403)
        self.assertEqual(self.http.get(f"{self.base}/api/state").status_code, 403)
        self.assertEqual(self.http.get(f"{self.base}/?key=wrong").status_code, 403)
        self.sign_in()
        self.assertEqual(self.http.get(f"{self.base}/").status_code, 200)
        page = self.http.get(f"{self.base}/app.js")
        self.assertEqual(page.status_code, 200)
        self.assertIn("script-src 'self'", page.headers["Content-Security-Policy"])
        self.assertEqual(self.state()["user"], {"name": "member", "group": "User"})
        # Another site's page (DNS rebinding: another host name) and forms without our header are refused.
        self.assertEqual(self.http.get(f"{self.base}/api/state", headers={"Host": f"evil.test:{self.server.port}"}).status_code, 403)
        self.assertEqual(self.http.get(f"{self.base}/api/state", headers={"Host": "localhost:9000", "Cookie": f"{self.server.cookie}={self.server.token}"}).status_code, 200)  # SSH tunnel
        raw = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)  # a broken length (requests fixes it)
        raw.putrequest("POST", "/api/clear")
        for name, value in (("X-ClearJAV-AIO", "1"), ("Content-Type", "application/json"), ("Content-Length", "x"),
                            ("Cookie", f"{self.server.cookie}={self.server.token}")):
            raw.putheader(name, value)
        raw.endheaders()
        self.assertEqual(raw.getresponse().status, 400)
        raw.close()
        self.assertEqual(self.http.post(f"{self.base}/api/clear", json={}).status_code, 403)
        self.assertEqual(self.http.post(f"{self.base}/api/clear", data="x", headers={"X-ClearJAV-AIO": "1"}).status_code, 415)
        self.assertNotIn("good-key", self.http.get(f"{self.base}/api/state").text)

    def test_the_page_links_to_the_program_downloads(self):
        self.sign_in()
        script = self.http.get(f"{self.base}/app.js").text
        for url in (tools.FFMPEG_WINDOWS_URL, tools.FFMPEG_URL, tools.MEDIAINFO_URL):  # the same pages as the terminal's hints
            self.assertIn(f'"{url}"', script)
        self.assertIn('target="_blank" rel="noopener noreferrer"', script)
        self.assertIn('id="toolHelp"', self.http.get(f"{self.base}/").text)
        settings = self.state()["settings"]
        self.assertEqual((settings["store_name"], settings["store_problem"]), (config.store_name(), ""))

    def test_settings_are_checked_and_saved(self):
        self.sign_in()
        self.assertIn("refused", self.post("/api/key", {"key": "wrong-key"}, 409)["error"])
        self.assertEqual(self.secrets["api-key"], "good-key")
        self.assertIn("announce URL", self.post("/api/announce", {"url": "https://example.test/announce/x"}, 409)["error"])
        self.post("/api/announce", {"url": f"{self.site.url}/announce/0123456789abcdef0123456789abcdef"})
        self.assertTrue(self.state()["settings"]["has_announce"])
        self.post("/api/seeding", {"seed_mode": "watch", "watch_dir": str(self.dir / "watch")})
        self.assertEqual(config.load().seeding, "watch")
        self.post("/api/options", {"anonymous": True, "tag": "Me", "dry_run": True})
        self.assertTrue(config.load().anonymous)
        self.assertIn("tag", self.post("/api/options", {"tag": "bad/tag"}, 409)["error"])
        self.post("/api/options", {"internal": True, "dry_run": False})
        error = self.post("/api/start", {}, 409)["error"]
        self.assertTrue("internal" in error or "missing" in error, error)  # the account may not; or no FFmpeg here

    @unittest.skipUnless(TOOLS and MEDIAINFO, "ffmpeg/mediainfo not installed")
    def test_queue_from_adding_to_uploading(self):
        self.sign_in()
        folder = self.dir / "videos"
        folder.mkdir()
        make_clip(folder / "abc00123.mkv", seconds=4, size="1280x720")
        make_clip(folder / "zzz00001.mkv", seconds=4)
        (folder / "ABP-123.iso").write_bytes(b"x")
        (folder / "mkon00148.avi").write_bytes(b"x")  # not uploaded, but not left out silently
        self.assertEqual(self.post("/api/add", {"paths": [str(folder)]}),
                         {"added": 3, "skipped": "1 file skipped: mkon00148.avi (only .mkv, .mp4 and .wmv are uploaded)"})
        state = self.wait_for(lambda s: all(j["state"] not in ("checking",) for j in s["jobs"]))
        jobs = {j["name"]: j for j in state["jobs"]}
        self.assertEqual((jobs["ABP-123.iso"]["state"], jobs["ABP-123.iso"]["cid_problem"]), ("bad", False))  # no "Wrong CID?"
        self.assertEqual((jobs["abc00123.mkv"]["state"], jobs["abc00123.mkv"]["title"]), ("ready", "ABC-123 2024-01-02 720p DMM WEB-DL H.264 AAC"))
        unknown = jobs["zzz00001.mkv"]
        # Unknown on r18.dev and not on ClearJAV: refused (the site would need a title and a cover); another CID can be tried ...
        self.assertEqual((unknown["state"], unknown["cid_problem"]), ("bad", True))
        self.assertIn("ClearJAV has no torrent of it", unknown["message"])
        # ... and a test run says the same: it reports what a real upload would meet.
        self.post("/api/options", {"dry_run": True})
        unknown = next(j for j in self.state()["jobs"] if j["name"] == "zzz00001.mkv")
        self.assertEqual((unknown["state"], unknown["questions"]), ("bad", []))
        self.assertEqual(self.post("/api/start")["started"], 1)
        state = self.wait_for(lambda s: not s["running"] and all(j["state"] in ("test", "bad") for j in s["jobs"]))
        self.assertEqual(self.site.uploads, [])

        self.post("/api/options", {"dry_run": False})
        for job in state["jobs"]:
            if job["state"] == "test":
                self.post("/api/answer", {"id": job["id"], "action": "retry"})
        self.wait_for(lambda s: sorted(j["state"] for j in s["jobs"]) == ["bad", "bad", "ready"])
        self.assertEqual(self.post("/api/start")["started"], 1)
        state = self.wait_for(lambda s: not s["running"] and [j["state"] for j in s["jobs"]].count("done") == 1)
        self.assertEqual(len(self.site.uploads), 1)
        done = next(j for j in state["jobs"] if j["name"] == "abc00123.mkv")
        self.assertEqual(done["page"], f"{self.site.url}/torrents/77")
        self.assertTrue(Path(done["seed"]).is_file())
        self.post("/api/clear")
        self.assertEqual([j["state"] for j in self.state()["jobs"]], [])

    @unittest.skipUnless(TOOLS and MEDIAINFO, "ffmpeg/mediainfo not installed")
    def test_dvd_id_name_and_torrents_already_there(self):
        self.sign_in()
        make_clip(self.dir / "GVH-882.mp4", seconds=4)
        self.post("/api/add", {"paths": [str(self.dir / "GVH-882.mp4")]})
        job = self.wait_for(lambda s: s["jobs"][0]["state"] != "checking")["jobs"][0]
        # The content id of the DVD id is filled in as a suggestion, never used by itself.
        self.assertEqual((job["state"], job["questions"], job["cid_suggestion"], job["cid_dvd_id"], job["cid_source"]),
                         ("ask", ["content_id"], "gvh00882", "GVH-882", "r18.dev"))
        # With what r18.dev says about that video (another maker may use the DVD id) and the file's length.
        self.assertEqual((job["cid_title"], job["cid_maker"], job["cid_date"], job["cid_runtime"]), ("Sample title", "Sample Maker", "2026-10-03", 121))
        self.assertAlmostEqual(job["file_duration"], 4, delta=0.5)
        self.site.existing = [{"name": "GVH-882 2026-10-03 2160p DMM WEB-DL H.264 AAC", "type": "WEB-DL", "resolution": "2160p", "seeders": 1}]
        self.post("/api/answer", {"id": job["id"], "action": "content_id", "content_id": "gvh00882"})
        job = self.wait_for(lambda s: s["jobs"][0]["state"] != "checking")["jobs"][0]
        self.assertEqual((job["state"], job["questions"], job["title"]), ("ask", ["duplicate"], "GVH-882 2026-10-03 SD DMM WEB-DL H.264 AAC"))
        # A test run doesn't ask, but the card still lists them.
        self.post("/api/options", {"dry_run": True})
        job = self.state()["jobs"][0]
        self.assertEqual((job["state"], len(job["existing"]), job["upload_anyway"]), ("ready", 1, False))
        # The same title as a torrent there: refused, in a test run too, until the title differs.
        self.site.existing[0]["name"] = "GVH-882 2026-10-03 SD DMM WEB-DL H.264 AAC"
        self.post("/api/answer", {"id": job["id"], "action": "content_id", "content_id": "gvh00882"})
        job = self.wait_for(lambda s: s["jobs"][0]["state"] != "checking")["jobs"][0]
        self.assertEqual((job["state"], job["cid_problem"]), ("bad", False))
        self.assertIn("exactly this title", job["message"])
        self.post("/api/options", {"personal_release": True, "tag": "Me"})
        self.assertEqual(self.state()["jobs"][0]["state"], "ready")

    def test_every_control_has_a_name(self):
        # Screen readers (and tools that read the page) announced bare checkboxes as "on": every input and select
        # carries its name itself, also the ones the script draws for each video.
        for name in ("index.html", "app.js"):
            text = (web.web_dir() / name).read_text(encoding="utf-8")
            for tag in re.findall(r"<(?:input|select)\b[^>]*>", text):
                self.assertIn("aria-label=", tag, f"{name}: {tag}")
        self.assertRegex((web.web_dir() / "app.js").read_text(encoding="utf-8"), r'class="itemrow"[^>]*\n?[^>]*aria-label=')

    def test_page_has_no_inline_style_or_script(self):
        # The page may only use its own files (style-src 'self', script-src 'self'): no style= or on...= attributes,
        # no <style>, no script inside the page.
        page = (web.web_dir() / "index.html").read_text(encoding="utf-8")
        for name, text in (("index.html", page), ("app.js", (web.web_dir() / "app.js").read_text(encoding="utf-8"))):
            self.assertIsNone(re.search(r"<[a-z][^>]*\sstyle\s*=", text, re.I), name)
            self.assertIsNone(re.search(r"<[a-z][^>]*\son[a-z]+\s*=", text, re.I), name)
            self.assertNotIn("<style", text, name)
            self.assertNotIn("javascript:", text, name)
        self.assertEqual(re.findall(r"<script\b([^>]*)>(.*?)</script>", page, re.S), [(' src="/app.js" defer', "")])
        self.sign_in()
        answer = self.http.get(f"{self.base}/")
        self.assertIn("style-src 'self';", answer.headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", answer.headers["Content-Security-Policy"])
        self.assertIn(b'<link rel="icon" href="/favicon.svg" type="image/svg+xml">', answer.content)
        favicon = self.http.get(f"{self.base}/favicon.svg")
        self.assertEqual(favicon.headers["Content-Type"], "image/svg+xml")
        self.assertIn(b'fill="#5ec4fa"', favicon.content)  # the seal (tests/test_packaging.py compares its paths)

    def test_parts_show_the_whole_duration(self):
        infos = [media.Media(Path(f"abc00123-pt{n}.mkv"), "", 3840, 2160, seconds, "VP9", "AAC", "abc00123", n)
                 for n, seconds in ((1, 5.0), (2, 7.5))]
        item = pipeline.Item(self.dir / "abc00123", [info.path for info in infos])
        plan = pipeline.Plan(item, infos, "abc00123", media.WEB_DL, "DMM", dvd_id="ABC-123", release_date="2024-01-02")
        app = self.server.app
        view = app.job_view(web.Job(99, item, state="prepared", plan=plan), app.options())
        self.assertEqual((view["duration"], view["files"]), (12.5, ["abc00123-pt1.mkv", "abc00123-pt2.mkv"]))

    def test_a_missing_release_date_is_shown_on_the_card(self):
        # The page has no date field for a video r18.dev knows: the card says the title goes without one.
        info = media.Media(Path("abc00123.mkv"), "", 1920, 1080, 60.0, "H.264", "AAC", "abc00123", None)
        item = pipeline.Item(self.dir / "abc00123.mkv", [info.path])
        plan = pipeline.Plan(item, [info], "abc00123", media.WEB_DL, "DMM", dvd_id="ABC-123", dvd_from="r18.dev")
        app = self.server.app
        view = app.job_view(web.Job(98, item, state="prepared", plan=plan), app.options())
        self.assertEqual((view["state"], view["questions"], view["title"]), ("ready", [], "ABC-123 1080p DMM WEB-DL H.264 AAC"))
        self.assertEqual(view["warnings"], ["r18.dev and DMM have no release date for abc00123: the title goes without one"])

    def test_theme_and_covers_settings(self):
        self.sign_in()
        settings = self.state()["settings"]
        self.assertEqual((settings["theme"], settings["covers"]), ("system", True))  # the defaults
        self.assertIn(b'<html lang="en">', self.http.get(f"{self.base}/").content)  # follows the computer's setting
        self.post("/api/page", {"theme": "light"})
        self.post("/api/page", {"covers": False})
        saved = config.load()
        self.assertEqual((saved.theme, saved.covers), ("light", False))
        settings = self.state()["settings"]
        self.assertEqual((settings["theme"], settings["covers"]), ("light", False))
        # The saved theme is in the page itself, so it shows in the right colors before any script runs.
        self.assertIn(b'<html lang="en" data-theme="light">', self.http.get(f"{self.base}/").content)
        for bad in ({"theme": "blue"}, {"theme": None}, {"theme": "Dark"}, {"covers": "no"}, {"covers": 1}):
            self.assertIn("error", self.post("/api/page", bad, 409), bad)
        self.assertEqual(self.http.post(f"{self.base}/api/page", json={"theme": "dark"}).status_code, 403)  # without our header
        self.post("/api/page", {"theme": "system", "covers": True})
        self.assertEqual((config.load().theme, config.load().covers), ("system", True))

    def add_job(self, cover=COVER, cid="abc00123", state="prepared"):
        """A checked video in the list, as r18.dev's lookup leaves it."""
        info = media.Media(Path(f"{cid}.mkv"), "", 1920, 1080, 60.0, "H.264", "AAC", cid, None)
        item = pipeline.Item(self.dir / f"{cid}.mkv", [info.path])
        plan = pipeline.Plan(item, [info], cid, media.WEB_DL, "DMM", dvd_id="ABC-123", release_date="2024-01-02", cover=cover)
        app = self.server.app
        with app.lock:
            job = web.Job(app.next_id, item, state=state, plan=plan)
            app.next_id += 1
            app.jobs.append(job)
        return job

    def test_covers_come_through_the_uploader(self):
        app = self.server.app
        job = self.add_job()
        url = f"{self.base}/cover/{job.id}?cid=abc00123"
        get = mock.patch.object(app.covers.session, "get", side_effect=lambda *a, **k: picture()).start()
        self.addCleanup(mock.patch.stopall)
        self.assertEqual(self.http.get(url).status_code, 403)  # the cookie, as for every other address
        self.sign_in()
        self.assertEqual(self.http.get(url, headers={"Host": f"evil.test:{self.server.port}"}).status_code, 403)
        get.assert_not_called()
        self.assertTrue(self.state()["jobs"][0]["cover"])
        answer = self.http.get(url)
        self.assertEqual((answer.status_code, answer.headers["Content-Type"], answer.content), (200, "image/jpeg", JPEG))
        self.assertIn("img-src 'self' data:;", answer.headers["Content-Security-Policy"])  # unchanged
        self.assertEqual(answer.headers["Cache-Control"], "private, max-age=86400")
        self.assertEqual(self.http.get(url).content, JPEG)
        self.assertEqual(get.call_count, 1)  # kept: DMM is asked once
        self.assertEqual(get.call_args.args[0], COVER)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertIn("clearjav-aio/", app.covers.session.headers["User-Agent"])
        # Not in the list, or no cover from r18.dev: nothing to show, nothing asked.
        self.assertEqual(self.http.get(f"{self.base}/cover/999").status_code, 404)
        none = self.add_job(cover="", cid="abc00124")
        self.assertEqual(self.http.get(f"{self.base}/cover/{none.id}").status_code, 404)
        self.assertFalse(next(j for j in self.state()["jobs"] if j["id"] == none.id)["cover"])
        # Only https links to DMM's image servers are fetched.
        for n, link in enumerate(("http://pics.dmm.co.jp/x/abc00125ps.jpg", "https://example.test/abc00125ps.jpg",
                                  "https://pics.dmm.co.jp.example.test/x.jpg", "https://user@pics.dmm.co.jp/x.jpg",
                                  "https://pics.dmm.co.jp:8443/x.jpg")):
            other = self.add_job(cover=link, cid=f"abc0013{n}")
            self.assertEqual(self.http.get(f"{self.base}/cover/{other.id}").status_code, 404, link)
            self.assertFalse(next(j for j in self.state()["jobs"] if j["id"] == other.id)["cover"], link)
        self.assertEqual(get.call_count, 1)
        # DMM's answer must be a picture of at most 2 MB; a redirect (DMM's "now printing") is no cover. Each failure
        # is one line in the log file, and the page's log says nothing.
        lines = len(self.state()["log"])
        with self.assertLogs("clearjav_aio.covers", "WARNING") as logged:
            for n, answer in enumerate((picture(b"<html>", "text/html"), picture(b"<svg/>", "image/svg+xml"),
                                        picture(b"not a jpeg", "image/jpeg"), picture(status=302),
                                        picture(length=covers.MAX_BYTES + 1),
                                        picture(JPEG + bytes(covers.MAX_BYTES), length=None))):
                bad = self.add_job(cover=f"https://pics.dmm.co.jp/digital/video/x/x{n}ps.jpg", cid=f"abc0014{n}")
                get.side_effect = lambda *a, answer=answer, **k: answer
                self.assertEqual(self.http.get(f"{self.base}/cover/{bad.id}").status_code, 404, n)
                self.assertEqual(self.http.get(f"{self.base}/cover/{bad.id}").status_code, 404, n)  # not asked again at once
            get.side_effect = requests.ConnectionError("no network")
            broken = self.add_job(cover="https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/x/xps.jpg", cid="abc00150")
            self.assertEqual(self.http.get(f"{self.base}/cover/{broken.id}").status_code, 404)
        self.assertEqual(get.call_args.args[0], "https://awsimgsrc.dmm.co.jp/pics_dig/digital/video/x/xps.jpg")
        self.assertEqual(len(logged.output), 7)
        self.assertEqual(get.call_count, 8)
        self.assertEqual(len(self.state()["log"]), lines)
        # Covers off: the list has none and nothing is fetched, also not a cover asked for before.
        calls = get.call_count
        self.post("/api/page", {"covers": False})
        self.assertEqual(self.http.get(url).status_code, 404)
        self.assertEqual(get.call_count, calls)
        self.assertFalse(any(j["cover"] for j in self.state()["jobs"]))

    def test_refused_videos_show_no_cover(self):
        self.sign_in()
        job = self.add_job()
        self.site.existing = []
        job.plan.known = False  # unknown on r18.dev and not on ClearJAV: refused
        job.plan.existing = []
        self.assertEqual([(j["state"], j["cover"]) for j in self.state()["jobs"]], [("bad", False)])

    def test_browse_lists_folders_and_videos(self):
        self.sign_in()
        (self.dir / "sub").mkdir()
        (self.dir / "a.mkv").write_bytes(b"1234")
        (self.dir / "notes.txt").write_text("x")
        answer = self.http.get(f"{self.base}/api/browse", params={"path": str(self.dir)}).json()
        self.assertEqual([(e["name"], e["dir"]) for e in answer["entries"] if e["name"] in ("sub", "a.mkv", "notes.txt")],
                         [("sub", True), ("a.mkv", False)])
        self.assertEqual(self.http.get(f"{self.base}/api/browse", params={"path": str(self.dir / "missing")}).status_code, 409)

    def test_quit(self):
        self.sign_in()
        self.post("/api/quit")
        self.assertTrue(self.server.app.quit.wait(5))


if __name__ == "__main__":
    unittest.main()
