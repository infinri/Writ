#!/usr/bin/env python3
"""Trial client for launch.sock (M5a-07; M5-INTERFACES J-2 framing, J-11.2, J-11.3, J-11.5).

PREPARED, NOT RUN. Used only by trial.sh inside the designated disposable VM, run as the
identity under test (normally `writ`). Prints `key=value` lines for the launcher probe reports.

usage: launch-client.py --socket PATH --request FILE [--mode MODE] [--stdout-file FILE]

MODE (default normal): normal, before-hello, oversized, unknown-field, data-frame, scm-rights,
no-request, cancel-after-started, peer, peer-access (a negative peer outside the socket group).
"""
import argparse
import array
import errno
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


def parse_hello(payload):
    def unique_fields(pairs):
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError("duplicate hello field")
        return fields

    hello = json.loads(payload, object_pairs_hook=unique_fields)
    strings = {"contract", "instance", "build", "launcher_digest", "exec_digest",
               "bwrap_digest", "bwrap_profile_digest", "apparmor_profile_digest", "maxima_digest"}
    if (not isinstance(hello, dict) or hello.keys() != strings | {"runtime_profiles"}
            or not all(isinstance(hello[key], str) for key in strings)
            or hello["contract"] != "writ-launch-hello/1"):
        raise ValueError("invalid launcher hello")
    profiles = hello["runtime_profiles"]
    if not isinstance(profiles, list) or not all(
        isinstance(p, dict) and p.keys() == {"id", "digest"}
        and all(isinstance(v, str) for v in p.values()) for p in profiles
    ):
        raise ValueError("invalid hello runtime profiles")
    return hello


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
    peer_test = a.mode in ("peer", "peer-access")
    connecting = False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(a.deadline)
        connecting = True
        s.connect(a.socket)
        connecting = False
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
        hello = parse_hello(f[1])
        out["hello"] = "1"
        out["instance"] = hello["instance"]
        if peer_test:
            out["outcome"] = "hello"
        elif a.mode == "no-request":
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
        while not peer_test:
            f = read_frame(s, deadline)
            if f is None:
                break
            kind, payload = f
            if kind == DATA:
                tag, idlen = payload[0], payload[1]
                if tag == 1:
                    stdout += payload[2 + idlen:]
                continue
            if kind != CONTROL:
                raise ValueError("unexpected frame kind")
            msg = json.loads(payload)
            c = msg.get("contract")
            if c not in ("writ-launch-refused/1", "writ-launch-started/1", "writ-launch-result/1"):
                raise ValueError("unexpected launcher response contract")
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
        if peer_test:
            out["outcome"] = "closed_before_read"
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
        print(f"launcher observation: {error}", file=sys.stderr)
        rc = 3
        if connecting and isinstance(error, PermissionError) and error.errno == errno.EACCES:
            out["outcome"] = "access_denied_before_connect"
            if a.mode == "peer-access":
                rc = 0
        elif isinstance(error, TimeoutError):
            out["outcome"] = "timeout"
        elif isinstance(error, OSError):
            out["outcome"] = "unavailable"
        else:
            out["outcome"] = "malformed_response"
    if a.stdout_file:
        open(a.stdout_file, "wb").write(bytes(stdout))
    for k, v in out.items():
        print(f"{k}={v}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
