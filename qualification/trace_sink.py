"""Loopback OTLP/HTTP trace sink used only by the qualification image; it needs no network."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        with Path(sys.argv[2]).open('a') as log:
            log.write(json.dumps({'path': self.path, 'bytes': len(body)}) + '\n')
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *_):
        pass


ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
