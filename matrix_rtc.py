"""MatrixRTC audio publisher for Element Call / Element X.

Supported wire profile (pinned intentionally): stable MatrixRTC ``m.call.member``
state events using the per-device ``memberships`` array and a LiveKit focus.  The
focus authorization service is called with a Matrix OpenID token.  All
version-sensitive event/token JSON is isolated in :class:`MatrixRTCWireAdapter`.
"""
import asyncio
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import yaml

from audio_quality import BYTES_PER_FRAME, SAMPLES_PER_FRAME
from meet_bot import copy_pcm_to_frame

logger = logging.getLogger(__name__)


class MatrixRTCError(RuntimeError):
    pass


def load_matrix_rtc_config(path=None):
    """Load the independent normal-device MatrixRTC section.

    ``matrix_rtc`` may coexist with a disabled or enabled ``appservice`` command
    plane. Legacy top-level AS keys are deliberately not interpreted as device
    credentials.
    """
    import os
    path = os.environ.get("MATRIX_CONFIG") or path or "matrix_config.yaml"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle) or {}
    except (OSError, PermissionError):
        return {"enabled": False}
    section = document.get("matrix_rtc", {}) if isinstance(document, dict) else {}
    if not isinstance(section, dict):
        logger.error("MatrixRTC disabled: matrix_rtc must be a YAML mapping")
        return {"enabled": False}
    return dict(section)


class MatrixRTCWireAdapter:
    """Pinned adapter for the stable Element MatrixRTC/LiveKit profile.

    Deployments that expose a differently-versioned authorization contract must
    select/configure an adapter rather than leaking experimental keys throughout
    the publisher.
    """
    PROFILE = "matrixrtc-m.call.member-v1-livekit-openid"
    EVENT_TYPE = "m.call.member"

    def membership(self, *, device_id, membership_id, focus_url, expires_ms):
        return {
            "memberships": [{
                "application": "m.call",
                "scope": "m.room",
                "device_id": device_id,
                "membershipID": membership_id,
                "expires": expires_ms,
                "foci_preferred": [{
                    "type": "livekit",
                    "livekit_service_url": focus_url,
                }],
                "feeds": [{"purpose": "m.usermedia"}],
            }]
        }

    def leave_membership(self):
        return {"memberships": []}

    def focus_request(self, *, room_id, openid_token, device_id):
        # Stable MatrixRTC authorization-service request used by Element Call.
        return {
            "room": room_id,
            "openid_token": openid_token,
            "device_id": device_id,
        }

    def parse_focus_response(self, payload, configured_livekit_url=None):
        token = payload.get("jwt") or payload.get("token")
        url = payload.get("livekit_service_url") or payload.get("url") or configured_livekit_url
        if not token or not url:
            raise MatrixRTCError("MatrixRTC focus returned no LiveKit JWT/service URL")
        if not url.startswith(("ws://", "wss://")):
            raise MatrixRTCError("MatrixRTC LiveKit service URL must use ws:// or wss://")
        return token, url


class MatrixRTCClient:
    """Normal Matrix-device client plus bounded, nonblocking LiveKit publisher."""

    def __init__(self, config, adapter=None, urlopen=None):
        self.config = dict(config or {})
        self.enabled = bool(self.config.get("enabled", False))
        self.adapter = adapter or MatrixRTCWireAdapter()
        self._urlopen = urlopen or urllib.request.urlopen
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._loop = None
        self._async_queue = None
        self._room = None
        self._source = None
        self._membership_id = None
        self._state = "disabled" if not self.enabled else "idle"
        self._error = None
        self._room_id = self.config.get("room_id")
        self._frames_enqueued = 0
        self._frames_dropped = 0
        self._frames_published = 0
        self._validate()
        if self.enabled and self.config.get("autostart", True):
            self.start()

    def _validate(self):
        if not self.enabled:
            return
        required = ("homeserver_url", "access_token", "user_id", "device_id", "room_id", "focus_url")
        missing = [key for key in required if not self.config.get(key)]
        if missing:
            self.enabled = False
            self._state = "error"
            self._error = "missing MatrixRTC configuration: " + ", ".join(missing)
            logger.error(self._error)  # names only; never values
            return
        self.config["homeserver_url"] = self.config["homeserver_url"].rstrip("/")
        self.config["focus_url"] = self.config["focus_url"].rstrip("/")
        self.config["membership_ttl_ms"] = int(self.config.get("membership_ttl_ms", 60 * 60 * 1000))
        self.config["membership_refresh_ms"] = int(
            self.config.get("membership_refresh_ms", self.config["membership_ttl_ms"] // 2)
        )
        self.config["queue_frames"] = max(1, int(self.config.get("queue_frames", 10)))
        if self.config.get("e2ee_required"):
            # LiveKit's Python RTC SDK does not currently expose MatrixRTC's
            # media-encryption key provider / Matrix key-distribution protocol.
            self.enabled = False
            self._state = "error"
            self._error = "MatrixRTC E2EE is required but unsupported by this publisher/SDK"
            logger.error(self._error)

    @property
    def configured(self):
        return self.enabled

    def status(self):
        with self._lock:
            return {
                "enabled": self.enabled,
                "state": self._state,
                "connected": self._state == "connected",
                "room_id": self._room_id,
                "user_id": self.config.get("user_id"),
                "device_id": self.config.get("device_id"),
                "protocol_profile": self.adapter.PROFILE,
                "e2ee": False,
                "e2ee_required": bool(self.config.get("e2ee_required")),
                "error": self._error,
                "frames_enqueued": self._frames_enqueued,
                "frames_dropped": self._frames_dropped,
                "frames_published": self._frames_published,
            }

    def start(self):
        with self._lock:
            if not self.enabled:
                return False, self._error or "MatrixRTC is disabled"
            if self._thread and self._thread.is_alive():
                return False, "MatrixRTC publisher is already running"
            self._state = "connecting"
            self._error = None
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="matrix-rtc", daemon=True)
            self._thread.start()
        return True, "MatrixRTC publisher connecting"

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=10)
        return True, "MatrixRTC publisher stopped"

    def feed(self, pcm):
        """Nonblocking feed of exactly one 48 kHz mono s16le 20 ms frame."""
        if len(pcm) != BYTES_PER_FRAME:
            raise ValueError(f"expected {BYTES_PER_FRAME} PCM bytes, got {len(pcm)}")
        loop, async_queue = self._loop, self._async_queue
        if loop and async_queue and self._state == "connected":
            loop.call_soon_threadsafe(self._enqueue, async_queue, bytes(pcm))

    def _enqueue(self, async_queue, pcm):
        if async_queue.full():
            async_queue.get_nowait()
            with self._lock:
                self._frames_dropped += 1
        async_queue.put_nowait(pcm)
        with self._lock:
            self._frames_enqueued += 1

    def _matrix_request(self, method, path, body=None, access_token=True):
        url = self.config["homeserver_url"] + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if access_token:
            req.add_header("Authorization", "Bearer " + self.config["access_token"])
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._urlopen(req, timeout=15) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            # Do not log response bodies: proxies can echo credentials.
            raise MatrixRTCError(f"Matrix request failed: {method} {path} HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise MatrixRTCError(f"Matrix request failed: {method} {path}: {exc.reason}") from None

    def _put_membership(self, content):
        room = urllib.parse.quote(self._room_id, safe="")
        user = urllib.parse.quote(self.config["user_id"], safe="")
        event = urllib.parse.quote(self.adapter.EVENT_TYPE, safe="")
        return self._matrix_request("PUT", f"/_matrix/client/v3/rooms/{room}/state/{event}/{user}", content)

    def _join_matrix_room(self):
        room = urllib.parse.quote(self._room_id, safe="")
        result = self._matrix_request("POST", f"/_matrix/client/v3/join/{room}", {})
        self._room_id = result.get("room_id", self._room_id)

    def _acquire_livekit(self):
        user = urllib.parse.quote(self.config["user_id"], safe="")
        openid = self._matrix_request("POST", f"/_matrix/client/v3/user/{user}/openid/request_token", {})
        body = self.adapter.focus_request(
            room_id=self._room_id,
            openid_token=openid,
            device_id=self.config["device_id"],
        )
        req = urllib.request.Request(
            self.config["focus_url"], json.dumps(body).encode(),
            {"Content-Type": "application/json"}, method="POST",
        )
        try:
            with self._urlopen(req, timeout=15) as response:
                payload = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise MatrixRTCError(f"MatrixRTC focus authorization failed: HTTP {exc.code}") from None
        return self.adapter.parse_focus_response(payload, self.config.get("livekit_url"))

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_async())
        except Exception as exc:
            logger.error("MatrixRTC publisher stopped: %s", exc)
            with self._lock:
                self._error = str(exc)
                self._state = "error"
        finally:
            self._loop.close()
            self._loop = None
            self._async_queue = None
            self._room = None
            self._source = None
            with self._lock:
                if self._state != "error":
                    self._state = "stopped"

    async def _run_async(self):
        from livekit import rtc

        self._join_matrix_room()
        token, livekit_url = self._acquire_livekit()
        self._membership_id = uuid.uuid4().hex
        self._put_membership(self.adapter.membership(
            device_id=self.config["device_id"], membership_id=self._membership_id,
            focus_url=self.config["focus_url"],
            expires_ms=int(time.time() * 1000) + self.config["membership_ttl_ms"],
        ))
        room = rtc.Room()
        self._room = room
        self._async_queue = asyncio.Queue(maxsize=self.config["queue_frames"])
        try:
            await room.connect(livekit_url, token)
            self._source = rtc.AudioSource(48000, 1)
            track = rtc.LocalAudioTrack.create_audio_track("MatrixRTC Soundboard", self._source)
            options = rtc.TrackPublishOptions()
            options.source = rtc.TrackSource.SOURCE_MICROPHONE
            await room.local_participant.publish_track(track, options)
            with self._lock:
                self._state = "connected"
            next_refresh = time.monotonic() + self.config["membership_refresh_ms"] / 1000
            while not self._stop.is_set() and room.isconnected():
                if time.monotonic() >= next_refresh:
                    self._put_membership(self.adapter.membership(
                        device_id=self.config["device_id"], membership_id=self._membership_id,
                        focus_url=self.config["focus_url"],
                        expires_ms=int(time.time() * 1000) + self.config["membership_ttl_ms"],
                    ))
                    next_refresh = time.monotonic() + self.config["membership_refresh_ms"] / 1000
                try:
                    pcm = await asyncio.wait_for(self._async_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue
                frame = rtc.AudioFrame.create(48000, 1, SAMPLES_PER_FRAME)
                copy_pcm_to_frame(frame, pcm)
                await self._source.capture_frame(frame)
                with self._lock:
                    self._frames_published += 1
        finally:
            try:
                await room.disconnect()
            finally:
                try:
                    self._put_membership(self.adapter.leave_membership())
                except Exception as exc:
                    logger.warning("Could not clear MatrixRTC membership: %s", exc)
