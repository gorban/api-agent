"""Synthetic seeded GraphQL read target and its independently owned backing."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
from urllib.request import urlopen


def serve(*, role, root, upstream='', port=8090, schema_path=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / 'seed.json'
    if not marker.exists():
        with marker.open('x') as stream:
            json.dump({'hello': 'value-' + secrets.token_hex(12), 'seed': 'done', 'runs': 1}, stream)
    seed = json.loads(marker.read_text())
    if upstream:
        with urlopen(upstream + '/seed', timeout=5) as response:
            if json.load(response)['seed'] != 'done':
                raise ValueError('Backing seed is incomplete')
    schema = json.loads(Path(schema_path).read_text()) if schema_path is not None else None
    identity = secrets.token_hex(12)
    counts = {'reads': 0, 'introspection': 0}
    observed_headers = []

    def value():
        if upstream:
            with urlopen(upstream + '/control/value', timeout=5) as response:
                return json.load(response)['hello']
        return seed['hello']

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/ready':
                self.respond({'service': role})
            elif self.path == '/seed':
                self.respond({'seed': seed['seed'], 'runs': seed['runs']})
            elif self.path == '/hello' and role == 'api':
                counts['reads'] += 1
                observed_headers.append(self.headers.get('X-Instance'))
                self.respond({'hello': value()})
            elif self.path == '/control/value':
                self.respond({'hello': value(), 'backend_id': identity,
                              'seed_runs': seed['runs'], 'headers': observed_headers, **counts})
            else:
                self.respond({'error': 'unknown route'}, 404)

        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0'))
            if self.path != '/graphql' or role != 'api' or not 0 < length <= 65536:
                self.respond({'errors': [{'message': 'Unsupported request'}]}, 400)
                return
            request = json.loads(self.rfile.read(length))
            query = request.get('query', '')
            if '__schema' in query:
                counts['introspection'] += 1
                self.respond({'data': schema})
            elif re.fullmatch(r'\s*(?:query(?:\s+[A-Za-z_][A-Za-z_0-9]*)?\s*)?\{\s*hello\s*\}\s*', query):
                counts['reads'] += 1
                observed_headers.append(self.headers.get('X-Instance'))
                self.respond({'data': {'hello': value()}})
            else:
                self.respond({'errors': [{'message': 'Only the literal hello read is supported'}]}, 400)

    return ThreadingHTTPServer(('0.0.0.0', port), Handler)


if __name__ == '__main__':
    server = serve(role=os.environ['ROLE'], root='/data', upstream=os.environ.get('UPSTREAM', ''),
                   schema_path='/opt/fixture/schema.json' if os.environ['ROLE'] == 'api' else None)
    server.serve_forever()
