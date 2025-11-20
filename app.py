import ssl
import threading
import logging

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

# --- FIX 4: SILENCE CONSOLE SPAM ---
# Disable Werkzeug logging for successful requests (200s)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR) 

warnings.filterwarnings("ignore", category=UserWarning, module='google.protobuf')

# Configuration
HOST = os.getenv("MUMBLE_HOST", "localhost")
PORT = int(os.getenv("MUMBLE_PORT", 64738))
USER = os.getenv("MUMBLE_USER", "SoundBot")
PASSWORD = os.getenv("MUMBLE_PASSWORD", "")
CHANNEL = os.getenv("MUMBLE_CHANNEL", "") 

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

# --- IMPROVED AUDIO ENGINE ---
class AudioEngine:
    def __init__(self):
        self.active_processes = []
        self.volume = 0.5
        self.lock = threading.Lock()
        self.current_metadata = None 

    def play_file(self, filepath, filename):
        # self.stop_all() # Optional: Uncomment to force single-track only
        self.current_metadata = {'type': 'file', 'text': filename, 'link': None}
        
        # FIX 1 (Quality): Add '-re' to read input at native frame rate.
        # This prevents ffmpeg from dumping the whole file instantly into the buffer,
        # which causes jitter/skipping in the mixer.
        cmd = ['ffmpeg', '-re', '-i', filepath, '-f', 's16le', '-ac', '1', '-ar', '48000', '-']
        self._start_process(cmd, shell=False)

    def play_url(self, url):
        print(f"[DEBUG] Playing URL: {url}")
        self.current_metadata = {'type': 'url', 'text': 'YouTube Stream', 'link': url}
        
        # FIX 3 (YouTube 403):
        # 1. --extractor-args "youtube:player_client=android" -> Mimics Android app (bypasses many bot checks)
        # 2. --force-ipv4 -> Often fixes 403s on IPv6 networks
        # 3. -f bestaudio/best -> Fallback to video if audio stream is locked
        cmd = (
            f'yt-dlp --quiet --no-warnings --force-ipv4 --no-playlist '
            f'--extractor-args "youtube:player_client=android" '
            f'-f bestaudio/best -o - "{url}" '
            f'| ffmpeg -i pipe:0 -f s16le -ac 1 -ar 48000 -'
        )
        self._start_process(cmd, shell=True)

    def _start_process(self, cmd, shell=False):
        process = subprocess.Popen(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.DEVNULL, # Keep stderr clean
            shell=shell, 
            bufsize=4096,
            preexec_fn=os.setsid 
        )
        with self.lock:
            self.active_processes.append(process)

    def stop_all(self):
        self.current_metadata = None
        with self.lock:
            for p in self.active_processes:
                try:
                    # FIX 2 (Stop Button): Use SIGKILL (9) instead of SIGTERM (15)
                    # And kill the Process Group (pgid) to get the shell + ffmpeg + yt-dlp
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except (ProcessLookupError, AttributeError, OSError):
                    pass
            self.active_processes = []

    def set_volume(self, vol):
        self.volume = max(0.0, min(1.0, float(vol) / 100.0))

    def get_chunk(self):
        # FIX 1 (Quality): Ensure chunk size is exact for 20ms @ 48kHz
        CHUNK_SIZE = 960 * 2 
        mixed_audio = [0] * 960
        
        # COPY list to avoid holding lock during reading
        with self.lock:
            if not self.active_processes:
                self.current_metadata = None
                return None
            current_procs = list(self.active_processes)

        active_now = []
        
        for p in current_procs:
            if p.poll() is not None:
                continue 
            
            try:
                # Non-blocking read is hard with pipes, so we rely on ffmpeg -re (files)
                # or natural streaming speed (youtube) to not block too long.
                raw = p.stdout.read(CHUNK_SIZE)
                if raw and len(raw) == CHUNK_SIZE:
                    samples = struct.unpack(f"<{len(raw)//2}h", raw)
                    for i, sample in enumerate(samples):
                        mixed_audio[i] += sample
                    active_now.append(p)
                else:
                    # Stream ended or broken
                    try:
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    except: pass
            except Exception:
                pass

        # Cleanup dead processes from the main list
        with self.lock:
            self.active_processes = [p for p in self.active_processes if p in active_now]

        if not active_now:
            return None

        # Clip and Pack
        final_bytes = bytearray()
        for sample in mixed_audio:
            val = int(sample * self.volume)
            # Hard Clip to avoid overflow wrapping (which sounds like horrible static)
            val = max(-32768, min(32767, val))
            final_bytes += struct.pack("<h", val)
            
        return bytes(final_bytes)

audio_engine = AudioEngine()

# --- Mumble Bot Thread ---
# --- Mumble Bot Thread (FIXED) ---
def mumble_loop():
    print(f"[MUMBLE] Connecting to {HOST}:{PORT} as {USER}...")
    mumble = pymumble.Mumble(HOST, USER, password=PASSWORD, port=PORT)
    
    # FIX: Manually initialize this attribute to prevent AttributeError
    mumble.server_max_bandwidth = None 
    
    mumble.start()
    mumble.is_ready()
    print("[MUMBLE] Connected.")

    # FIX: Set Quality safely
    try:
        # Set Bandwidth to ~96kbps (Music Quality)
        mumble.set_bandwidth(96000)
        print("[MUMBLE] Audio quality set to High (96kbps)")
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

    # Main Audio Loop
    next_tick = time.time()
    
    while True:
        pcm_chunk = audio_engine.get_chunk()
        if pcm_chunk:
            mumble.sound_output.add_sound(pcm_chunk)
        
        # Precision Timing Logic
        next_tick += PYMUMBLE_AUDIO_PER_PACKET
        sleep_time = next_tick - time.time()
        if sleep_time > 0:
            time.sleep(sleep_time)
        else:
            next_tick = time.time()


threading.Thread(target=mumble_loop, daemon=True).start()

# --- Flask Routes ---
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
    url = request.args.get('url')
    if url:
        audio_engine.play_url(url)
        update_stat("YOUTUBE_LINK")
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
    html = "<h1>Statistics</h1><table border='1'><tr><th>File</th><th>Plays</th></tr>"
    for k, v in sorted(stats.items(), key=lambda item: item[1], reverse=True):
        html += f"<tr><td>{k}</td><td>{v}</td></tr>"
    html += "</table><br><a href='/'>Back</a>"
    return html

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=False)

