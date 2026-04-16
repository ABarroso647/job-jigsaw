"""Tiny HTTP server — POST /run triggers scrape.py. No extra dependencies."""

import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/run":
            params = parse_qs(parsed.query)
            deep = params.get("deep", ["0"])[0] == "1"
            env = {**os.environ, "DEEP_SEARCH": "1" if deep else "0"}
            subprocess.Popen([sys.executable, "scrape.py"], env=env)
            self._respond(200, b'{"ok": true}')
        else:
            self._respond(404, b'{"error": "not found"}')

    def _respond(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # suppress access logs


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 3007), Handler)
    print("Trigger server listening on :3007", flush=True)
    server.serve_forever()
