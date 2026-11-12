"""Loopback GraphQL endpoint used only by the qualification image; it needs no network."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

from graphql import build_schema, graphql_sync

GREETING = 'qualification-stub-greeting'
SCHEMA = build_schema('type Query { greeting: String }')


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with Path(sys.argv[2]).open('a') as log:
            log.write(json.dumps({'query': request['query'],
                                  'authorized': self.headers.get('Authorization') == sys.argv[3]}) + '\n')
        body = json.dumps(graphql_sync(SCHEMA, request['query'], {'greeting': GREETING}).formatted).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
