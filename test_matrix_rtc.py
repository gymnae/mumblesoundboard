import asyncio
import json
import os
import tempfile
import unittest
import urllib.parse
from unittest import mock

import yaml

from audio_quality import BYTES_PER_FRAME
from matrix_rtc import MatrixRTCClient, MatrixRTCError, MatrixRTCWireAdapter, load_matrix_rtc_config


BASE = {
    "enabled": True,
    "autostart": False,
    "homeserver_url": "https://matrix.example/",
    "access_token": "device-secret",
    "user_id": "@soundbot:example.org",
    "device_id": "DEVICE",
    "room_id": "!call:example.org",
    "focus_url": "https://focus.example/livekit/jwt",
}


class Response:
    def __init__(self, value):
        self.value = value
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self):
        return json.dumps(self.value).encode()


class WireAdapterTests(unittest.TestCase):
    def test_stable_membership_shape_and_leave(self):
        adapter = MatrixRTCWireAdapter()
        content = adapter.membership(device_id="D", membership_id="M", focus_url="https://f", expires_ms=123)
        member = content["memberships"][0]
        self.assertEqual((adapter.EVENT_TYPE, member["application"], member["scope"]), ("m.call.member", "m.call", "m.room"))
        self.assertEqual(member["device_id"], "D")
        self.assertEqual(member["membershipID"], "M")
        self.assertEqual(member["foci_preferred"][0], {"type": "livekit", "livekit_service_url": "https://f"})
        self.assertEqual(adapter.leave_membership(), {"memberships": []})

    def test_focus_contract_and_validation(self):
        adapter = MatrixRTCWireAdapter()
        openid = {"access_token": "openid-secret", "token_type": "Bearer", "matrix_server_name": "example.org", "expires_in": 300}
        self.assertEqual(adapter.focus_request(room_id="!r:x", openid_token=openid, device_id="D"), {
            "room": "!r:x", "openid_token": openid, "device_id": "D",
        })
        self.assertEqual(adapter.parse_focus_response({"jwt": "j", "url": "wss://lk"}), ("j", "wss://lk"))
        with self.assertRaises(MatrixRTCError):
            adapter.parse_focus_response({"jwt": "j", "url": "https://lk"})


class ConfigAndStatusTests(unittest.TestCase):
    def test_loads_only_nested_matrix_rtc(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "matrix.yaml")
            with open(path, "w") as handle:
                yaml.safe_dump({"enabled": True, "as_token": "as", "matrix_rtc": BASE}, handle)
            self.assertEqual(load_matrix_rtc_config(path)["access_token"], "device-secret")

    def test_missing_and_required_e2ee_fail_clearly_without_secret_status(self):
        missing = MatrixRTCClient({"enabled": True, "autostart": False})
        self.assertEqual(missing.status()["state"], "error")
        encrypted = MatrixRTCClient(dict(BASE, e2ee_required=True))
        status = encrypted.status()
        self.assertFalse(status["enabled"])
        self.assertIn("unsupported", status["error"])
        self.assertNotIn("device-secret", json.dumps(status))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        def opener(request, timeout=0):
            self.requests.append(request)
            if request.full_url.endswith("/openid/request_token"):
                return Response({"access_token": "openid", "token_type": "Bearer", "matrix_server_name": "example.org", "expires_in": 300})
            if request.full_url == "https://focus.example/livekit/jwt":
                return Response({"jwt": "livekit-jwt", "livekit_service_url": "wss://livekit.example"})
            return Response({"room_id": "!call:example.org"})
        self.client = MatrixRTCClient(BASE, urlopen=opener)

    def test_matrix_device_bearer_join_openid_focus_and_encoded_membership(self):
        self.client._join_matrix_room()
        token, url = self.client._acquire_livekit()
        self.client._put_membership(MatrixRTCWireAdapter().leave_membership())
        self.assertEqual((token, url), ("livekit-jwt", "wss://livekit.example"))
        matrix_requests = [r for r in self.requests if r.full_url.startswith("https://matrix.example")]
        self.assertTrue(all(r.get_header("Authorization") == "Bearer device-secret" for r in matrix_requests))
        focus_body = json.loads(self.requests[2].data)
        self.assertEqual(focus_body["openid_token"]["access_token"], "openid")
        self.assertIn("/state/m.call.member/%40soundbot%3Aexample.org", self.requests[3].full_url)

    def test_feed_is_bounded_drops_oldest_and_validates_frame(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        self.client._loop = mock.Mock()
        q = asyncio.Queue(maxsize=1)
        self.client._async_queue = q
        self.client._state = "connected"
        with self.assertRaises(ValueError):
            self.client.feed(bytes(BYTES_PER_FRAME - 1))
        first = bytes(BYTES_PER_FRAME)
        second = b"x" * BYTES_PER_FRAME
        self.client._enqueue(q, first)
        self.client._enqueue(q, second)
        self.assertEqual(q.get_nowait(), second)
        self.assertEqual(self.client.status()["frames_dropped"], 1)
        self.client.feed(first)
        self.client._loop.call_soon_threadsafe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
