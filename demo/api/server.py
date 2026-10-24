"""Small independently authored, read-only GraphQL demonstration target."""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import BoundedSemaphore

sys.path.insert(0, str(Path(__file__).with_name('graphql_core-3.2.13-py3-none-any.whl')))
from graphql import build_schema, graphql_sync


SCHEMA = build_schema('''
  type Item { id: ID!, name: String!, category: String! }
  type Query { items: [Item!]!, item(id: ID!): Item, brokenItem: Item }
''')
ITEMS = (
    {'id': '1', 'name': 'Amber', 'category': 'color'},
    {'id': '2', 'name': 'Birch', 'category': 'tree'},
    {'id': '3', 'name': 'Cedar', 'category': 'tree'},
)


def _item(_root, _info, id):
    return next((item for item in ITEMS if item['id'] == id), None)


def _broken(_root, _info):
    raise ValueError('Synthetic item lookup failed')


SCHEMA.query_type.fields['items'].resolve = lambda _root, _info: ITEMS
SCHEMA.query_type.fields['item'].resolve = _item
SCHEMA.query_type.fields['brokenItem'].resolve = _broken


class Handler(BaseHTTPRequestHandler):
    MAX_BODY = 32768

    def _json(self, status, payload):
        body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/ready':
            self._json(200, {'service': 'demo-graphql'})
        elif self.path == '/items':
            self._json(200, {'items': ITEMS})
        else:
            self._json(404, {'error': 'not_found'})

    def do_POST(self):
        if self.path != '/graphql':
            self._json(404, {'error': 'not_found'})
            return
        if self.headers.get_content_type() != 'application/json':
            self._json(415, {'error': 'content_type'})
            return
        try:
            size = int(self.headers.get('Content-Length', ''))
        except ValueError:
            self._json(400, {'error': 'content_length'})
            return
        if not 0 < size <= self.MAX_BODY:
            self._json(413, {'error': 'body_size'})
            return
        try:
            document = json.loads(self.rfile.read(size))
        except (UnicodeError, json.JSONDecodeError):
            self._json(400, {'error': 'json'})
            return
        if (type(document) is not dict or type(document.get('query')) is not str
                or len(document['query']) > 16000 or document.get('variables') not in (None, {})
                or document.get('operationName') not in (None, '')):
            self._json(400, {'error': 'document'})
            return
        result = graphql_sync(SCHEMA, document['query'])
        payload = {}
        if result.data is not None:
            payload['data'] = result.data
        if result.errors:
            payload['errors'] = [error.formatted for error in result.errors]
        self._json(200, payload)

    def log_message(self, _format, *_args):
        pass


class BoundedServer(ThreadingHTTPServer):
    request_queue_size = 4
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, handler):
        super().__init__(address, handler)
        self._workers = BoundedSemaphore(4)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address

    def process_request(self, request, client_address):
        if not self._workers.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._workers.release()
            raise

    def finish_request(self, request, client_address):
        try:
            super().finish_request(request, client_address)
        finally:
            self._workers.release()

    def handle_error(self, _request, _client_address):
        pass


if __name__ == '__main__':
    BoundedServer(('0.0.0.0', 8090), Handler).serve_forever()
