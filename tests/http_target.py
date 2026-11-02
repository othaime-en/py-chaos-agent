"""A tiny HTTP server whose answers a test can change while it runs."""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Target:
    """A tiny HTTP server whose answer can be changed while a test runs."""

    def __init__(self):
        self.status = 200
        self.delay = 0.0
        self.hits = 0
        self.location = None
        # Statuses to answer with, one per request, before falling back to
        # ``status``. Makes "fails exactly once" scenarios deterministic.
        self.script = []
        target = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                target.hits += 1
                if target.delay:
                    time.sleep(target.delay)
                status = target.script.pop(0) if target.script else target.status
                self.send_response(status)
                if target.location:
                    self.send_header("Location", target.location)
                self.send_header("Content-Length", "2")
                self.end_headers()
                try:
                    self.wfile.write(b"ok")
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/health"
        threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        ).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
