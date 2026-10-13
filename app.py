import ssl
import asyncio
import threading
import logging
import re
import base64
import signal
import struct
import subprocess
import os
import time
import sqlite3
import warnings
import json
import urllib.request
import urllib.error
import fcntl
import io
import itertools
from urllib.parse import urlparse, urlunparse
from flask import Flask, render_template, request, jsonify, send_file, send_from_directory, url_for

import pymumble_py3 as pymumble

from sound_metadata import MetadataCache, sniff_mime
from audio_quality import (
    BYTES_PER_FRAME,
    FRAME_DURATION_SECONDS,
    SAMPLES_PER_FRAME,
    enqueue_latest,
    mix_pcm,
    pack_pcm,
    unpack_pcm,
)

def make_non_blocking(fd):
    """Sets a file descriptor to non-blocking mode."""
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR) 

warnings.filterwarnings("ignore", category=UserWarning, module='google.protobuf')

# Configuration
HOST = os.getenv("MUMBLE_HOST", "localhost")
PORT = int(os.getenv("MUMBLE_PORT", 64738))
USER = os.getenv("MUMBLE_USER", "SoundBot")
PASSWORD = os.getenv("MUMBLE_PASSWORD", "")
CHANNEL = os.getenv("MUMBLE_CHANNEL", "")
# 128 kbit/s is a transparent-ish mono music target. Murmur may negotiate it
# downward when its server-wide maximum bandwidth is lower.
MUMBLE_BITRATE = max(32_000, min(256_000, int(os.getenv("MUMBLE_BITRATE", "128000"))))
AUDIO_METRICS_INTERVAL = max(5.0, float(os.getenv("AUDIO_METRICS_INTERVAL", "30")))

# --- INVIDIOUS CONFIGURATION ---
INVIDIOUS_HOST = os.getenv("INVIDIOUS_HOST", "")
if INVIDIOUS_HOST and INVIDIOUS_HOST.endswith('/'):
    INVIDIOUS_HOST = INVIDIOUS_HOST[:-1]

INVIDIOUS_USER = os.getenv("INVIDIOUS_USER", "")
INVIDIOUS_PASS = os.getenv("INVIDIOUS_PASS", "")

FFMPEG_HEADERS = ""
AUTH_HEADER_VAL = ""

if INVIDIOUS_USER and INVIDIOUS_PASS:
    auth_str = f"{INVIDIOUS_USER}:{INVIDIOUS_PASS}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    AUTH_HEADER_VAL = f"Basic {b64_auth}"
    FFMPEG_HEADERS = f"Authorization: {AUTH_HEADER_VAL}\r\n"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOUNDS_DIR = os.path.join(BASE_DIR, "sounds")
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "stats.db")

os.makedirs(SOUNDS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)

# --- SSL PATCH ---
if not hasattr(ssl, 'wrap_socket'):
    def dummy_wrap_socket(sock, keyfile=None, certfile=None,
                          server_side=False, cert_reqs=ssl.CERT_NONE,
                          ssl_version=ssl.PROTOCOL_TLS, ca_certs=None,
                          do_handshake_on_connect=True,
                          suppress_ragged_eofs=True,
                          ciphers=None):
        context = ssl.SSLContext(ssl_version)
        if certfile or keyfile:
            context.load_cert_chain(certfile=certfile, keyfile=keyfile)
        if ca_certs:
            context.load_verify_locations(ca_certs)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context.wrap_socket(sock, server_side=server_side,
                                   do_handshake_on_connect=do_handshake_on_connect,
                                   suppress_ragged_eofs=suppress_ragged_eofs)
    ssl.wrap_socket = dummy_wrap_socket

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS stats (filename TEXT PRIMARY KEY, count INTEGER)')
        conn.commit()

init_db()

def update_stat(filename):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT INTO stats (filename, count) VALUES (?, 1) ON CONFLICT(filename) DO UPDATE SET count = count + 1', (filename,))
        conn.commit()

def get_stats():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM stats")
        return {row['filename']: row['count'] for row in cursor.fetchall()}

# Thumbnail qualities to try, best first. maxres does not exist for every
# video; medium (320x180, 16:9 without letterbox bars) always does.
THUMBNAIL_QUALITIES = ('maxres', 'maxresdefault', 'medium')
MAX_THUMBNAIL_BYTES = 5 * 1024 * 1024

def invidious_thumbnail_urls(thumbnails):
    """Candidate thumbnail URLs from an Invidious API response, all forced onto INVIDIOUS_HOST."""
    by_quality = {t.get('quality'): t.get('url') for t in thumbnails or [] if t.get('url')}
    parsed_proxy = urlparse(INVIDIOUS_HOST)
    urls = []
    for quality in THUMBNAIL_QUALITIES:
        raw = by_quality.get(quality)
        if not raw:
            continue
        parsed = urlparse(raw)
        # Same rule as for the audio stream: never contact Google directly.
        urls.append(urlunparse((parsed_proxy.scheme, parsed_proxy.netloc, parsed.path, '', parsed.query, '')))
    return urls

def fetch_thumbnail(urls):
    """First thumbnail that loads, as bytes, or None."""
    for url in urls:
        req = urllib.request.Request(url)
        if AUTH_HEADER_VAL:
            req.add_header("Authorization", AUTH_HEADER_VAL)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                if not response.headers.get_content_type().startswith('image/'):
                    continue
                data = response.read(MAX_THUMBNAIL_BYTES + 1)
        except Exception as e:
            print(f"[THUMBNAIL] {url}: {e}")
            continue
        # A missing maxres thumbnail can come back as YouTube's tiny grey
        # 120x90 placeholder instead of an error.
        if 2048 < len(data) <= MAX_THUMBNAIL_BYTES:
            return data
    return None

# --- STRICT CONFIGURED HOST REWRITE RESOLVER ---
def resolve_video_data(url):
    """Returns (stream_url, title, is_direct, thumbnail_urls); stream_url is None on failure."""
    # 1. Match standard YouTube URLs OR extract the v= ID from a custom companion domain
    youtube_regex = r'(?:youtube\.com\/(?:[^\/]+\/.+\/|(?:v|e(?:mbed)?)\/|.*[?&]v=)|youtu\.be\/)([^"&?\/\s]{11})'
    match = re.search(youtube_regex, url)
    
    video_id = None
    if match:
        video_id = match.group(1)
    else:
        # Fallback: catch standard ?v= parameter from a pasted companion URL
        custom_match = re.search(r'[?&]v=([^"&?\/\s]{11})', url)
        if custom_match:
            video_id = custom_match.group(1)

    if video_id:
        # 2. STRICTLY enforce the use of the configured INVIDIOUS_HOST
        if not INVIDIOUS_HOST:
            print("[SECURITY BLOCK] INVIDIOUS_HOST not set. Blocking direct Google contact.")
            return None, None, False, []

        # 3. Fetch Metadata and Stream Data via API from the configured host ONLY
        title = f"YouTube ID: {video_id}"
        api_url = f"{INVIDIOUS_HOST}/api/v1/videos/{video_id}"
        
        req = urllib.request.Request(api_url)
        if AUTH_HEADER_VAL: 
            req.add_header("Authorization", AUTH_HEADER_VAL)
            
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())
                title = data.get('title', title)
                
                # Choose the highest-bitrate audio-only source exposed by the
                # configured proxy. Preferring a fixed 128 kbit/s AAC stream here
                # caused avoidable loss before Mumble's second (Opus) encode.
                formats = [f for f in data.get('adaptiveFormats', []) if 'audio' in f.get('type', '')]
                target_format = max(
                    formats,
                    key=lambda item: int(item.get('bitrate') or 0),
                    default=None,
                )
                    
                if not target_format or 'url' not in target_format:
                    print("[API FAIL] No audio streams found in API response.")
                    return None, None, False, []
                    
                raw_stream_url = target_format['url']
                
                # 5. Intercept the googlevideo.com domain and rewrite it strictly to INVIDIOUS_HOST
                if "googlevideo.com" in raw_stream_url:
                    parsed_stream = urlparse(raw_stream_url)
                    parsed_proxy = urlparse(INVIDIOUS_HOST)
                    
                    # Force the instance to proxy the traffic so we don't touch Google
                    query = parsed_stream.query
                    if "local=true" not in query:
                        # Handle case where query might be empty
                        query += "&local=true" if query else "local=true"
                        
                    final_stream_url = urlunparse((
                        parsed_proxy.scheme, 
                        parsed_proxy.netloc, 
                        parsed_stream.path, 
                        parsed_stream.params, 
                        query, 
                        parsed_stream.fragment
                    ))
                    
                    print(f"[DEBUG] Intercepted stream, routing strictly via configured Invidious: {final_stream_url}")
                    thumbnails = invidious_thumbnail_urls(data.get('videoThumbnails'))
                    return final_stream_url, f"YouTube: {title}", True, thumbnails
                    
        except Exception as e:
            print(f"[API FAIL] Could not fetch data from configured host {INVIDIOUS_HOST}: {e}")
            return None, None, False, []

    # Allow non-YouTube URLs (e.g., direct mp3 links) to pass through to yt-dlp naturally
    return url, "External Stream", False, []

class AudioEngine:
    def __init__(self):
        self.active_processes = []
        self.volume_local = 0.75  
        self.volume_remote = 0.50 
        self.lock = threading.Lock()
        self.current_metadata = None 
        # The running web stream: {'id', 'title', 'link', 'thumbnail'}.
        self.stream_info = None
        self._stream_ids = itertools.count(1)
        self.MAX_CHANNELS = 8 

    def _kill_process(self, p):
        try:
            if p.pid:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except: pass
        
        source = getattr(p, 'source_proc', None)
        if source and source.pid:
            try: os.killpg(os.getpgid(source.pid), signal.SIGKILL)
            except: pass

    def _cleanup_dead_and_limit(self):
        self.active_processes = [p for p in self.active_processes if p.poll() is None]
        if len(self.active_processes) >= self.MAX_CHANNELS:
            oldest = self.active_processes.pop(0)
            self._kill_process(oldest)

    def play_file(self, filepath, filename):
        self.current_metadata = {'type': 'file', 'text': filename, 'link': None}
        
        with self.lock:
            remaining = []
            for p in self.active_processes:
                if getattr(p, 'tag_filename', None) == filename:
                    self._kill_process(p)
                else:
                    remaining.append(p)
            self.active_processes = remaining

            self._cleanup_dead_and_limit()

            cmd = ['ffmpeg', '-re', '-i', filepath, '-f', 's16le', '-ac', '1', '-ar', '48000', '-']
            self._start_process_internal(cmd, source_type='local', tag_filename=filename)

    def _stop_existing_remote(self):
        with self.lock:
            remaining = []
            for p in self.active_processes:
                if getattr(p, 'source_type', 'local') == 'remote':
                    self._kill_process(p)
                else:
                    remaining.append(p)
            self.active_processes = remaining

    def play_direct_stream(self, url, display_title):
        print(f"[DEBUG] FFMPEG Connecting to Direct Stream: {url}")
        self._stop_existing_remote() 
        self.current_metadata = {'type': 'url', 'text': display_title, 'link': url}
        self.stream_info = self._new_stream_info(display_title, url)
        
        cmd = ['ffmpeg']
        if FFMPEG_HEADERS:
             cmd.extend(['-headers', f"Authorization: {AUTH_HEADER_VAL}\r\n"])
             
        # Add reconnect flags here before the input (-i) flag
        cmd.extend([
            '-reconnect', '1', 
            '-reconnect_streamed', '1', 
            '-reconnect_delay_max', '5',
            '-i', url, 
            '-f', 's16le', '-ac', '1', '-ar', '48000', '-'
        ])
        
        with self.lock:
            self._start_process_internal(cmd, capture_stderr=True, source_type='remote')

    def play_via_ytdlp(self, url, display_title):
        print(f"[DEBUG] Processing via yt-dlp: {url}")
        self._stop_existing_remote()
        self.current_metadata = {'type': 'url', 'text': display_title, 'link': url}
        self.stream_info = self._new_stream_info(display_title, url)
        
        dlp_cmd = ['yt-dlp', '--no-cache-dir', '--yes-playlist']
        
        if AUTH_HEADER_VAL:
            dlp_cmd.extend(['--add-header', f"Authorization: {AUTH_HEADER_VAL}"])
        
        cookie_file = os.path.join(DATA_DIR, 'cookies.txt')
        if os.path.exists(cookie_file):
             dlp_cmd.extend(['--cookies', cookie_file])

        dlp_cmd.extend(['-f', 'bestaudio/best', '--force-ipv4', '--no-check-certificate', '-o', '-'])
        dlp_cmd.append(url)

        try:
            p_dlp = subprocess.Popen(
                dlp_cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid
            )
        except Exception as e:
            print(f"[ERROR] yt-dlp fail: {e}")
            return

        def log_dlp_errors(proc):
            for line in iter(proc.stderr.readline, b''):
                print(f"[YT-DLP ERROR] {line.decode('utf-8', errors='ignore').strip()}")
            proc.stderr.close()
        t = threading.Thread(target=log_dlp_errors, args=(p_dlp,), daemon=True)
        t.start()

        ffmpeg_cmd = ['ffmpeg', '-i', 'pipe:0', '-f', 's16le', '-ac', '1', '-ar', '48000', '-']
        
        try:
            p_ffmpeg = subprocess.Popen(ffmpeg_cmd, stdin=p_dlp.stdout, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
        except:
            try: os.killpg(os.getpgid(p_dlp.pid), signal.SIGKILL)
            except: pass
            return
        
        p_dlp.stdout.close()
        p_ffmpeg.source_proc = p_dlp 
        p_ffmpeg.source_type = 'remote' 
        p_ffmpeg.tag_filename = None
        
        make_non_blocking(p_ffmpeg.stdout.fileno())
        
        with self.lock:
            self.active_processes.append(p_ffmpeg)

    def _new_stream_info(self, title, link):
        return {'id': next(self._stream_ids), 'title': title, 'link': link, 'thumbnail': None}

    def set_stream_thumbnail(self, info, data):
        """Attach a thumbnail, unless another stream has started in the meantime."""
        with self.lock:
            if self.stream_info is info:
                self.stream_info = dict(info, thumbnail=data)

    def now_playing(self):
        """(newest still-running sound button filename or None, web stream info or None)."""
        with self.lock:
            alive = [p for p in self.active_processes if p.poll() is None]
            sound = next((p.tag_filename for p in reversed(alive)
                          if getattr(p, 'source_type', 'local') == 'local' and p.tag_filename), None)
            stream_alive = any(getattr(p, 'source_type', 'local') == 'remote' for p in alive)
            return sound, self.stream_info if stream_alive else None

    def _start_process_internal(self, cmd, capture_stderr=False, source_type='local', tag_filename=None):
        stderr_dest = subprocess.PIPE if capture_stderr else subprocess.DEVNULL
        
        process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=stderr_dest,
            preexec_fn=os.setsid 
        )
        process.source_proc = None
        process.source_type = source_type 
        process.tag_filename = tag_filename 
        
        # NEW: Make the ffmpeg output non-blocking
        make_non_blocking(process.stdout.fileno())
        
        if capture_stderr:
            def log_errors(proc):
                for line in iter(proc.stderr.readline, b''):
                    print(f"[FFMPEG ERROR] {line.decode('utf-8', errors='ignore').strip()}")
                proc.stderr.close()
            t = threading.Thread(target=log_errors, args=(process,), daemon=True)
            t.start()
        
        self.active_processes.append(process)

    def stop_all(self):
        self.current_metadata = None
        self.stream_info = None
        with self.lock:
            for p in self.active_processes[:]: 
                self._kill_process(p)
            self.active_processes = []

    def set_volume_local(self, vol):
        self.volume_local = max(0.0, min(1.0, float(vol) / 100.0))

    def set_volume_remote(self, vol):
        self.volume_remote = max(0.0, min(1.0, float(vol) / 100.0))

    def get_chunk(self):
        """Next 20ms of everything that plays (sound buttons and web streams), mixed."""
        return mix_pcm(*self.get_chunks())

    def get_chunks(self):
        """Next 20ms as two separate mixes: (sound buttons, web streams).
        Each is 1920 bytes of s16le mono 48kHz, or None when nothing of that kind plays."""
        CHUNK_SIZE = BYTES_PER_FRAME
        mixed = {'local': [0] * SAMPLES_PER_FRAME, 'remote': [0] * SAMPLES_PER_FRAME}
        heard = set()

        with self.lock:
            if not self.active_processes:
                self.current_metadata = None
                return None, None
            current_procs = list(self.active_processes)

        active_now = []

        for p in current_procs:
            # Initialize a persistent buffer for this process
            if not hasattr(p, '_buffer'):
                p._buffer = bytearray()
                
            # If process is dead and buffer is empty, skip
            if p.poll() is not None and len(p._buffer) < CHUNK_SIZE:
                self._kill_process(p)
                continue
            
            try:
                # Calculate how many bytes we need to complete a chunk
                needed = CHUNK_SIZE - len(p._buffer)
                if needed > 0:
                    raw = p.stdout.read(needed)
                    if raw:
                        p._buffer.extend(raw)
                        
                # If we have collected a full chunk, process it
                if len(p._buffer) >= CHUNK_SIZE:
                    raw_chunk = bytes(p._buffer[:CHUNK_SIZE])
                    p._buffer = p._buffer[CHUNK_SIZE:] # Keep remainder
                    
                    samples = unpack_pcm(raw_chunk)
                    kind = 'local' if getattr(p, 'source_type', 'local') == 'local' else 'remote'
                    current_vol = self.volume_local if kind == 'local' else self.volume_remote
                    target = mixed[kind]
                    heard.add(kind)

                    for i, sample in enumerate(samples):
                        target[i] += int(sample * current_vol)

                    active_now.append(p)
                else:
                    # Not enough data yet. Check if it died or is just buffering.
                    if p.poll() is not None:
                        self._kill_process(p)
                    else:
                        active_now.append(p) # Keep alive, wait for network
                        
            except BlockingIOError:
                # Non-blocking IO exception: No data available right now, let it buffer
                active_now.append(p)
            except Exception as e:
                self._kill_process(p)

        with self.lock:
            self.active_processes = [p for p in self.active_processes if p in active_now]

        if not active_now:
            return None, None

        return tuple(pack_pcm(mixed[kind]) if kind in heard else None for kind in ('local', 'remote'))

audio_engine = AudioEngine()

# --- SCHNACKN (LiveKit) BOTS ---
# Two independent participants, each invited to one room from the web UI:
# SoundBot plays the sound buttons, DJ plays YouTube and other links. Listeners can
# turn the DJ down in Schnackn (per-person volume) and still hear the sound buttons.
# Each bot also shows its artwork (cover / video thumbnail) as a camera image.
from meet_bot import MeetBot
from artwork import Artwork, thumbnail

def sound_artwork():
    name, _ = audio_engine.now_playing()
    path = resolve_sound(name) if name else None
    if not path:
        return None
    try:
        meta = sound_metadata.metadata(path)
    except OSError:
        return None
    def load():
        art = sound_metadata.cover(path)
        return art[1] if art else None
    subtitle = ' · '.join(filter(None, [meta['artist'], meta['album'], meta['year']]))
    return Artwork(('sound', name), meta['title'] or name, subtitle or None, load)

def stream_artwork():
    _, stream = audio_engine.now_playing()
    if not stream:
        return None
    host = urlparse(stream['link']).hostname
    return Artwork(('stream', stream['id'], stream['thumbnail'] is not None),
                   stream['title'], host, lambda: stream['thumbnail'])

meet_bots = {
    'sound': MeetBot(audio_engine, os.getenv("MEET_BOT_NAME") or USER, label="SOUNDBOT", artwork=sound_artwork),
    'dj': MeetBot(audio_engine, os.getenv("MEET_DJ_NAME") or "DJ", label="DJ", artwork=stream_artwork),
}

# --- MATRIXRTC MEDIA (normal Matrix device; independent of AS commands) ---
from matrix_rtc import MatrixRTCClient, load_matrix_rtc_config
matrix_rtc = MatrixRTCClient(load_matrix_rtc_config())

# --- CENTRAL MIXER ---
# A single mixer thread pulls PCM from the audio engine at real-time pace
# (20ms chunks) and fans it out: sound buttons to the SoundBot, web streams to
# the DJ, and both mixed to Mumble.
import queue

# Keep at most 500 ms queued. Older audio is discarded instead of allowing
# latency to grow without bound when encoding or networking stalls.
mumble_queue = queue.Queue(maxsize=25)
audio_metrics = {
    'frames_produced': 0,
    'queue_drops': 0,
    'deadline_misses': 0,
    'mumble_underruns': 0,
    'peak_queue_depth': 0,
}
metrics_lock = threading.Lock()

def _increment_metric(name, amount=1):
    with metrics_lock:
        audio_metrics[name] += amount

def mixer_loop():
    next_tick = time.monotonic()
    next_report = next_tick + AUDIO_METRICS_INTERVAL
    while True:
        sound_chunk, stream_chunk = audio_engine.get_chunks()
        # feed Schnackn (non-blocking; drops chunk if consumer is behind)
        if sound_chunk:
            meet_bots['sound'].feed(sound_chunk)
        if stream_chunk:
            meet_bots['dj'].feed(stream_chunk)
        pcm_chunk = mix_pcm(sound_chunk, stream_chunk)
        if pcm_chunk:
            _increment_metric('frames_produced')
            # MatrixRTC receives the complete global mix through its bounded,
            # nonblocking queue; it never consumes AS transactions as media.
            matrix_rtc.feed(pcm_chunk)
            # feed Mumble (drop oldest chunk if consumer is behind)
            if enqueue_latest(mumble_queue, pcm_chunk):
                _increment_metric('queue_drops')
            with metrics_lock:
                audio_metrics['peak_queue_depth'] = max(
                    audio_metrics['peak_queue_depth'], mumble_queue.qsize()
                )
        next_tick += FRAME_DURATION_SECONDS
        now = time.monotonic()
        sleep_time = next_tick - now
        if sleep_time > 0:
            time.sleep(sleep_time)
        else:
            _increment_metric('deadline_misses')
            # Never burst old frames in an attempt to catch up.
            next_tick = now
        if now >= next_report:
            with metrics_lock:
                snapshot = dict(audio_metrics)
            print(f"[AUDIO METRICS] {snapshot}; queue_depth={mumble_queue.qsize()}")
            next_report = now + AUDIO_METRICS_INTERVAL

threading.Thread(target=mixer_loop, daemon=True).start()

def mumble_loop():
    while True:
        try:
            print(f"[MUMBLE] Connecting to {HOST}:{PORT} as {USER}...")
            mumble = pymumble.Mumble(HOST, USER, password=PASSWORD, port=PORT)
            mumble.start()
            mumble.is_ready()
            print("[MUMBLE] Connected.")

            try:
                mumble.set_bandwidth(MUMBLE_BITRATE)
                server_limit = getattr(mumble, 'server_max_bandwidth', None)
                effective = min(MUMBLE_BITRATE, server_limit) if server_limit else MUMBLE_BITRATE
                print(f"[MUMBLE] Requested Opus bandwidth: {MUMBLE_BITRATE} bit/s; "
                      f"server limit: {server_limit if server_limit else 'not advertised'}; "
                      f"effective target: {effective} bit/s")
            except Exception as e:
                print(f"[MUMBLE WARNING] Could not set requested bandwidth: {e}")

            if CHANNEL:
                print(f"[MUMBLE] Attempting to join channel: {CHANNEL}")
                time.sleep(2)
                target = None
                for channel_id, channel_obj in mumble.channels.items():
                    if channel_obj['name'] == CHANNEL:
                        target = channel_obj
                        break
                if target:
                    mumble.users.myself.move_in(target['channel_id'])

            while mumble.is_alive():
                try:
                    pcm_chunk = mumble_queue.get(timeout=FRAME_DURATION_SECONDS * 2)
                    mumble.sound_output.add_sound(pcm_chunk)
                except queue.Empty:
                    _increment_metric('mumble_underruns')
                    continue
        
        except Exception as e:
            print(f"[MUMBLE ERROR] Connection lost: {e}")
        
        print("[MUMBLE] Reconnecting in 5s...")
        time.sleep(5)

threading.Thread(target=mumble_loop, daemon=True).start()

def play_external(raw_url):
    """Resolve and start a web stream; fetch its thumbnail in the background.
    Returns False if the URL could not be resolved."""
    stream_url, title, is_direct, thumbnail_urls = resolve_video_data(raw_url)
    if not stream_url:
        return False
    if is_direct:
        audio_engine.play_direct_stream(stream_url, title)
    else:
        audio_engine.play_via_ytdlp(stream_url, title)
    info = audio_engine.stream_info
    if thumbnail_urls and info:
        threading.Thread(
            target=lambda: audio_engine.set_stream_thumbnail(info, fetch_thumbnail(thumbnail_urls)),
            daemon=True,
        ).start()
    update_stat(title)
    return True

# --- MATRIX APPLICATION SERVICE ---
def handle_matrix_command(command, argument):
    """Execute an already authenticated/allowlisted Matrix command in-process."""
    if command == 'ping':
        return "Pong! Ready to pump audio."
    if command == 'stop':
        audio_engine.stop_all()
        return "Stopped audio."
    if command == 'play':
        if not argument:
            return "Usage: !play <sound filename or URL>"
        if argument.startswith(('http://', 'https://')):
            if not play_external(argument):
                return "Could not resolve stream."
            return "Loading stream..."
        if '..' in argument or argument.startswith('/'):
            return "Invalid filename."
        sound_path = os.path.join(SOUNDS_DIR, argument)
        if not os.path.isfile(sound_path):
            return "File not found in sounds directory."
        audio_engine.play_file(sound_path, argument)
        update_stat(argument)
        return f"Playing: {argument}"

from matrix_bot import MatrixAppService
matrix_service = MatrixAppService(handle_matrix_command, data_dir=DATA_DIR)
if matrix_service.enabled:
    print(f"[MATRIX] Application Service enabled as {matrix_service.config['bot_mxid']}")
else:
    print("[MATRIX] Application Service disabled")


def matrix_authenticated():
    return matrix_service.authenticated(
        request.headers.get('Authorization', ''),
        request.args.get('access_token', ''),
    )


@app.route('/_matrix/app/v1/transactions/<path:txn_id>', methods=['PUT'])
def matrix_transaction(txn_id):
    if not matrix_authenticated():
        return jsonify({'errcode': 'M_FORBIDDEN', 'error': 'Invalid homeserver token'}), 401
    try:
        matrix_service.receive_transaction(txn_id, request.get_json(force=True))
    except (ValueError, TypeError):
        return jsonify({'errcode': 'M_BAD_JSON', 'error': 'Invalid transaction'}), 400
    return jsonify({})


@app.route('/_matrix/app/v1/users/<path:user_id>', methods=['GET'])
def matrix_user(user_id):
    if not matrix_authenticated():
        return jsonify({'errcode': 'M_FORBIDDEN', 'error': 'Invalid homeserver token'}), 401
    if matrix_service.claim_user(user_id):
        return jsonify({})
    return jsonify({'errcode': 'M_NOT_FOUND', 'error': 'User not in namespace'}), 404


@app.route('/_matrix/app/v1/rooms/<path:room_alias>', methods=['GET'])
def matrix_room_alias(room_alias):
    if not matrix_authenticated():
        return jsonify({'errcode': 'M_FORBIDDEN', 'error': 'Invalid homeserver token'}), 401
    if matrix_service.claim_alias(room_alias):
        return jsonify({})
    return jsonify({'errcode': 'M_NOT_FOUND', 'error': 'Alias not in namespace'}), 404


sound_metadata = MetadataCache()

def allowed_extensions():
    return tuple(os.environ.get("ALLOWED_EXTENSIONS", "mp3,wav,m4a,ogg").split(','))

def list_sounds():
    if not os.path.exists(SOUNDS_DIR):
        return []
    return sorted(f for f in os.listdir(SOUNDS_DIR) if f.endswith(allowed_extensions()))

def resolve_sound(filename):
    """Absolute path of a playable file inside SOUNDS_DIR, or None."""
    if ".." in filename or filename.startswith("/") or not filename.endswith(allowed_extensions()):
        return None
    path = os.path.join(SOUNDS_DIR, filename)
    return path if os.path.isfile(path) else None

def sound_info(filename, path, count=0):
    """Public metadata record for one sound, as served by /meta and used by the tiles."""
    try:
        meta = sound_metadata.metadata(path)
    except OSError:
        return None
    return dict(
        meta,
        name=filename,
        count=count,
        play_url=url_for('play', filename=filename),
        cover_url=url_for('cover', filename=filename),
        meta_url=url_for('sound_meta', filename=filename),
    )

@app.route('/')
def index():
    sort_type = request.args.get('sort', 'alpha')
    stats = get_stats()
    files = []
    for f in list_sounds():
        info = sound_info(f, os.path.join(SOUNDS_DIR, f), stats.get(f, 0))
        if info:
            files.append(info)
    if sort_type == 'pop': files.sort(key=lambda x: x['count'], reverse=True)
    else: files.sort(key=lambda x: x['name'])
    
    return render_template('index.html', 
                         files=files, 
                         vol_local=int(audio_engine.volume_local * 100),
                         vol_remote=int(audio_engine.volume_remote * 100))

@app.route('/meta')
def all_meta():
    stats = get_stats()
    files = []
    for f in list_sounds():
        info = sound_info(f, os.path.join(SOUNDS_DIR, f), stats.get(f, 0))
        if info:
            files.append(info)
    return jsonify(files)

@app.route('/meta/<path:filename>')
def sound_meta(filename):
    path = resolve_sound(filename)
    info = sound_info(filename, path, get_stats().get(filename, 0)) if path else None
    if not info:
        return jsonify({'error': 'File not found'}), 404
    return jsonify(info)

COVER_SIZE_RANGE = (16, 1024)

def requested_cover_size():
    """?size=N on cover endpoints: longest edge in px, clamped. None when absent.
    Raises ValueError when it is not a number."""
    raw = request.args.get('size')
    if not raw:
        return None
    low, high = COVER_SIZE_RANGE
    return min(high, max(low, int(raw)))

def default_cover(size, max_age):
    """Generic retro cover: the SVG, or a PNG of the pixel art when a size is asked for."""
    if size:
        mime, data = thumbnail(None, size)
        response = send_file(io.BytesIO(data), mimetype=mime, max_age=max_age)
    else:
        response = send_from_directory(app.static_folder, 'cover_default.svg', max_age=max_age)
    response.headers['X-Cover-Source'] = 'default'
    return response

@app.route('/cover/<path:filename>')
def cover(filename):
    """Embedded cover art, or the generic retro cover when the file has none.
    X-Cover-Source tells API clients which one they got. ?size=N scales it to
    fit N×N px (covers are only shrunk), for clients that store the image."""
    try:
        px = requested_cover_size()
    except ValueError:
        return "size must be a number of pixels", 400
    path = resolve_sound(filename)
    if not path:
        return "File not found", 404
    try:
        art = sound_metadata.cover(path)
    except OSError:
        return "File not found", 404
    if art is None:
        return default_cover(px, 3600)
    mime, data, (mtime_ns, size) = art
    if px:
        mime, data = thumbnail(data, px)
    response = send_file(
        io.BytesIO(data),
        mimetype=mime,
        etag=f"{mtime_ns:x}-{size:x}" + (f"-{px}" if px else ""),
        last_modified=mtime_ns / 1e9,
        max_age=3600,
    )
    response.headers['X-Cover-Source'] = 'embedded'
    return response

@app.route('/play/<path:filename>')
def play(filename):
    if ".." in filename or filename.startswith("/"): return "Invalid filename", 400
    path = os.path.join(SOUNDS_DIR, filename)
    if os.path.exists(path):
        audio_engine.play_file(path, filename)
        update_stat(filename)
        return "Playing", 200
    return "File not found", 404

@app.route('/play_url')
def play_external_url():
    raw_url = request.args.get('url')
    if raw_url:
        if play_external(raw_url):
            return "Playing URL", 200
        else:
            return "Could not resolve stream", 500
    return "No URL", 400

@app.route('/stop')
def stop():
    audio_engine.stop_all()
    return "Stopped", 200

@app.route('/volume/local/<int:vol>')
def set_volume_local(vol):
    audio_engine.set_volume_local(vol)
    return "Local Volume Set", 200

@app.route('/volume/remote/<int:vol>')
def set_volume_remote(vol):
    audio_engine.set_volume_remote(vol)
    return "Remote Volume Set", 200

@app.route('/status')
def get_status():
    is_playing = len(audio_engine.active_processes) > 0
    with metrics_lock:
        metrics = dict(audio_metrics)
    metrics['queue_depth'] = mumble_queue.qsize()
    return jsonify({
        'playing': is_playing,
        'meta': audio_engine.current_metadata if is_playing else None,
        'now_playing': now_playing_info(),
        'audio': metrics,
        'meet': meet_status_all(),
        'matrix_rtc': matrix_rtc.status(),
        'matrix_appservice': {'enabled': matrix_service.enabled},
    })

def now_playing_info():
    """What each bot is showing: the newest sound button and the web stream."""
    name, stream = audio_engine.now_playing()
    sound = None
    path = resolve_sound(name) if name else None
    if path:
        info = sound_info(name, path)
        if info:
            sound = {key: info[key] for key in ('name', 'title', 'artist', 'album', 'has_cover', 'cover_url', 'meta_url')}
    if stream:
        stream = {
            'title': stream['title'],
            'link': stream['link'],
            'has_thumbnail': stream['thumbnail'] is not None,
            # ?v= changes with every stream (and once its thumbnail arrives) so browsers refetch
            'thumbnail_url': url_for('stream_thumbnail', v=f"{stream['id']}{'t' if stream['thumbnail'] else ''}"),
        }
    return {'sound': sound, 'stream': stream}

@app.route('/now_playing/thumbnail')
def stream_thumbnail():
    """Thumbnail of the running web stream, or the generic cover if there is none.
    Takes ?size=N like /cover."""
    try:
        px = requested_cover_size()
    except ValueError:
        return "size must be a number of pixels", 400
    _, stream = audio_engine.now_playing()
    data = stream and stream['thumbnail']
    if not data:
        return default_cover(px, 60)
    mime = sniff_mime(data)
    if px:
        mime, data = thumbnail(data, px)
    response = send_file(io.BytesIO(data), mimetype=mime, max_age=3600)
    response.headers['X-Cover-Source'] = 'embedded'
    return response

def meet_status_all():
    return {key: bot.status() for key, bot in meet_bots.items()}

def selected_meet_bot():
    """The bot named by ?bot=sound|dj (default: sound), or None if unknown."""
    key = request.args.get('bot') or request.form.get('bot') or 'sound'
    return meet_bots.get(key)

@app.route('/meet/connect', methods=['GET', 'POST'])
def meet_connect():
    bot = selected_meet_bot()
    if not bot:
        return "Unknown bot (use bot=sound or bot=dj)", 400
    room = request.args.get('room') or request.form.get('room', '')
    password = request.args.get('password') or request.form.get('password', '')
    if not room.strip():
        return "No room given", 400
    ok, msg = bot.connect(room.strip(), password)
    return ("Schnackn connect: " + msg, 200) if ok else (msg, 409)

@app.route('/meet/disconnect', methods=['GET', 'POST'])
def meet_disconnect():
    bot = selected_meet_bot()
    if not bot:
        return "Unknown bot (use bot=sound or bot=dj)", 400
    ok, msg = bot.disconnect()
    return ("Schnackn disconnect: " + msg, 200) if ok else (msg, 409)

@app.route('/meet/status')
def meet_status():
    return jsonify(meet_status_all())

@app.route('/matrix/rtc/status')
def matrix_rtc_status():
    return jsonify(matrix_rtc.status())

@app.route('/matrix/rtc/connect', methods=['POST'])
def matrix_rtc_connect():
    ok, message = matrix_rtc.start()
    return jsonify({'ok': ok, 'message': message, 'status': matrix_rtc.status()}), (202 if ok else 409)

@app.route('/matrix/rtc/disconnect', methods=['POST'])
def matrix_rtc_disconnect():
    ok, message = matrix_rtc.stop()
    return jsonify({'ok': ok, 'message': message, 'status': matrix_rtc.status()})

@app.route('/stats')
def view_stats():
    stats = get_stats()
    html = "<h1>Statistics</h1><table border='1'><tr><th>File / Video</th><th>Plays</th></tr>"
    for k, v in sorted(stats.items(), key=lambda item: item[1], reverse=True):
        html += f"<tr><td>{k}</td><td>{v}</td></tr>"
    html += "</table><br><a href='/'>Back</a>"
    return html

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
