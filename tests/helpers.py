from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from clearjav_aio import tools
from clearjav_aio.torrent import bencode

try:
    TOOLS = tools.find_tools()
    MEDIAINFO = tools.find_tool("mediainfo")
except tools.ToolError:
    TOOLS = MEDIAINFO = None


def make_clip(path: Path, seconds: float = 20, size: str = "640x360", codec: str = "libx264", comment: dict | None = None, rate: int = 10,
              audio: str = "aac", extra: list[str] | None = None) -> Path:
    encoders = subprocess.run([str(TOOLS.ffmpeg), "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if codec not in encoders:
        codec = "mpeg4"
    cmd = [str(TOOLS.ffmpeg), "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:v", codec, "-pix_fmt", "yuv420p", "-c:a", audio, "-shortest"]
    if codec == "libvpx-vp9":
        cmd += ["-deadline", "realtime", "-cpu-used", "8"]
    if comment:
        cmd += ["-metadata", "comment=" + json.dumps(comment, separators=(",", ":"))]
    cmd += extra or []
    subprocess.run(cmd + [str(path)], check=True)
    return path


SITE_TORRENT = bencode({"announce": "https://clearjav.com/announce/PASSKEY", "info": {"name": "x", "length": 1, "piece length": 16384, "pieces": b"0" * 20, "private": 1}})


class FakeSite:
    """A stand-in for ClearJAV's API. `existing` = torrents the filter returns; `upload_status`
    = statuses to answer in turn (then 200)."""

    def __init__(self, existing=None, upload_status=None, key="good-key", refuse=None, flags=None):
        self.existing = existing or []
        self.upload_status = list(upload_status or [])
        self.refuse = refuse
        self.uploads: list[dict] = []
        self.requests: list[str] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, payload, content_type="application/json"):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def authorised(self):
                if self.headers.get("Authorization") != f"Bearer {key}":
                    self.reply(401, {"message": "Unauthenticated."})
                    return False
                return True

            def do_GET(self):
                site.requests.append(self.path)
                if self.path.startswith("/torrent/download/"):
                    return self.reply(200, SITE_TORRENT, "application/x-bittorrent")
                if not self.authorised():
                    return
                if self.path == "/api/user":
                    answer = {"username": "member", "group": "User"}
                    if flags is not None:
                        answer["upload"] = flags
                    return self.reply(200, answer)
                if self.path.startswith("/api/torrents/filter"):
                    data = [{"type": "torrent", "id": str(i), "attributes": t} for i, t in enumerate(site.existing)]
                    return self.reply(200, {"data": data})
                self.reply(404, {"message": "not found"})

            def do_POST(self):
                site.requests.append(self.path)
                if not self.authorised():
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                if site.upload_status:
                    return self.reply(site.upload_status.pop(0), {"message": "busy"})
                if site.refuse:
                    return self.reply(404, {"success": False, "message": "Validation Error.", "data": site.refuse})
                site.uploads.append({"body": body, "type": self.headers.get("Content-Type")})
                return self.reply(200, {"success": True, "data": f"http://127.0.0.1:{port}/torrent/download/77.RSSKEY123", "message": "Torrent uploaded successfully."})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
