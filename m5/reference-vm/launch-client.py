#!/usr/bin/env python3
"""Trial client for launch.sock (M5a-07; M5-INTERFACES J-2 framing, J-11.2, J-11.3, J-11.5).

PREPARED, NOT RUN. Used only by trial.sh inside the designated disposable VM, run as the
identity under test (normally `writ`). Prints `key=value` lines for the launcher probe reports.

usage: launch-client.py --socket PATH --request FILE [--mode MODE] [--stdout-file FILE]

MODE (default normal): normal, before-hello, oversized, unknown-field, data-frame, scm-rights,
no-request, cancel-after-started.
"""
import argparse
import array
import json
import socket
import struct
import sys
import time

CONTROL, DATA = 0x01, 0x02


def frame(kind, payload):
    return struct.pack(">IB", len(payload), kind) + payload


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def read_frame(sock, deadline):
    buf = b""
    while len(buf) < 5:
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        chunk = sock.recv(5 - len(buf))
        if not chunk:
            if buf:
                raise ValueError("truncated frame header")
            return None
        buf += chunk
    length, kind = struct.unpack(">IB", buf)
    payload = b""
    while len(payload) < length:
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        chunk = sock.recv(length - len(payload))
        if not chunk:
            raise ValueError("truncated frame payload")
        payload += chunk
    return kind, payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default="/run/writ-launcher/launch.sock")
    ap.add_argument("--request", required=True)
    ap.add_argument("--mode", default="normal")
    ap.add_argument("--stdout-file")
    ap.add_argument("--deadline", type=float, default=60.0)
    a = ap.parse_args()
    request = json.load(open(a.request))
    out = {"connected": "0", "hello": "0", "started": "0", "outcome": "closed"}
    deadline = time.monotonic() + a.deadline
    stdout = bytearray()
    rc = 0
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(a.socket)
        out["connected"] = "1"
        cred = s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", cred)
        out["peer_uid"] = str(uid)
        if a.mode == "before-hello":
            s.sendall(frame(CONTROL, canonical(request)))
        f = read_frame(s, deadline)
        if f is None:
            raise EOFError
        if f[0] != CONTROL:
            raise ValueError("expected launcher hello frame")
        hello = json.loads(f[1])
        if hello.get("contract") != "writ-launch-hello/1":
            raise ValueError("unexpected launcher hello contract")
        out["hello"] = "1"
        out["instance"] = hello.get("instance", "")
        if a.mode == "no-request":
            pass
        elif a.mode == "oversized":
            s.sendall(struct.pack(">IB", 64 * 1024 + 1, CONTROL))
        elif a.mode == "data-frame":
            s.sendall(frame(DATA, b"input"))
        elif a.mode == "scm-rights":
            fds = array.array("i", [sys.stdin.fileno()])
            s.sendmsg([frame(CONTROL, canonical(request))], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])
        elif a.mode == "unknown-field":
            r = dict(request)
            r["source"] = "/etc"
            s.sendall(frame(CONTROL, canonical(r)))
        elif a.mode in ("normal", "cancel-after-started"):
            s.sendall(frame(CONTROL, canonical(request)))
        while True:
            f = read_frame(s, deadline)
            if f is None:
                break
            kind, payload = f
            if kind == DATA:
                tag, idlen = payload[0], payload[1]
                if tag == 1:
                    stdout += payload[2 + idlen:]
                continue
            msg = json.loads(payload)
            c = msg.get("contract")
            if c == "writ-launch-refused/1":
                out["outcome"] = "refused:" + msg["code"]
                break
            if c == "writ-launch-started/1":
                out["started"] = "1"
                if a.mode == "cancel-after-started":
                    s.sendall(frame(CONTROL, canonical({"contract": "writ-launch-cancel/1", "job_id": request["job_id"]})))
            if c == "writ-launch-result/1":
                out["outcome"] = "finished"
                out["result_reason"] = msg["reason"]
                out["result_exit_status"] = "" if msg["exit_status"] is None else str(msg["exit_status"])
                out["result_populated_zero"] = "true" if msg["kill"]["populated_zero"] else "false"
                out["result_request_digest"] = msg["request_digest"]
                break
    except EOFError:
        pass
    except (OSError, ValueError, KeyError) as error:
        print(f"launcher observation unavailable: {error}", file=sys.stderr)
        out["outcome"] = "unavailable"
        rc = 3
    if a.stdout_file:
        open(a.stdout_file, "wb").write(bytes(stdout))
    for k, v in out.items():
        print(f"{k}={v}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
