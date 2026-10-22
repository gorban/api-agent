"""Synthetic image-native HTTP fixture with persistent once-only initialization."""
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import urlopen

ROLE = os.environ.get('ROLE', 'api')
ROOT = Path('/data')
ROOT.mkdir(exist_ok=True)
MARKER = ROOT / 'seed.json'
if not MARKER.exists():
    with MARKER.open('x') as stream:
        json.dump({'seed': 'done', 'runs': 1}, stream)
UPSTREAM = os.environ.get('UPSTREAM', '')
if UPSTREAM:
    with urlopen(UPSTREAM + '/seed', timeout=3) as response:
        if json.load(response).get('seed') != 'done':
            raise RuntimeError('Synthetic dependency is not initialized')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/ready':
            payload = {'service': ROLE, 'upstream': UPSTREAM}
        elif self.path == '/seed':
            payload = json.loads(MARKER.read_text())
        else:
            self.send_error(404)
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


HTTPServer(('0.0.0.0', 8090), Handler).serve_forever()
