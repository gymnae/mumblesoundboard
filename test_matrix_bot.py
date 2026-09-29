import io
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

import yaml

from matrix_bot import MatrixAppService, load_matrix_config


BASE_CONFIG = {
    "enabled": True,
    "homeserver_url": "https://hs.example/base/",
    "server_name": "example.org",
    "sender_localpart": "sound.bot",
    "as_token": "as-secret",
    "hs_token": "hs-secret",
    "allowed_rooms": ["!allowed:example.org"],
    "startup_rooms": [],
    "allow_all_joined_rooms": False,
    "command_prefix": "!",
}


class MatrixTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config_path = os.path.join(self.temp.name, "matrix.yaml")
        self.data_dir = os.path.join(self.temp.name, "data")
        self.calls = []

    def write_config(self, **changes):
        config = dict(BASE_CONFIG)
        config.update(changes)
        with open(self.config_path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle)
        return config

    def service(self, handler=None, **changes):
        self.write_config(**changes)
        handler = handler or (lambda command, argument: self.calls.append((command, argument)) or "ok")
        with mock.patch("matrix_bot.threading.Thread.start"):
            return MatrixAppService(handler, self.config_path, self.data_dir)


class ConfigTests(MatrixTestCase):
    def test_missing_disabled_and_environment_path_precedence(self):
        self.assertFalse(load_matrix_config(os.path.join(self.temp.name, "missing"))["enabled"])
        self.write_config(enabled=False)
        self.assertFalse(load_matrix_config(self.config_path)["enabled"])
        alternate = os.path.join(self.temp.name, "alternate.yaml")
        with open(alternate, "w", encoding="utf-8") as handle:
            yaml.safe_dump(dict(BASE_CONFIG, server_name="env.example"), handle)
        with mock.patch.dict(os.environ, {"MATRIX_CONFIG": alternate}):
            self.assertEqual(load_matrix_config(self.config_path)["server_name"], "env.example")

    def test_derives_normalized_url_defaults_and_bot_mxid(self):
        self.write_config()
        config = load_matrix_config(self.config_path)
        self.assertEqual(config["homeserver_url"], "https://hs.example/base")
        self.assertEqual(config["bot_mxid"], "@sound.bot:example.org")
        self.assertEqual(config["command_prefix"], "!")
        self.assertTrue(config["enabled"])

    def test_legacy_aliases(self):
        legacy = dict(BASE_CONFIG)
        legacy.pop("homeserver_url")
        legacy.pop("as_token")
        legacy.pop("allowed_rooms")
        legacy.update(homeserver="https://legacy.example/", appservice_token="legacy-as", rooms=["!legacy:x"], user_id="@sound.bot:example.org")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(legacy, handle)
        config = load_matrix_config(self.config_path)
        self.assertEqual((config["homeserver_url"], config["as_token"], config["allowed_rooms"]), ("https://legacy.example", "legacy-as", ["!legacy:x"]))

    def test_invalid_configs_are_disabled(self):
        cases = [
            {"server_name": ""}, {"allowed_rooms": "!room:x"}, {"startup_rooms": [1]},
            {"command_prefix": ""}, {"as_token": "YOUR_AS_TOKEN"},
            {"user_id": "@other:example.org"}, {"as_token": "same", "hs_token": "same"},
        ]
        for index, changes in enumerate(cases):
            with self.subTest(index=index):
                self.write_config(**changes)
                self.assertFalse(load_matrix_config(self.config_path)["enabled"])


class AuthenticationNamespaceTests(MatrixTestCase):
    def test_hs_token_bearer_and_query_authentication(self):
        service = self.service()
        self.assertTrue(service.authenticated("Bearer hs-secret", ""))
        self.assertTrue(service.authenticated("", "hs-secret"))
        self.assertFalse(service.authenticated("Bearer wrong", "hs-secret"))
        self.assertFalse(service.authenticated("bearer hs-secret", ""))
        self.assertFalse(service.authenticated("Bearer as-secret", ""))
        self.assertFalse(service.authenticated("", "as-secret"))

    def test_claims_only_exact_bot_user_and_no_aliases(self):
        service = self.service()
        self.assertTrue(service.claim_user("@sound.bot:example.org"))
        for value in ("@soundXbot:example.org", "@sound.bot:other.org", "@sound.bot:example.org.extra", ""):
            self.assertFalse(service.claim_user(value))
        self.assertFalse(service.claim_alias("#anything:example.org"))


class TransactionAndEventTests(MatrixTestCase):
    def test_transaction_validation_and_nondict_event_filtering(self):
        service = self.service()
        for payload in (None, [], {}, {"events": {}}, {"events": None}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    service.receive_transaction("bad" + str(id(payload)), payload)
        self.assertTrue(service.receive_transaction("valid", {"events": [None, "bad", 3]}))
        with service._connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM inbox").fetchone()[0], 0)

    def test_transaction_deduplication_survives_reconstruction(self):
        service = self.service()
        event = {"event_id": "$one", "room_id": "!allowed:example.org", "type": "m.room.message"}
        self.assertTrue(service.receive_transaction("txn", {"events": [event]}))
        self.assertFalse(service.receive_transaction("txn", {"events": [dict(event, event_id="$two")]}))
        reconstructed = self.service()
        self.assertFalse(reconstructed.receive_transaction("txn", {"events": []}))
        with reconstructed._connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM inbox").fetchone()[0], 1)

    def test_commands_are_parsed_and_replies_sent(self):
        service = self.service(command_prefix="!!")
        sent = []
        service._send_message = lambda room, body: sent.append((room, body))
        service._process_event({"type": "m.room.message", "room_id": "!allowed:example.org", "sender": "@alice:x", "content": {"msgtype": "m.text", "body": "  !!play   clip.ogg  "}})
        self.assertEqual(self.calls, [("play", "clip.ogg")])
        self.assertEqual(sent, [("!allowed:example.org", "ok")])

    def test_rejects_disallowed_unknown_noncommand_encrypted_bot_edit_and_notice(self):
        service = self.service()
        base = {"type": "m.room.message", "room_id": "!allowed:example.org", "sender": "@alice:x", "content": {"msgtype": "m.text", "body": "!ping"}}
        variants = [
            dict(base, room_id="!denied:x"), dict(base, sender="@sound.bot:example.org"),
            dict(base, type="m.room.encrypted"),
            dict(base, content={"msgtype": "m.notice", "body": "!ping"}),
            dict(base, content={"msgtype": "m.text", "body": "hello"}),
            dict(base, content={"msgtype": "m.text", "body": "!unknown"}),
            dict(base, content={"msgtype": "m.text", "body": "* !ping", "m.relates_to": {"rel_type": "m.replace", "event_id": "$old"}}),
        ]
        for event in variants:
            service._process_event(event)
        self.assertEqual(self.calls, [])

    def test_invitations_only_join_allowlisted_rooms_and_leave_forgets_join(self):
        service = self.service(allow_all_joined_rooms=True)
        joined = []
        service._join_room = joined.append
        invite = {"type": "m.room.member", "state_key": "@sound.bot:example.org", "sender": "@alice:x", "content": {"membership": "invite"}}
        service._process_event(dict(invite, room_id="!denied:x"))
        service._process_event(dict(invite, room_id="!allowed:example.org"))
        self.assertEqual(joined, ["!allowed:example.org"])
        with service._connect() as conn:
            conn.execute("INSERT INTO joined_rooms VALUES (?, ?)", ("!joined:x", 1))
        self.assertTrue(service._room_allowed("!joined:x"))
        service._process_event(dict(invite, room_id="!joined:x", content={"membership": "leave"}))
        self.assertFalse(service._room_allowed("!joined:x"))

    def test_startup_rooms_are_limited_by_allowlist(self):
        service = self.service(startup_rooms=["!allowed:example.org", "!denied:x"])
        joined = []
        service._join_room = joined.append
        service._join_startup_rooms()
        self.assertEqual(joined, ["!allowed:example.org"])


class OutboundTests(MatrixTestCase):
    def test_request_uses_v3_bearer_and_bot_user_id(self):
        service = self.service()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        with mock.patch("matrix_bot.urllib.request.urlopen", return_value=response) as opener:
            result = service._request("POST", "/_matrix/client/v3/join/%23room%3Aexample.org", {})
        request = opener.call_args.args[0]
        parsed = urllib.parse.urlparse(request.full_url)
        self.assertEqual(parsed.path, "/base/_matrix/client/v3/join/%23room%3Aexample.org")
        self.assertEqual(urllib.parse.parse_qs(parsed.query), {"user_id": ["@sound.bot:example.org"]})
        self.assertEqual(request.get_header("Authorization"), "Bearer as-secret")
        self.assertEqual(request.method, "POST")
        self.assertEqual(result, {"ok": True})

    def test_join_and_send_use_encoded_v3_paths(self):
        service = self.service()
        requests = []
        service._request = lambda method, path, body=None: requests.append((method, path, body)) or ({"room_id": "!joined:x"} if "/join/" in path else {})
        service._join_room("#room name:example.org")
        service._send_message("!room/id:example.org", "reply")
        self.assertEqual(requests[0][0:2], ("POST", "/_matrix/client/v3/join/%23room%20name%3Aexample.org"))
        self.assertRegex(requests[1][1], r"^/_matrix/client/v3/rooms/%21room%2Fid%3Aexample\.org/send/m\.room\.message/[0-9a-f]{32}$")
        self.assertEqual(requests[1][2], {"msgtype": "m.text", "body": "reply"})


class GeneratorTests(unittest.TestCase):
    def run_generator(self, directory, *extra):
        registration = os.path.join(directory, "registration.yaml")
        config = os.path.join(directory, "config.yaml")
        command = [sys.executable, "generate_matrix_registration.py", "--server-name", "matrix.example.org", "--homeserver-url", "https://hs.example/", "--sender-localpart", "sound.bot+one", "--url", "http://bot:5000/", "--registration", registration, "--config", config, *extra]
        return subprocess.run(command, cwd=os.path.dirname(__file__) or ".", text=True, capture_output=True), registration, config

    def test_generates_independent_tokens_exact_namespace_and_secure_files(self):
        with tempfile.TemporaryDirectory() as directory:
            result, registration_path, config_path = self.run_generator(directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(registration_path, encoding="utf-8") as handle:
                registration = yaml.safe_load(handle)
            with open(config_path, encoding="utf-8") as handle:
                config = yaml.safe_load(handle)
            self.assertNotEqual(registration["as_token"], registration["hs_token"])
            self.assertEqual((registration["as_token"], registration["hs_token"]), (config["as_token"], config["hs_token"]))
            self.assertGreaterEqual(len(registration["as_token"]), 48)
            pattern = registration["namespaces"]["users"][0]["regex"]
            self.assertEqual(pattern, r"^@sound\.bot\+one:matrix\.example\.org$")
            self.assertTrue(re.fullmatch(pattern, "@sound.bot+one:matrix.example.org"))
            self.assertFalse(re.fullmatch(pattern, "@soundXbot+one:matrix.example.org"))
            self.assertEqual(registration["url"], "http://bot:5000")
            self.assertEqual(config["homeserver_url"], "https://hs.example")
            self.assertEqual(stat.S_IMODE(os.stat(config_path).st_mode), 0o600)

    def test_refuses_overwrite_without_force_and_force_replaces(self):
        with tempfile.TemporaryDirectory() as directory:
            first, registration, config = self.run_generator(directory)
            self.assertEqual(first.returncode, 0)
            with open(registration, encoding="utf-8") as handle:
                old_token = yaml.safe_load(handle)["as_token"]
            refused, _, _ = self.run_generator(directory)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("refusing to overwrite", refused.stderr)
            forced, _, _ = self.run_generator(directory, "--force")
            self.assertEqual(forced.returncode, 0, forced.stderr)
            with open(registration, encoding="utf-8") as handle:
                self.assertNotEqual(yaml.safe_load(handle)["as_token"], old_token)


if __name__ == "__main__":
    unittest.main()
