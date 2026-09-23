import asyncio
import logging
import yaml
import os
from nio import AsyncClient, AsyncClientConfig, MatrixRoom, RoomMessageText
from livekit import rtc

logger = logging.getLogger(__name__)

class MatrixAppServiceBot:
    def __init__(self, config_path="matrix_config.yaml", audio_engine=None):
        self.config_path = config_path
        self.audio_engine = audio_engine
        self.client = None
        self.livekit_room = rtc.Room()
        self.livekit_source = None
        self.load_config()

    def load_config(self):
        try:
            with open(self.config_path, "r") as f:
                self.config = yaml.safe_load(f)
        except FileNotFoundError:
            self.config = {
                "homeserver": "https://matrix.org",
                "user_id": "@soundbot:matrix.org",
                "device_id": "SOUNDBOARD",
                "appservice_token": "YOUR_AS_TOKEN",
                "rooms": [],  # List of room IDs to join automatically
                "livekit_url": "",
                "livekit_token": "" # Usually orchestrated via MatrixMSC3401
            }
            try:
                with open(self.config_path, "w") as f:
                    yaml.dump(self.config, f)
                logger.warning("Created default matrix_config.yaml. Please configure it.")
            except PermissionError:
                logger.warning("Cannot write %s (read-only). Starting with defaults.", self.config_path)
        except PermissionError:
            logger.warning(
                "Cannot read %s (permission denied). Run: chown 1000:1000 %s on the host. "
                "Matrix bot starting with defaults (will not connect).",
                self.config_path, self.config_path,
            )
            self.config = {
                "homeserver": "https://matrix.org",
                "user_id": "@soundbot:matrix.org",
                "device_id": "SOUNDBOARD",
                "appservice_token": "YOUR_AS_TOKEN",
                "rooms": [],
                "livekit_url": "",
                "livekit_token": ""
            }

    async def push_audio_loop(self):
        """Continuously pulls PCM audio from the soundboard engine and sends it to LiveKit"""
        logger.info("Starting Audio Push Loop for LiveKit")
        while True:
            # get_chunk usually returns 1920 bytes (960 samples @ 48kHz 16-bit Mono)
            chunk = self.audio_engine.get_chunk()
            if not chunk:
                await asyncio.sleep(0.01)
                continue

            frame = rtc.AudioFrame(
                chunk,
                sample_rate=48000,
                num_channels=1,
                samples_per_channel=960
            )
            
            if self.livekit_source:
                # Capture frame asynchronously as required by livekit-rtc
                await self.livekit_source.capture_frame(frame)

    async def connect_livekit(self, url: str, token: str):
        if not url or not token:
            logger.warning("LiveKit URL or Token missing. Skipping WebRTC connection.")
            return

        logger.info(f"Connecting to LiveKit room at {url}...")
        await self.livekit_room.connect(url, token)
        logger.info("Connected to LiveKit room!")
        
        # Initialize Audio Source
        self.livekit_source = rtc.AudioSource(48000, 1)
        track = rtc.LocalAudioTrack.create_audio_track("soundboard-audio", self.livekit_source)
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        
        await self.livekit_room.local_participant.publish_track(track, options)
        
        # Start pulling audio chunks and sending to LiveKit
        if self.audio_engine:
            asyncio.create_task(self.push_audio_loop())

    async def message_callback(self, room: MatrixRoom, event: RoomMessageText):
        if event.sender == self.client.user:
            return
            
        body = event.body.strip()
        if body.startswith("!ping"):
            await self.client.room_send(
                room_id=room.room_id,
                message_type="m.room.message",
                content={"msgtype": "m.text", "body": "Pong! Ready to pump audio."},
                ignore_unverified_devices=True
            )
        elif body.startswith("!stop"):
            await self._trigger_api("http://127.0.0.1:5000/stop", room.room_id, "Stopped audio.")
        elif body.startswith("!play "):
            target = body.split(" ", 1)[1].strip()
            import urllib.parse
            if target.startswith("http://") or target.startswith("https://"):
                url = f"http://127.0.0.1:5000/play_url?url={urllib.parse.quote(target)}"
                await self._trigger_api(url, room.room_id, f"Loading stream...")
            else:
                url = f"http://127.0.0.1:5000/play/{urllib.parse.quote(target)}"
                await self._trigger_api(url, room.room_id, f"Playing: {target}")

    async def _trigger_api(self, url, room_id, success_msg):
        import urllib.request
        def fetch():
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    return r.status
            except Exception as e:
                return str(e)
        
        status = await asyncio.to_thread(fetch)
        if status == 200:
            msg = success_msg
        elif status == 404:
            msg = "File not found in sounds directory."
        else:
            msg = f"API Error: {status}"
            
        await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": msg},
            ignore_unverified_devices=True
        )

    @property
    def configured(self):
        token = self.config.get("appservice_token", "")
        return bool(token) and token != "YOUR_AS_TOKEN"

    async def start(self):
        if not self.configured:
            logger.warning(
                "Matrix bot not configured (no valid appservice_token). Skipping sync loop."
            )
            return
        # Configure local state store for holding E2E keys and session data
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        store_path = os.path.join(data_dir, "nio_store")
        os.makedirs(store_path, exist_ok=True)

        client_config = AsyncClientConfig(
            store_sync_tokens=True,
            encryption_enabled=True
        )

        self.client = AsyncClient(
            self.config["homeserver"],
            self.config["user_id"],
            device_id=self.config.get("device_id", "SOUNDBOARD"),
            store_path=store_path,
            config=client_config
        )
        self.client.access_token = self.config["appservice_token"]
        self.client.add_event_callback(self.message_callback, RoomMessageText)

        logger.info(f"Matrix AppService Bot starting for {self.config['user_id']}")
        
        for room_id in self.config.get("rooms", []):
            await self.client.join(room_id)
            logger.info(f"Joined {room_id}")

        # Connect to LiveKit if configured explicitly
        if self.config.get("livekit_url") and self.config.get("livekit_token"):
            await self.connect_livekit(self.config["livekit_url"], self.config["livekit_token"])

        # In a full MSC3401 Element Call integration, we'd also listen for m.call.member events
        # from the matrix-nio sync loop and obtain the LiveKit connect token/URL dynamically here.
        
        await self.client.sync_forever(timeout=30000)

    def run_sync(self):
        asyncio.run(self.start())
