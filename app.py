import os
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager

import yt_dlp
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pymumble_py3 import Mumble
from pymumble_py3.callbacks import PYMUMBLE_CLBK_CONNECTED, PYMUMBLE_CLBK_DISCONNECTED
from starlette.responses import JSONResponse

# --- Constants ---
SOUNDS_DIR = "sounds"
FFMPEG_PATH = "ffmpeg"  # Assumes ffmpeg is in the system's PATH
YTDL_PATH = "yt-dlp"    # Assumes yt-dlp is in the system's PATH
BITRATE = 48000  # Mumble's audio bitrate

# --- Global State ---
state = {
    "mumble": None,
    "ffmpeg_process": None,
    "connected": False,
    "mumble_address": "",
    "current_music_title": "",
    "current_music_audio_url": "",
    "should_reconnect": False, # Control flag for the reconnection loop
    "state_lock": threading.Lock(),
}

# --- yt-dlp Setup ---
ydl_opts = {
    "format": "bestaudio/best",
    "quiet": True,
    "outtmpl": "%(id)s.%(ext)s",
    "noplaylist": True,
    "ffmpeg_location": FFMPEG_PATH,
    "yt_dlp_path": YTDL_PATH,
}

# --- Helper Functions ---

def stop_audio_stream():
    """Safely stops any active ffmpeg process."""
    with state["state_lock"]:
        if state["ffmpeg_process"]:
            try:
                state["ffmpeg_process"].stdin.close()
                state["ffmpeg_process"].terminate()
                state["ffmpeg_process"].wait(timeout=2)
            except Exception as e:
                print(f"Error stopping ffmpeg: {e}")
            state["ffmpeg_process"] = None

def play_audio_source(ffmpeg_args: list):
    """Starts a new ffmpeg process with the given args."""
    stop_audio_stream() # Ensure previous stream is dead first

    with state["state_lock"]:
        if not state["connected"] or not state["mumble"]:
            print("Cannot play audio: Mumble not connected.")
            return

        try:
            state["ffmpeg_process"] = subprocess.Popen(
                [FFMPEG_PATH] + ffmpeg_args,
                stdin=subprocess.PIPE,
                stdout=state["mumble"].sound_output.get_input_stream(),
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            print(f"Error starting ffmpeg: {e}")

def play_youtube_stream(audio_url: str, title: str):
    """Helper to start a music-only stream."""
    with state["state_lock"]:
        state["current_music_title"] = title
        state["current_music_audio_url"] = audio_url
    
    args = [
        "-re", "-i", audio_url,
        "-f", "s16le", "-ar", str(BITRATE), "-ac", "1", "-",
    ]
    play_audio_source(args)
    
    threading.Thread(target=monitor_stream_end, args=(audio_url,), daemon=True).start()

def monitor_stream_end(audio_url_being_played: str):
    """Waits for the ffmpeg process to end and clears the music state."""
    process_to_watch = None
    with state["state_lock"]:
        process_to_watch = state["ffmpeg_process"]

    if process_to_watch:
        try:
            process_to_watch.wait()
        except:
            pass

    with state["state_lock"]:
        if state["current_music_audio_url"] == audio_url_being_played:
            print(f"Stream finished: {state['current_music_title']}")
            state["current_music_title"] = ""
            state["current_music_audio_url"] = ""
            state["ffmpeg_process"] = None

# --- Mumble Thread Target (With Reconnection Logic) ---

def mumble_thread_target(address, username, password):
    """Manages Mumble connection with auto-reconnect logic."""
    print(f"Starting connection loop for {address}...")
    
    with state["state_lock"]:
        state["should_reconnect"] = True

    while True:
        # Check if we should stop trying (e.g. user clicked Disconnect)
        with state["state_lock"]:
            if not state["should_reconnect"]:
                print("Reconnection loop stopping by user request.")
                break

        print(f"Connecting to {address} as {username}...")
        mumble = Mumble(address, username, password=password, port=64738, debug=False)

        def on_connected():
            print("Mumble: Connected!")
            with state["state_lock"]:
                state["connected"] = True
                state["mumble_address"] = address

        def on_disconnected():
            print("Mumble: Disconnected.")
            stop_audio_stream()
            with state["state_lock"]:
                state["connected"] = False
                state["mumble"] = None
                # We do NOT clear mumble_address here so status endpoint knows where we are trying to go
                state["current_music_title"] = ""
                state["current_music_audio_url"] = ""

        mumble.callbacks.set_callback(PYMUMBLE_CLBK_CONNECTED, on_connected)
        mumble.callbacks.set_callback(PYMUMBLE_CLBK_DISCONNECTED, on_disconnected)
        
        with state["state_lock"]:
            state["mumble"] = mumble

        try:
            mumble.start()
            mumble.join() # This blocks until connection is lost or stop() is called
        except Exception as e:
            print(f"Mumble connection error: {e}")
        
        # If we get here, mumble.join() has returned, meaning we disconnected.
        on_disconnected()

        # Check if we should retry
        with state["state_lock"]:
            if not state["should_reconnect"]:
                break
        
        print("Connection lost. Retrying in 5 seconds...")
        time.sleep(5)

# --- FastAPI App ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(SOUNDS_DIR, exist_ok=True)
    yield
    print("Shutting down...")
    with state["state_lock"]:
        state["should_reconnect"] = False # Stop the loop
        if state["mumble"]:
            state["mumble"].stop()

app = FastAPI(lifespan=lifespan)

@app.post("/api/connect")
async def connect_to_mumble(
    address: str = Form(...),
    username: str = Form(...),
    password: str = Form(""),
):
    with state["state_lock"]:
        if state["connected"] or state["should_reconnect"]:
             # If we are already connected OR in the middle of a retry loop
            if state["connected"]:
                 raise HTTPException(status_code=400, detail="Already connected.")
            else:
                 # If we are strictly not connected, but should_reconnect is true, 
                 # we might be in the 5-second sleep. We'll allow a "reset" here
                 # by letting the new thread start and setting the flag.
                 pass

    # Start the Mumble client in a background thread
    threading.Thread(
        target=mumble_thread_target,
        args=(address, username, password),
        daemon=True,
    ).start()
    
    return JSONResponse({"success": True, "message": "Connection initiated."})

@app.post("/api/disconnect")
async def disconnect_from_mumble():
    with state["state_lock"]:
        # Set flag to False so the loop in the thread stops
        state["should_reconnect"] = False
        
        if state["mumble"]:
            state["mumble"].stop() # This breaks the mumble.join() in the thread
        
        state["connected"] = False # Immediate UI update
        state["mumble_address"] = ""

    return JSONResponse({"success": True, "message": "Disconnected."})

@app.get("/api/status")
async def get_status():
    with state["state_lock"]:
        try:
            files = os.listdir(SOUNDS_DIR)
            snippets = [f for f in files if os.path.isfile(os.path.join(SOUNDS_DIR, f))]
        except Exception:
            snippets = []
        
        # If we are not connected but 'should_reconnect' is true, we are 'Reconnecting...'
        status_msg = ""
        if state["connected"]:
            status_msg = "Connected"
        elif state["should_reconnect"]:
            status_msg = "Reconnecting..."
        else:
            status_msg = "Not Connected"

        return {
            "connected": state["connected"],
            "statusMessage": status_msg, # New field for UI
            "mumbleAddress": state["mumble_address"],
            "currentMusic": state["current_music_title"],
            "snippets": snippets,
        }

@app.post("/api/play-youtube")
async def play_youtube(url: str = Form(...)):
    if not state["connected"]:
        raise HTTPException(status_code=400, detail="Not connected to Mumble.")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            audio_url = None
            for f in info.get("formats", []):
                if f.get("acodec") != "none" and f.get("vcodec") == "none":
                    audio_url = f.get("url")
                    break
            if not audio_url:
                 audio_url = info.get("url")

            if not audio_url:
                raise HTTPException(status_code=404, detail="Could not find a suitable audio stream.")
                
            title = info.get("title", "Unknown Title")
            
            threading.Thread(target=play_youtube_stream, args=(audio_url, title), daemon=True).start()
            
            return JSONResponse({"success": True, "message": f"Playing: {title}"})

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing YouTube URL: {e}")

@app.post("/api/play-snippet")
async def play_snippet(filename: str = Form(...)):
    if not state["connected"]:
        raise HTTPException(status_code=400, detail="Not connected to Mumble.")

    snippet_path = os.path.join(SOUNDS_DIR, os.path.basename(filename))
    if not os.path.exists(snippet_path):
        raise HTTPException(status_code=404, detail="Snippet not found.")

    with state["state_lock"]:
        music_url = state["current_music_audio_url"]
        music_title = state["current_music_title"]
    
    stop_audio_stream()

    if music_url:
        print(f"Mixing {filename} over {music_title}")
        args = [
            "-re", "-i", music_url,
            "-i", snippet_path,
            "-filter_complex", "[0:a]volume=0.5[a];[1:a]volume=1.0[b];[a][b]amix=inputs=2:duration=shortest[out]",
            "-map", "[out]",
            "-f", "s16le", "-ar", str(BITRATE), "-ac", "1", "-",
        ]
    else:
        print(f"Playing snippet {filename}")
        args = [
            "-i", snippet_path,
            "-f", "s16le", "-ar", str(BITRATE), "-ac", "1", "-",
        ]
        
    play_audio_source(args)

    if music_url:
        threading.Thread(
            target=monitor_and_restart_music,
            args=(music_url, music_title),
            daemon=True
        ).start()

    return JSONResponse({"success": True, "message": f"Playing snippet: {filename}"})

def monitor_and_restart_music(music_url: str, music_title: str):
    """Waits for the *mixed* stream to end, then restarts the music."""
    process_to_watch = None
    with state["state_lock"]:
        process_to_watch = state["ffmpeg_process"]

    if process_to_watch:
        try:
            process_to_watch.wait()
        except:
            pass
    
    time.sleep(0.1)

    with state["state_lock"]:
        if not state["ffmpeg_process"] and state["current_music_audio_url"] == music_url:
            print(f"Snippet finished, restarting music: {music_title}")
            threading.Thread(target=play_youtube_stream, args=(music_url, music_title), daemon=True).start()

@app.post("/api/upload-snippet")
async def upload_snippet(snippet: UploadFile = File(...)):
    if not snippet.filename:
        raise HTTPException(status_code=400, detail="No file provided.")
        
    filename = os.path.basename(snippet.filename).replace(" ", "_")
    dst_path = os.path.join(SOUNDS_DIR, filename)

    try:
        with open(dst_path, "wb") as buffer:
            shutil.copyfileobj(snippet.file, buffer)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error saving file: {e}")
    finally:
        snippet.file.close()

    return JSONResponse({"success": True, "message": f"File uploaded: {filename}"})

app.mount("/", StaticFiles(directory="static", html=True), name="static")
