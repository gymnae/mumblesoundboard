import ssl
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
from urllib.parse import urlparse, urlunparse
from flask import Flask, render_template, request, jsonify

import pymumble_py3 as pymumble
from pymumble_py3.constants import PYMUMBLE_AUDIO_PER_PACKET

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR) 

warnings.filterwarnings("ignore", category=UserWarning, module='google.protobuf')

# Configuration
HOST = os.getenv("MUMBLE_HOST", "localhost")
PORT = int(os.getenv("MUMBLE_PORT", 64738))
USER = os.getenv("MUMBLE_USER", "SoundBot")
PASSWORD = os.getenv("MUMBLE_PASSWORD", "")
CHANNEL = os.getenv("MUMBLE_CHANNEL", "") 

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

# --- PROXY REWRITE RESOLVER ---
def resolve_video_data(url):
    youtube_regex = r'(?:youtube\.com\/(?:[^\/]+\/.+\/|(?:v|e(?:mbed)?)\/|.*[?&]v=)|youtu\.be\/)([^"&?\/\s]{11})'
    match = re.search(youtube_regex, url)
    
    if match and INVIDIOUS_HOST:
        video_id = match.group(1)
        api_url = f"{INVIDIOUS_HOST}/api/v1/videos/{video_id}"
        
        # 1. Fetch Metadata
        req = urllib.request.Request(api_url)
        if AUTH_HEADER_VAL:
            req.add_header("Authorization", AUTH_HEADER_VAL)
            print(f"[DEBUG] Fetching API with Auth: {api_url}")
        else:
            print(f"[DEBUG] Fetching API Anonymously: {api_url}")

        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())
            
            title = data.get('title', f"YouTube ID: {video_id}")
            
            # 2. Find Best Audio Stream
            audio_formats = [f for f in data.get('adaptiveFormats', []) if 'audio' in f.get('type', '')]
            audio_formats.sort(key=lambda x: int(x.get('bitrate', 0)), reverse=True)
            
            stream_url = None
            if audio_formats:
                stream_url = audio_formats[0].get('url')
            else:
                mixed = data.get('formatStreams', [])
                if mixed: stream_url = mixed[0].get('url')
            
            if stream_url:
                # 3. REWRITE TO FORCE PROXY
                if "googlevideo.com" in stream_url:
                    try:
                        parsed_stream = urlparse(stream_url)
                        parsed_inv = urlparse(INVIDIOUS_HOST)
                        
                        # Rebuild query with &local=true
                        query = parsed_stream.query
                        if "local=true" not in query:
                            query += "&local=true"
                            
                        final_url = urlunparse((
                            parsed_inv.scheme, 
                            parsed_inv.netloc, 
                            parsed_stream.path, 
                            parsed_stream.params, 
                            query, 
                            parsed_stream.fragment
                        ))
                        print(f"[DEBUG] Rewrote Google URL to Proxy: {final_url}")
                        return final_url, f"YouTube: {title}", True
                    except Exception as e:
                        print(f"[REWRITE ERROR] {e}. Using original.")
                
                # Handle relative URLs
                elif stream_url.startswith('/'):
                    stream_url = f"{INVIDIOUS_HOST}{stream_url}"
                    print(f"[DEBUG] Relative URL Resolved: {stream_url}")
                    return stream_url, f"YouTube: {title}", True
                
                return stream_url, f"YouTube: {title}", True
            else:
                print("[API WARNING] No stream URL found in API response.")
                
        except Exception as e:
            print(f"[API FAIL] {e}")

    return url, "External Stream", False

class AudioEngine:
    def __init__(self):
        self.active_processes = []
        self.volume_local = 0.75  
        self.volume_remote = 0.50 
        self.lock = threading.Lock()
        self.current_metadata = None 
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
        
        dlp_cmd = ['yt-dlp', '--no-cache-dir', '--no-playlist']
        
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
        
        with self.lock:
            self.active_processes.append(p_ffmpeg)

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
        with self.lock:
            for p in self.active_processes[:]: 
                self._kill_process(p)
            self.active_processes = []

    def set_volume_local(self, vol):
        self.volume_local = max(0.0, min(1.0, float(vol) / 100.0))

    def set_volume_remote(self, vol):
        self.volume_remote = max(0.0, min(1.0, float(vol) / 100.0))

    def get_chunk(self):
        CHUNK_SIZE = 960 * 2 
        mixed_audio = [0] * 960
        
        with self.lock:
            if not self.active_processes:
                self.current_metadata = None
                return None
            current_procs = list(self.active_processes)

        active_now = []
        
        for p in current_procs:
            if p.poll() is not None: continue
            
            try:
                raw = p.stdout.read(CHUNK_SIZE)
                if raw and len(raw) == CHUNK_SIZE:
                    samples = struct.unpack(f"<{len(raw)//2}h", raw)
                    current_vol = self.volume_local if getattr(p, 'source_type', 'local') == 'local' else self.volume_remote
                    
                    for i, sample in enumerate(samples):
                        val = int(sample * current_vol)
                        mixed_audio[i] += val
                    active_now.append(p)
                else:
                    self._kill_process(p)
            except: pass

        with self.lock:
            self.active_processes = [p for p in self.active_processes if p in active_now]

        if not active_now:
            return None

        final_bytes = bytearray()
        for sample in mixed_audio:
            val = max(-32768, min(32767, sample))
            final_bytes += struct.pack("<h", val)
            
        return bytes(final_bytes)

audio_engine = AudioEngine()

def mumble_loop():
    while True:
        try:
            print(f"[MUMBLE] Connecting to {HOST}:{PORT} as {USER}...")
            mumble = pymumble.Mumble(HOST, USER, password=PASSWORD, port=PORT)
            mumble.server_max_bandwidth = None 
            mumble.start()
            mumble.is_ready()
            print("[MUMBLE] Connected.")

            try:
                mumble.set_bandwidth(96000)
            except: pass

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

            next_tick = time.time()
            while mumble.is_alive():
                pcm_chunk = audio_engine.get_chunk()
                if pcm_chunk:
                    mumble.sound_output.add_sound(pcm_chunk)
                next_tick += PYMUMBLE_AUDIO_PER_PACKET
                sleep_time = next_tick - time.time()
                if sleep_time > 0: time.sleep(sleep_time)
                else: next_tick = time.time()
        
        except Exception as e:
            print(f"[MUMBLE ERROR] Connection lost: {e}")
        
        print("[MUMBLE] Reconnecting in 5s...")
        time.sleep(5)

threading.Thread(target=mumble_loop, daemon=True).start()

@app.route('/')
def index():
    sort_type = request.args.get('sort', 'alpha')
    stats = get_stats()
    files = []
    valid_exts = tuple(os.environ.get("ALLOWED_EXTENSIONS", "mp3,wav,m4a,ogg").split(','))
    if os.path.exists(SOUNDS_DIR):
        for f in os.listdir(SOUNDS_DIR):
            if f.endswith(valid_exts):
                files.append({ 'name': f, 'count': stats.get(f, 0) })
    if sort_type == 'pop': files.sort(key=lambda x: x['count'], reverse=True)
    else: files.sort(key=lambda x: x['name'])
    
    return render_template('index.html', 
                         files=files, 
                         vol_local=int(audio_engine.volume_local * 100),
                         vol_remote=int(audio_engine.volume_remote * 100))

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
        stream_url, title, is_direct = resolve_video_data(raw_url)
        if stream_url:
            if is_direct:
                audio_engine.play_direct_stream(stream_url, title)
            else:
                audio_engine.play_via_ytdlp(stream_url, title)
            update_stat(title)
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
    return jsonify({
        'playing': is_playing,
        'meta': audio_engine.current_metadata if is_playing else None
    })

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
