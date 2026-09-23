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
        self.connecting = False
        self.error = None

        # MEET_URL points at the Meet web app / token gateway
        # (e.g. https://meet.example.com). LIVEKIT_URL is a fallback if the
        # gateway does not return a serverUrl. Accept legacy ws(s):// values
        # and normalize them to http(s):// for the HTTP token request.
        base = (os.getenv("MEET_URL") or "").rstrip('/')
        if base.startswith('wss://'):
            base = 'https://' + base[len('wss://'):]
        elif base.startswith('ws://'):
            base = 'http://' + base[len('ws://'):]
        self.meet_base_url = base
        fallback_lk = (os.getenv("LIVEKIT_URL") or "").rstrip('/')
        if fallback_lk and not fallback_lk.startswith(('ws://', 'wss://')):
            fallback_lk = 'wss://' + fallback_lk
        self._fallback_livekit_url = fallback_lk

        self.api_key = os.getenv("LIVEKIT_API_KEY", "")
        self.api_secret = os.getenv("LIVEKIT_API_SECRET", "")

        # Optional: force a specific LiveKit signal URL, e.g. the internal
        # wireguard address (ws://10.1.1.x:7880) to avoid hairpinning through
        # the public internet. Takes precedence over serverUrl from meet.
        self._override_livekit_url = (os.getenv("LIVEKIT_FORCE_URL") or "").rstrip('/') or None

        # Optional: explicit ICE servers (TURN) for the media path.
        # LIVEKIT_ICE_URLS = comma separated, e.g. "turn:turn.example.com:443?transport=tcp"
        # LIVEKIT_ICE_USERNAME / LIVEKIT_ICE_CREDENTIAL for auth.
        ice_urls = [u for u in (os.getenv("LIVEKIT_ICE_URLS") or "").split(',') if u.strip()]
        self._ice_servers = []
        if ice_urls:
            self._ice_servers.append({
                'urls': ice_urls,
                'username': os.getenv("LIVEKIT_ICE_USERNAME", ""),
                'credential': os.getenv("LIVEKIT_ICE_CREDENTIAL", ""),
            })

        # Bot display name in the meet session: reuse the Mumble bot name
        # unless an explicit MEET_BOT_NAME is set.
        self.bot_name = os.getenv("MEET_BOT_NAME") or os.getenv("MUMBLE_USER", "SoundBot")

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
            'nickname': self.bot_name,
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
        # allow forcing the signal endpoint (e.g. internal wireguard address)
        if self._override_livekit_url:
            server_url = self._override_livekit_url
        return token, server_url

    # --- public API ------------------------------------------------------

    def connect(self, room, password=""):
        with self.lock:
            if self.connected or self.connecting:
                return False, "Already connected to a session. Disconnect first."
            if not self.configured:
                return False, ("Meet integration not configured "
                               "(MEET_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET missing).")

            # tear down any stale previous session to avoid duplicate bots
            if self._thread and self._thread.is_alive():
                self._stop_event.set()
                self._thread.join(timeout=10)

            self.room_name = room
            self.password = password
            self.error = None
            self.connecting = True
            self._stop_event.clear()

            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True, "Connecting..."

    def disconnect(self):
        with self.lock:
            if not (self.connected or self.connecting):
                return False, "Not connected."
            self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        return True, "Disconnected."

    def status(self):
        return {
            'configured': self.configured,
            'connected': self.connected,
            'connecting': self.connecting,
            'room': self.room_name,
            'error': self.error,
        }

    def feed(self, pcm):
        """Called by the mixer thread with 20ms s16le mono 48kHz chunks."""
        if self._loop and self._queue and self.connected:
            q = self._queue
            try:
                q.get_nowait()  # drop oldest if full to keep latency low
            except asyncio.QueueEmpty:
                pass
            self._loop.call_soon_threadsafe(q.put_nowait, pcm)

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
            # make sure a partially-connected session is torn down
            room = self._room
            if room is not None:
                try:
                    self._loop.run_until_complete(room.disconnect())
                except Exception:
                    pass
        finally:
            with self.lock:
                self.connected = False
                self.connecting = False
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
        # ICE over all transports: containers behind NAT often block outbound
        # UDP, which otherwise leads to 'wait_pc_connection timed out'.
        # TRANSPORT_ALL lets the peer connection also try TCP/relay candidates.
        rtc_config = rtc.RtcConfiguration(
            ice_transport_type=rtc.IceTransportType.TRANSPORT_ALL,
        )
        room_options = rtc.RoomOptions(rtc_config=rtc_config)
        if self._ice_servers:
            ice_servers = []
            for s in self._ice_servers:
                ice_servers.append(rtc.IceServer(
                    urls=s['urls'],
                    username=s['username'] or None,
                    credential=s['credential'] or None,
                ))
            room_options.ice_servers = ice_servers
            print(f"[MEET] Using explicit ICE servers: {self._ice_servers[0]['urls']}")
        await room.connect(server_url, token, room_options)
        print("[MEET] Connected (media path established).")

        self._source = rtc.AudioSource(48000, 1)
        track = rtc.LocalAudioTrack.create_audio_track("Soundboard", self._source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        await room.local_participant.publish_track(track, options)
        print("[MEET] Audio track published.")

        with self.lock:
            self.connected = True
            self.connecting = False

        # consumer: publish queued audio at real-time pace.
        # The mixer produces exactly 960 samples (20ms) per chunk.
        async def publisher():
            while not self._stop_event.is_set() and room.isconnected():
                try:
                    pcm = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                try:
                    frame = rtc.AudioFrame.create(48000, 1, 960)
                    # AudioFrame.data is a writable int16 memoryview in
                    # livekit-rtc 0.18.x; copy the s16le PCM into it
                    frame.data[:] = memoryview(pcm)[:1920]
                    await self._source.capture_frame(frame)
                except Exception as e:
                    print(f"[MEET WARN] frame publish failed: {e}")

        publisher_task = asyncio.ensure_future(publisher())

        # watcher: wait for stop or remote disconnect
        while not self._stop_event.is_set() and room.isconnected():
            await asyncio.sleep(0.5)

        publisher_task.cancel()
        try:
            await room.disconnect()
        except Exception:
            pass
