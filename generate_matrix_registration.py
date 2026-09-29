#!/usr/bin/env python3
"""Generate matching Matrix AS registration/runtime YAML with independent secrets."""
import argparse
import os
import re
import secrets
import sys

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--url", default="http://soundboard:5000", help="Homeserver callback URL")
    parser.add_argument("--homeserver-url", required=True, help="Client API base URL")
    parser.add_argument("--sender-localpart", default="soundbot")
    parser.add_argument("--id", default="mumblesoundboard")
    parser.add_argument("--registration", default="matrix-registration.yaml")
    parser.add_argument("--config", default="matrix_config.yaml")
    parser.add_argument("--force", action="store_true", help="Overwrite existing output files")
    args = parser.parse_args()
    outputs = (args.registration, args.config)
    existing = [path for path in outputs if os.path.exists(path)]
    if existing and not args.force:
        parser.error("refusing to overwrite: " + ", ".join(existing) + " (use --force)")

    as_token, hs_token = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
    mxid = f"@{args.sender_localpart}:{args.server_name}"
    registration = {
        "id": args.id, "url": args.url.rstrip("/"), "as_token": as_token,
        "hs_token": hs_token, "sender_localpart": args.sender_localpart,
        "rate_limited": False,
        "namespaces": {"users": [{"exclusive": True, "regex": "^" + re.escape(mxid) + "$"}], "aliases": [], "rooms": []},
    }
    config = {
        "enabled": True, "homeserver_url": args.homeserver_url.rstrip("/"),
        "server_name": args.server_name, "sender_localpart": args.sender_localpart,
        "as_token": as_token, "hs_token": hs_token, "allowed_rooms": [],
        "startup_rooms": [], "allow_all_joined_rooms": False, "command_prefix": "!",
    }
    for path, data in ((args.registration, registration), (args.config, config)):
        flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if args.force else os.O_EXCL)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False)
        print(f"wrote {path} (mode 0600)")


if __name__ == "__main__":
    main()
