"""Add rich HTTP error pages to an existing loopback fixture only.

Kept separate so acceptance tests can overlay an exact old source archive without
changing any original file, including its original transport fixture.
"""


def install_error_response_pages(fixture):
    fixture.error_bodies = {}
    fixture.extra_headers = {}
    inherited = fixture.server.RequestHandlerClass

    class Handler(inherited):
        def do_GET(self, head=False):
            if self.path not in fixture.statuses:
                return super().do_GET(head=head)
            fixture.requests.append((self.path, dict(self.headers)))
            self.send_response(fixture.statuses[self.path])
            self.send_header('Retry-After', '0')
            for key, value in fixture.extra_headers.get(self.path, {}).items():
                self.send_header(key, value)
            body = fixture.error_bodies.get(self.path, b'')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)
    fixture.server.RequestHandlerClass = Handler
