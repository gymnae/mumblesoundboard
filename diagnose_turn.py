"""
TURN reachability + auth diagnostic for the Meet soundboard bot.

Run inside the soundboard container:

    python3 diagnose_turn.py turn:192.168.20.41:3478?transport=tcp SECRET

or with explicit username/password:

    python3 diagnose_turn.py turn:192.168.20.41:3478?transport=tcp user password

It performs a raw TURN Allocate request over UDP/TCP and prints the reply,
so you can see whether the server is reachable and whether the credentials
(static auth secret -> ephemeral HMAC credentials) are accepted.
"""

import socket
import struct
import sys
import os
import time
import hmac
import hashlib
import base64


def stun_allocate(sock, addr, username=None, password=None):
    # STUN: MAGIC 0x2112A442, method Allocate (0x003), T=Allocate
    txid = os.urandom(12)
    attrs = b''
    # REQUESTED-TRANSPORT (0x0016): UDP
    attrs += struct.pack('>HHH', 0x0019, 4, 17) if False else struct.pack('>HH', 0x0019, 4) + struct.pack('>B', 17) + b'\x00'*3
    if username:
        u = username.encode()
        attrs += struct.pack('>HH', 0x0006, len(u)) + u
        if len(u) % 4:
            attrs += b'\x00' * (4 - len(u) % 4)
    # MESSAGE-INTEGRITY needs SOFTWARE? not strictly; add dummy
    if password:
        # PRIORITY attr so integrity covers something
        key = password.encode()
        m = attrs + b'\x00\x00\x00'  # padding dummy for message integrity calc
        length = 20 + len(attrs) + 24 - 8
        mac = hmac.new(key, struct.pack('>HH', 0x0003, length) + b'\x21\x12\xA4\x42' + txid + attrs, hashlib.sha1).digest()
        attrs_integrity = attrs + struct.pack('>HH', 0x0008, 20) + mac
        msg = struct.pack('>HH', 0x0003, len(attrs_integrity)) + b'\x21\x12\xA4\x42' + txid + attrs_integrity
    else:
        msg = struct.pack('>HH', 0x0003, len(attrs)) + b'\x21\x12\xA4\x42' + txid + attrs

    sock.sendto(msg, 0, addr)
    sock.settimeout(5)
    try:
        data, _ = sock.recvfrom(2048)
    except socket.timeout:
        print("TIMEOUT: no response from TURN server (unreachable or port blocked)")
        return
    mtype, mlen = struct.unpack('>HH', msg[:4])
    resp_type = struct.unpack('>HH', data[:2])[0]
    print(f"TURN responded: message type 0x{resp_type:04x} "
          f"({ 'success (2xx) - credentials OK' if resp_type & 0x0110 == 0x0100 else 'error' })")
    if resp_type & 0x0110 == 0x0111:  # error response
        # parse ERROR-CODE attribute 0x0009
        i = 20
        while i + 4 <= len(data):
            atype, alen = struct.unpack('>HH', data[i:i+4])
            if atype if False else atype == 0x0009:
                code = data[i+6] * 100 + data[i+7] & 0xFF
                print(f"TURN error code: {data[i+6]}{data[i+7]:02d}: {data[i+8:i+4+alen].decode(errors='ignore')}")
            i += 4 + alen + ((4 - alen % 4) % 4)
    # also print XOR-MAPPED/RELAYED address if present
    i = 20
    while i + 4 <= len(data):
        atype, alen = struct.unpack('>HH', data[i:i+4])
        aval = data[i+4:i+4+alen]
        if atype in (0x0020, 0x0038):  # XOR-MAPPED / XOR-RELAYED
            port = struct.unpack('>H', aval[2:4])[0] ^ 0x2112
            ip = socket.inet_ntoa(bytes(b ^ m for b, m in zip(aval[4:8], b'\x21\x12\xA4\x42')))
            label = "relayed" if atype == 0x0038 else "mapped"
            print(f"  {atype:#06x} {ip}:{port}")
        i += 4 + alen + ((4 - alen % 4) % 4)


def main():
    if len(os.sys.argv) < 2:
        print(__doc__)
        return
    url = os.sys.argv[1]
    secret_or_pass = os.sys.argv[2] if len(os.sys.argv) > 2 else ""
    username = os.sys.argv[3] if len(os.sys.argv) > 3 else None

    # parse turn:host:port?transport=xxx
    scheme, rest = url.split(':', 1)
    transport = 'udp'
    if '?' in rest:
        rest, q = rest.split('?', 1)
        for kv in q.split('&'):
            k, v = kv.split('=', 1)
            if k == 'transport':
                transport = v
    host, port = rest.rsplit(':', 1)
    port = int(port)

    if secret_or_pass and not username:
        # static-auth-secret: derive ephemeral credentials
        username = str(int(time.time()) + 3600)
        password = base64.b64encode(
            hmac.new(secret_or_pass.encode(), username.encode(), hashlib.sha1).digest()
        ).decode()

    print(f"Testing {url} with username={username!r}")
    if transport == 'tcp':
        print("TCP transport: testing raw connect...")
        try:
            s = socket.create_connection((host, int(port)), timeout=5)
            s.close()
            print(f"TCP connect to {host}:{port} OK (auth not fully testable over raw TCP, but server reachable)")
        except Exception as e:
            print(f"TCP connect FAILED: {e}")
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(5)
        stun_allocate(s, (host, port), username, password)


if __name__ == '__main__':
    main()
