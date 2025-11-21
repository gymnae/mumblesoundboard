import ssl
import threading
import logging
import re
import base64

# --- MONKEY PATCH FOR PYTHON 3.13 COMPATIBILITY ---
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
# --------------------------------------------------

import os
import time
import sqlite3
import subprocess
import struct
import signal
import warnings
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
INVIDIOUS_HOST = "https://tube.wxbu.de"
INVIDIOUS_USER = "tube"
INVIDIOUS_PASS = "tube"

# Basic Auth Header
auth_str = f"{INVIDIOUS_USER}:{INVIDIOUS_PASS}"
b64_auth = base64.b64encode(auth_str.encode()).decode()
AUTH_HEADER = f"Authorization: Basic {b64_auth}"

# Browser Headers to prevent 403s
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOUNDS_DIR = os.path.join(BASE_DIR, "sounds")
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "stats.db")

os.makedirs(SOUNDS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS stats (filename TEXT PRIMARY KEY, count INTEGER)')
        conn.commit()

def update_stat(filename):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('INSERT INTO stats (filename, count) VALUES (?, 1) ON CONFLICT(filename) DO UPDATE SET count = count + 1', (filename,))
        conn.commit()

def get_stats():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM stats")
        return {row['filename']: row['count'] for row in cursor.fetchall()}

# --- INVIDIOUS STRICT LOGIC ---
def get_clean_video_data(url):
    # Extract ID
    youtube_regex = r'(?:youtube\.com\/(?:[^\/]+\/.+\/|(?:v|e(?:mbed)?)\/|.*[?&]v=)|youtu\.be\/)([^"&?\/\s]{11})'
    match = re.search(youtube_regex, url)
    
    if match:
        video_id = match.group(1)
        
        # STRICT RULE: Use Invidious URL.
        # append &local=true to force the stream through the instance (keeps Auth valid)
        # instead of redirecting to Google (where Auth fails)
        clean_url = f"{INVIDIOUS_HOST}/watch?v={video_id}&local=true"
        
        try:
            cmd = [
                'yt-dlp', 
                '--get-title', 
                '--no-warnings', 
                '--add-header', AUTH_HEADER,
                '--user-agent', USER_AGENT,
                clean_url
            ]
            title = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode('utf-8').strip()
            if not title: title = f"YouTube ID: {video_id}"
        except:
            title = f"YouTube ID: {video_id}"
            
        return clean_url, f"YouTube: {title}"
    
    return url, "External URL"

class AudioEngine:
    def __init__(self):
        self.active_processes = []
        self.volume = 0.5
        self.lock = threading.Lock()
        self.current_metadata = None 

    def play_file(self, filepath, filename):
        self.current_metadata = {'type': 'file', 'text': filename, 'link': None}
        cmd = ['ffmpeg', '-re', '-i', filepath, '-f', 's16le', '-ac', '1', '-ar', '48000', '-']
        self._start_process(cmd)

    def play_url(self, url, display_title="YouTube Stream"):
        print(f"[DEBUG] Playing Invidious URL: {url}")
        self.current_metadata = {'type': 'url', 'text': display_title, 'link': url}
        
        cookie_file = os.path.join(DATA_DIR, 'cookies.txt')
        
        # Configure yt-dlp to act like a Browser hitting Invidious
        dlp_cmd = [
            'yt-dlp', 
            '--no-cache-dir', '--no-warnings', '--no-playlist',
            
            # 1. Pass Basic Auth
            '--add-header', AUTH_HEADER,
            
            # 2. Mimic Browser User-Agent
            '--user-agent', USER_AGENT,
            
            # 3. Set Referer to the instance (some configs require this)
            '--referer', f"{INVIDIOUS_HOST}/",
            
            '-f', 'bestaudio/best', 
            '-o', '-'
        ]

        # We remove the "youtube:player_client" args because we aren't 
        # talking to YouTube anymore; we are talking to Invidious.

        if os.path.exists(cookie_file):
            # Cookies might still help if Invidious passes them through
            dlp_cmd.extend(['--cookies', cookie_file])

        dlp_cmd.append(url)
        
        try:
            p_dlp = subprocess.Popen(
                dlp_cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE, 
                preexec_fn=os.setsid
            )
        except Exception as e:
            print(f"[ERROR] Could not start yt-dlp: {e}")
            return

        ffmpeg_cmd = [
            'ffmpeg', 
            '-i', 'pipe:0', 
            '-f', 's16le', '-ac', '1', '-ar', '48000', '-'
        ]
        
        try:
            p_ffmpeg = subprocess.Popen(
                ffmpeg_cmd, 
                stdin=p_dlp.stdout, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid
            )
        except Exception as e:
            print(f"[ERROR] Could not start ffmpeg: {e}")
            p_dlp.kill()
            return
        
        p_dlp.stdout.close()
        p_ffmpeg.source_proc = p_dlp

        with self.lock:
            self.active_processes.append(p_ffmpeg)

    def _start_process(self, cmd):
        process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid 
        )
        process.source_proc = None
        
        with self.lock:
            self.active_processes.append(process)

    def stop_all(self):
        self.current_metadata = None
        with self.lock:
            for p in self.active_processes:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except: pass
                
                if getattr(p, 'source_proc', None):
                    try:
                        os.killpg(os.getpgid(p.source_proc.pid), signal.SIGKILL)
                    except: pass
            self.active_processes = []

    def set_volume(self, vol):
        self.volume = max(0.0, min(1.0, float(vol) / 100.0))

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
            if p.poll() is not None:
                if getattr(p, 'source_proc', None):
                    if p.source_proc.poll() is not None and p.source_proc.returncode != 0:
                        err = p.source_proc.stderr.read().decode('utf-8', errors='ignore')
                        if err: print(f"[INVIDIOUS ERROR] {err.strip()}")
                continue 
            
            try:
                raw = p.stdout.read(CHUNK_SIZE)
                if raw and len(raw) == CHUNK_SIZE:
                    samples = struct.unpack(f"<{len(raw)//2}h", raw)
                    for i, sample in enumerate(samples):
                        mixed_audio[i] += sample
                    active_now.append(p)
                else:
                    if getattr(p, 'source_proc', None):
                         time.sleep(0.1)
                         if p.source_proc.poll() is not None and p.source_proc.returncode != 0:
                             err = p.source_proc.stderr.read().decode('utf-8', errors='ignore')
                             if err: print(f"[INVIDIOUS ERROR] {err.strip()}")

                    try:
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                        if getattr(p, 'source_proc', None):
                            os.killpg(os.getpgid(p.source_proc.pid), signal.SIGKILL)
                    except: pass
            except Exception as e:
                print(f"[STREAM EXCEPTION] {e}")

        with self.lock:
            self.active_processes = [p for p in self.active_processes if p in active_now]

        if not active_now:
            return None

        final_bytes = bytearray()
        for sample in mixed_audio:
            val = int(sample * self.volume)
            val = max(-32768, min(32767, val))
            final_bytes += struct.pack("<h", val)
            
        return bytes(final_bytes)

audio_engine = AudioEngine()

def mumble_loop():
    print(f"[MUMBLE] Connecting to {HOST}:{PORT} as {USER}...")
    mumble = pymumble.Mumble(HOST, USER, password=PASSWORD, port=PORT)
    mumble.server_max_bandwidth = None 
    mumble.start()
    mumble.is_ready()
    print("[MUMBLE] Connected.")

    try:
        mumble.set_bandwidth(96000)
    except Exception as e:
        print(f"[MUMBLE] Warning: Could not set high bandwidth: {e}")

    if CHANNEL:
        print(f"[MUMBLE] Attempting to join channel: {CHANNEL}")
        time.sleep(2) 
        target = None
        for channel_id, channel_obj in mumble.channels.items():
            if channel_obj['name'] == CHANNEL:
                target = channel_obj
                break
        
        if target:
            print(f"[MUMBLE] Moving to channel ID: {target['channel_id']}")
            mumble.users.myself.move_in(target['channel_id'])
        else:
            print(f"[MUMBLE] Channel '{CHANNEL}' not found. Staying in Root.")

    next_tick = time.time()
    
    while True:
        pcm_chunk = audio_engine.get_chunk()
        if pcm_chunk:
            mumble.sound_output.add_sound(pcm_chunk)
        
        next_tick += PYMUMBLE_AUDIO_PER_PACKET
        sleep_time = next_tick - time.time()
        if sleep_time > 0:
            time.sleep(sleep_time)
        else:
            next_tick = time.time()

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
    
    if sort_type == 'pop':
        files.sort(key=lambda x: x['count'], reverse=True)
    else:
        files.sort(key=lambda x: x['name'])

    return render_template('index.html', files=files, volume=int(audio_engine.volume * 100))

@app.route('/play/<path:filename>')
def play(filename):
    if ".." in filename or filename.startswith("/"):
        return "Invalid filename", 400
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
        clean_url, title = get_clean_video_data(raw_url)
        audio_engine.play_url(clean_url, display_title=title)
        update_stat(title)
        return "Playing URL", 200
    return "No URL", 400

@app.route('/stop')
def stop():
    audio_engine.stop_all()
    return "Stopped", 200

@app.route('/volume/<int:vol>')
def set_volume(vol):
    audio_engine.set_volume(vol)
    return "Volume Set", 200

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
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=False)
