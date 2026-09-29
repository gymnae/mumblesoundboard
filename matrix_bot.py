"""Small, homeserver-agnostic Matrix Application Service v1 runtime.

Only unencrypted room messages are supported.  The homeserver pushes events to the
Flask routes; this module never performs /sync.
"""
import contextlib
import hashlib
import hmac
import json
import logging
import os
import queue
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import yaml

logger = logging.getLogger(__name__)


class MatrixConfigError(ValueError):
    pass


def load_matrix_config(path=None):
    path = os.environ.get("MATRIX_CONFIG") or path or "matrix_config.yaml"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except (FileNotFoundError, PermissionError, OSError) as exc:
        logger.info("Matrix disabled: cannot read %s (%s)", path, exc)
        return {"enabled": False, "config_path": path}

    if not isinstance(raw, dict):
        logger.error("Matrix disabled: %s must contain a YAML mapping", path)
        return {"enabled": False, "config_path": path}
    if not raw.get("enabled", True):
        return {"enabled": False, "config_path": path}

    config = dict(raw)
    config["homeserver_url"] = raw.get("homeserver_url") or raw.get("homeserver")
    config["as_token"] = raw.get("as_token") or raw.get("appservice_token")
    config["allowed_rooms"] = raw.get("allowed_rooms", raw.get("rooms", []))
    config["startup_rooms"] = raw.get("startup_rooms", [])
    config["sender_localpart"] = raw.get("sender_localpart", "soundbot")
    config["command_prefix"] = raw.get("command_prefix", "!")
    config["allow_all_joined_rooms"] = bool(raw.get("allow_all_joined_rooms", False))

    required = ("homeserver_url", "server_name", "sender_localpart", "as_token", "hs_token")
    missing = [key for key in required if not config.get(key)]
    if missing:
        logger.error("Matrix disabled: missing configuration: %s", ", ".join(missing))
        return {"enabled": False, "config_path": path}
    for key in ("allowed_rooms", "startup_rooms"):
        if not isinstance(config[key], list) or not all(isinstance(v, str) for v in config[key]):
            logger.error("Matrix disabled: %s must be a list of strings", key)
            return {"enabled": False, "config_path": path}
    if not isinstance(config["command_prefix"], str) or not config["command_prefix"]:
        logger.error("Matrix disabled: command_prefix must be a non-empty string")
        return {"enabled": False, "config_path": path}

    config["homeserver_url"] = config["homeserver_url"].rstrip("/")
    config["bot_mxid"] = f"@{config['sender_localpart']}:{config['server_name']}"
    legacy_user = raw.get("user_id")
    if legacy_user and legacy_user != config["bot_mxid"]:
        logger.error("Matrix disabled: legacy user_id must equal %s", config["bot_mxid"])
        return {"enabled": False, "config_path": path}
    placeholders = {"YOUR_AS_TOKEN", "YOUR_APPSERVICE_TOKEN_HERE", "YOUR_HS_TOKEN"}
    if config["as_token"] in placeholders or config["hs_token"] in placeholders:
        logger.error("Matrix disabled: replace example tokens with generated secrets")
        return {"enabled": False, "config_path": path}
    if hmac.compare_digest(config["as_token"], config["hs_token"]):
        logger.error("Matrix disabled: as_token and hs_token must be independent secrets")
        return {"enabled": False, "config_path": path}
    config["enabled"] = True
    config["config_path"] = path
    return config


class MatrixAppService:
    def __init__(self, command_handler, config_path=None, data_dir="data"):
        self.config = load_matrix_config(config_path)
        self.command_handler = command_handler
        self.enabled = self.config.get("enabled", False)
        self._wake = threading.Event()
        self._db_path = os.path.join(data_dir, "matrix_appservice.db")
        if not self.enabled:
            return
        os.makedirs(data_dir, exist_ok=True)
        self._init_db()
        threading.Thread(target=self._worker, name="matrix-as-worker", daemon=True).start()
        threading.Thread(target=self._join_startup_rooms, name="matrix-as-startup", daemon=True).start()

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._db_path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS transactions (
                    txn_id TEXT PRIMARY KEY, received_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inbox (
                    event_key TEXT PRIMARY KEY, room_id TEXT NOT NULL,
                    event_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS joined_rooms (
                    room_id TEXT PRIMARY KEY, joined_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS inbox_status_idx ON inbox(status, created_at);
                UPDATE inbox SET status='pending' WHERE status='processing';
            """)

    def authenticated(self, authorization, query_token):
        token = ""
        if authorization and authorization.startswith("Bearer "):
            token = authorization[7:]
        elif query_token:
            token = query_token
        expected = self.config.get("hs_token", "") if self.enabled else ""
        return bool(token and expected) and hmac.compare_digest(token, expected)

    def claim_user(self, user_id):
        return self.enabled and user_id == self.config["bot_mxid"]

    def claim_alias(self, _room_alias):
        return False

    def receive_transaction(self, txn_id, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise ValueError("transaction body must contain an events array")
        now = int(time.time())
        with self._connect() as conn:
            inserted = conn.execute(
                "INSERT OR IGNORE INTO transactions(txn_id, received_at) VALUES (?, ?)",
                (txn_id, now),
            ).rowcount
            if not inserted:
                return False
            for index, event in enumerate(payload.get("events", [])):
                if not isinstance(event, dict):
                    continue
                event_id = event.get("event_id")
                event_key = event_id or hashlib.sha256(
                    (txn_id + ":" + str(index) + ":" + json.dumps(event, sort_keys=True)).encode()
                ).hexdigest()
                room_id = event.get("room_id", "")
                conn.execute(
                    "INSERT OR IGNORE INTO inbox(event_key, room_id, event_json, created_at) VALUES (?, ?, ?, ?)",
                    (event_key, room_id, json.dumps(event), now),
                )
        self._wake.set()
        return True

    def _worker(self):
        while True:
            item = None
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT event_key, event_json FROM inbox WHERE status='pending' ORDER BY created_at LIMIT 1"
                ).fetchone()
                if row:
                    conn.execute(
                        "UPDATE inbox SET status='processing', attempts=attempts+1 WHERE event_key=?",
                        (row[0],),
                    )
                    item = row
            if not item:
                self._wake.wait(1.0)
                self._wake.clear()
                continue
            try:
                self._process_event(json.loads(item[1]))
            except Exception:
                logger.exception("Matrix event processing failed: %s", item[0])
            finally:
                # Marking failures done avoids unsafe command replay after restart.
                with self._connect() as conn:
                    conn.execute("UPDATE inbox SET status='done' WHERE event_key=?", (item[0],))

    def _room_allowed(self, room_id):
        if room_id in self.config["allowed_rooms"]:
            return True
        if not self.config["allow_all_joined_rooms"]:
            return False
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM joined_rooms WHERE room_id=?", (room_id,)).fetchone() is not None

    def _process_event(self, event):
        event_type = event.get("type")
        room_id = event.get("room_id", "")
        sender = event.get("sender")
        if sender == self.config["bot_mxid"] or event_type == "m.room.encrypted":
            return
        if event_type == "m.room.member" and event.get("state_key") == self.config["bot_mxid"]:
            membership = (event.get("content") or {}).get("membership")
            if membership == "invite" and room_id in self.config["allowed_rooms"]:
                self._join_room(room_id)
            elif membership == "leave":
                with self._connect() as conn:
                    conn.execute("DELETE FROM joined_rooms WHERE room_id=?", (room_id,))
            return
        if event_type != "m.room.message" or not self._room_allowed(room_id):
            return
        content = event.get("content") or {}
        relates_to = content.get("m.relates_to") or {}
        if relates_to.get("rel_type") == "m.replace":
            return
        if content.get("msgtype") != "m.text" or not isinstance(content.get("body"), str):
            return
        body = content["body"].strip()
        prefix = self.config["command_prefix"]
        if not body.startswith(prefix):
            return
        command_line = body[len(prefix):].strip()
        command, _, argument = command_line.partition(" ")
        if command not in ("ping", "play", "stop"):
            return
        reply = self.command_handler(command, argument.strip())
        if reply:
            self._send_message(room_id, str(reply))

    def _request(self, method, path, body=None):
        query = urllib.parse.urlencode({"user_id": self.config["bot_mxid"]})
        url = self.config["homeserver_url"] + path + ("&" if "?" in path else "?") + query
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", "Bearer " + self.config["as_token"])
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read() or b"{}")

    def _join_room(self, room):
        try:
            encoded = urllib.parse.quote(room, safe="")
            result = self._request("POST", f"/_matrix/client/v3/join/{encoded}", {})
            room_id = result.get("room_id", room)
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO joined_rooms(room_id, joined_at) VALUES (?, ?)",
                    (room_id, int(time.time())),
                )
        except Exception:
            logger.exception("Matrix bot could not join %s", room)

    def _join_startup_rooms(self):
        for room in self.config["startup_rooms"]:
            if room in self.config["allowed_rooms"]:
                self._join_room(room)
            else:
                logger.warning("Ignoring startup room not present in allowed_rooms: %s", room)

    def _send_message(self, room_id, message):
        encoded_room = urllib.parse.quote(room_id, safe="")
        txn_id = uuid.uuid4().hex
        try:
            self._request(
                "PUT",
                f"/_matrix/client/v3/rooms/{encoded_room}/send/m.room.message/{txn_id}",
                {"msgtype": "m.text", "body": message},
            )
        except (urllib.error.URLError, TimeoutError, ValueError):
            logger.exception("Could not send Matrix reply to %s", room_id)
