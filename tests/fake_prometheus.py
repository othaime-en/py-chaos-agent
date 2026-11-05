"""A fake Prometheus whose answers a test can change while it runs."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def vector(*values):
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {"series": str(i)}, "value": [1700000000, str(v)]}
                for i, v in enumerate(values)
            ],
        },
    }


def scalar(value):
    return {
        "status": "success",
        "data": {"resultType": "scalar", "result": [1700000000, str(value)]},
    }


class FakePrometheus:
    def __init__(self, body=None):
        self.body = body if body is not None else vector(0.001)
        self.status = 200
        self.delay = 0.0
        self.raw = None  # bytes to send instead of the JSON body
        self.queries = []
        self.headers_seen = []
        self.redirect = None
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                fake.headers_seen.append(dict(self.headers))
                fake.queries.append(
                    (parsed.path, parse_qs(parsed.query).get("query", [""])[0])
                )
                if fake.delay:
                    time.sleep(fake.delay)
                if fake.redirect:
                    self.send_response(302)
                    self.send_header("Location", fake.redirect)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = (
                    fake.raw if fake.raw is not None else json.dumps(fake.body).encode()
                )
                self.send_response(fake.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        ).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
