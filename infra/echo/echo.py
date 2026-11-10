"""Temporary stand-in for Heimdall's alert ingest, used until M3.

Accepts POST /alerts with the bearer token, prints the payload to stdout, and
answers 401 without the token. It exists so M1 can prove that alerts travel
from Prometheus through Alertmanager to an authenticated endpoint.
"""

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ["ALERT_TOKEN"]


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status: int, body: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self) -> None:
        self._reply(200 if self.path == "/healthz" else 404, "ok\n")

    def do_POST(self) -> None:
        if self.path != "/alerts":
            return self._reply(404, "not found\n")
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        given = self.headers.get("Authorization", "")
        if not hmac.compare_digest(given, f"Bearer {TOKEN}"):
            print(json.dumps({"event": "rejected", "reason": "bad or missing token"}), flush=True)
            return self._reply(401, "unauthorized\n")
        try:
            payload = json.loads(raw)
        except ValueError:
            return self._reply(400, "invalid json\n")
        alerts = [
            {
                "alertname": a.get("labels", {}).get("alertname"),
                "status": a.get("status"),
                "labels": a.get("labels", {}),
            }
            for a in payload.get("alerts", [])
        ]
        print(json.dumps({"event": "alerts", "alerts": alerts}), flush=True)
        self._reply(202, "accepted\n")

    def log_message(self, *args: object) -> None:  # silence per-request access logs
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()  # noqa: S104
