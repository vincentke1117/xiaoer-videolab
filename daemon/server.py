#!/usr/bin/env python3
"""Xiaoer VideoLab daemon — receive a URL from the browser extension, download it via yt-dlp.

Pure Python standard library. No third-party packages. Listens on localhost only.

Configuration is via environment variables (all optional):

    VIDEOLAB_PORT             TCP port to listen on            (default: 7788)
    VIDEOLAB_DOWNLOADS        download directory               (default: ~/Downloads)
    VIDEOLAB_YT_DLP           path to the yt-dlp binary        (default: auto-detect)
    VIDEOLAB_PREFIX           filename prefix                  (default: "" / none)
    VIDEOLAB_MAX_HEIGHT       max video height in pixels       (default: 1080)
    VIDEOLAB_COOKIES_BROWSER  pull cookies from this browser   (default: "" / off)
                              e.g. "chrome", "brave", "firefox", "edge", "safari"
                              — needed for login-gated / private videos.
    VIDEOLAB_APP_NAME         name shown in desktop notifications (default: "Xiaoer VideoLab")
"""

import json
import os
import shlex
import shutil
import datetime
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from typing import Optional

IS_WINDOWS = sys.platform == "win32"


def _detect_yt_dlp() -> str:
    # Prefer a nightly build when present. Fast-moving sites (notably Bilibili)
    # roll out anti-bot changes that stable yt-dlp releases lag behind — the
    # symptom is "HTTP Error 412: Precondition Failed". Nightly ships extractor
    # fixes within days, so auto-detect it ahead of stable. See the FAQ.
    nightly = shutil.which("yt-dlp-nightly") or str(Path.home() / ".local" / "bin" / "yt-dlp-nightly")
    if Path(nightly).is_file():
        return nightly
    found = shutil.which("yt-dlp")
    if found:
        return found
    candidates = ["/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp"]
    if IS_WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        candidates += [
            str(local / "yt-dlp" / "yt-dlp.exe"),
            str(local / "Programs" / "yt-dlp" / "yt-dlp.exe"),
        ]
    for cand in candidates:
        if Path(cand).is_file():
            return cand
    return "yt-dlp"  # last resort; relies on PATH at exec time


PORT = int(os.environ.get("VIDEOLAB_PORT", "7788"))
HOST = "127.0.0.1"
DOWNLOADS = Path(os.environ.get("VIDEOLAB_DOWNLOADS", str(Path.home() / "Downloads")))
YT_DLP = os.environ.get("VIDEOLAB_YT_DLP") or _detect_yt_dlp()
PREFIX = os.environ.get("VIDEOLAB_PREFIX", "")
MAX_HEIGHT = int(os.environ.get("VIDEOLAB_MAX_HEIGHT", "1080"))
COOKIES_BROWSER = os.environ.get("VIDEOLAB_COOKIES_BROWSER", "").strip()
APP_NAME = os.environ.get("VIDEOLAB_APP_NAME", "Xiaoer VideoLab")
if IS_WINDOWS:
    _LOG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "xiaoer-videolab"
else:
    _LOG_DIR = Path.home() / "Library" / "Logs"
LOG_FILE = _LOG_DIR / "xiaoer-videolab.log"
HISTORY_FILE = _LOG_DIR / "xiaoer-videolab-history.jsonl"
_history_lock = threading.Lock()


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(f"{msg}\n")
    print(msg, flush=True)


def notify(title: str, message: str) -> None:
    """Desktop notification. Supports macOS (osascript) and Windows (toast)."""
    safe_title = title.replace('"', "'").replace("'", "''")
    safe_msg = message.replace('"', "'").replace("'", "''").replace("\n", " ")
    try:
        if IS_WINDOWS:
            ps_cmd = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$nodes = $template.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($template.CreateTextNode('{safe_title}')) | Out-Null
$nodes.Item(1).AppendChild($template.CreateTextNode('{safe_msg}')) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Xiaoer VideoLab').Show($toast)
"""
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                check=False, timeout=10,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
        elif shutil.which("osascript"):
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{safe_msg}" with title "{safe_title}"'],
                check=False, timeout=5,
            )
    except Exception as e:
        log(f"notify failed: {e}")


# Track active download processes so they can be cancelled.
_active_downloads: dict[str, subprocess.Popen] = {}
_active_downloads_lock = threading.Lock()


def _simple_hash(url: str) -> str:
    """Short deterministic hash for a URL (used as history entry id)."""
    h = 0
    for c in url:
        h = ((h << 5) - h) + ord(c)
        h &= 0xFFFFFFFF
    return f"dl_{abs(h):08x}"[:16]


def _append_history(entry: dict) -> None:
    """Thread-safe append to the JSONL history file."""
    with _history_lock:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_FILE.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def cancel_download(url: str) -> bool:
    """Kill a running download process by URL. Returns True if a process was killed."""
    with _active_downloads_lock:
        proc = _active_downloads.get(url)
        if proc and proc.poll() is None:
            proc.kill()
            log(f"[cancelled] {url}")
            return True
    return False


_PLATFORM_MAP = [
    ("douyin", "抖音"), ("iesdouyin", "抖音"),
    ("xiaohongshu", "小红书"), ("xhslink", "小红书"), ("xhscdn", "小红书"),
    ("bilibili", "B站"), ("b23.tv", "B站"),
    ("youtube", "YouTube"), ("youtu.be", "YouTube"),
    ("weibo", "微博"), ("zhihu", "知乎"), ("ixigua", "西瓜视频"),
    ("twitter", "X"), ("x.com", "X"), ("vimeo", "Vimeo"),
    ("instagram", "Instagram"), ("tiktok", "TikTok"), ("kuaishou", "快手"),
    ("youku", "优酷"), ("iqiyi", "爱奇艺"), ("facebook", "Facebook"),
    ("reddit", "Reddit"), ("dailymotion", "Dailymotion"),
]


def _platform_name(url: str) -> str:
    """Friendly platform label from a URL host, for filenames (平台_标题_时间)."""
    host = (urlparse(url).hostname or "").lower()
    for key, name in _PLATFORM_MAP:
        if key in host:
            return name
    parts = host.replace("www.", "").split(".")
    return parts[0] if parts and parts[0] else "video"


def download(url: str) -> None:
    log(f"[start] {url}")
    notify(APP_NAME, f"Downloading {url[:80]}")
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    # Filename = 平台_标题_下载日期 (no 小耳 branding — files get shared with others).
    platform = _platform_name(url)
    date = datetime.datetime.now().strftime("%Y%m%d")
    output_tpl = str(DOWNLOADS / f"{platform}_%(title).120s_{date}.%(ext)s")
    fmt = (
        f"bv*[height<={MAX_HEIGHT}][ext=mp4]+ba[ext=m4a]/"
        f"b[height<={MAX_HEIGHT}][ext=mp4]/"
        f"bv*[height<={MAX_HEIGHT}]+ba/"
        f"b[height<={MAX_HEIGHT}]/best"
    )
    cmd = [
        YT_DLP,
        "--no-playlist",
        "--playlist-items", "1",   # if the URL is a profile/playlist, take just one — don't churn
        "--socket-timeout", "30",  # don't hang forever on a stalled connection
        "--no-mtime",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--replace-in-metadata", "title", r"[\\/:*?\"<>|]", "_",
        "--output", output_tpl,
    ]
    if COOKIES_BROWSER:
        cmd += ["--cookies-from-browser", COOKIES_BROWSER]
    cmd.append(url)

    log("$ " + " ".join(shlex.quote(c) for c in cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        with _active_downloads_lock:
            _active_downloads[url] = proc
        try:
            stdout, stderr = proc.communicate(timeout=3600)
            result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        finally:
            with _active_downloads_lock:
                _active_downloads.pop(url, None)
        tail = (result.stdout + result.stderr).strip().splitlines()
        timestamp = datetime.datetime.now().isoformat()

        def _parse_filepath() -> str:
            for line in reversed(tail):
                if "[download] Destination:" in line:
                    return line.split("Destination:", 1)[1].strip()
                if "has already been downloaded" in line:
                    return line.split("]", 1)[1].split(" has already")[0].strip()
                if "[Merger]" in line and "into" in line:
                    parts = line.split('"')
                    return parts[-2] if len(parts) >= 2 else ""
            return ""

        def _parse_filesize() -> Optional[int]:
            for line in tail:
                if "[download]" in line and "% of" in line and "in" in line:
                    m = re.search(r"(\d+\.?\d*)\s*(KiB|MiB|GiB)", line)
                    if m:
                        val = float(m.group(1))
                        unit = m.group(2)
                        multipliers = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
                        return int(val * multipliers.get(unit, 1))
            return None

        def _extract_title(filepath: str) -> str:
            name = Path(filepath).stem  # filename without extension
            # Remove trailing [id] like " [abc123]"
            cleaned = re.sub(r"\s\[[\w-]+\]$", "", name)
            return cleaned if cleaned else name

        if result.returncode == 0:
            log(f"[done] {url}")
            for line in tail[-15:]:
                log("  " + line)
            filepath = _parse_filepath()
            filesize = _parse_filesize()
            title = _extract_title(filepath) if filepath else "Unknown"
            short = Path(filepath).name if filepath else "Done"
            notify(f"{APP_NAME} ✅", short)

            history_entry = {
                "id": _simple_hash(url),
                "url": url,
                "title": title,
                "filename": Path(filepath).name if filepath else "",
                "filepath": str(Path(filepath).resolve()) if filepath else "",
                "filesize": filesize,
                "timestamp": timestamp,
                "status": "done",
            }
            _append_history(history_entry)
        else:
            log(f"[fail] {url} rc={result.returncode}")
            for line in tail[-15:]:
                log("  " + line)
            err = tail[-1] if tail else f"rc={result.returncode}"
            notify(f"{APP_NAME} ❌", err[:120])
            _append_history({
                "id": _simple_hash(url),
                "url": url,
                "title": "",
                "filename": "",
                "filepath": "",
                "filesize": None,
                "timestamp": timestamp,
                "status": "failed",
            })
    except subprocess.TimeoutExpired:
        log(f"[timeout] {url}")
        notify(f"{APP_NAME} ⏱", "Download timed out (1 hour)")
        _append_history({
            "id": _simple_hash(url),
            "url": url,
            "title": "",
            "filename": "",
            "filepath": "",
            "filesize": None,
            "timestamp": datetime.datetime.now().isoformat(),
            "status": "failed",
        })
    except Exception as e:
        log(f"[err] {url}: {e}")
        notify(f"{APP_NAME} ❌", str(e)[:120])
        _append_history({
            "id": _simple_hash(url),
            "url": url,
            "title": "",
            "filename": "",
            "filepath": "",
            "filesize": None,
            "timestamp": datetime.datetime.now().isoformat(),
            "status": "failed",
        })


def _safe_filename(name: str) -> str:
    """Sanitize a user/page-supplied filename: strip path separators and illegal
    characters so it can't escape DOWNLOADS or break the filesystem."""
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip()
    name = name.lstrip(".") or "video"
    return name[:180]


def download_direct(direct_url: str, referer: str, filename: str, page_url: str) -> None:
    """Download an already-resolved media URL via curl, sending a Referer header.

    Used for sites whose anti-bot (e.g. Douyin's a_bogus signing) blocks yt-dlp at
    the network layer, but whose real stream URL the browser extension can read off
    the playing page. Tracked in _active_downloads (keyed by the page URL) and writes
    history exactly like download(), so /cancel and the popup history both just work.
    """
    log(f"[start-direct] {page_url}")
    notify(APP_NAME, f"Downloading {filename[:80]}")
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    # Filename = 平台_标题_下载日期 (no 小耳 branding — files get shared with others).
    ext = Path(filename).suffix or ".mp4"
    title = Path(filename).stem
    platform = _platform_name(page_url)
    date = datetime.datetime.now().strftime("%Y%m%d")
    out = DOWNLOADS / (_safe_filename(f"{platform}_{title}_{date}") + ext)
    cmd = [
        # --noproxy '*' : these are domestic CDNs (xhscdn is plain http, zjcdn etc).
        # Without it the request goes through the system proxy (Clash) and hangs.
        "curl", "-fL", "--retry", "2", "--noproxy", "*",
        "-H", f"Referer: {referer}",
        "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
        "-o", str(out), direct_url,
    ]
    # macOS quirk: a stale SSL_CERT_FILE in the daemon env makes curl's HTTPS fail.
    env = os.environ.copy()
    env.pop("SSL_CERT_FILE", None)
    timestamp = datetime.datetime.now().isoformat()

    def _fail_history() -> None:
        _append_history({
            "id": _simple_hash(page_url), "url": page_url, "title": "",
            "filename": "", "filepath": "", "filesize": None,
            "timestamp": datetime.datetime.now().isoformat(), "status": "failed",
        })

    log("$ " + " ".join(shlex.quote(c) for c in cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env)
        with _active_downloads_lock:
            _active_downloads[page_url] = proc
        try:
            _, stderr = proc.communicate(timeout=3600)
        finally:
            with _active_downloads_lock:
                _active_downloads.pop(page_url, None)

        ok = proc.returncode == 0 and out.is_file() and out.stat().st_size > 0
        if ok:
            size = out.stat().st_size
            log(f"[done-direct] {out.name} ({size} bytes)")
            notify(f"{APP_NAME} ✅", out.name)
            _append_history({
                "id": _simple_hash(page_url), "url": page_url,
                "title": Path(filename).stem, "filename": out.name,
                "filepath": str(out.resolve()), "filesize": size,
                "timestamp": timestamp, "status": "done",
            })
        else:
            # Drop a zero-byte / partial file left by a failed or cancelled curl.
            if out.exists() and (not out.is_file() or out.stat().st_size == 0):
                out.unlink(missing_ok=True)
            err = (stderr or "").strip().splitlines()
            msg = err[-1] if err else f"curl rc={proc.returncode}"
            log(f"[fail-direct] {page_url}: {msg}")
            notify(f"{APP_NAME} ❌", msg[:120])
            _fail_history()
    except subprocess.TimeoutExpired:
        cancel_download(page_url)
        log(f"[timeout-direct] {page_url}")
        notify(f"{APP_NAME} ⏱", "Download timed out (1 hour)")
        _fail_history()
    except Exception as e:
        log(f"[err-direct] {page_url}: {e}")
        notify(f"{APP_NAME} ❌", str(e)[:120])
        _fail_history()


class Handler(BaseHTTPRequestHandler):
    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _handle_open_or_reveal(self, cmd: str, flag: Optional[str] = None) -> None:
        """Open a file with macOS `open`, optionally with a flag like `-R`."""
        origin = self.headers.get("Origin", "")
        if origin.startswith(("http://", "https://")):
            self.send_response(403)
            self._cors()
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            data = json.loads(raw)
            filepath = data.get("path", "")
            if not filepath or not Path(filepath).is_file():
                raise ValueError(f"file not found")
        except Exception as e:
            self.send_response(400)
            self._cors()
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"bad request: {e}".encode())
            return

        argv = [cmd]
        if flag:
            argv.append(flag)
        argv.append(filepath)
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"opened":true}')

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"service":"xiaoer-videolab"}')
        elif path == "/probe":
            params = parse_qs(urlparse(self.path).query)
            target_url = params.get("url", [None])[0]
            if not target_url:
                self.send_response(400)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"missing url"}')
                return

            try:
                probe_cmd = [YT_DLP, "--dump-json", "--no-playlist", "--playlist-items", "1",
                             "--socket-timeout", "20"]
                # Same cookies as the real download — otherwise login-gated sites
                # (YouTube's "confirm you're not a bot") probe as "no video" on
                # pages we can actually download fine, and the button lies.
                if COOKIES_BROWSER:
                    probe_cmd += ["--cookies-from-browser", COOKIES_BROWSER]
                probe_cmd.append(target_url)
                # 45s, not 15s: over a proxy, `--dump-json` on X/YouTube measured
                # 30-55s here. A 15s cap made every probe time out, so the popup
                # reported "no video" on pages that download fine.
                result = subprocess.run(
                    probe_cmd, capture_output=True, text=True, timeout=45
                )
                if result.returncode == 0:
                    info = json.loads(result.stdout.splitlines()[0])
                    data = {
                        "has_video": True,
                        "title": info.get("title"),
                        "duration": info.get("duration"),
                        "extractor": info.get("extractor_key"),
                    }
                else:
                    data = {"has_video": False}
            except subprocess.TimeoutExpired:
                data = {"has_video": False, "error": "timeout"}
            except Exception as e:
                data = {"has_video": False, "error": str(e)[:100]}

            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/history":
            entries = []
            if HISTORY_FILE.is_file():
                with _history_lock:
                    lines = HISTORY_FILE.read_text().strip().splitlines()
                for line in lines[-50:]:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            entries.reverse()
            body = json.dumps(entries, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self._cors()
            self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/open":
            self._handle_open_or_reveal("open")
            return
        if path == "/reveal":
            self._handle_open_or_reveal("open", flag="-R")
            return
        if path == "/cancel":
            self._handle_cancel()
            return
        if path == "/history-delete":
            self._handle_history_delete()
            return
        if path == "/download-direct":
            self._handle_download_direct()
            return
        if path != "/download":
            self.send_response(404)
            self._cors()
            self.end_headers()
            return
        # Security: stop drive-by downloads. A real web page that tries to call us
        # carries an http(s) Origin header — refuse those. The extension sends
        # Origin: chrome-extension://..., and curl/CLI send none — both allowed.
        origin = self.headers.get("Origin", "")
        if origin.startswith(("http://", "https://")):
            self.send_response(403)
            self._cors()
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"forbidden: web-page origins cannot trigger downloads")
            log(f"[blocked] web origin {origin} tried to POST /download")
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            data = json.loads(raw)
            url = data["url"]
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                raise ValueError("url must be http(s)")
        except Exception as e:
            self.send_response(400)
            self._cors()
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"bad request: {e}".encode())
            return

        threading.Thread(target=download, args=(url,), daemon=True).start()

        self.send_response(202)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"queued":true}')

    def _handle_download_direct(self) -> None:
        """Queue a direct (browser-extracted) media URL for download with a Referer.

        Payload: {url, referer, filename, pageUrl}. `url` is the resolved stream the
        extension read off the page; `pageUrl` is the original site link (used as the
        history/cancel key). Same Origin guard as /download — web pages can't trigger us.
        """
        origin = self.headers.get("Origin", "")
        if origin.startswith(("http://", "https://")):
            self.send_response(403)
            self._cors()
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"forbidden: web-page origins cannot trigger downloads")
            log(f"[blocked] web origin {origin} tried to POST /download-direct")
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            data = json.loads(raw)
            direct_url = data["url"]
            if not isinstance(direct_url, str) or not direct_url.startswith(("http://", "https://")):
                raise ValueError("url must be http(s)")
            page_url = data.get("pageUrl") or direct_url
            referer = data.get("referer", "")
            filename = _safe_filename(data.get("filename") or "video.mp4")
            if not filename.lower().endswith((".mp4", ".webm", ".mkv", ".mov", ".m4v")):
                filename += ".mp4"
        except Exception as e:
            self.send_response(400)
            self._cors()
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"bad request: {e}".encode())
            return

        threading.Thread(
            target=download_direct,
            args=(direct_url, referer, filename, page_url),
            daemon=True,
        ).start()

        self.send_response(202)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"queued":true}')

    def _handle_history_delete(self) -> None:
        """Remove entries from the history file. Payload: {url} to drop one, or
        {clear:"all"} / {clear:"failed"} to bulk-clear. Lets the popup declutter."""
        origin = self.headers.get("Origin", "")
        if origin.startswith(("http://", "https://")):
            self.send_response(403)
            self._cors()
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        url = data.get("url", "")
        clear = data.get("clear", "")
        with _history_lock:
            if HISTORY_FILE.is_file():
                lines = HISTORY_FILE.read_text().strip().splitlines()
                kept = []
                for line in lines:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if clear == "all":
                        continue
                    if clear == "failed" and e.get("status") == "failed":
                        continue
                    if url and e.get("url") == url:
                        continue
                    kept.append(line)
                HISTORY_FILE.write_text(("\n".join(kept) + "\n") if kept else "")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def _handle_cancel(self) -> None:
        """Cancel a running download by URL."""
        origin = self.headers.get("Origin", "")
        if origin.startswith(("http://", "https://")):
            self.send_response(403)
            self._cors()
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            data = json.loads(raw)
            url = data["url"]
        except Exception as e:
            self.send_response(400)
            self._cors()
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"bad request: {e}".encode())
            return

        killed = cancel_download(url)
        body = json.dumps({"cancelled": killed}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args) -> None:
        log("http: " + (fmt % args))


def main() -> None:
    log(f"{APP_NAME} daemon listening on http://{HOST}:{PORT}  (yt-dlp: {YT_DLP})")
    # ThreadingHTTPServer (not plain HTTPServer): /probe runs yt-dlp --dump-json
    # synchronously and can take many seconds, which on a single-threaded server
    # blocks /history and /health and makes the popup falsely report "daemon down".
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("daemon stopping")
        server.shutdown()


if __name__ == "__main__":
    main()
