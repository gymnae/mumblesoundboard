"""
LiveKit / Meet (meet.wxbu.de) integration for the Mumble Retro Soundboard.

Connects as a headless participant (bot) to a Meet room and streams the
audio engine's mixed PCM output into the room. One Meet session at a time.

The Meet gateway URL and API credentials are provided via environment
variables (MEET_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET). Room name and
optional password come from the web UI.

Flow (matches https://github.com/gymnae/meet):
  1. POST {MEET_URL}/api/token  {roomName, nickname, password}
     -> { token, serverUrl }   (serverUrl = the actual LiveKit endpoint)
  2. Connect to serverUrl with the returned JWT and publish audio.
"""

import threading
import asyncio
import os
import json
import urllib.request
import urllib.error


class MeetBot:
    def __init__(self, audio_engine):
        self.audio_engine = audio_engine

        self.lock = threading.Lock()
        self.room_name = None
        self.password = None
        self.connected = False
        self.error = None

        # MEET_URL points at the Meet web app / token gateway
        # (e.g. https://meet.example.com). LIVEKIT_URL is a fallback if the
        # gateway does not return a serverUrl.
        self.meet_base_url = (os.getenv("MEET_URL") or "").rstrip('/')
        fallback_lk = (os.getenv("LIVEKIT_URL") or "").rstrip('/')
        if fallback_lk and not fallback_lk.startswith(('ws://', 'wss://')):
            fallback_lk = 'wss://' + fallback_lk
        self._fallback_livekit_url = fallback_lk

        self.api_key = os.getenv("LIVEKIT_API_KEY", "")
        self.api_secret = os.getenv("LIVEKIT_API_SECRET", "")

        self._loop = None
        self._room = None
        self._source = None
        self._queue = None
        self._stop_event = threading.Event()
        self._thread = None

    @property
    def configured(self):
        return bool(self.meet_base_url and self.api_key and self.api_secret)

    def _request_token(self):
        """Ask the Meet gateway for a join token + LiveKit server URL."""
        # Normalize the room name exactly like the Meet backend does
        clean_room = self.room_name.strip().lower()
        clean_room = ''.join(c for c in clean_room if c.isalnum() or c in '-_')

        payload = json.dumps({
            'roomName': clean_room,
            'nickname': "SoundBot",
            'password': self.password or "",
        }).encode()

        req = urllib.request.Request(
            f"{self.meet_base_url}/api/token",
            data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        token = data.get('token')
        server_url = data.get('serverUrl') or self._fallback_livekit_url
        if not token or not server_url:
            raise RuntimeError("Meet gateway returned no token/serverUrl")
        if data.get('requiresPassword'):
            raise RuntimeError("Room requires a password")
        return token, server_url

    # --- public API ------------------------------------------------------

    def connect(self, room, password=""):
        with self.lock:
            if self.connected:
                return False, "Already connected to a session. Disconnect first."
            if not self.configured:
                return False, ("Meet integration not configured "
                               "(MEET_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET missing).")

            self.room_name = room
            self.password = password
            self.error = None
            self._stop_event.clear()

            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True, "Connecting..."

    def disconnect(self):
        with self.lock:
            if not self.connected:
                return False, "Not connected."
            self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        return True, "Disconnected."

    def status(self):
        return {
            'configured': self.configured,
            'connected': self.connected,
            'room': self.room_name,
            'error': self.error,
        }

    def feed(self, pcm):
        """Called by the mixer thread with 20ms s16le mono 48kHz chunks."""
        if self._loop and self._queue and self.connected:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, pcm)

    # --- internals --------------------------------------------------------

    def _run(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._run_async())
        except Exception as e:
            print(f"[MEET ERROR] {e}")
            with self.lock:
                self.error = str(e)
        finally:
            with self.lock:
                self.connected = False
                self._room = None
                self._source = None
                self._queue = None
            print("[MEET] Stopped.")

    async def _run_async(self):
        from livekit import rtc

        token, server_url = self._request_token()
        room = rtc.Room()
        self._room = room
        self._queue = asyncio.Queue(maxsize=50)

        print(f"[MEET] Connecting to {server_url} room '{self.room_name}'...")
        await room.connect(server_url, token)
        print("[MEET] Connected.")

        self._source = rtc.AudioSource(48000, 1)
        track = rtc.LocalAudioTrack.create_audio_track("Soundboard", self._source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        await room.local_participant.publish_track(track, options)

        with self.lock:
            self.connected = True

        # consumer: publish queued audio at real-time pace
        async def publisher():
            while not self._stop_event.is_set() and room.isconnected():
                try:
                    pcm = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                frame = rtc.AudioFrame.create(960, 48000, 1)
                frame.data = pcm[:1920]
                await self._source.capture_frame(frame)

        publisher_task = asyncio.ensure_future(publisher())

        # watcher: wait for stop or remote disconnect
        while not self._stop_event.is_set() and room.isconnected():
            await asyncio.sleep(0.5)

        publisher_task.cancel()
        try:
            await room.disconnect()
        except Exception:
            pass
