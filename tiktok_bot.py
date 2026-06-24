import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from keep_alive import keep_alive

keep_alive()

import re
import math
import time
import uuid
import tempfile
import subprocess
import shutil
import logging
import traceback
import telebot
from telebot import types
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("media-bot")

TOKEN = os.environ.get("TIKTOK_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TIKTOK_BOT_TOKEN environment variable is not set")

bot = telebot.TeleBot(TOKEN)

# Configurable via env: set TELEGRAM_MAX_BYTES_MB=2000 when using a local Bot API server
TELEGRAM_MAX_BYTES = int(os.environ.get("TELEGRAM_MAX_BYTES_MB", "50")) * 1024 * 1024
# Minimum free disk space required before starting any download
MIN_FREE_DISK_BYTES = int(os.environ.get("MIN_FREE_DISK_GB", "3")) * 1024**3

URL_REGEX = re.compile(r"https?://\S+")


def format_size(n):
    """Human-readable byte count, e.g. 1.4 GB."""
    if n is None:
        return "؟"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def check_disk_space(required_bytes=None, path="/tmp"):
    """Return (ok, free_bytes). ok=True means enough free space."""
    try:
        free = shutil.disk_usage(path).free
        needed = required_bytes or MIN_FREE_DISK_BYTES
        return free >= needed, free
    except Exception as e:
        log.warning("disk_usage check failed: %s", e)
        return True, None  # assume OK if check fails


def estimate_download_size(info):
    """Best-effort size estimate (bytes) from a yt-dlp info dict."""
    if not info:
        return None
    for key in ("filesize", "filesize_approx"):
        sz = info.get(key)
        if sz and sz > 0:
            return int(sz)
    # Sum requested formats if merged video+audio
    total = 0
    for fmt in info.get("requested_formats") or []:
        for key in ("filesize", "filesize_approx"):
            sz = fmt.get(key)
            if sz and sz > 0:
                total += int(sz)
                break
    return total or None


PLATFORM_PATTERNS = [
    ("TikTok", ("tiktok.com", "vt.tiktok.com")),
    ("YouTube", ("youtube.com", "youtu.be", "m.youtube.com")),
    ("Instagram", ("instagram.com",)),
    ("Facebook", ("facebook.com", "fb.watch", "fb.com", "m.facebook.com")),
    ("Twitter/X", ("twitter.com", "x.com", "mobile.twitter.com")),
]


def detect_platform(url):
    low = url.lower()
    for name, hosts in PLATFORM_PATTERNS:
        if any(h in low for h in hosts):
            return name
    return None


PENDING_URLS = {}
PENDING_TTL_SECONDS = 60 * 30


def remember_url(url, platform, meta=None):
    now = time.time()
    expired = [k for k, v in PENDING_URLS.items() if now - v[2] > PENDING_TTL_SECONDS]
    for k in expired:
        PENDING_URLS.pop(k, None)
    key = uuid.uuid4().hex[:12]
    PENDING_URLS[key] = (url, platform, now, meta or {})
    return key


def pop_url(key):
    item = PENDING_URLS.pop(key, None)
    if not item:
        return None, None, {}
    return item[0], item[1], item[3] if len(item) > 3 else {}


# ---------- TikTok URL helpers ----------

import threading

TIKTOK_SHORT_HOSTS = ("vm.tiktok.com", "vt.tiktok.com")
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def unwrap_short_url(url):
    """Follow vm/vt.tiktok.com redirects to the canonical URL. Best effort."""
    if not url:
        return url
    low = url.lower()
    if not any(h in low for h in TIKTOK_SHORT_HOSTS):
        return url
    try:
        r = requests.get(
            url,
            allow_redirects=True,
            timeout=10,
            headers={"User-Agent": _BROWSER_UA},
        )
        return r.url or url
    except Exception:
        return url


def clean_tiktok_url(url):
    """Resolve short links, strip query/tracking params and trailing slashes."""
    url = unwrap_short_url(url)
    if "?" in url:
        url = url.split("?", 1)[0]
    if "#" in url:
        url = url.split("#", 1)[0]
    return url.rstrip("/")


# ---------- TikTok via tikwm.com (with rate-limit throttle + retry) ----------

_tikwm_lock = threading.Lock()
_tikwm_last = [0.0]


def _tikwm_throttle():
    with _tikwm_lock:
        wait = 1.05 - (time.time() - _tikwm_last[0])
        if wait > 0:
            time.sleep(wait)
        _tikwm_last[0] = time.time()


def fetch_tiktok_raw(url):
    """Return the parsed tikwm JSON response (or {'_error': ...} on failure)."""
    _tikwm_throttle()
    try:
        r = requests.get(
            "https://www.tikwm.com/api/",
            params={"url": url},
            timeout=15,
            headers={"User-Agent": _BROWSER_UA},
        )
        try:
            return r.json()
        except Exception:
            return {"_error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def fetch_tiktok_data(url):
    """Normalize tikwm response. Returns (data_dict, error_msg)."""
    resp = fetch_tiktok_raw(url)
    if "_error" in resp:
        log.error("tikwm request failed for url=%s err=%s", url, resp["_error"])
        return None, resp["_error"]
    code = resp.get("code")
    msg = (resp.get("msg") or "").strip()

    # Retry once if we hit the free-tier rate limit
    if code == -1 and "limit" in msg.lower():
        log.info("tikwm rate-limited, retrying once after pause")
        time.sleep(1.2)
        resp = fetch_tiktok_raw(url)
        if "_error" in resp:
            return None, resp["_error"]
        code = resp.get("code")
        msg = (resp.get("msg") or "").strip()

    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    log.info(
        "tikwm url=%s code=%s msg=%s images=%d has_play=%s",
        url,
        code,
        msg,
        len(data.get("images") or []) if data else 0,
        bool(data.get("play")) if data else False,
    )
    if code == 0:
        return (data or {}), None
    return None, msg or f"tikwm code={code}"


def download_tiktok_no_wm(url):
    data, _err = fetch_tiktok_data(url)
    if data:
        return data.get("play")
    return None


def yt_dlp_tiktok_video_fallback(url, dest_dir):
    """yt-dlp fallback for TikTok /video/ URLs only. Raises on failure."""
    import yt_dlp

    outtmpl = os.path.join(dest_dir, "%(id)s.%(ext)s")
    opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": outtmpl,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "socket_timeout": 60,
        "retries": 5,
        "fragment_retries": 5,
        "buffersize": 1024 * 1024,  # 1 MB stream buffer
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    return path, info


def download_to_file(url, suffix, max_retries=3):
    """Stream-download url to a temp file. Retries on network errors."""
    last_exc = None
    for attempt in range(1, max_retries + 1):
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        try:
            with requests.get(
                url,
                stream=True,
                timeout=120,
                headers={"User-Agent": _BROWSER_UA},
            ) as r:
                r.raise_for_status()
                written = 0
                with open(path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=512 * 1024):  # 512 KB chunks
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
            log.info("download_to_file: wrote %s to %s", format_size(written), path)
            return path
        except Exception as e:
            last_exc = e
            log.warning(
                "download_to_file attempt %d/%d failed: %s", attempt, max_retries, e
            )
            try:
                os.remove(path)
            except OSError:
                pass
            if attempt < max_retries:
                time.sleep(4 * attempt)
    log.error(
        "download_to_file: all %d attempts failed. Last error: %s",
        max_retries,
        last_exc,
    )
    return None


# ---------- Audio extraction ----------


def extract_mp3_ffmpeg(video_path, mp3_path):
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        video_path,
        "-vn",
        "-acodec",
        "libmp3lame",
        "-q:a",
        "2",
        mp3_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if (
        result.returncode == 0
        and os.path.exists(mp3_path)
        and os.path.getsize(mp3_path) > 0
    ):
        return True, None
    err = (result.stderr or "").strip().splitlines()[-5:]
    return False, "ffmpeg: " + " | ".join(
        err
    ) if err else f"ffmpeg exited with code {result.returncode}"


def extract_mp3_moviepy(video_path, mp3_path):
    try:
        try:
            from moviepy import VideoFileClip
        except ImportError:
            from moviepy.editor import VideoFileClip
        clip = VideoFileClip(video_path)
        try:
            if clip.audio is None:
                return False, "moviepy: no audio track in video"
            clip.audio.write_audiofile(mp3_path, logger=None)
        finally:
            try:
                clip.close()
            except Exception:
                pass
        if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0:
            return True, None
        return False, "moviepy: produced empty file"
    except Exception as e:
        log.exception("moviepy extraction failed")
        return False, f"moviepy: {type(e).__name__}: {e}"


def extract_mp3(video_path, mp3_path):
    ok, err1 = extract_mp3_ffmpeg(video_path, mp3_path)
    if ok:
        return True, None
    log.warning("ffmpeg extract failed, trying moviepy. err=%s", err1)
    if os.path.exists(mp3_path):
        try:
            os.remove(mp3_path)
        except OSError:
            pass
    ok, err2 = extract_mp3_moviepy(video_path, mp3_path)
    if ok:
        return True, None
    return False, f"{err1} || {err2}"


# ---------- yt-dlp helpers (YouTube, Instagram, Facebook, Twitter/X) ----------

INSTAGRAM_COOKIES_FILE = "instagram_cookies.txt"
TWITTER_COOKIES_FILE = "twitter_cookies.txt"


def _twitter_cookies_path():
    return TWITTER_COOKIES_FILE if os.path.exists(TWITTER_COOKIES_FILE) else None


def _is_twitter(url):
    low = (url or "").lower()
    return any(h in low for h in ("twitter.com", "x.com", "mobile.twitter.com"))


def _apply_twitter_cookies(opts, url):
    if _is_twitter(url):
        ck = _twitter_cookies_path()
        if ck:
            opts["cookiefile"] = ck
    return opts


INSTAGRAM_NEEDS_COOKIES_MSG = "⚠️ انستجرام محتاج تسجيل دخول. كلم المطور يضيف Cookies"
_IG_FAILURE_KEYWORDS = (
    "login required",
    "rate-limit",
    "rate limit",
    "requested content is not available",
    "empty media response",
    "cookies",
)


def _instagram_cookies_path():
    return INSTAGRAM_COOKIES_FILE if os.path.exists(INSTAGRAM_COOKIES_FILE) else None


INSTAGRAM_STRATEGIES = [
    (
        "A:iPhone Safari + AJAX",
        {
            "user_agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.5 Mobile/15E148 Safari/604.1"
            ),
            "http_headers": {
                "X-Instagram-AJAX": "1",
                "X-IG-App-ID": "936619743392459",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://www.instagram.com/",
            },
            "extractor_args": {"instagram": {"api_version": ["v1"]}},
        },
    ),
    (
        "B:Android Samsung",
        {
            "user_agent": (
                "Mozilla/5.0 (Linux; Android 13; SM-S901B) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Mobile Safari/537.36"
            ),
            "extractor_args": {"instagram": {"api_version": ["v1"]}},
        },
    ),
    (
        "C:Windows Chrome + Referer",
        {
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "http_headers": {"Referer": "https://www.instagram.com/"},
        },
    ),
    ("D:Cookies only (basic)", {}),
]

IG_ALL_FAILED_MSG = (
    "❌ فشل كل المحاولات. IP سيرفر Replit محظور من انستجرام. الحل الوحيد: بروكسي مدفوع"
)

_ig_winning_strategy = {"index": None, "name": None}


def _ig_try_strategies(action_fn, url, base_opts):
    """Try each Instagram strategy with retries. Cache the winner.
    Per strategy: 1 attempt + 2 retries (3 total) with 2s sleep between attempts.
    Returns the first successful result, or raises RuntimeError(IG_ALL_FAILED_MSG)."""
    indices = list(range(len(INSTAGRAM_STRATEGIES)))
    if _ig_winning_strategy["index"] is not None:
        winner = _ig_winning_strategy["index"]
        log.info(
            "instagram: starting with cached winning strategy [%s]",
            _ig_winning_strategy["name"],
        )
        indices = [winner] + [i for i in indices if i != winner]

    last_err_text = ""
    ck = _instagram_cookies_path()
    for idx in indices:
        name, strat_opts = INSTAGRAM_STRATEGIES[idx]
        for attempt in range(1, 4):
            opts = dict(base_opts)
            opts.update(strat_opts)
            if ck:
                opts["cookiefile"] = ck
            log.info(
                "instagram: trying strategy [%s] attempt %d/3 for %s",
                name,
                attempt,
                url,
            )
            try:
                result = action_fn(opts)
                log.info("instagram: SUCCESS with strategy [%s]", name)
                _ig_winning_strategy["index"] = idx
                _ig_winning_strategy["name"] = name
                return result
            except Exception as e:
                last_err_text = str(e).replace("\n", " ")[:160]
                log.warning(
                    "instagram: [%s] attempt %d/3 failed: %s",
                    name,
                    attempt,
                    last_err_text,
                )
                if attempt < 3:
                    time.sleep(2)
    raise RuntimeError(f"{IG_ALL_FAILED_MSG} (last error: {last_err_text})")


def _is_instagram(url):
    return "instagram.com" in (url or "").lower()


def _instagram_cookie_status():
    """Return (path, sessionid_present, cookie_count). path is None if file missing."""
    path = _instagram_cookies_path()
    if not path:
        return None, False, 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data_lines = [
                ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")
            ]
        names = []
        for ln in data_lines:
            parts = ln.rstrip("\n").split("\t")
            if len(parts) >= 7:
                names.append(parts[5])
        return path, ("sessionid" in names), len(data_lines)
    except Exception as e:
        log.warning("could not parse cookie file %s: %s", path, e)
        return path, False, 0


def log_cookie_status_at_startup():
    path, has_session, count = _instagram_cookie_status()
    if not path:
        log.info(
            "instagram cookies: NO FILE at %s — IG will use friendly error",
            INSTAGRAM_COOKIES_FILE,
        )
        return
    log.info(
        "instagram cookies: loaded %s (%d cookies, sessionid=%s)",
        path,
        count,
        "yes" if has_session else "NO",
    )


def instagram_friendly_error(platform, exc):
    """Translate raw yt-dlp Instagram errors into a clear Arabic message."""
    if platform != "Instagram":
        return None
    text = str(exc).lower()
    path, has_session, _count = _instagram_cookie_status()

    # No cookies file uploaded at all
    if not path:
        if any(k in text for k in _IG_FAILURE_KEYWORDS):
            return INSTAGRAM_NEEDS_COOKIES_MSG
        return None

    # Cookies file is present but malformed (no sessionid)
    if not has_session and any(k in text for k in _IG_FAILURE_KEYWORDS):
        return (
            "⚠️ ملف الكوكيز ناقص (مفيش sessionid). "
            "صدّر الكوكيز تاني وانت Logged-in في انستجرام"
        )

    # If our strategy loop already produced a final Arabic message, surface it as-is
    if str(exc).startswith("❌ فشل كل المحاولات"):
        return str(exc)
    # Cookies are valid but Instagram still refuses (datacenter IP block / expired session)
    if "empty media response" in text or "rate-limit" in text or "rate limit" in text:
        return (
            "⚠️ انستجرام رفض الرد رغم وجود الكوكيز. "
            "غالباً الـ IP بتاع السيرفر متحظور من انستجرام، أو الجلسة انتهت. "
            "جرّب تصدير كوكيز جديدة من جلسة ماسكة دلوقتي"
        )
    return None


def yt_dlp_extract_info(url):
    import yt_dlp

    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
        "socket_timeout": 30,
    }
    if _is_instagram(url):

        def _action(opts):
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        return _ig_try_strategies(_action, url, base_opts)
    _apply_twitter_cookies(base_opts, url)
    with yt_dlp.YoutubeDL(base_opts) as ydl:
        return ydl.extract_info(url, download=False)


def yt_dlp_download_video(url, dest_dir):
    import yt_dlp

    outtmpl = os.path.join(dest_dir, "%(id)s.%(ext)s")
    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": outtmpl,
        # No size cap — caller checks size vs TELEGRAM_MAX_BYTES after download
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "socket_timeout": 60,
        "retries": 5,
        "fragment_retries": 5,
        "buffersize": 1024 * 1024,  # 1 MB stream buffer
        "http_chunk_size": 10 * 1024 * 1024,  # 10 MB HTTP chunks for large files
    }
    if _is_instagram(url):

        def _action(opts):
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return info, ydl.prepare_filename(info)

        info, path = _ig_try_strategies(_action, url, base_opts)
        return path, info
    _apply_twitter_cookies(base_opts, url)
    with yt_dlp.YoutubeDL(base_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    return path, info


def yt_dlp_download_audio(url, dest_dir):
    import yt_dlp

    outtmpl = os.path.join(dest_dir, "%(id)s.%(ext)s")
    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": outtmpl,
        "format": "bestaudio/best",
        "noplaylist": True,
        "socket_timeout": 60,
        "retries": 5,
        "fragment_retries": 5,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }
    if _is_instagram(url):

        def _action(opts):
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                base, _ = os.path.splitext(ydl.prepare_filename(info))
                return info, base + ".mp3"

        info, mp3_path = _ig_try_strategies(_action, url, base_opts)
        return mp3_path, info
    _apply_twitter_cookies(base_opts, url)
    with yt_dlp.YoutubeDL(base_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
        base, _ = os.path.splitext(path)
        mp3_path = base + ".mp3"
    return mp3_path, info


def collect_image_urls_from_info(info):
    """Return a list of image URLs if the info represents an image post / carousel, else None."""
    if not info:
        return None

    def entry_image_url(e):
        ext = (e.get("ext") or "").lower()
        if ext in ("jpg", "jpeg", "png", "webp"):
            url = e.get("url")
            if url:
                return url
            for f in e.get("formats") or []:
                if (f.get("ext") or "").lower() in (
                    "jpg",
                    "jpeg",
                    "png",
                    "webp",
                ) and f.get("url"):
                    return f["url"]
        return None

    if info.get("_type") in ("playlist", "multi_video"):
        entries = info.get("entries") or []
        urls = []
        for e in entries:
            if not e:
                continue
            u = entry_image_url(e)
            if not u:
                return None
            urls.append(u)
        return urls or None

    u = entry_image_url(info)
    if u:
        return [u]
    return None


# ---------- Helpers ----------


def safe_filename(name, fallback="audio"):
    if not name:
        return fallback
    cleaned = re.sub(r"[^\w\-. ]+", "", name).strip()
    cleaned = cleaned[:60].strip()
    return cleaned or fallback


def build_choice_keyboard(key, compress_label=None, split_options=None):
    """Build inline keyboard.
    compress_label=None + split_options=None → small video (2-button).
    split_options is list of (height, n_parts, label) from compute_split_options()."""
    kb = types.InlineKeyboardMarkup()
    if not compress_label:
        kb.row(
            types.InlineKeyboardButton("🎬 فيديو", callback_data=f"v:{key}"),
            types.InlineKeyboardButton("🎵 صوت MP3", callback_data=f"a:{key}"),
        )
    else:
        kb.row(types.InlineKeyboardButton("🎬 فيديو كامل ⚠️", callback_data=f"v:{key}"))
        kb.row(types.InlineKeyboardButton(compress_label, callback_data=f"c:{key}"))
        for height, n_parts, lbl in split_options or []:
            # callback: sp:{height}:{n_parts}:{key}  (≤ 64 chars)
            cb = f"sp:{height}:{n_parts}:{key}"
            kb.row(types.InlineKeyboardButton(lbl, callback_data=cb))
        kb.row(types.InlineKeyboardButton("🎵 صوت MP3 فقط", callback_data=f"a:{key}"))
    return kb


def safe_edit_text(chat_id, message_id, text):
    try:
        bot.edit_message_text(text, chat_id, message_id)
    except Exception:
        pass


def send_video_with_retry(chat_id, path, caption, max_retries=3):
    """Upload a video file to Telegram with automatic retry on transient errors.
    Uses a 5-minute timeout per attempt to handle large files over slow connections."""
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            with open(path, "rb") as f:
                result = bot.send_video(chat_id, f, caption=caption, timeout=300)
            log.info(
                "send_video: uploaded %s on attempt %d",
                format_size(os.path.getsize(path)),
                attempt,
            )
            return result
        except Exception as e:
            last_exc = e
            log.warning(
                "send_video attempt %d/%d failed (%s): %s",
                attempt,
                max_retries,
                type(e).__name__,
                e,
            )
            if attempt < max_retries:
                time.sleep(5 * attempt)
    raise last_exc


def compress_to_target(input_path, target_bytes, duration_secs):
    """Re-encode video with ffmpeg to fit within target_bytes.
    Returns (output_path, final_size_bytes). Raises on failure."""
    fd, out = tempfile.mkstemp(suffix="_cmp.mp4")
    os.close(fd)
    # Leave 8% headroom for container overhead
    target_bits = int(target_bytes * 8 * 0.92)
    audio_bps = 96_000  # 96 kbps AAC audio
    video_bps = max(int(target_bits / max(duration_secs, 1)) - audio_bps, 80_000)
    log.info(
        "compress_to_target: duration=%.1fs target=%s → vbr=%d bps",
        duration_secs,
        format_size(target_bytes),
        video_bps,
    )
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-c:v",
        "libx264",
        "-b:v",
        str(video_bps),
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-preset",
        "faster",
        "-movflags",
        "+faststart",
        out,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        try:
            os.remove(out)
        except OSError:
            pass
        raise RuntimeError("ffmpeg: " + (result.stderr or "")[-500:].strip())
    final_size = os.path.getsize(out)
    log.info("compress_to_target: result %s", format_size(final_size))
    return out, final_size


PROMO_MSG = (
    "انت تستخدم افضل بوت لتحميل وضغط الفيديوهات ليناسب احتياجك واستخدامك "
    "ويرجى نشر البوت اكثر فضلا وليس امرا 🙏"
)

# Typical total bitrate (bps) per quality level — calibrated for YouTube/modern platforms
TYPICAL_BITRATES_BPS = {
    1080: 2_500_000,
    720: 1_500_000,
    480: 500_000,
    360: 250_000,
    240: 150_000,
    144: 80_000,
}
_QUALITY_HEIGHTS = [1080, 720, 480, 360, 240, 144]


def calculate_target_quality(duration_secs, target_bytes):
    """Return (height_int, label_str) for the best resolution that fits target_bytes."""
    if not duration_secs or duration_secs <= 0:
        return 480, "480p"
    target_bits = target_bytes * 8 * 0.92
    audio_bps = 96_000
    video_bps = target_bits / duration_secs - audio_bps
    if video_bps >= 2_500_000:
        return 1080, "1080p"
    elif video_bps >= 1_500_000:
        return 720, "720p"
    elif video_bps >= 500_000:
        return 480, "480p"
    elif video_bps >= 250_000:
        return 360, "360p"
    elif video_bps >= 100_000:
        return 240, "240p"
    else:
        return 144, "144p"


def compute_split_options(size_bytes, duration_secs, native_height):
    """Return list of (height, n_parts, label) for each quality level,
    computed dynamically from the video's known size/duration.
    Deduplicates by n_parts so only the best quality per part-count appears.
    Skips any quality that needs more than 64 parts."""
    MAX_PARTS = 64
    native_h = native_height or 1080
    native_bps = TYPICAL_BITRATES_BPS.get(native_h, 2_500_000)
    heights = [h for h in _QUALITY_HEIGHTS if h <= native_h]
    if not heights:
        heights = _QUALITY_HEIGHTS[:]

    seen_parts: dict[int, tuple] = {}
    for height in heights:
        height_bps = TYPICAL_BITRATES_BPS[height]
        if size_bytes and duration_secs:
            est_size = int(size_bytes * height_bps / native_bps)
        elif duration_secs:
            est_size = int(height_bps / 8 * duration_secs)
        else:
            continue
        n_parts = max(1, math.ceil(est_size / TELEGRAM_MAX_BYTES))
        if n_parts > MAX_PARTS:
            continue
        if n_parts not in seen_parts:
            lbl = (
                f"📥 {height}p (جزء واحد)"
                if n_parts == 1
                else f"✂️ {height}p ({n_parts} {'جزء' if n_parts < 11 else 'جزء'})"
            )
            seen_parts[n_parts] = (height, n_parts, lbl)

    # Sort: most parts first (= highest quality first)
    return sorted(seen_parts.values(), key=lambda x: -x[1])


def ytdlp_format_for_height(max_height):
    h = max_height
    return (
        f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={h}]+bestaudio"
        f"/best[height<={h}]"
        f"/best"
    )


def split_video_parts(input_path, n_parts, duration_secs, output_dir):
    """Split video into n_parts equal segments via ffmpeg stream-copy. Returns list of paths."""
    part_dur = duration_secs / n_parts
    paths = []
    for i in range(n_parts):
        start = i * part_dur
        out = os.path.join(output_dir, f"part_{i + 1:02d}_of_{n_parts:02d}.mp4")
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-i",
            input_path,
            "-t",
            f"{part_dur:.3f}",
            "-c",
            "copy",
            "-avoid_negative_ts",
            "make_zero",
            out,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if res.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            raise RuntimeError(
                f"ffmpeg split part {i + 1}/{n_parts} failed:\n{res.stderr[-300:]}"
            )
        sz = os.path.getsize(out)
        log.info("split: part %d/%d → %s", i + 1, n_parts, format_size(sz))
        paths.append(out)
    return paths


def process_video_split(
    chat_id, status_message_id, url, platform, n_parts, meta, target_height=None
):
    """Download at adaptive quality for n_parts, split, and send each part in order.
    target_height: explicit resolution to download (overrides budget-based calculation)."""
    safe_edit_text(
        chat_id,
        status_message_id,
        f"🎯 منصة: {platform}\n⏳ جاري التحضير للتقسيم لـ {n_parts} أجزاء...",
    )
    try:
        ok, free = check_disk_space()
        if not ok:
            safe_edit_text(
                chat_id,
                status_message_id,
                f"❌ مساحة القرص غير كافية (متاح: {format_size(free)})",
            )
            return

        duration_hint = meta.get("duration") or 0
        if target_height:
            quality_label = f"{target_height}p"
        else:
            total_budget = n_parts * TELEGRAM_MAX_BYTES
            target_height, quality_label = calculate_target_quality(
                duration_hint, total_budget
            )

        import yt_dlp

        with tempfile.TemporaryDirectory() as tmpdir:
            outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
            opts = {
                "quiet": True,
                "no_warnings": True,
                "outtmpl": outtmpl,
                "format": ytdlp_format_for_height(target_height),
                "merge_output_format": "mp4",
                "noplaylist": True,
                "socket_timeout": 60,
                "retries": 5,
                "fragment_retries": 5,
                "buffersize": 1024 * 1024,
            }
            _apply_twitter_cookies(opts, url)

            safe_edit_text(
                chat_id,
                status_message_id,
                f"🎯 منصة: {platform}\n"
                f"⏳ جاري التحميل بجودة {quality_label} للتقسيم لـ {n_parts} أجزاء...",
            )
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = ydl.prepare_filename(info)

            if not path or not os.path.exists(path):
                safe_edit_text(chat_id, status_message_id, "❌ مقدرتش احمل الفيديو")
                return

            total_size = os.path.getsize(path)
            duration_secs = float((info or {}).get("duration") or duration_hint or 0)
            log.info(
                "split-download: %s quality=%s dur=%.1fs n=%d url=%s",
                format_size(total_size),
                quality_label,
                duration_secs,
                n_parts,
                url,
            )

            if not duration_secs:
                safe_edit_text(
                    chat_id,
                    status_message_id,
                    "❌ مش قادر أحدد مدة الفيديو عشان أقسمه. جرب خيار تاني.",
                )
                return

            if total_size > total_budget:
                log.warning(
                    "split: total %s > budget %d × %s",
                    format_size(total_size),
                    n_parts,
                    format_size(TELEGRAM_MAX_BYTES),
                )
                safe_edit_text(
                    chat_id,
                    status_message_id,
                    f"⚠️ حتى بعد تخفيض الجودة ({quality_label}) الفيديو "
                    f"({format_size(total_size)}) أكبر من {n_parts} × "
                    f"{format_size(TELEGRAM_MAX_BYTES)}. جرب 🎵 صوت MP3",
                )
                return

            safe_edit_text(
                chat_id,
                status_message_id,
                f"🎯 منصة: {platform}\n"
                f"✅ تم التحميل: {format_size(total_size)} ({quality_label})\n"
                f"✂️ جاري التقسيم لـ {n_parts} أجزاء...",
            )
            parts = split_video_parts(path, n_parts, duration_secs, tmpdir)

            for i, part_path in enumerate(parts, 1):
                part_size = os.path.getsize(part_path)
                if part_size > TELEGRAM_MAX_BYTES:
                    log.warning(
                        "split: part %d/%d too large: %s",
                        i,
                        n_parts,
                        format_size(part_size),
                    )
                    safe_edit_text(
                        chat_id,
                        status_message_id,
                        f"⚠️ الجزء {i} من {n_parts} ({format_size(part_size)}) "
                        f"أكبر من الحد ({format_size(TELEGRAM_MAX_BYTES)}). جرب 🎵 صوت MP3",
                    )
                    return
                safe_edit_text(
                    chat_id,
                    status_message_id,
                    f"🎯 منصة: {platform}\n"
                    f"⬆️ جاري إرسال الجزء {i} من {n_parts} ({format_size(part_size)})...",
                )
                send_video_with_retry(
                    chat_id,
                    part_path,
                    caption=f"🎯 {platform} — الجزء {i} من {n_parts} ({quality_label})",
                )
                log.info("split: sent part %d/%d", i, n_parts)

            try:
                bot.delete_message(chat_id, status_message_id)
            except Exception:
                pass
            bot.send_message(chat_id, PROMO_MSG)

    except Exception as e:
        log.exception("process_video_split failed url=%s n_parts=%d", url, n_parts)
        safe_edit_text(
            chat_id,
            status_message_id,
            truncate(f"❌ خطأ في التقسيم:\n{type(e).__name__}: {e}"),
        )


def process_video_compressed(chat_id, status_message_id, url, platform, meta=None):
    """Download at adaptive low resolution then compress with ffmpeg if still too large."""
    duration_hint = (meta or {}).get("duration") or 0
    target_height, quality_label = calculate_target_quality(
        duration_hint, TELEGRAM_MAX_BYTES
    )
    safe_edit_text(
        chat_id,
        status_message_id,
        f"🎯 منصة: {platform}\n⏳ جاري التحميل بجودة {quality_label}...",
    )
    try:
        ok, free = check_disk_space()
        if not ok:
            safe_edit_text(
                chat_id,
                status_message_id,
                f"❌ مساحة القرص غير كافية (متاح: {format_size(free)})",
            )
            return

        import yt_dlp

        with tempfile.TemporaryDirectory() as tmpdir:
            outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
            opts = {
                "quiet": True,
                "no_warnings": True,
                "outtmpl": outtmpl,
                "format": ytdlp_format_for_height(target_height),
                "merge_output_format": "mp4",
                "noplaylist": True,
                "socket_timeout": 60,
                "retries": 5,
                "fragment_retries": 5,
                "buffersize": 1024 * 1024,
            }
            _apply_twitter_cookies(opts, url)

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = ydl.prepare_filename(info)

            if not path or not os.path.exists(path):
                safe_edit_text(chat_id, status_message_id, "❌ مقدرتش احمل الفيديو")
                return

            size = os.path.getsize(path)
            duration = float((info or {}).get("duration") or 0)
            height = (info or {}).get("height") or "؟"
            log.info(
                "compress-download: %s height=%s duration=%.1fs url=%s",
                format_size(size),
                height,
                duration,
                url,
            )

            compressed_path = None
            try:
                if size <= TELEGRAM_MAX_BYTES:
                    # Already fits at low resolution — send directly
                    safe_edit_text(
                        chat_id,
                        status_message_id,
                        f"🎯 منصة: {platform}\n✅ الحجم {format_size(size)} — جاري الرفع...",
                    )
                    send_video_with_retry(
                        chat_id, path, caption=f"🎯 {platform} 🗜️ {height}p"
                    )
                else:
                    # Need ffmpeg re-encode to hit the size target
                    if not duration:
                        safe_edit_text(
                            chat_id,
                            status_message_id,
                            f"⚠️ الفيديو ({format_size(size)}) كبير جداً ومش قادر أحدد مدته "
                            f"عشان أضغطه. جرب 🎵 صوت MP3",
                        )
                        return
                    safe_edit_text(
                        chat_id,
                        status_message_id,
                        f"🎯 منصة: {platform}\n"
                        f"📥 تم التحميل: {format_size(size)}\n"
                        f"🗜️ جاري الضغط للـ {format_size(TELEGRAM_MAX_BYTES)}... (قد يستغرق دقيقة)",
                    )
                    compressed_path, comp_size = compress_to_target(
                        path, TELEGRAM_MAX_BYTES, duration
                    )
                    if comp_size > TELEGRAM_MAX_BYTES:
                        log.warning(
                            "compress still too large: %s > %s",
                            format_size(comp_size),
                            format_size(TELEGRAM_MAX_BYTES),
                        )
                        safe_edit_text(
                            chat_id,
                            status_message_id,
                            f"⚠️ حتى بعد الضغط الفيديو ({format_size(comp_size)}) أكبر من الحد. "
                            f"جرب 🎵 صوت MP3 بدل كده",
                        )
                        return
                    safe_edit_text(
                        chat_id,
                        status_message_id,
                        f"🎯 منصة: {platform}\n"
                        f"✅ بعد الضغط: {format_size(comp_size)} — جاري الرفع...",
                    )
                    send_video_with_retry(
                        chat_id,
                        compressed_path,
                        caption=f"🎯 {platform} 🗜️ مضغوط ({format_size(comp_size)})",
                    )

                try:
                    bot.delete_message(chat_id, status_message_id)
                except Exception:
                    pass
                bot.send_message(chat_id, PROMO_MSG)
            finally:
                if compressed_path and os.path.exists(compressed_path):
                    try:
                        os.remove(compressed_path)
                    except OSError:
                        pass

    except Exception as e:
        log.exception(
            "process_video_compressed failed for url=%s platform=%s", url, platform
        )
        safe_edit_text(
            chat_id,
            status_message_id,
            truncate(f"❌ خطأ في الضغط:\n{type(e).__name__}: {e}"),
        )


def send_audio_with_retry(chat_id, path, title, performer, max_retries=3):
    """Upload an audio file to Telegram with automatic retry."""
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            with open(path, "rb") as f:
                result = bot.send_audio(
                    chat_id, f, title=title, performer=performer, timeout=300
                )
            log.info(
                "send_audio: uploaded %s on attempt %d",
                format_size(os.path.getsize(path)),
                attempt,
            )
            return result
        except Exception as e:
            last_exc = e
            log.warning(
                "send_audio attempt %d/%d failed (%s): %s",
                attempt,
                max_retries,
                type(e).__name__,
                e,
            )
            if attempt < max_retries:
                time.sleep(5 * attempt)
    raise last_exc


def truncate(text, n=350):
    if not text:
        return ""
    return text if len(text) <= n else text[: n - 1] + "…"


def send_image_album(chat_id, urls, caption=None):
    if not urls:
        return
    sent_caption = False
    for i in range(0, len(urls), 10):
        chunk = urls[i : i + 10]
        media = []
        for u in chunk:
            if not sent_caption and caption:
                media.append(types.InputMediaPhoto(u, caption=caption))
                sent_caption = True
            else:
                media.append(types.InputMediaPhoto(u))
        bot.send_media_group(chat_id, media)


# ---------- Probing: figure out video vs images ----------


def probe_content(url, platform):
    """Returns (kind, image_urls, title, size_bytes, duration_secs, native_height).
    kind is 'video' or 'images'. size_bytes/duration_secs/native_height may be None."""
    if platform == "TikTok":
        data, err = fetch_tiktok_data(url)
        if data:
            title = data.get("title") or ""
            if data.get("images"):
                return "images", list(data["images"]), title, None, None, None
            if data.get("play"):
                size = data.get("size") or None
                if size:
                    size = int(size)
                dur = data.get("duration") or None
                if dur:
                    dur = float(dur)
                h = data.get("height") or None
                if h:
                    h = int(h)
                return "video", None, title, size, dur, h
        log.warning(
            "tikwm probe failed for url=%s err=%s; trying yt-dlp fallback", url, err
        )
        if "/photo/" in url:
            raise RuntimeError(err or "البوست ده مش متاح")
        try:
            info = yt_dlp_extract_info(url)
            title = (info or {}).get("title") or ""
            size = estimate_download_size(info)
            dur = float((info or {}).get("duration") or 0) or None
            h = (info or {}).get("height") or None
            log.info(
                "probe TikTok yt-dlp: title=%r size=%s dur=%s height=%s",
                title,
                format_size(size),
                dur,
                h,
            )
            return "video", None, title, size, dur, h
        except Exception as ydl_err:
            raise RuntimeError(f"{err or 'TikTok'} | yt-dlp: {ydl_err}")

    info = yt_dlp_extract_info(url)
    title = (info or {}).get("title") or ""
    images = collect_image_urls_from_info(info)
    if images:
        return "images", images, title, None, None, None
    size = estimate_download_size(info)
    dur = float((info or {}).get("duration") or 0) or None
    h = (info or {}).get("height") or None
    log.info(
        "probe %s: title=%r size=%s dur=%s height=%s",
        platform,
        title,
        format_size(size),
        dur,
        h,
    )
    return "video", None, title, size, dur, h


# ---------- Processors ----------


def process_video(chat_id, status_message_id, url, platform):
    safe_edit_text(
        chat_id, status_message_id, f"🎯 منصة: {platform}\n⏳ جاري التحميل..."
    )
    try:
        # ── Disk space guard ──────────────────────────────────────────────────
        ok, free = check_disk_space()
        if not ok:
            log.error(
                "process_video: insufficient disk space (free=%s)", format_size(free)
            )
            safe_edit_text(
                chat_id,
                status_message_id,
                f"❌ مساحة القرص غير كافية على السيرفر\n"
                f"المتاح: {format_size(free)} — المطلوب على الأقل: {format_size(MIN_FREE_DISK_BYTES)}",
            )
            return

        if platform == "TikTok":
            video_url = download_tiktok_no_wm(url)
            if video_url:
                # TikTok sends via direct URL — no disk I/O, no size limit from our side
                for attempt in range(1, 4):
                    try:
                        bot.send_video(
                            chat_id, video_url, caption=f"🎯 {platform}", timeout=120
                        )
                        break
                    except Exception as e:
                        log.warning(
                            "send_video (URL) attempt %d/3 failed: %s", attempt, e
                        )
                        if attempt < 3:
                            time.sleep(5 * attempt)
                        else:
                            raise
                try:
                    bot.delete_message(chat_id, status_message_id)
                except Exception:
                    pass
                return
            # tikwm failed: fall back to yt-dlp for /video/ URLs
            if "/photo/" in url:
                safe_edit_text(
                    chat_id, status_message_id, "❌ تيكتوك: البوست ده مش متاح"
                )
                return
            log.warning("tikwm video failed for url=%s; trying yt-dlp fallback", url)
            with tempfile.TemporaryDirectory() as tmpdir:
                path, _info = yt_dlp_tiktok_video_fallback(url, tmpdir)
                if not path or not os.path.exists(path):
                    safe_edit_text(chat_id, status_message_id, "مقدرتش احمل الفيديو")
                    return
                size = os.path.getsize(path)
                log.info("tiktok yt-dlp fallback: downloaded %s", format_size(size))
                if size > TELEGRAM_MAX_BYTES:
                    safe_edit_text(
                        chat_id,
                        status_message_id,
                        f"⚠️ الفيديو كبير ({format_size(size)}) وتيليجرام مش بيقبل أكتر من "
                        f"{format_size(TELEGRAM_MAX_BYTES)}. جرب 🎵 صوت بس",
                    )
                    return
                send_video_with_retry(chat_id, path, caption=f"🎯 {platform}")
                try:
                    bot.delete_message(chat_id, status_message_id)
                except Exception:
                    pass
            return

        # ── Non-TikTok: yt-dlp download ──────────────────────────────────────
        with tempfile.TemporaryDirectory() as tmpdir:
            log.info(
                "process_video: starting yt-dlp download url=%s platform=%s",
                url,
                platform,
            )
            safe_edit_text(
                chat_id,
                status_message_id,
                f"🎯 منصة: {platform}\n⏳ جاري التحميل... (قد يستغرق وقتاً للملفات الكبيرة)",
            )
            path, info = yt_dlp_download_video(url, tmpdir)
            if not path or not os.path.exists(path):
                log.error("process_video: yt-dlp returned no file for url=%s", url)
                safe_edit_text(chat_id, status_message_id, "مقدرتش احمل الفيديو")
                return
            size = os.path.getsize(path)
            log.info("process_video: downloaded %s to %s", format_size(size), path)
            if size > TELEGRAM_MAX_BYTES:
                log.warning(
                    "process_video: file too large for Telegram (%s > %s) url=%s",
                    format_size(size),
                    format_size(TELEGRAM_MAX_BYTES),
                    url,
                )
                safe_edit_text(
                    chat_id,
                    status_message_id,
                    f"⚠️ الفيديو كبير ({format_size(size)}) وتيليجرام مش بيقبل أكتر من "
                    f"{format_size(TELEGRAM_MAX_BYTES)}.\n"
                    f"جرب 🎵 صوت MP3 بدل كده — أو شغّل Local Bot API Server على السيرفر لحدود 2 جيجا",
                )
                return
            log.info("process_video: uploading %s to Telegram…", format_size(size))
            send_video_with_retry(chat_id, path, caption=f"🎯 {platform}")
            try:
                bot.delete_message(chat_id, status_message_id)
            except Exception:
                pass
    except Exception as e:
        log.exception("process_video failed for url=%s platform=%s", url, platform)
        friendly = instagram_friendly_error(platform, e)
        safe_edit_text(
            chat_id,
            status_message_id,
            friendly or truncate(f"❌ خطأ في الفيديو:\n{type(e).__name__}: {e}"),
        )


def process_mp3(chat_id, status_message_id, url, platform):
    safe_edit_text(
        chat_id, status_message_id, f"🎯 منصة: {platform}\n⏳ جاري التحميل..."
    )

    if platform == "TikTok":
        video_path = None
        mp3_path = None
        try:
            data, err = fetch_tiktok_data(url)
            title = "audio"
            if data and data.get("play"):
                title = data.get("title") or "audio"
                video_path = download_to_file(data["play"], ".mp4")
            elif "/photo/" not in url:
                # tikwm couldn't find an audio source; try yt-dlp fallback for /video/
                log.warning(
                    "tikwm mp3 failed for url=%s err=%s; trying yt-dlp fallback",
                    url,
                    err,
                )
                with tempfile.TemporaryDirectory() as tmpdir:
                    try:
                        path, info = yt_dlp_download_audio(url, tmpdir)
                        title = (info or {}).get("title") or "audio"
                        # Move out of the temp dir so it survives the with-block
                        fd, dst = tempfile.mkstemp(suffix=".mp3")
                        os.close(fd)
                        shutil.copyfile(path, dst)
                        mp3_path = dst
                    except Exception as ydl_err:
                        safe_edit_text(
                            chat_id,
                            status_message_id,
                            truncate(f"❌ تيكتوك:\n{err or ''} | yt-dlp: {ydl_err}"),
                        )
                        return
            else:
                safe_edit_text(
                    chat_id,
                    status_message_id,
                    truncate(f"❌ تيكتوك:\n{err or 'البوست ده مش متاح'}"),
                )
                return

            if not video_path and not mp3_path:
                safe_edit_text(chat_id, status_message_id, "مقدرتش احمل الفيديو")
                return

            if not mp3_path:
                fd, mp3_path = tempfile.mkstemp(suffix=".mp3")
                os.close(fd)
                ok, err2 = extract_mp3(video_path, mp3_path)
                if not ok:
                    safe_edit_text(
                        chat_id,
                        status_message_id,
                        truncate(f"❌ فشل استخراج الصوت:\n{err2}"),
                    )
                    return
            mp3_size = os.path.getsize(mp3_path)
            if mp3_size > TELEGRAM_MAX_BYTES:
                log.warning(
                    "process_mp3 TikTok: audio too large (%s > %s)",
                    format_size(mp3_size),
                    format_size(TELEGRAM_MAX_BYTES),
                )
                safe_edit_text(
                    chat_id,
                    status_message_id,
                    f"⚠️ الملف كبير جداً ({format_size(mp3_size)}) وتيليجرام مش بيقبل أكتر من "
                    f"{format_size(TELEGRAM_MAX_BYTES)}",
                )
                return
            log.info("process_mp3 TikTok: uploading %s", format_size(mp3_size))
            send_audio_with_retry(
                chat_id, mp3_path, title=safe_filename(title), performer="TikTok"
            )
            try:
                bot.delete_message(chat_id, status_message_id)
            except Exception:
                pass
        except Exception as e:
            log.error(
                "process_mp3 (TikTok) failed for url=%s\n%s",
                url,
                traceback.format_exc(),
            )
            safe_edit_text(
                chat_id,
                status_message_id,
                truncate(f"❌ خطأ:\n{type(e).__name__}: {e}"),
            )
        finally:
            for p in (video_path, mp3_path):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        return

    # Other platforms: yt-dlp
    try:
        # Disk space guard
        ok, free = check_disk_space()
        if not ok:
            log.error(
                "process_mp3: insufficient disk space (free=%s)", format_size(free)
            )
            safe_edit_text(
                chat_id,
                status_message_id,
                f"❌ مساحة القرص غير كافية (متاح: {format_size(free)})",
            )
            return
        with tempfile.TemporaryDirectory() as tmpdir:
            log.info(
                "process_mp3: starting yt-dlp audio download url=%s platform=%s",
                url,
                platform,
            )
            mp3_path, info = yt_dlp_download_audio(url, tmpdir)
            if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) == 0:
                log.error("process_mp3: yt-dlp returned empty/no file for url=%s", url)
                safe_edit_text(chat_id, status_message_id, "مقدرتش استخرج الصوت")
                return
            mp3_size = os.path.getsize(mp3_path)
            log.info(
                "process_mp3: downloaded %s from %s", format_size(mp3_size), platform
            )
            if mp3_size > TELEGRAM_MAX_BYTES:
                log.warning(
                    "process_mp3: audio too large (%s > %s) url=%s",
                    format_size(mp3_size),
                    format_size(TELEGRAM_MAX_BYTES),
                    url,
                )
                safe_edit_text(
                    chat_id,
                    status_message_id,
                    f"⚠️ الملف كبير جداً ({format_size(mp3_size)}) وتيليجرام مش بيقبل أكتر من "
                    f"{format_size(TELEGRAM_MAX_BYTES)}",
                )
                return
            title = info.get("title") or "audio"
            log.info("process_mp3: uploading %s to Telegram…", format_size(mp3_size))
            send_audio_with_retry(
                chat_id, mp3_path, title=safe_filename(title), performer=platform
            )
            try:
                bot.delete_message(chat_id, status_message_id)
            except Exception:
                pass
    except Exception as e:
        log.error(
            "process_mp3 (%s) failed for url=%s\n%s",
            platform,
            url,
            traceback.format_exc(),
        )
        friendly = instagram_friendly_error(platform, e)
        safe_edit_text(
            chat_id,
            status_message_id,
            friendly or truncate(f"❌ خطأ:\n{type(e).__name__}: {e}"),
        )


def process_images(chat_id, status_message_id, image_urls, platform):
    safe_edit_text(
        chat_id,
        status_message_id,
        f"🎯 منصة: {platform}\n🖼️ بحمل {len(image_urls)} صورة...",
    )
    try:
        caption = f"🎯 منصة: {platform} | 📸 عدد الصور: {len(image_urls)}"
        send_image_album(chat_id, image_urls, caption=caption)
        try:
            bot.delete_message(chat_id, status_message_id)
        except Exception:
            pass
    except Exception as e:
        log.exception("process_images failed")
        safe_edit_text(
            chat_id,
            status_message_id,
            truncate(f"❌ فشل إرسال الصور:\n{type(e).__name__}: {e}"),
        )


# ---------- Handlers ----------


@bot.message_handler(commands=["debug"])
def handle_debug(message):
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    match = URL_REGEX.search(arg)
    url = match.group(0) if match else ""
    if not url:
        bot.reply_to(message, "Send like this:\n/debug TIKTOK_URL")
        return

    cleaned = clean_tiktok_url(url)
    resp = fetch_tiktok_raw(cleaned)
    err = resp.get("_error")
    code = resp.get("code")
    api_msg = resp.get("msg")
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    images = data.get("images") if isinstance(data, dict) else None
    has_video = bool(data.get("play")) if isinstance(data, dict) else False
    music = data.get("music") if isinstance(data, dict) else None

    lines = [
        "🔎 tikwm raw response:",
        f"• input URL: {url}",
        f"• cleaned URL: {cleaned}" if cleaned != url else None,
        f"• HTTP/transport error: {err}" if err else f"• code: {code}",
        f"• msg: {api_msg}" if api_msg and not err else None,
        f"• data.images: {len(images) if images else 0}",
        f"• data.video (play): {'yes' if has_video else 'no'}",
        f"• data.music: {'yes' if music else 'no'}",
    ]
    if isinstance(data, dict) and data:
        lines.append(f"• data keys: {', '.join(list(data.keys())[:15])}")
    if images:
        lines.append("• first image:")
        lines.append(str(images[0])[:200])
    body = "\n".join(l for l in lines if l)
    bot.reply_to(message, truncate(body, 3500))


INSTAGRAM_DISABLED_MSG = (
    "❌ عذراً، تحميل فيديوهات انستجرام محظور حالياً\n\n"
    "السبب: سيرفرات انستجرام عاملة حظر على كل سيرفرات التحميل بسبب الصيانة\n\n"
    "✅ البديل: ابعتلي لينك من تيك توك أو يوتيوب أو فيسبوك وهحملهولك فوراً\n\n"
    "هنرجع نشغل انستجرام أول ما المشكلة تتحل من عندهم"
)


@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(
        message,
        "ابعتلي رابط من تيكتوك / يوتيوب / انستجرام / فيسبوك / تويتر\n"
        "هتختار فيديو ولا MP3 من الأزرار 🎬🎵\n"
        "لو الرابط صور (سلايدشو أو كاروسيل) هحملهم على طول 🖼️\n"
        "اختصار: /mp3 ورابط للصوت بس\n\n"
        "⚠️ تنبيه: تحميل فيديوهات انستجرام متوقف مؤقتاً بسبب صيانة في "
        "سيرفرات انستجرام. نقدر نحمل من تيك توك ويوتيوب وفيسبوك عادي ✅",
    )


@bot.message_handler(commands=["mp3"])
def handle_mp3_command(message):
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    match = URL_REGEX.search(arg)
    url = match.group(0) if match else ""

    if not url:
        bot.reply_to(message, "Send like this: /mp3 URL")
        return

    platform = detect_platform(url)
    if not platform:
        bot.reply_to(message, "المنصة دي مش مدعومة")
        return

    if platform == "Instagram":
        bot.reply_to(message, INSTAGRAM_DISABLED_MSG)
        return

    if platform == "TikTok":
        url = clean_tiktok_url(url)

    status = bot.reply_to(message, f"🎯 منصة: {platform}\n⏳ جاري التحميل...")
    process_mp3(message.chat.id, status.message_id, url, platform)


@bot.message_handler(func=lambda message: True)
def handle_link(message):
    text = (message.text or "").strip()
    match = URL_REGEX.search(text)
    url = match.group(0) if match else ""

    if not url:
        bot.reply_to(
            message, "ابعت رابط من تيكتوك / يوتيوب / انستجرام / فيسبوك / تويتر"
        )
        return

    platform = detect_platform(url)
    if not platform:
        bot.reply_to(
            message,
            "المنصة دي مش مدعومة. جرب تيكتوك أو يوتيوب أو انستجرام أو فيسبوك أو تويتر",
        )
        return

    if platform == "Instagram":
        bot.reply_to(message, INSTAGRAM_DISABLED_MSG)
        return

    if platform == "TikTok":
        url = clean_tiktok_url(url)

    status = bot.reply_to(message, f"🎯 منصة: {platform}\n⏳ جاري التحقق...")

    try:
        kind, image_urls, _title, size_bytes, duration_secs, native_height = (
            probe_content(url, platform)
        )
    except Exception as e:
        log.exception("probe failed for url=%s", url)
        friendly = instagram_friendly_error(platform, e)
        safe_edit_text(
            message.chat.id,
            status.message_id,
            friendly
            or truncate(f"🎯 منصة: {platform}\n❌ خطأ:\n{type(e).__name__}: {e}"),
        )
        return

    if kind == "images":
        process_images(message.chat.id, status.message_id, image_urls, platform)
        return

    meta = {
        "size": size_bytes,
        "duration": duration_secs,
        "native_height": native_height,
    }
    key = remember_url(url, platform, meta=meta)

    is_large = bool(size_bytes and size_bytes > TELEGRAM_MAX_BYTES)
    size_line = f"\n📦 الحجم المتوقع: {format_size(size_bytes)}" if size_bytes else ""

    compress_label = None
    split_options = []

    if is_large:
        _h, ql_c = calculate_target_quality(duration_secs, TELEGRAM_MAX_BYTES)
        compress_label = f"🗜️ ضغط ({ql_c})"
        split_options = compute_split_options(size_bytes, duration_secs, native_height)
        n_opts = len(split_options)
        warn_line = (
            f"\n⚠️ الفيديو كبير — تيليجرام بيقبل حتى {format_size(TELEGRAM_MAX_BYTES)}.\n"
            f"• ضغط: تحميل بـ {ql_c} وضغط ffmpeg\n"
            f"• تقسيم: {n_opts} خيار متاح — كل جزء مرقّم بالترتيب"
        )
    else:
        warn_line = ""

    safe_edit_text(
        message.chat.id,
        status.message_id,
        f"🎯 منصة: {platform}{size_line}{warn_line}\nاختار تحب تحمله إزاي؟",
    )
    try:
        bot.edit_message_reply_markup(
            message.chat.id,
            status.message_id,
            reply_markup=build_choice_keyboard(
                key,
                compress_label=compress_label,
                split_options=split_options,
            ),
        )
    except Exception:
        pass


_VALID_ACTIONS = ("v", "a", "c", "sp")


@bot.callback_query_handler(
    func=lambda c: bool(c.data) and c.data.split(":")[0] in _VALID_ACTIONS
)
def handle_choice(call):
    try:
        action, raw_tail = call.data.split(":", 1)
    except ValueError:
        bot.answer_callback_query(call.id, "خطأ في البيانات")
        return

    # "sp" callbacks carry extra routing: sp:{height}:{n_parts}:{key}
    if action == "sp":
        try:
            h_str, n_str, key = raw_tail.split(":", 2)
            sp_height = int(h_str)
            sp_n_parts = int(n_str)
        except (ValueError, TypeError):
            bot.answer_callback_query(call.id, "خطأ في البيانات")
            return
        url, platform, meta = pop_url(key)
    else:
        key = raw_tail
        url, platform, meta = pop_url(key)
        sp_height = None
        sp_n_parts = None

    chat_id = call.message.chat.id
    message_id = call.message.message_id

    if not url:
        try:
            bot.edit_message_text(
                "الرابط ده انتهت صلاحيته، ابعته تاني",
                chat_id,
                message_id,
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id, "Loading...")

    try:
        bot.edit_message_reply_markup(chat_id, message_id, reply_markup=None)
    except Exception:
        pass

    if action == "v":
        process_video(chat_id, message_id, url, platform)
    elif action == "c":
        process_video_compressed(chat_id, message_id, url, platform, meta=meta)
    elif action == "sp":
        process_video_split(
            chat_id,
            message_id,
            url,
            platform,
            n_parts=sp_n_parts,
            meta=meta,
            target_height=sp_height,
        )
    else:
        process_mp3(chat_id, message_id, url, platform)


def _update_ytdlp_at_startup():
    """Force-update yt-dlp on boot. Failure is non-fatal (logged only)."""
    try:
        log.info("updating yt-dlp to latest version...")
        result = subprocess.run(
            [
                "pip",
                "install",
                "-U",
                "--quiet",
                "--disable-pip-version-check",
                "yt-dlp",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            try:
                import yt_dlp

                log.info("yt-dlp updated OK (now at %s)", yt_dlp.version.__version__)
            except Exception:
                log.info("yt-dlp updated OK")
        else:
            log.warning(
                "yt-dlp update failed (rc=%d): %s",
                result.returncode,
                (result.stderr or result.stdout or "").strip()[:300],
            )
    except Exception as e:
        log.warning("yt-dlp update skipped: %s: %s", type(e).__name__, e)


def _heartbeat_thread():
    """Log a heartbeat every 60 seconds so we can confirm the process is alive."""
    import threading

    _start = time.time()

    def _beat():
        while True:
            time.sleep(60)
            uptime_min = int((time.time() - _start) / 60)
            log.info("heartbeat — uptime %d min — bot alive", uptime_min)

    t = threading.Thread(target=_beat, daemon=True)
    t.start()


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        print(
            "WARNING: ffmpeg not found in PATH; audio extraction will fail", flush=True
        )
    _update_ytdlp_at_startup()
    log_cookie_status_at_startup()
    _heartbeat_thread()
    print(
        "بوت التحميل شغال (TikTok / YouTube / Instagram / Facebook / Twitter)...",
        flush=True,
    )
    restart_count = 0
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as exc:
            restart_count += 1
            log.warning(
                "polling crashed (restart #%d): %s — retrying in 5s", restart_count, exc
            )
            time.sleep(5)
