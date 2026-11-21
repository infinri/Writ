#!/usr/bin/env python3
"""Trial client for launch.sock (M5a-07; M5-INTERFACES J-2 framing, J-11.2, J-11.3, J-11.5).

PREPARED, NOT RUN. Used only by trial.sh inside the designated disposable VM, run as the
identity under test (normally `writ`). Prints `key=value` lines for the launcher probe reports.

usage: launch-client.py --socket PATH --request FILE [--mode MODE] [--stdout-file FILE]

MODE (default normal): normal, before-hello, oversized, unknown-field, data-frame, scm-rights,
no-request, cancel-after-started, gate-eof, peer, peer-access (a negative peer outside the socket group).
"""
import argparse
import array
import errno
import json
import re
import socket
import struct
import sys
import time

CONTROL, DATA = 0x01, 0x02


def frame(kind, payload):
    return struct.pack(">IB", len(payload), kind) + payload


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def parse_control(payload):
    def unique_fields(pairs):
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError("duplicate control field")
        return fields

    return json.loads(payload.decode("utf-8"), object_pairs_hook=unique_fields)


def valid_digest(value):
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def parse_hello(payload):
    hello = parse_control(payload)
    strings = {"contract", "instance", "build", "launcher_digest", "exec_digest",
               "bwrap_digest", "bwrap_profile_digest", "apparmor_profile_digest", "maxima_digest"}
    if (not isinstance(hello, dict) or hello.keys() != strings | {"runtime_profiles"}
            or not all(isinstance(hello[key], str) for key in strings)
            or hello["contract"] != "writ-launch-hello/1"
            or hello["build"] not in ("release", "test-faults")
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", hello["instance"])
            or ".." in hello["instance"]
            or not all(valid_digest(hello[k]) for k in strings if k.endswith("_digest"))):
        raise ValueError("invalid launcher hello")
    profiles = hello["runtime_profiles"]
    if not isinstance(profiles, list) or not all(
        isinstance(p, dict) and p.keys() == {"id", "digest"}
        and isinstance(p["id"], str) and re.fullmatch(r"[a-z0-9-][a-z0-9.-]{0,63}", p["id"])
        and valid_digest(p["digest"]) for p in profiles
    ):
        raise ValueError("invalid hello runtime profiles")
    if len({p["id"] for p in profiles}) != len(profiles):
        raise ValueError("duplicate runtime profile")
    return hello


def read_frame(sock, deadline):
    def receive(count):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("launcher observation deadline elapsed")
        sock.settimeout(remaining)
        return sock.recv(count)

    header = bytearray()
    while len(header) < 5:
        chunk = receive(5 - len(header))
        if not chunk:
            if header:
                raise ValueError("truncated frame header")
            return None
        header.extend(chunk)
    length, kind = struct.unpack(">IB", header)
    limits = {CONTROL: 64 * 1024, DATA: 1024 * 1024}
    if kind not in limits or length > limits[kind]:
        raise ValueError("invalid frame header")
    payload = bytearray()
    while len(payload) < length:
        chunk = receive(length - len(payload))
        if not chunk:
            raise ValueError("truncated frame payload")
        payload.extend(chunk)
    return kind, bytes(payload)


def check_result(msg):
    if msg.keys() != {"contract", "job_id", "request_digest", "reason", "exit_status",
                      "signal", "kill", "memory_events", "pids_events", "cpu_usage_usec",
                      "output_bytes", "digests"}:
        raise ValueError("invalid result fields")
    if msg["reason"] not in ("exited", "signaled", "timed_out", "output_exceeded",
                             "cancelled", "start_failed") or not valid_digest(msg["request_digest"]):
        raise ValueError("invalid result reason or digest")
    for field, minimum, maximum in (("signal", 1, 64), ("exit_status", 0, 255)):
        value = msg[field]
        if value is not None and (type(value) is not int or not minimum <= value <= maximum):
            raise ValueError("invalid result status")
    if ((msg["reason"] == "exited" and msg["exit_status"] is None)
            or (msg["reason"] == "signaled" and msg["signal"] is None)):
        raise ValueError("missing result status")
    if (msg["kill"].keys() != {"used", "populated_zero"}
            or any(type(v) is not bool for v in msg["kill"].values())):
        raise ValueError("invalid kill result")
    counters = [msg["cpu_usage_usec"]]
    for field, keys in (("memory_events", {"max", "oom", "oom_kill"}),
                        ("pids_events", {"max"}), ("output_bytes", {"stdout", "stderr"})):
        if msg[field].keys() != keys:
            raise ValueError("invalid result counter fields")
        counters.extend(msg[field].values())
    if any(type(v) is not int or not 0 <= v < 2**53 for v in counters):
        raise ValueError("invalid result counter")
    if (msg["digests"].keys() != {"bwrap", "bwrap_profile", "apparmor_profile",
                                  "runtime_profile", "exec"}
            or not all(valid_digest(v) for v in msg["digests"].values())):
        raise ValueError("invalid result digests")


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
            if not peer_test and a.mode != "before-hello":
                raise ValueError("missing launcher hello")
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
        elif a.mode in ("normal", "cancel-after-started", "gate-eof"):
            s.sendall(frame(CONTROL, canonical(request)))
        job_id = request.get("job_id")
        output_size = {"stdout": 0, "stderr": 0}
        while not peer_test:
            f = read_frame(s, deadline)
            if f is None:
                if out["started"] == "0" and a.mode in (
                    "no-request", "oversized", "data-frame", "scm-rights", "gate-eof"
                ):
                    break
                raise ValueError("launcher closed without terminal response")
            kind, payload = f
            if kind == DATA:
                if out["started"] != "1" or len(payload) < 2 or payload[0] not in (1, 2):
                    raise ValueError("invalid output frame")
                end = 2 + payload[1]
                if end > len(payload) or payload[2:end].decode("utf-8") != job_id:
                    raise ValueError("invalid output job id")
                stream = "stdout" if payload[0] == 1 else "stderr"
                output_size[stream] += len(payload) - end
                if sum(output_size.values()) > request["limits"]["output_max_bytes"]:
                    raise ValueError("output exceeds request limit")
                if stream == "stdout":
                    stdout.extend(payload[end:])
                continue
            if kind != CONTROL:
                raise ValueError("unexpected frame kind")
            msg = parse_control(payload)
            c = msg["contract"]
            if c == "writ-launch-refused/1":
                if (out["started"] != "0" or msg.keys() != {"contract", "job_id", "code"}
                        or not isinstance(msg["job_id"], str) or msg["job_id"] not in ("", job_id)
                        or msg["code"] not in (
                            "LAUNCH_INVALID", "LAUNCH_PROFILE_UNKNOWN", "LAUNCH_SOURCE_INVALID",
                            "LAUNCH_LIMIT_EXCEEDED", "LAUNCH_BUSY", "LAUNCH_UNAVAILABLE")):
                    raise ValueError("invalid launcher refusal")
                out["outcome"] = "refused:" + msg["code"]
                break
            if msg["job_id"] != job_id or not isinstance(job_id, str):
                raise ValueError("response job id differs")
            if c == "writ-launch-started/1":
                if out["started"] != "0" or msg.keys() != {"contract", "job_id"}:
                    raise ValueError("invalid started response")
                out["started"] = "1"
                if a.mode == "cancel-after-started":
                    s.sendall(frame(CONTROL, canonical({"contract": "writ-launch-cancel/1", "job_id": job_id})))
            elif c == "writ-launch-result/1":
                check_result(msg)
                if out["started"] == "0" and msg["reason"] not in ("start_failed", "output_exceeded"):
                    raise ValueError("result before started")
                if (out["started"] == "1" and msg["reason"] != "output_exceeded"
                        and msg["output_bytes"] != output_size):
                    raise ValueError("incomplete output")
                out["outcome"] = "finished"
                out["result_reason"] = msg["reason"]
                out["result_exit_status"] = "" if msg["exit_status"] is None else str(msg["exit_status"])
                out["result_populated_zero"] = "true" if msg["kill"]["populated_zero"] else "false"
                out["result_request_digest"] = msg["request_digest"]
                break
            else:
                raise ValueError("unexpected launcher response contract")
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
