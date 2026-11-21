import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
import unittest


HERE = Path(__file__).resolve().parent


HELLO = {
    "contract": "writ-launch-hello/1", "instance": "fixture", "build": "release",
    "launcher_digest": "a" * 64, "exec_digest": "b" * 64, "bwrap_digest": "c" * 64,
    "bwrap_profile_digest": "d" * 64, "apparmor_profile_digest": "e" * 64,
    "maxima_digest": "f" * 64, "runtime_profiles": [{"id": "busybox", "digest": "0" * 64}],
}


def control_frame(message):
    payload = json.dumps(message).encode()
    return struct.pack(">IB", len(payload), 1) + payload


class TrialLaunchClientTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir="/tmp")))
        self.root.chmod(0o755)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir(mode=0o700)
        self.client = self.root / "launch-client.py"
        shutil.copyfile(HERE / self.client.name, self.client)
        self.request = self.root / "request.json"
        self.request.write_text("{}\n")
        self.request.chmod(0o644)
        self.job_stdout = self.root / "stdout"
        self.socket = self.root / "launch.sock"

    def launch_command(self, runner=(), mode="normal", deadline="5"):
        return [
            "bash", "-euc",
            'share=$1; ev=$2; . "$3"; shift 3; '
            'record_launch_client ident "$@"; printf "consume job output\\n"',
            "trial", str(self.root), str(self.evidence), str(HERE / "trial-launch-client.sh"),
            *runner, "python3", str(self.client), "--socket", str(self.socket),
            "--request", str(self.request), "--stdout-file", str(self.job_stdout),
            "--deadline", deadline, "--mode", mode,
        ]

    def test_unreadable_client_stops_before_consuming_job_stdout(self):
        self.client.chmod(0)
        runner = ("runuser", "-u", "nobody", "--") if os.geteuid() == 0 else ()
        result = subprocess.run(self.launch_command(runner), capture_output=True, text=True, timeout=10)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.evidence / "launch-ident.exit").read_text(), f"{result.returncode}\n")
        self.assertIn("Permission denied", (self.evidence / "launch-ident.txt").read_text())
        self.assertIn("launch client ident exited", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.job_stdout.exists())

    def test_missing_socket_is_unavailable(self):
        result = subprocess.run(self.launch_command(), capture_output=True, text=True, timeout=10)
        diagnostic = (self.evidence / "launch-ident.txt").read_text()
        self.assertEqual(result.returncode, 3)
        self.assertEqual((self.evidence / "launch-ident.exit").read_text(), "3\n")
        self.assertIn("No such file or directory", diagnostic)
        self.assertIn("connected=0\n", diagnostic)
        self.assertIn("outcome=unavailable\n", diagnostic)
        self.assertEqual(result.stdout, "")

    def test_peer_close_and_incomplete_hello_are_distinct(self):
        for index, (reply, status, outcome) in enumerate([
            (b"", 0, "closed_before_read"),
            (b"\x00", 3, "malformed_response"),
            (control_frame(dict(HELLO, contract="unexpected")), 3, "malformed_response"),
            (control_frame({"contract": HELLO["contract"]}), 3, "malformed_response"),
            (control_frame(dict(HELLO, runtime_profiles=[{}])), 3, "malformed_response"),
        ]):
            with self.subTest(reply=reply), socket.socket(socket.AF_UNIX) as server:
                self.socket = self.root / f"hello-{index}.sock"
                server.bind(str(self.socket))
                server.listen()
                server.settimeout(5)
                with subprocess.Popen(self.launch_command(mode="peer-access"), stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True) as process:
                    conn, _ = server.accept()
                    with conn:
                        conn.sendall(reply)
                    process.communicate(timeout=10)
                diagnostic = (self.evidence / "launch-ident.txt").read_text()
                self.assertEqual(process.returncode, status)
                self.assertIn("connected=1\n", diagnostic)
                self.assertIn(f"outcome={outcome}\n", diagnostic)

    def test_connect_permission_denial_is_expected_only_for_access_peer(self):
        runner = ("runuser", "-u", "nobody", "--") if os.geteuid() == 0 else ()
        self.client.chmod(0o644)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(self.socket))
            server.listen()
            self.socket.chmod(0)
            self.job_stdout.touch(mode=0o666)
            self.job_stdout.chmod(0o666)
            for mode, status in [("peer-access", 0), ("peer", 3), ("normal", 3)]:
                with self.subTest(mode=mode):
                    result = subprocess.run(self.launch_command(runner, mode), capture_output=True)
                    self.assertEqual(result.returncode, status, result.stderr)
                    diagnostic = (self.evidence / "launch-ident.txt").read_text().splitlines()
                    self.assertIn("connected=0", diagnostic)
                    self.assertIn("outcome=access_denied_before_connect", diagnostic)

    def test_peer_hello_and_timeout(self):
        for mode, hello, status, outcome in [("peer", True, 0, "hello"),
                                              ("peer-access", False, 3, "timeout")]:
            with self.subTest(mode=mode), socket.socket(socket.AF_UNIX) as server:
                self.socket = self.root / f"{mode}.sock"
                server.bind(str(self.socket))
                server.listen()
                server.settimeout(5)
                with subprocess.Popen(self.launch_command(mode=mode, deadline="0.3"),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
                    conn, _ = server.accept()
                    with conn:
                        if hello:
                            conn.sendall(control_frame(HELLO))
                        process.communicate(timeout=5)
                self.assertEqual(process.returncode, status)
                diagnostic = (self.evidence / "launch-ident.txt").read_text().splitlines()
                self.assertIn("connected=1", diagnostic)
                self.assertIn(f"outcome={outcome}", diagnostic)

    @unittest.skipUnless(os.geteuid() == 0, "requires root to stage files and switch UID")
    def test_staged_client_runs_under_a_separate_uid(self):
        account = pwd.getpwnam("nobody")
        private = self.root / "private"
        private.mkdir(mode=0o700)
        shutil.copyfile(HERE / "launch-client.py", private / "launch-client.py")
        share = self.root / "share"
        share.mkdir(mode=0o755)
        share.chmod(0o755)
        (share / "req-default.json").write_text("{}\n")
        command = [
            "bash", "-euc",
            'here=$1; share=$2; ev=$3; sock=$4; . "$5"; stage_launch_client; launch "$6" ident',
            "trial", str(private), str(share), str(self.evidence), str(self.socket),
            str(HERE / "trial-launch-client.sh"), account.pw_name,
        ]
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(self.socket))
            self.socket.chmod(0o666)
            server.listen()
            server.settimeout(5)
            with subprocess.Popen(command) as process:
                conn, _ = server.accept()
                with conn, conn.makefile("rb") as request:
                    conn.settimeout(5)
                    peer = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                    self.assertEqual(struct.unpack("3i", peer)[1], account.pw_uid)
                    conn.sendall(control_frame(HELLO))
                    length, _ = struct.unpack(">IB", request.read(5))
                    self.assertEqual(json.loads(request.read(length)), {})
                    data = bytes([1, 0]) + b"fixture stdout"
                    conn.sendall(struct.pack(">IB", len(data), 2) + data)
                    conn.sendall(control_frame({
                        "contract": "writ-launch-result/1", "reason": "exited", "exit_status": 0,
                        "kill": {"populated_zero": True}, "request_digest": "fixture",
                    }))
                self.assertEqual(process.wait(timeout=10), 0)
        staged = share / "launch-client.py"
        output = share / "launch-ident.out"
        for path, owner, mode in [(share, 0, 0o755), (staged, 0, 0o644),
                                  (output, account.pw_uid, 0o600), (private, 0, 0o700)]:
            info = path.stat()
            self.assertEqual((info.st_uid, stat.S_IMODE(info.st_mode)), (owner, mode))
        self.assertEqual(staged.read_bytes(), (private / staged.name).read_bytes())
        self.assertEqual(output.read_bytes(), b"fixture stdout")
        self.assertEqual((self.evidence / "launch-ident.exit").read_text(), "0\n")

    def test_launcher_refusal_is_preserved_as_a_successful_client_outcome(self):
        self.client.chmod(0o644)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(self.socket))
            server.listen()
            server.settimeout(5)
            with subprocess.Popen(self.launch_command(), stdout=subprocess.PIPE, text=True) as process:
                conn, _ = server.accept()
                with conn, conn.makefile("rb") as request:
                    conn.settimeout(5)
                    conn.sendall(control_frame(HELLO))
                    length, _ = struct.unpack(">IB", request.read(5))
                    self.assertEqual(json.loads(request.read(length)), {})
                    conn.sendall(control_frame({
                        "contract": "writ-launch-refused/1", "code": "LAUNCH_UNAVAILABLE",
                    }))
                stdout, _ = process.communicate(timeout=10)

        self.assertEqual(process.returncode, 0)
        self.assertEqual((self.evidence / "launch-ident.exit").read_text(), "0\n")
        self.assertIn("outcome=refused:LAUNCH_UNAVAILABLE\n", (self.evidence / "launch-ident.txt").read_text())
        self.assertEqual(stdout, "consume job output\n")
        self.assertEqual(self.job_stdout.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
