from http.server import HTTPServer, BaseHTTPRequestHandler
import time

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            # Health check endpoint
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "healthy"}')
        else:
            # Regular endpoint
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Hello from resilient app! Uptime: " + str(time.time()).encode())

print("Target app starting on port 8080...")
HTTPServer(('', 8080), Handler).serve_forever()