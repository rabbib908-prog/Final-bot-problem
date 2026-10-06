import array
import asyncio
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import time
import zipfile

import imageio_ffmpeg
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

# ================= SETTINGS =================
# Telegram bot token from BotFather (use this token in only ONE running program).
BOT_TOKEN = "8843329986:AAFcwKRAUbV05uSRj28m1GJFmxwgqu2-tAc"

PASSWORD = "890890"   # each Telegram chat must send this once

BUILTIN_KEYS = [
    "sk_car_p6chtkf7y7r3NErUEq5uNL",
    "sk_car_scU89HposTgdbhw7WHDCqa",
    "sk_car_b3KnjZYcjU7x5GrcLChCqW",
    "sk_car_VoTpjfP37LNfEqLjhJBGCh",
]

VOICE_ID = "4877b818-c7fe-4c89-b1cf-eadf8e23da72"  # Rohan
MODEL_ID = "sonic-3"
LANGUAGE = "hi"
CARTESIA_VERSION = "2025-04-16"
CARTESIA_URL = "https://api.cartesia.ai/tts/bytes"

SAMPLE_RATE = 44100
SILENCE_THRESHOLD = 200      # quieter than this = silence (trimmed from clip edges)
EDGE_PAD_SEC = 0.04
KEEP_TOLERANCE_PCT = 0.03    # voice within 3% (or 0.10s) of the slot -> "no change needed"
KEEP_TOLERANCE_SEC = 0.10
SPEED_WARN_LOW = 0.5         # warn when the video would have to be slower than this
SPEED_WARN_HIGH = 2.0        # ... or faster than this
DEAD_COOLDOWN = 30 * 60      # key out of credits / invalid: skip 30 minutes
RATE_COOLDOWN = 20           # key busy: skip 20 seconds
WAIT_SEC = 4                 # wait for the other pieces of a long paste
# ============================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("retime-bot")
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# ---------------- saved state (authorised chats, added keys, switched-off keys) ----------------
STATE = {"auth": [], "extra": [], "disabled": []}
try:
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        STATE.update({k: v for k, v in json.load(f).items() if k in STATE})
except Exception:
    pass


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(STATE, f)
    except Exception as e:
        log.warning("could not save state: %s", e)


# ---------------- API keys ----------------
KEY_DEAD_UNTIL = {}   # key -> time until which it is skipped
KEY_STATE = {}        # key -> ok / limit / invalid / busy / error  (last known)
KEY_LOG = []          # notes shown at the end of a run
_current_key = None


def all_keys():
    out = [(k, True) for k in BUILTIN_KEYS]
    out += [(k, False) for k in STATE["extra"] if k not in BUILTIN_KEYS]
    return out


def enabled_keys():
    return [k for k, _ in all_keys() if k not in STATE["disabled"]]


def kid(key):
    return hashlib.sha1(key.encode()).hexdigest()[:8]


def find_key(h):
    return next((k for k, _ in all_keys() if kid(k) == h), None)


def key_label(key):
    keys = [k for k, _ in all_keys()]
    n = keys.index(key) + 1 if key in keys else 0
    return f"Key #{n} (…{key[-4:]})"


def classify(status, text):
    low = (text or "").lower()
    if status == 200:
        return "ok"
    if status in (401, 403):
        return "invalid"
    if status == 402 or "quota" in low or "credit" in low:
        return "limit"
    if status == 429 or "concurren" in low or "rate" in low:
        return "busy"
    if status in (400, 422):
        return "badrequest"
    return "error"


def call_cartesia(key, text):
    """One key, with a few retries for network / server errors. Returns (status, bytes, error_text)."""
    err = ""
    for retry in range(3):
        try:
            r = requests.post(
                CARTESIA_URL,
                headers={"X-API-Key": key, "Cartesia-Version": CARTESIA_VERSION, "Content-Type": "application/json"},
                json={
                    "model_id": MODEL_ID, "transcript": text,
                    "voice": {"mode": "id", "id": VOICE_ID}, "language": LANGUAGE,
                    "output_format": {"container": "mp3", "sample_rate": SAMPLE_RATE, "bit_rate": 128000},
                },
                timeout=120,
            )
        except requests.RequestException as e:
            err = f"network error: {e}"
            time.sleep(2 * (retry + 1))
            continue
        if r.status_code in (500, 502, 503, 504):
            err = f"HTTP {r.status_code}"
            time.sleep(2 * (retry + 1))
            continue
        if r.status_code == 200:
            return 200, r.content, ""
        return r.status_code, b"", r.text[:300]
    return 0, b"", err


def test_key(key):
    """Tiny request (about 2 credits). Returns state string."""
    status, _, err = call_cartesia(key, "ok")
    state = classify(status, err)
    KEY_STATE[key] = state
    if state == "ok":
        KEY_DEAD_UNTIL.pop(key, None)
    elif state in ("invalid", "limit"):
        KEY_DEAD_UNTIL[key] = time.time() + DEAD_COOLDOWN
    elif state == "busy":
        KEY_DEAD_UNTIL[key] = time.time() + RATE_COOLDOWN
    return state


def cartesia_tts(text):
    """Blocking. Uses the current key; when it fails, switches to the next usable key."""
    global _current_key
    last_error = "কোনো key চালু নেই"
    for _round in range(3):
        keys = enabled_keys()
        if not keys:
            raise RuntimeError("কোনো API key চালু নেই। /keys দিয়ে চালু করুন বা /addkey দিয়ে নতুন key দিন।")
        start = keys.index(_current_key) if _current_key in keys else 0
        now = time.time()
        usable = [keys[(start + a) % len(keys)] for a in range(len(keys))]
        usable = [k for k in usable if KEY_DEAD_UNTIL.get(k, 0) <= now]
        if not usable:
            wait = min(KEY_DEAD_UNTIL.get(k, 0) for k in keys) - now
            if wait > 60:
                break
            time.sleep(max(wait, 1))
            continue
        for key in usable:
            status, data, err = call_cartesia(key, text)
            if status == 200:
                if key != _current_key and _current_key is not None:
                    KEY_LOG.append(f"{key_label(key)} ব্যবহার শুরু হয়েছে।")
                _current_key = key
                KEY_STATE[key] = "ok"
                return data
            state = classify(status, err)
            last_error = f"{key_label(key)}: HTTP {status} {err}".strip()
            if state == "badrequest":
                raise RuntimeError(f"Cartesia এই লেখা নেয়নি ({last_error})")
            KEY_STATE[key] = state
            if state == "invalid":
                KEY_DEAD_UNTIL[key] = time.time() + DEAD_COOLDOWN
                KEY_LOG.append(f"{key_label(key)} ভুল বা ব্লক (HTTP {status})। বাদ দিলাম।")
            elif state == "limit":
                KEY_DEAD_UNTIL[key] = time.time() + DEAD_COOLDOWN
                KEY_LOG.append(f"{key_label(key)}-র লিমিট শেষ (HTTP {status})। পরের key-তে গেলাম।")
            elif state == "busy":
                KEY_DEAD_UNTIL[key] = time.time() + RATE_COOLDOWN
                KEY_LOG.append(f"{key_label(key)} এখন ব্যস্ত। পরের key-তে গেলাম।")
            else:
                KEY_DEAD_UNTIL[key] = time.time() + 10
                KEY_LOG.append(f"{key_label(key)}-এ সমস্যা ({last_error[:80]})। পরের key-তে গেলাম।")
    raise RuntimeError(f"সব API key-র লিমিট শেষ বা সমস্যা। /addkey দিয়ে নতুন key দিন, তারপর /retry। শেষ এরর: {last_error}")


# ---------------- transcript parsing ----------------
TSPAT = r"\d{1,2}(?:[:.]\d{1,3}){1,2}"
TS_RE = re.compile(r"\[?\s*(" + TSPAT + r")\s*[-\u2013\u2014]+\s*(" + TSPAT + r")\s*\]?")
QUOTES = " \t\r\n\"'\u201c\u201d\u2018\u2019[]()"


def to_seconds(ts):
    parts = re.split(r"([:.])", ts)
    nums, seps = parts[0::2], parts[1::2]
    if len(nums) == 2:
        return int(nums[0]) * 60 + int(nums[1])
    if seps[-1] == "." and seps[0] == ":":
        return int(nums[0]) * 60 + int(nums[1]) + float("0." + nums[2])
    return int(nums[0]) * 3600 + int(nums[1]) * 60 + int(nums[2])


def parse_transcript(text):
    """Returns [(start, end, label, paragraph)]. Paragraphs without a timestamp are skipped by the caller."""
    matches = list(TS_RE.finditer(text))
    items = []
    for i, m in enumerate(matches):
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():seg_end].strip(QUOTES + ":-")
        body = re.sub(r"\s+", " ", body).strip(QUOTES + ":-")
        if body:
            items.append((to_seconds(m.group(1)), to_seconds(m.group(2)), f"{m.group(1)}-{m.group(2)}", body))
    return items


# ---------------- audio ----------------
def run_ffmpeg(args, data):
    p = subprocess.run([FFMPEG, "-v", "error"] + args, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg error: " + p.stderr.decode(errors="ignore")[:300])
    return p.stdout


def mp3_to_pcm(mp3):
    return run_ffmpeg(["-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "pipe:1"], mp3)


def pcm_to_mp3(pcm):
    return run_ffmpeg(["-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
                       "-b:a", "128k", "-f", "mp3", "pipe:1"], pcm)


def pcm_to_wav(pcm):
    import wave
    bio = io.BytesIO()
    with wave.open(bio, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return bio.getvalue()


def trim_silence(pcm):
    s = array.array("h")
    s.frombytes(pcm[: len(pcm) // 2 * 2])
    n = len(s)
    first = next((i for i in range(n) if abs(s[i]) > SILENCE_THRESHOLD), None)
    if first is None:
        return pcm
    last = next(i for i in range(n - 1, -1, -1) if abs(s[i]) > SILENCE_THRESHOLD)
    pad = int(EDGE_PAD_SEC * SAMPLE_RATE)
    return s[max(0, first - pad): min(n, last + 1 + pad)].tobytes()


def peak_of(pcm):
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    return max((abs(v) for v in a), default=0)


VOICE_CACHE = {}   # (voice, text) -> trimmed pcm. A re-run (/retry) does not spend credits again.


def make_voice(text):
    """Blocking: Hindi voice at normal 1x speed, edges trimmed. Returns pcm bytes."""
    ck = (VOICE_ID, text)
    if ck in VOICE_CACHE:
        return VOICE_CACHE[ck]
    pcm = trim_silence(mp3_to_pcm(cartesia_tts(text)))
    if len(VOICE_CACHE) > 400:
        VOICE_CACHE.clear()
    VOICE_CACHE[ck] = pcm
    return pcm


def fmt_t(t):
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return (f"{h}:{m:02d}:{s:04.1f}" if h else f"{m:02d}:{s:04.1f}")


def fmt_ts_plain(t):
    t = int(round(t))
    return f"{t // 60:02d}:{t % 60:02d}"


# ---------------- the plan: where the video must get longer / shorter ----------------
def make_plan(items, voices):
    """items: [(start, end, label, text)], voices: [pcm bytes or None].
    The Hindi voice stays at 1x. For each slot we work out the new length (= voice length) and the video speed
    that makes the video fill it. Gaps between slots stay untouched."""
    rows, prev_new_end, prev_orig_end = [], 0.0, 0.0
    for i, ((s, e, label, text), pcm) in enumerate(zip(items, voices)):
        D = len(pcm) / 2 / SAMPLE_RATE if pcm is not None else None
        L = e - s if e > s else None
        notes = []
        if pcm is None:
            E, orig_e = (L if L else 0.0), (e if L else s)
            kind = "novoice"
        elif L is None:
            E, orig_e, kind = D, s + D, "badtime"
            notes.append("টাইমস্ট্যাম্প ঠিক নেই (শেষ সময় শুরুর সমান বা আগে)। ১x ধরা হয়েছে।")
        else:
            tol = max(KEEP_TOLERANCE_SEC, KEEP_TOLERANCE_PCT * L)
            if abs(D - L) <= tol:
                E, kind = L, "keep"
            else:
                E, kind = D, ("slow" if D > L else "fast")
            orig_e = e
        if i == 0:
            gap = s
        else:
            gap = s - prev_orig_end
            if gap < 0:
                notes.append("আগের টাইমস্ট্যাম্পের সাথে ওভারল্যাপ করছে।")
                gap = 0.0
        ns = prev_new_end + gap
        ne = ns + E
        speed = (L / E) if (L and E) else 1.0
        if kind in ("slow", "fast") and (speed < SPEED_WARN_LOW or speed > SPEED_WARN_HIGH):
            notes.append("স্পিড খুব বেশি বদলাতে হচ্ছে, ভিডিও অস্বাভাবিক লাগতে পারে। লেখা ছোট/বড় করে দেখতে পারেন।")
        rows.append(dict(n=i + 1, label=label, s=s, e=e, L=L, D=D, E=E, kind=kind, speed=speed,
                         ns=ns, ne=ne, notes=notes, pcm=pcm))
        prev_new_end, prev_orig_end = ne, orig_e
    return rows, prev_orig_end, prev_new_end


def build_audio(rows):
    """One track: every voice at 1x, placed at its NEW start time."""
    out = array.array("h")
    filled = 0
    for r in rows:
        if r["pcm"] is None:
            continue
        clip = array.array("h")
        clip.frombytes(r["pcm"][: len(r["pcm"]) // 2 * 2])
        off = int(round(r["ns"] * SAMPLE_RATE))
        end = off + len(clip)
        if len(out) < end:
            out.frombytes(bytes(2 * (end - len(out))))
        overlap = max(0, min(filled, end) - off)
        for i in range(overlap):                       # tiny overlaps are mixed, not cut
            v = out[off + i] + clip[i]
            out[off + i] = 32767 if v > 32767 else -32768 if v < -32768 else v
        if overlap < len(clip):
            out[off + overlap:end] = clip[overlap:]
        filled = max(filled, end)
    return out.tobytes()


def simple_report(rows):
    """Short report: only the parts whose video speed must change. Everything else stays 1x."""
    lines, slow, fast, wa
