"""Run the disposable policy and acceptance management UI on ephemeral IPv4 loopback."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
from threading import BoundedSemaphore


HERE = Path(__file__).resolve().parent
BIN = Path(os.environ.get("WRIT_MANAGEMENT_BIN_DIR", HERE / "target/debug")).resolve()


class LifecycleServer(ThreadingHTTPServer):
    def __init__(self, *args):
        self.slots = BoundedSemaphore(8)
        super().__init__(*args)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except RuntimeError:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class LifecycleHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass  # Keep credentials and request URLs out of logs.

    def reply(self, status, body=b"", media="text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                         "connect-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'; "
                         "form-action 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def expected_host(self):
        return self.headers.get_all("Host") == [self.server.address]

    def do_GET(self):
        if not self.expected_host():
            return self.reply(403)
        assets = {"/": ("lifecycle-ui.html", "text/html; charset=utf-8"),
                  "/lifecycle-ui.js": ("lifecycle-ui.js", "text/javascript")}
        if self.path not in assets:
            return self.reply(404)
        name, media = assets[self.path]
        self.reply(200, (HERE / name).read_bytes(), media)

    def do_POST(self):
        if (not self.expected_host() or
                self.headers.get_all("Origin") != ["http://" + self.server.address]):
            return self.reply(403)
        if self.path not in self.server.routes:
            return self.reply(404)
        if (self.headers.get_all("Content-Type") != ["application/json"] or
                self.headers.get("Transfer-Encoding") is not None):
            return self.reply(415)
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit() or len(lengths[0]) > 6:
            return self.reply(400)
        size = int(lengths[0])
        if size > 40 * 1024:
            return self.reply(413)
        auth = self.headers.get_all("Authorization") or []
        credential = (auth[0][7:].encode("latin1") if len(auth) == 1 and
                      auth[0].startswith("Bearer ") else b"")
        if len(credential) > 512:
            credential = b""
        body = self.rfile.read(size)
        if len(body) != size:
            return self.reply(400)
        try:
            mode, *owners = self.server.routes[self.path]
            with tempfile.TemporaryFile() as private:
                private.write(credential)
                private.flush()
                fd = private.fileno()
                proc = subprocess.run(
                    [BIN / "writ-manage-fixture", *self.server.prefix, mode, self.server.management,
                     f"/proc/self/fd/{fd}", *owners],
                    input=body, capture_output=True, timeout=30, pass_fds=(fd,),
                )
            result = json.loads(proc.stdout)
            if (result["version"] != "/manage/v1" or type(result["ok"]) is not bool or
                    proc.returncode != (0 if result["ok"] else 1)):
                raise ValueError("invalid owner response")
        except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired):
            failure = {"version": "/manage/v1", "ok": False, "error": {
                "code": "UNAVAILABLE", "message": "The management service is unavailable.", "pointer": "",
            }}
            return self.reply(503, json.dumps(failure).encode(), "application/json")
        self.reply(200 if result["ok"] else 400, proc.stdout, "application/json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("management_db", type=Path)
    parser.add_argument("publication_db", type=Path)
    parser.add_argument("--contract-db", type=Path, required=True)
    parser.add_argument("--evaluator-db", type=Path, required=True)
    parser.add_argument("--fixture-credentials", action="store_true",
                        help="allow deterministic identities in a disposable test profile")
    args = parser.parse_args()
    if not args.fixture_credentials and not os.environ.get("WRIT_MANAGEMENT_IDENTITY_DB"):
        parser.error("configure WRIT_MANAGEMENT_IDENTITY_DB or select --fixture-credentials")
    with LifecycleServer(("127.0.0.1", 0), LifecycleHandler) as server:
        server.address = f"127.0.0.1:{server.server_port}"
        server.management = args.management_db
        server.prefix = [] if args.fixture_credentials else ["authenticated"]
        server.routes = {
            "/manage/v1": ("v1", args.publication_db),
            "/manage/v1/contracts": ("v1-contracts", args.contract_db),
            "/manage/v1/packs": ("v1-packs", args.contract_db, args.evaluator_db),
        }
        print(f"http://{server.address}/", flush=True)
        server.serve_forever()
