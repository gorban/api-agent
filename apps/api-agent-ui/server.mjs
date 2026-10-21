/** Dependency-free, explicit-route production host for the included Studio. */
import http from 'node:http';
import https from 'node:https';
import { readFileSync } from 'node:fs';

const assets = new Map([
  ['/', ['index.html', 'text/html; charset=utf-8']],
  ...['app.js', 'appearance.js', 'markdown.js', 'marked.js', 'dompurify.js', 'transport.js', 'catalog.js', 'reply.js'].map(name => ['/' + name, [name, 'text/javascript; charset=utf-8']]),
  ...['style.css', 'midnight.css', 'markdown.css'].map(name => ['/' + name, [name, 'text/css; charset=utf-8']]),
  ['/Geist.woff2', ['Geist.woff2', 'font/woff2']],
  ...['THIRD-PARTY.md', 'Geist-OFL.txt', 'marked-LICENSE.txt', 'dompurify-LICENSE.txt'].map(name => ['/' + name, ['../' + name, 'text/plain; charset=utf-8']]),
]);

export function origin(value, { listener = false } = {}) {
  let url;
  try { url = new URL(value); } catch { throw new Error('invalid_ui_origin'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.port === '0' || url.username || url.password || url.pathname !== '/' || url.search || url.hash || (listener && !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))) {
    throw new Error('invalid_ui_origin');
  }
  return url;
}

export function createStudioServer(env = process.env) {
  for (const relative of ['public/marked.js', 'public/dompurify.js']) {
    try { readFileSync(new URL('./' + relative, import.meta.url)); }
    catch { throw new Error('Studio frontend dependencies are missing. In a source checkout run npm ci from the repository root; reinstall a complete distribution for an installed package.'); }
  }
  const listener = origin(env.UI_LISTENER_URL || 'https://localhost:9443', { listener: true });
  const api = origin(env.AGENT_DJANGO_URL || 'https://localhost:8443');
  const accessToken = env.API_AGENT_UI_ACCESS_TOKEN;
  if (accessToken !== undefined && (typeof accessToken !== 'string' || !/^[A-Za-z0-9_-]{32,128}$/.test(accessToken))) throw new Error('invalid_ui_access_token');
  const token = env.API_AGENT_UI_LAUNCH_TOKEN;
  if (typeof token !== 'string' || !/^[A-Za-z0-9_-]{20,128}$/.test(token)) throw new Error('invalid_ui_launch_token');
  const handler = (request, response) => {
    response.setHeader('Cache-Control', 'no-store');
    response.setHeader('X-Content-Type-Options', 'nosniff');
    response.setHeader('Referrer-Policy', 'no-referrer');
    response.setHeader('Content-Security-Policy', `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self' ${api.origin}; base-uri 'none'; object-src 'none'; frame-ancestors 'none'`);
    if (!['GET', 'HEAD'].includes(request.method)) {
      response.writeHead(405, { Allow: 'GET, HEAD' }); response.end(); return;
    }
    const route = request.url;
    if (route === '/config.json') {
      response.setHeader('Content-Type', 'application/json');
      response.end(request.method === 'HEAD' ? undefined : JSON.stringify({ apiOrigin: api.origin, ...(accessToken === undefined ? {} : {apiHeaders: {'X-API-Agent-Session': accessToken}}) })); return;
    }
    const asset = assets.get(route);
    if (!asset) { response.writeHead(404); response.end(); return; }
    let content;
    try { content = readFileSync(new URL('./public/' + asset[0], import.meta.url)); }
    catch { response.writeHead(404); response.end(); return; }
    response.setHeader('Content-Type', asset[1]);
    response.setHeader('Content-Length', content.length);
    response.setHeader('X-UI-Launch-Token', token);
    response.end(request.method === 'HEAD' ? undefined : content);
  };
  const server = listener.protocol === 'https:'
    ? https.createServer({ cert: readFileSync(env.API_AGENT_UI_TLS_CERT), key: readFileSync(env.API_AGENT_UI_TLS_KEY) }, handler)
    : http.createServer(handler);
  const connections = new Set();
  server.on('connection', socket => {
    connections.add(socket);
    socket.once('close', () => connections.delete(socket));
  });
  const stop = () => {
    server.close();
    server.closeAllConnections();
    for (const socket of connections) socket.destroy();
  };
  return { server, listener, stop };
}

export function listenerPort(listener) {
  return Number(listener.port || (listener.protocol === 'https:' ? 443 : 80));
}

export async function launch(env = process.env) {
  const { server, listener, stop } = createStudioServer(env);
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(listenerPort(listener), listener.hostname.replace(/^\[|\]$/g, ''), resolve);
  });
  const parentManaged = env.API_AGENT_UI_PARENT_LIVENESS === 'stdin-v1';
  if (parentManaged) {
    process.stdin.once('end', stop);
    process.stdin.resume();
  }
  process.once('SIGTERM', stop);
  process.once('SIGINT', stop);
  server.once('close', () => {
    process.off('SIGTERM', stop);
    process.off('SIGINT', stop);
    if (parentManaged) {
      process.stdin.off('end', stop);
      process.stdin.pause();
    }
  });
  return server;
}
