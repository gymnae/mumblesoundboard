"""
LiveKit / Meet (meet.wxbu.de) integration for the Mumble Retro Soundboard.

Connects as a headless participant (bot) to a LiveKit room and streams the
audio engine's mixed PCM output into the room. One Meet session at a time.

The LiveKit server URL and API credentials are provided via environment
variables (MEET_URL / LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET).
Room name and optional password come from the web UI.
"""

import threading
import asyncio
import os


class MeetBot:
    def __init__(self, audio_engine):
        self.audio_engine = audio_engine

        self.lock = threading.Lock()
        self.room_name = None
        self.password = None
        self.connected = False
        self.error = None

        self.livekit_url = (
            os.getenv("MEET_URL")
            or os.getenv("LIVEKIT_URL")
            or ""
        ).rstrip('/')
        self.api_key = os.getenv("LIVEKIT_API_KEY", "")
        self.api_secret = os.getenv("LIVEKIT_API_SECRET", "")

        self._loop = None
        self._room = None
        self._source = None
        self._queue = None
        self._stop_event = threading.Event()
        self._thread = None

        # allow wss:// URLs directly or plain hostnames
        if self.livekit_url and not self.livekit_url.startswith(('ws://', 'wss://')):
            self.livekit_url = 'wss://' + self.livekit_url

    @property
    def configured(self):
        return bool(self.livekit_url and self.api_key and self.api_secret)

    def _mint_token(self, room, identity):
        from livekit.api import AccessToken, VideoGrants

        token = AccessToken(self.api_key, self.api_secret) \
            .with_identity(identity) \
            .with_name(identity) \
            .with_grants(VideoGrants(room_join=True, room=room, can_publish=True))
        if self.password:
            # forward password as token metadata; a customized meet backend
            # may validate it, LiveKit itself ignores it
            token = token.with_metadata(self.password)
        return token.to_jwt()

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

        token = self._mint_token(self.room_name, "SoundBot")
        room = rtc.Room()
        self._room = room
        self._queue = asyncio.Queue(maxsize=50)

        print(f"[MEET] Connecting to {self.livekit_url} room '{self.room_name}'...")
        await room.connect(self.livekit_url, token)
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
