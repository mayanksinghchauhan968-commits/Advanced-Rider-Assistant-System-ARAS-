"""
smart_helmet_server.py  —  v6  (Wake Word + Greeting + AUTO YOUTUBE + Auto-Open Links + Multilingual)
═══════════════════════════════════════════════════════════════════════════════
Features:
  • "AI" wake word detection on BT mic (always listening)
  • ✨ WAKE WORD GREETING - Says "Hey, how can I assist you today?" when woken
  • ✨ AUTO YOUTUBE PLAYBACK - says "play <video> on youtube" → finds it and
    opens it automatically in your default browser
  • ✨ AUTO-OPEN LINKS - navigation / maps / weather / hospital / petrol links
    are no longer just printed, they open automatically in your browser
  • Auto language detection — English / Hindi / Gujarati
  • Google Maps + YouTube hyperlink responses
  • ESP32-S3 receives status updates (IDLE/LISTENING/PROCESSING/PLAYING)

Install:
  pip install sounddevice numpy scipy groq gtts

Usage:
  python smart_helmet_server.py
═══════════════════════════════════════════════════════════════════════════════
"""

import socket
import threading
import os
import io
import time
import subprocess
import re
import urllib.parse
import urllib.request
import webbrowser

import numpy as np
import sounddevice as sd
import scipy.io.wavfile as wavfile
from groq import Groq
from gtts import gTTS

# ══════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════

GROQ_API_KEY      = ""

TRIGGER_HOST      = "0.0.0.0"
TRIGGER_PORT      = 6000              # ESP32-S3 status connection

SAMPLE_RATE       = 16000
CHANNELS          = 1

# BT Headset — partial name match (case-insensitive)
SHOW_DEVICES      = False             # set True once to find device names
BT_MIC_NAME       = "Mivi DuoPods A25"
BT_SPEAKER_NAME   = "Mivi DuoPods A25"

# Wake word detection window — only need ~2s of audio for "AI"
# (shorter clip = faster Whisper transcription = faster response)
WAKE_WINDOW_SEC      = 2.0
# Command recording — max duration after wake word
MAX_COMMAND_SEC      = 12.0
# Silence threshold to auto-stop recording (RMS of float32 audio)
SILENCE_RMS          = 0.008
SILENCE_DURATION     = 1.8              # seconds of silence → stop recording

# ── Wake word ONSET detection (event-driven, not polling) ──────────
# How many consecutive audio blocks above threshold before we even
# bother calling Whisper. Each block ≈ 1024 samples ≈ 64ms at 16kHz.
ONSET_BLOCKS_NEEDED  = 3                # ≈ 190ms of sustained sound
# Minimum gap between two wake-word checks (avoids hammering the API
# during continuous noise like wind/engine).
WAKE_COOLDOWN_SEC    = 1.0
# Fallback fixed threshold used until ambient calibration runs.
WAKE_TRIGGER_RMS     = 0.015
# Multiplier applied over measured ambient noise to get the real
# trigger threshold (set by calibrate_ambient_noise() at startup).
AMBIENT_MULTIPLIER   = 3.0
AMBIENT_RMS          = 0.005

# ══════════════════════════════════════════
# GLOBALS
# ══════════════════════════════════════════

groq_client    = Groq(api_key=GROQ_API_KEY)

# Circular buffer for wake word detection (always filled)
wake_buf_lock  = threading.Lock()
wake_frames    = []
wake_buf_secs  = 0.0

# Command recording
cmd_recording  = False
cmd_frames     = []
cmd_lock       = threading.Lock()

# ESP32 status socket (optional — works without it)
esp_conn       = None
esp_lock       = threading.Lock()

# ── Onset detection state (event-driven wake checking) ─────────────
onset_lock          = threading.Lock()
loud_block_count    = 0
last_check_time      = 0.0
wake_trigger_event  = threading.Event()

# ══════════════════════════════════════════
# YOUTUBE AUTO-PLAY  (no API key needed)
# ══════════════════════════════════════════

def youtube_search_and_play(query: str) -> bool:
    """Search YouTube and open the first result automatically in the browser."""
    try:
        print(f"[YOUTUBE] 🔍 Searching for: \"{query}\"...")
        search_url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)

        req  = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")

        video_ids = re.findall(r"watch\?v=(\S{11})", html)
        if not video_ids:
            print(f"[YOUTUBE] ❌ No results found for: {query}")
            return False

        video_id  = video_ids[0]
        video_url = f"https://www.youtube.com/watch?v={video_id}"

        print(f"[YOUTUBE] ▶️  Opening: {video_url}")
        webbrowser.open(video_url, new=2, autoraise=True)
        send_status("PLAYING")
        return True

    except Exception as e:
        print(f"[YOUTUBE] Playback error: {e}")
        return False

# ══════════════════════════════════════════
# DEVICE HELPERS
# ══════════════════════════════════════════

def list_audio_devices():
    host_apis      = sd.query_hostapis()
    host_api_names = [h['name'] for h in host_apis]

    print("\n" + "─"*72)
    print("  Available Audio Devices:")
    print("─"*72)
    for i, d in enumerate(sd.query_devices()):
        if d['max_input_channels'] > 0 and d['max_output_channels'] > 0:
            tag = "[IN+OUT]"
        elif d['max_input_channels'] > 0:
            tag = "[INPUT] "
        elif d['max_output_channels'] > 0:
            tag = "[OUTPUT]"
        else:
            continue
        hostapi_name = host_api_names[d['hostapi']] if d['hostapi'] < len(host_api_names) else "?"
        print(f"  {i:2d}  {tag}  [{hostapi_name:18s}]  {d['name']}")
    print("─"*72 + "\n")

# Host APIs that reliably start Bluetooth mic/speaker streams on Windows,
# in order of preference. "Windows WDM-KS" is deliberately deprioritized —
# it tries to take exclusive low-level control of the device, which most
# Bluetooth headset drivers reject with PortAudioError -9999.
HOSTAPI_PREFERENCE = ["MME", "Windows DirectSound", "Windows WASAPI"]
HOSTAPI_AVOID      = ["Windows WDM-KS"]

def find_device(name: str, kind: str):
    """
    Find the best matching device index for a partial name match.
    The same physical device (e.g. a BT headset) usually shows up multiple
    times — once per Windows host API (MME / DirectSound / WASAPI / WDM-KS).
    We rank candidates so a reliable host API is picked instead of whichever
    one happened to appear first.
    """
    if not name:
        return None

    host_apis      = sd.query_hostapis()
    host_api_names = [h['name'] for h in host_apis]

    candidates = []
    for i, d in enumerate(sd.query_devices()):
        if name.lower() not in d['name'].lower():
            continue
        if kind == 'input'  and d['max_input_channels']  <= 0:
            continue
        if kind == 'output' and d['max_output_channels'] <= 0:
            continue
        hostapi_name = host_api_names[d['hostapi']] if d['hostapi'] < len(host_api_names) else ""
        candidates.append((i, d['name'], hostapi_name))

    if not candidates:
        return None

    def sort_key(item):
        _, _, hostapi_name = item
        if hostapi_name in HOSTAPI_AVOID:
            return (2, 0)
        if hostapi_name in HOSTAPI_PREFERENCE:
            return (0, HOSTAPI_PREFERENCE.index(hostapi_name))
        return (1, 0)

    candidates.sort(key=sort_key)
    best_idx, best_name, best_host = candidates[0]
    print(f"[AUDIO] '{name}' → using device #{best_idx} '{best_name}' via {best_host or 'unknown host API'}")
    return best_idx

# ══════════════════════════════════════════
# ESP32 STATUS SENDER
# ══════════════════════════════════════════

def send_status(status: str):
    """Send status string to ESP32-S3 if connected: IDLE/LISTENING/PROCESSING/PLAYING"""
    global esp_conn
    with esp_lock:
        if esp_conn:
            try:
                esp_conn.sendall(f"STATUS:{status}\n".encode())
            except Exception:
                esp_conn = None

# ══════════════════════════════════════════
# AUDIO CALLBACK  (always running)
# ══════════════════════════════════════════

def audio_callback(indata, frames, time_info, status):
    global loud_block_count, last_check_time
    data = indata.copy()

    # Fill wake word rolling buffer
    with wake_buf_lock:
        wake_frames.append(data)
        total = sum(len(f) for f in wake_frames) / SAMPLE_RATE
        # Keep only last WAKE_WINDOW_SEC
        while len(wake_frames) > 1 and \
              sum(len(f) for f in wake_frames[1:]) / SAMPLE_RATE >= WAKE_WINDOW_SEC:
            wake_frames.pop(0)

    # Fill command buffer if recording, and skip onset detection meanwhile
    with cmd_lock:
        if cmd_recording:
            cmd_frames.append(data)
            return

    # ── Cheap per-block onset detection (runs in the audio thread,
    # so it must stay fast — just one RMS calc on ~1024 samples) ──
    block_rms = float(np.sqrt(np.mean(data ** 2)))
    with onset_lock:
        if block_rms >= WAKE_TRIGGER_RMS:
            loud_block_count += 1
        else:
            loud_block_count = 0

        if loud_block_count >= ONSET_BLOCKS_NEEDED:
            now = time.time()
            if now - last_check_time >= WAKE_COOLDOWN_SEC and not wake_trigger_event.is_set():
                last_check_time   = now
                loud_block_count  = 0
                wake_trigger_event.set()

# ══════════════════════════════════════════
# WAKE WORD DETECTION
# ══════════════════════════════════════════

def get_wake_audio() -> np.ndarray:
    with wake_buf_lock:
        if not wake_frames:
            return np.zeros((1,1), dtype=np.float32)
        return np.concatenate(wake_frames, axis=0)

def calibrate_ambient_noise(duration: float = 2.0):
    """
    Sample the room's ambient noise for a couple of seconds at startup and
    set the onset-detection threshold relative to it. This is the main fix
    for slow/noisy wake detection — a fixed threshold either misses the
    wake word in loud environments (bike engine, wind, traffic) or fires
    constantly on background noise, both of which cause delay because every
    false trigger still costs a Whisper API round trip.
    """
    global AMBIENT_RMS, WAKE_TRIGGER_RMS
    print(f"[CALIBRATE] Measuring ambient noise for {duration}s — stay quiet...")
    time.sleep(duration)
    audio = get_wake_audio()
    if audio is not None and len(audio) > 0:
        AMBIENT_RMS = float(np.sqrt(np.mean(audio ** 2)))
    WAKE_TRIGGER_RMS = max(AMBIENT_RMS * AMBIENT_MULTIPLIER, 0.012)
    print(f"[CALIBRATE] Ambient RMS = {AMBIENT_RMS:.4f}  →  trigger threshold = {WAKE_TRIGGER_RMS:.4f}")

def check_wake_word(audio: np.ndarray) -> bool:
    """Transcribe short audio clip and check for wake word."""
    if audio is None or len(audio) < SAMPLE_RATE * 0.5:
        return False
    try:
        # In-memory WAV — no disk write/delete round trip on every check
        audio_int16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        if audio_int16.ndim > 1:
            audio_int16 = audio_int16[:, 0]
        buf = io.BytesIO()
        wavfile.write(buf, SAMPLE_RATE, audio_int16)
        buf.seek(0)

        result = groq_client.audio.transcriptions.create(
            file=("wake_check.wav", buf.read()),
            model="whisper-large-v3-turbo",   # fast model for wake word
            response_format="text"
        )
        text = (result.strip() if isinstance(result, str) else result.text.strip()).lower()
        print(f"[WAKE] Heard: \"{text}\"")

        # Accept variations of "AI" — Whisper may transcribe it as
        # "a.i.", "a i", "ai", "hey ai", "ए आई", "એ આઈ", etc.
        triggers = [
            "ai",
            "a.i.",
            "a i",
            "hey ai",
            "ए आई",
            "एआई",
            "એ આઈ",
            "એઆઈ",
        ]
        return any(t in text for t in triggers)
    except Exception as e:
        print(f"[WAKE] Check error: {e}")
        return False

# ══════════════════════════════════════════
# COMMAND RECORDING
# ══════════════════════════════════════════

def record_command() -> np.ndarray | None:
    """Record until silence or MAX_COMMAND_SEC."""
    global cmd_recording, cmd_frames

    with cmd_lock:
        cmd_frames    = []
        cmd_recording = True

    print("[REC] Recording command...")
    send_status("LISTENING")

    start         = time.time()
    silence_start = None

    while True:
        time.sleep(0.1)
        elapsed = time.time() - start

        with cmd_lock:
            if not cmd_frames:
                continue
            recent = cmd_frames[-min(5, len(cmd_frames)):]

        # RMS of recent frames
        recent_audio = np.concatenate(recent, axis=0)
        rms = float(np.sqrt(np.mean(recent_audio ** 2)))

        if rms < SILENCE_RMS:
            if silence_start is None:
                silence_start = time.time()
            elif time.time() - silence_start >= SILENCE_DURATION:
                print(f"[REC] Silence detected after {elapsed:.1f}s")
                break
        else:
            silence_start = None

        if elapsed >= MAX_COMMAND_SEC:
            print("[REC] Max duration reached.")
            break

    with cmd_lock:
        cmd_recording = False
        if not cmd_frames:
            return None
        audio = np.concatenate(cmd_frames, axis=0)

    print(f"[REC] Captured {len(audio)/SAMPLE_RATE:.1f}s of audio")
    return audio

# ══════════════════════════════════════════
# AUDIO UTILS
# ══════════════════════════════════════════

def save_wav(audio: np.ndarray, path: str):
    audio_int16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    if audio_int16.ndim > 1:
        audio_int16 = audio_int16[:, 0]
    wavfile.write(path, SAMPLE_RATE, audio_int16)

def play_audio(mp3_path: str):
    print("[PLAY] Playing on BT headset...")
    send_status("PLAYING")
    speaker_idx = find_device(BT_SPEAKER_NAME, 'output')
    wav_path    = mp3_path.replace(".mp3", "_out.wav")
    subprocess.run([
        "ffmpeg", "-y", "-i", mp3_path,
        "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-sample_fmt", "s16", wav_path
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    rate, data = wavfile.read(wav_path)
    data_float = data.astype(np.float32) / 32767.0
    sd.play(data_float, samplerate=rate,
            device=speaker_idx, blocking=True)
    sd.wait()
    try: os.remove(wav_path)
    except: pass
    print("[PLAY] Done.")

# ══════════════════════════════════════════
# LANGUAGE DETECTION
# ══════════════════════════════════════════

LANG_MAP = {
    # spoken phrase → (gtts lang code, llm instruction, display name)
    "gujarati" : ("gu", "Reply in Gujarati language only.",         "Gujarati"),
    "ગુજરાતી"  : ("gu", "Reply in Gujarati language only.",         "Gujarati"),
    "hindi"    : ("hi", "Reply in Hindi language only.",            "Hindi"),
    "हिंदी"    : ("hi", "Reply in Hindi language only.",            "Hindi"),
    "english"  : ("en", "Reply in English language only.",          "English"),
}

DEFAULT_LANG = ("en", "Reply concisely in English.", "English")

def detect_language(text: str):
    """Check if user asked to switch language. Returns (tts_lang, llm_instr, name)"""
    tl = text.lower()
    for keyword, config in LANG_MAP.items():
        if keyword in tl:
            return config
    # Auto-detect by script
    if any('\u0A80' <= c <= '\u0AFF' for c in text):   # Gujarati unicode block
        return LANG_MAP["gujarati"]
    if any('\u0900' <= c <= '\u097F' for c in text):   # Devanagari (Hindi)
        return LANG_MAP["hindi"]
    return DEFAULT_LANG

# persistent language across turns
current_lang = list(DEFAULT_LANG)   # [tts_code, llm_instr, name]

# ══════════════════════════════════════════
# WAKE WORD GREETING
# ══════════════════════════════════════════

def speak_greeting(tts_lang: str):
    """Speak a friendly greeting when wake word is detected."""
    print("[GREETING] 🎤 Speaking greeting...")

    # Greeting messages in different languages
    greetings = {
        "en": "Hey, how can I assist you today?",
        "hi": "हेलो, मैं आपकी कैसे मदद कर सकता हूँ?",
        "gu": "હેલો, હું આજે તમને કેવી રીતે મદદ કરી શકું છું?"
    }

    greeting_msg = greetings.get(tts_lang, greetings["en"])
    mp3_path = "helmet_greeting.mp3"

    try:
        # Generate greeting speech
        tts = gTTS(greeting_msg, lang=tts_lang, slow=False)
        tts.save(mp3_path)

        # Play greeting
        play_audio(mp3_path)

        # Cleanup
        if os.path.exists(mp3_path):
            os.remove(mp3_path)
    except Exception as e:
        print(f"[GREETING] Error: {e}")

# ══════════════════════════════════════════
# LINK GENERATOR  (now auto-opens the link too)
# ══════════════════════════════════════════

def extract_links(text: str, user_query: str) -> list[dict]:
    """
    Generate useful hyperlinks based on LLM response content and user query.
    Returns list of {label, url} dicts.
    """
    links  = []
    tq     = user_query.lower()
    tr     = text.lower()

    # ── Navigation ───────────────────────────────────────────────
    nav_keywords = ["navigate", "direction", "route", "go to", "how to reach",
                    "where is", "nearest", "closest", "maps", "location",
                    "રસ્તો", "નેવિગેટ", "रास्ता", "नेविगेट"]
    if any(k in tq for k in nav_keywords):
        dest = re.sub(r'(navigate|directions?|route|go to|how to reach|where is|nearest|closest)\s*', '',
                      user_query, flags=re.IGNORECASE).strip()
        if dest:
            enc = urllib.parse.quote(dest)
            links.append({"label": f"📍 Maps: {dest[:30]}",
                          "url": f"https://www.google.com/maps/search/{enc}"})
            links.append({"label": f"🧭 Navigate to {dest[:25]}",
                          "url": f"https://www.google.com/maps/dir/?api=1&destination={enc}"})

    # ── YouTube ───────────────────────────────────────────────────
    yt_keywords = ["youtube", "video", "watch", "tutorial", "how to",
                   "વીડિઓ", "यूट्यूब", "वीडियो"]
    if any(k in tq for k in yt_keywords):
        query = re.sub(r'\b(youtube|video|watch|tutorial)\b', '',
                       user_query, flags=re.IGNORECASE).strip()
        if query:
            enc = urllib.parse.quote(query)
            links.append({"label": f"📺 YouTube: {query[:30]}",
                          "url": f"https://www.youtube.com/results?search_query={enc}"})

    # ── Weather ───────────────────────────────────────────────────
    if any(k in tq for k in ["weather", "rain", "temperature", "હવામાન", "मौसम"]):
        location = re.sub(r'\b(weather|rain|temperature|in|at|of)\b', '',
                          user_query, flags=re.IGNORECASE).strip()
        enc = urllib.parse.quote(location or "current location")
        links.append({"label": "🌤️ Weather Forecast",
                      "url": f"https://www.google.com/search?q=weather+{enc}"})

    # ── Fuel / Petrol ─────────────────────────────────────────────
    if any(k in tq for k in ["petrol", "fuel", "gas station", "pump",
                              "પેટ્રોલ", "पेट्रोल"]):
        links.append({"label": "⛽ Nearest Petrol Pump",
                      "url": "https://www.google.com/maps/search/petrol+pump+near+me"})

    # ── Hospital / Emergency ──────────────────────────────────────
    if any(k in tq for k in ["hospital", "doctor", "emergency", "accident",
                              "હોસ્પિટલ", "अस्पताल"]):
        links.append({"label": "🏥 Nearest Hospital",
                      "url": "https://www.google.com/maps/search/hospital+near+me"})

    return links

def open_links_automatically(links: list[dict]):
    """Open every generated link in the default browser, one tab each."""
    for lnk in links:
        try:
            print(f"[OPEN] 🌐 Opening: {lnk['label']} → {lnk['url']}")
            webbrowser.open(lnk['url'], new=2, autoraise=True)
        except Exception as e:
            print(f"[OPEN] Failed to open {lnk['url']}: {e}")

# ══════════════════════════════════════════
# AI PIPELINE
# ══════════════════════════════════════════

def transcribe(wav_path: str) -> str:
    print("[STT] Transcribing...")
    with open(wav_path, "rb") as f:
        result = groq_client.audio.transcriptions.create(
            file=(wav_path, f.read()),
            model="whisper-large-v3",
            response_format="text"
        )
    text = result.strip() if isinstance(result, str) else result.text.strip()
    print(f"[STT] → \"{text}\"")
    return text

def ask_llama(command: str, lang_instruction: str) -> str:
    print(f"[LLM] Asking LLaMA... (lang: {current_lang[2]})")
    response = groq_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a smart helmet voice assistant for a motorcycle rider. "
                    "Answer concisely in 1-2 sentences. "
                    "No markdown, no bullet points, plain speech only. "
                    f"{lang_instruction}"
                )
            },
            {"role": "user", "content": command}
        ],
        max_tokens=200
    )
    answer = response.choices[0].message.content.strip()
    print(f"[LLM] → \"{answer}\"")
    return answer

def process_command(audio: np.ndarray):
    global current_lang
    send_status("PROCESSING")

    try:
        wav_path = "helmet_cmd.wav"
        mp3_path = "helmet_reply.mp3"

        save_wav(audio, wav_path)
        print(f"[PIPE] WAV saved ({len(audio)/SAMPLE_RATE:.1f}s)")

        # Transcribe
        user_text = transcribe(wav_path)
        if not user_text:
            print("[PIPE] Empty — skipping.")
            send_status("IDLE")
            return

        # ✨ CHECK FOR YOUTUBE PLAY COMMAND  (auto-plays, like Spotify used to)
        if "youtube" in user_text.lower() and "play" in user_text.lower():
            print("\n[YOUTUBE] 🎬 YouTube play command detected!")
            # Extract video/song name
            video_query = re.sub(r'\b(play|on|from|youtube)\b', '', user_text, flags=re.IGNORECASE).strip()

            if video_query:
                if youtube_search_and_play(video_query):
                    detected = detect_language(user_text)
                    if detected[2] != current_lang[2]:
                        current_lang = list(detected)

                    tts_lang, _, lang_name = current_lang

                    confirm_msg = f"Now playing {video_query} on YouTube"
                    print(f"[TTS] Speaking: {confirm_msg}")
                    tts = gTTS(confirm_msg, lang=tts_lang)
                    tts.save(mp3_path)
                    play_audio(mp3_path)

                    for f in [wav_path, mp3_path]:
                        if os.path.exists(f): os.remove(f)
                    send_status("IDLE")
                    return
                else:
                    print("[YOUTUBE] Playback failed, falling back to LLM response")

        # Detect language switch request
        detected = detect_language(user_text)
        if detected[2] != current_lang[2]:
            print(f"[LANG] Switching to {detected[2]}")
            current_lang = list(detected)

        tts_lang, llm_instr, lang_name = current_lang

        # LLM
        answer = ask_llama(user_text, llm_instr)

        # Generate useful links and open them automatically
        links = extract_links(answer, user_text)
        if links:
            print("\n[LINKS] Relevant links:")
            for lnk in links:
                print(f"  {lnk['label']}")
                print(f"  → {lnk['url']}")
            print()
            open_links_automatically(links)

        # TTS
        print(f"[TTS] Generating speech in {lang_name}...")
        tts = gTTS(answer, lang=tts_lang)
        tts.save(mp3_path)

        # Play on BT headset
        play_audio(mp3_path)

        # Cleanup
        for f in [wav_path, mp3_path]:
            if os.path.exists(f): os.remove(f)

    except Exception as e:
        print(f"[PIPE] Error: {e}")
    finally:
        send_status("IDLE")

# ══════════════════════════════════════════
# WAKE WORD LOOP  (main thread)
# ══════════════════════════════════════════

def wake_word_loop():
    global current_lang
    print("\n[WAKE] Listening for 'AI'...\n")
    send_status("IDLE")

    while True:
        # Blocks until audio_callback fires the event on sound onset —
        # no fixed polling delay, so detection starts the instant you
        # start speaking instead of up to 2s later.
        triggered = wake_trigger_event.wait(timeout=0.5)
        if not triggered:
            continue
        wake_trigger_event.clear()

        # Don't check while command is being recorded/processed
        with cmd_lock:
            if cmd_recording:
                continue

        # Small buffer so the rolling window captures the tail of the
        # phrase too (onset fires ~190ms into the word, not at the end).
        time.sleep(0.3)

        audio = get_wake_audio()
        if audio is None or len(audio) < SAMPLE_RATE * 0.6:
            continue

        rms = float(np.sqrt(np.mean(audio ** 2)))
        print(f"[WAKE] Onset detected (RMS={rms:.4f}) — checking...")
        if not check_wake_word(audio):
            continue

        # ── Wake word confirmed ───────────────────────────────────
        print("\n" + "═"*45)
        print("  🎙️  AI detected!")
        print("═"*45 + "\n")
        send_status("WAKE")

        # ✨ SPEAK GREETING IN CURRENT LANGUAGE
        tts_lang, _, lang_name = current_lang
        speak_greeting(tts_lang)

        # Brief pause so user knows we heard them
        time.sleep(0.2)

        print("  Speak your command now...")
        print("═"*45 + "\n")

        # Clear wake buffer so it doesn't re-trigger
        with wake_buf_lock:
            wake_frames.clear()

        # Record command
        command_audio = record_command()

        if command_audio is None or len(command_audio) < SAMPLE_RATE * 0.5:
            print("[WAKE] Command too short — ignoring.")
            send_status("IDLE")
            continue

        # Process in thread so wake loop can keep running
        threading.Thread(
            target=process_command,
            args=(command_audio,),
            daemon=True
        ).start()

        # Short cooldown before listening again
        time.sleep(2.0)
        print("\n[WAKE] Listening for 'AI'...\n")

# ══════════════════════════════════════════
# ESP32-S3 STATUS SERVER
# ══════════════════════════════════════════

def handle_esp32(conn: socket.socket, addr):
    global esp_conn
    print(f"[ESP32] Connected from {addr}")
    with esp_lock:
        esp_conn = conn

    conn.settimeout(60)
    buf = ""
    try:
        while True:
            try:
                data = conn.recv(64).decode(errors="ignore")
            except socket.timeout:
                try: conn.sendall(b"PING\n")
                except: break
                continue
            if not data:
                break
            buf += data
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if line == "PING":
                    conn.sendall(b"PONG\n")
                elif line.startswith("LOG:"):
                    print(f"[ESP32] {line[4:]}")
    except Exception as e:
        print(f"[ESP32] Disconnected: {e}")
    finally:
        with esp_lock:
            esp_conn = None
        conn.close()
        print("[ESP32] Connection closed.")

def esp32_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((TRIGGER_HOST, TRIGGER_PORT))
    srv.listen(1)
    print(f"[ESP32] Status server on port {TRIGGER_PORT}")
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=handle_esp32,
                         args=(conn, addr), daemon=True).start()

# ══════════════════════════════════════════
# RESILIENT STREAM OPEN
# ══════════════════════════════════════════

def open_audio_stream(mic_idx):
    """
    Open the mic InputStream with fallbacks, so a flaky Bluetooth driver
    config doesn't just crash the whole program (PortAudioError -9999 etc).
    Tries, in order:
      1. The chosen device at SAMPLE_RATE (16kHz)
      2. The chosen device at its own native default sample rate
      3. The system's default input device at SAMPLE_RATE
      4. The system's default input device at its native default sample rate
    Returns an already-started stream. Raises RuntimeError if everything fails.
    """
    global SAMPLE_RATE

    attempts = []
    if mic_idx is not None:
        attempts.append((mic_idx, SAMPLE_RATE))
        try:
            dev_default_sr = int(round(sd.query_devices(mic_idx)['default_samplerate']))
            if dev_default_sr and dev_default_sr != SAMPLE_RATE:
                attempts.append((mic_idx, dev_default_sr))
        except Exception:
            pass
    attempts.append((None, SAMPLE_RATE))
    try:
        default_dev = sd.default.device[0]
        dev_default_sr = int(round(sd.query_devices(default_dev)['default_samplerate']))
        if dev_default_sr and dev_default_sr != SAMPLE_RATE:
            attempts.append((None, dev_default_sr))
    except Exception:
        pass

    last_err = None
    for dev, rate in attempts:
        label = f"device #{dev}" if dev is not None else "system default device"
        try:
            print(f"[AUDIO] Trying {label} @ {rate}Hz...")
            stream = sd.InputStream(
                samplerate = rate,
                channels   = CHANNELS,
                dtype      = 'float32',
                device     = dev,
                callback   = audio_callback,
                blocksize  = 1024
            )
            stream.start()
            if rate != SAMPLE_RATE:
                print(f"[AUDIO] ⚠️  Requested {SAMPLE_RATE}Hz not supported — using {rate}Hz instead.")
                SAMPLE_RATE = rate
            print(f"[AUDIO] ✓ Stream opened on {label} @ {rate}Hz\n")
            return stream
        except Exception as e:
            print(f"[AUDIO] ✗ Failed on {label} @ {rate}Hz → {e}")
            last_err = e
            continue

    raise RuntimeError(
        "Could not open ANY audio input stream.\n"
        "Try:\n"
        "  • Setting SHOW_DEVICES = True at the top of this file and picking\n"
        "    the MME or DirectSound entry for your headset (avoid WDM-KS).\n"
        "  • Re-pairing the Bluetooth headset / setting it as default mic\n"
        "    in Windows Sound settings.\n"
        f"Last error: {last_err}"
    )

# ══════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════

if __name__ == "__main__":
    print("═"*60)
    print("  Smart Helmet Voice Assistant  v6")
    print("  (Wake Word + Greeting + AUTO YOUTUBE + Auto-Open Links)")
    print("  Wake word : 'AI'")
    print("  Languages : English / Hindi / Gujarati")
    print(f"  BT Mic    : '{BT_MIC_NAME or 'system default'}'")
    print(f"  BT Spkr   : '{BT_SPEAKER_NAME or 'system default'}'")
    print("═"*60 + "\n")

    if SHOW_DEVICES:
        list_audio_devices()
        print("Set SHOW_DEVICES=False after finding your BT device name.\n")

    mic_idx     = find_device(BT_MIC_NAME,     'input')
    speaker_idx = find_device(BT_SPEAKER_NAME, 'output')

    print(f"[INFO] Mic     device : {mic_idx     if mic_idx     is not None else 'system default'}")
    print(f"[INFO] Speaker device : {speaker_idx if speaker_idx is not None else 'system default'}")

    if mic_idx is None and BT_MIC_NAME:
        print(f"\n[WARN] BT mic '{BT_MIC_NAME}' not found!")
        print("[WARN] Connect headset in Windows Bluetooth settings.")
        print("[WARN] Set as default mic in Sound settings.\n")

    # Start ESP32 status server in background
    threading.Thread(target=esp32_server, daemon=True).start()

    # Open audio input stream with automatic fallback (handles the
    # PortAudioError -9999 / WDM-KS issue some BT headsets hit on Windows)
    stream = open_audio_stream(mic_idx)

    try:
        print("[INFO] Audio stream open.\n")

        # ✨ Calibrate trigger threshold to the room's actual noise floor —
        # this is what stops engine/wind/traffic noise from causing
        # constant false wake checks (and the lag that comes with them).
        calibrate_ambient_noise(duration=2.0)

        print("[INFO] Say 'AI' to wake up!")
        print("[INFO] You'll get a greeting response:")
        print("       🇬🇧 English: 'Hey, how can I assist you today?'")
        print("       🇮🇳 Hindi: 'हेलो, मैं आपकी कैसे मदद कर सकता हूँ?'")
        print("       🇮🇳 Gujarati: 'હેલો, હું આજે તમને કેવી રીતે મદદ કરી શકું છું?'\n")
        print("[INFO] Example commands:")
        print("       'AI, play Hanuman Chalisa on YouTube'")
        print("       'AI, navigate to nearest hospital'")
        print("       'AI, what is the weather'\n")
        wake_word_loop()   # blocks forever
    finally:
        stream.stop()
        stream.close()
