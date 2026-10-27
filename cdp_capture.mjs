// Capture Chrome DevTools Protocol network traffic while a human operates the page.
// Usage: node cdp_capture.mjs <output-dir> [port]
import fs from 'node:fs';
import path from 'node:path';

const OUT_DIR = process.argv[2] || './captures';
const PORT = process.argv[3] || '9222';
const BODY_DIR = path.join(OUT_DIR, 'bodies');
fs.mkdirSync(BODY_DIR, { recursive: true });

const eventsPath = path.join(OUT_DIR, 'cdp-events.jsonl');
const out = fs.createWriteStream(eventsPath, { flags: 'a' });
const startedAt = new Date().toISOString();
log({ t: 'session_start', at: startedAt, out: eventsPath });

function log(obj) {
  out.write(JSON.stringify({ at: new Date().toISOString(), ...obj }) + '\n');
}

const version = await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json();
console.log('browser:', version.Browser, version.webSocketDebuggerUrl);

const ws = new WebSocket(version.webSocketDebuggerUrl);
let nextId = 0;
const pending = new Map();
const sessions = new Map();          // sessionId -> { targetId, type, url }
const targetSessions = new Map();    // targetId -> active sessionId
const requests = new Map();          // `${sid}:${reqId}` -> request/response info
const savedBodies = new Set();

function send(method, params = {}, sessionId) {
  const id = ++nextId;
  const msg = { id, method, params };
  if (sessionId) msg.sessionId = sessionId;
  ws.send(JSON.stringify(msg));
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject, method });
    setTimeout(() => {
      if (pending.delete(id)) reject(new Error(`timeout: ${method}`));
    }, 15000);
  });
}

ws.addEventListener('message', (ev) => {
  let msg;
  try {
    msg = JSON.parse(typeof ev.data === 'string' ? ev.data : ev.data.toString());
  } catch {
    return;
  }
  if (msg.id !== undefined && pending.has(msg.id)) {
    const p = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) p.reject(new Error(`${p.method}: ${JSON.stringify(msg.error)}`));
    else p.resolve(msg.result);
    return;
  }
  handleEvent(msg).catch((e) => log({ t: 'handler_error', error: String(e) }));
});

async function handleEvent(msg) {
  const { method, params = {}, sessionId } = msg;
  const key = (reqId) => `${sessionId}:${reqId}`;

  switch (method) {
    case 'Target.attachedToTarget': {
      const info = params.targetInfo || {};
      await registerSession(params.sessionId, info);
      break;
    }
    case 'Target.detachedFromTarget': {
      const info = sessions.get(params.sessionId);
      sessions.delete(params.sessionId);
      if (info && targetSessions.get(info.targetId) === params.sessionId) {
        targetSessions.delete(info.targetId);
      }
      log({ t: 'target_detached', sid: params.sessionId, targetId: info?.targetId });
      break;
    }

    case 'Network.requestWillBeSent': {
      const r = params.request || {};
      const rec = {
        t: 'request', sid: sessionId, reqId: params.requestId,
        url: r.url, method: r.method, headers: r.headers || {},
        resourceType: params.type, documentURL: params.documentURL,
        wallTime: params.wallTime, initiator: params.initiator?.type,
        postData: trunc(r.postData, 100_000),
        postDataTruncated: !!r.postData && r.postData.length > 100_000,
        hasPostData: !!params.request?.hasPostData,
      };
      requests.set(key(params.requestId), { request: rec, response: null });
      log(rec);
      break;
    }
    case 'Network.requestWillBeSentExtraInfo': {
      // Real headers (including Cookie) live here, keyed by requestId.
      const rec = requests.get(key(params.requestId));
      if (rec) rec.extraRequestHeaders = params.headers;
      log({ t: 'request_extra', sid: sessionId, reqId: params.requestId, headers: params.headers });
      break;
    }
    case 'Network.responseReceived': {
      const r = params.response || {};
      const rec = requests.get(key(params.requestId));
      if (rec) rec.response = { status: r.status, mimeType: r.mimeType, headers: r.headers, url: r.url };
      log({
        t: 'response', sid: sessionId, reqId: params.requestId,
        status: r.status, mimeType: r.mimeType, headers: r.headers,
        url: r.url, fromDiskCache: params.response?.fromDiskCache,
      });
      break;
    }
    case 'Network.responseReceivedExtraInfo':
      log({ t: 'response_extra', sid: sessionId, reqId: params.requestId, statusCode: params.statusCode, headers: params.headers });
      break;

    case 'Network.loadingFinished': {
      const rec = requests.get(key(params.requestId));
      log({ t: 'finished', sid: sessionId, reqId: params.requestId, encodedDataLength: params.encodedDataLength });
      if (rec?.response && isInteresting(rec.response.url || rec.request.url, rec.response.mimeType)) {
        await saveBody(sessionId, params.requestId, rec).catch((e) =>
          log({ t: 'body_error', sid: sessionId, reqId: params.requestId, error: String(e) }));
      }
      break;
    }
    case 'Network.loadingFailed':
      log({ t: 'failed', sid: sessionId, reqId: params.requestId, errorText: params.errorText, blockedReason: params.blockedReason });
      break;

    case 'Network.webSocketCreated':
      log({ t: 'ws_created', sid: sessionId, reqId: params.requestId, url: params.url });
      break;
    case 'Network.webSocketFrameSent':
    case 'Network.webSocketFrameReceived':
      log({
        t: method === 'Network.webSocketFrameSent' ? 'ws_sent' : 'ws_received',
        sid: sessionId, reqId: params.requestId,
        opcode: params.response?.opcode,
        payload: trunc(params.response?.payloadData, 5000),
      });
      break;
  }
}

async function saveBody(sessionId, requestId, rec) {
  const bodyKey = `${sessionId}:${requestId}`;
  if (savedBodies.has(bodyKey)) return;
  savedBodies.add(bodyKey);
  const { body, base64Encoded } = await send('Network.getResponseBody', { requestId }, sessionId);
  if (body == null) return;
  if (body.length > 3_000_000) {
    log({ t: 'body_skipped', sid: sessionId, reqId: requestId, size: body.length, mimeType: rec.response.mimeType });
    return;
  }
  const ext = base64Encoded ? '.b64' : (rec.response.mimeType?.includes('json') ? '.json' : '.txt');
  const file = path.join(BODY_DIR, `s${String(sessionId).slice(-6)}-${String(requestId).replace(/[^\w.-]/g, '_')}${ext}`);
  fs.writeFileSync(file, base64Encoded ? body : body, 'utf8');
  log({
    t: 'body', sid: sessionId, reqId: requestId, file: path.relative(OUT_DIR, file),
    url: rec.response.url || rec.request.url, mimeType: rec.response.mimeType,
    size: body.length, base64Encoded,
  });
}

function trunc(s, n) {
  return typeof s === 'string' && s.length > n ? s.slice(0, n) + `…[truncated ${s.length - n}]` : s;
}

function isInteresting(url, mimeType) {
  let u;
  try { u = new URL(url); } catch { return false; }
  if (!/(^|\.)(adobe\.com|adobe\.io|adobelogin\.com|adobe\.net)$/.test(u.hostname)) return false;
  if (/\.(js|mjs|css|woff2?|ttf|otf|png|jpe?g|gif|webp|svg|ico|mp4|webm|map)(\?|$)/i.test(u.pathname)) return false;
  const m = (mimeType || '').toLowerCase();
  if (m.startsWith('image/') || m.startsWith('font/') || m.includes('javascript') || m.includes('css')) return false;
  return m === '' || m.includes('json') || m.includes('text') || m.includes('xml') || m.includes('protobuf') || m.includes('octet-stream');
}

async function enableNetwork(sessionId) {
  try {
    await send('Network.enable', {
      maxTotalBufferSize: 200_000_000,
      maxResourceBufferSize: 30_000_000,
      maxPostDataSize: 200_000,
    }, sessionId);
    await send('Network.setCacheDisabled', { cacheDisabled: false }, sessionId).catch(() => {});
    log({ t: 'network_enabled', sid: sessionId });
  } catch (e) {
    log({ t: 'network_enable_failed', sid: sessionId, error: String(e) });
  }
}

async function registerSession(sessionId, info) {
  if (sessions.has(sessionId)) return false;
  if (targetSessions.has(info.targetId)) {
    // Another session already watches this target; keep only the first to
    // avoid duplicated network events.
    log({ t: 'target_duplicate', sid: sessionId, activeSid: targetSessions.get(info.targetId), targetId: info.targetId, url: info.url });
    send('Target.detachFromTarget', { sessionId }).catch(() => {});
    return false;
  }
  sessions.set(sessionId, { targetId: info.targetId, type: info.type, url: info.url });
  targetSessions.set(info.targetId, sessionId);
  log({ t: 'target_attached', sid: sessionId, targetId: info.targetId, type: info.type, url: info.url, title: info.title });
  if (info.type === 'page') await enableNetwork(sessionId);
  return true;
}

async function attachToExistingPages() {
  const { targetInfos = [] } = await send('Target.getTargets');
  for (const ti of targetInfos) {
    if (ti.type !== 'page' || targetSessions.has(ti.targetId)) continue;
    try {
      const { sessionId } = await send('Target.attachToTarget', { targetId: ti.targetId, flatten: true });
      await registerSession(sessionId, ti);
    } catch (e) {
      log({ t: 'attach_failed', targetId: ti.targetId, error: String(e) });
    }
  }
}

async function dumpCookies(reason) {
  try {
    const res = await send('Storage.getCookies', {});
    const cookies = (res.cookies || []).map((c) => ({
      name: c.name, value: c.value, domain: c.domain, path: c.path,
      expires: c.expires, httpOnly: c.httpOnly, secure: c.secure, sameSite: c.sameSite,
      session: c.session,
    }));
    const file = path.join(OUT_DIR, 'cookies.json');
    fs.writeFileSync(file, JSON.stringify({ at: new Date().toISOString(), reason, cookies }, null, 2));
    log({ t: 'cookies_dumped', reason, count: cookies.length, file: 'cookies.json' });
  } catch (e) {
    log({ t: 'cookies_error', reason, error: String(e) });
  }
}

ws.addEventListener('open', async () => {
  log({ t: 'ws_open' });
  console.log('CDP connected');
  await dumpCookies('startup');
  try {
    await send('Target.setAutoAttach', {
      autoAttach: true, waitForDebuggerOnStart: false, flatten: true,
      filter: [{ type: 'page', exclude: false }],
    });
  } catch {
    await send('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true }).catch((e) =>
      log({ t: 'autoattach_error', error: String(e) }));
  }
  await attachToExistingPages();
  log({ t: 'ready' });
  console.log('capture ready; events ->', eventsPath);
});

ws.addEventListener('close', () => {
  log({ t: 'ws_close' });
  out.end();
  process.exit(0);
});
ws.addEventListener('error', (e) => {
  log({ t: 'ws_error', error: String(e?.message || e) });
});

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, async () => {
    try { await dumpCookies(sig); } catch {}
    log({ t: 'session_end', signal: sig });
    out.end();
    process.exit(0);
  });
}
