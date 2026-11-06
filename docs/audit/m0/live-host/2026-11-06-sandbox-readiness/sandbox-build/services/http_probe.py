#!/usr/bin/env python3
"""Loopback HTTP probe: logs every request line to $SR06_RUN/http.log. Binds 127.0.0.1 only."""
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.path.join(os.environ.get("SR06_RUN", "/home/tester/run/current"), "http.log")


class Handler(BaseHTTPRequestHandler):
    def _log(self):
        with open(LOG, "a") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat()}\t{self.command}\t{self.headers.get('Host')}\t{self.path}\n")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"sr06 probe ok\n")

    do_GET = do_POST = do_HEAD = _log

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", 18080), Handler).serve_forever()
