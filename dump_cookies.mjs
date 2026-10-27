// Dump the current Adobe cookies from Chrome's DevTools Protocol.
//
// Chrome must have been started with --remote-debugging-port=<port>:
//   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
//     --remote-debugging-port=9222 \
//     --user-data-dir="$PWD/.chrome-profile" https://lightroom.adobe.com/
//
// Usage:
//   node dump_cookies.mjs [port] [output-file]
//
// The output contains live session credentials. Keep it local; never commit it.
import fs from 'node:fs';
import path from 'node:path';

const port = process.argv[2] || '9222';
const outPath = process.argv[3] || 'captures/cookies-adobe.json';
const isAdobe = (domain) =>
  /(^|\.)adobe\.com$|(^|\.)adobe\.io$|(^|\.)adobelogin\.com$|(^|\.)adobe\.net$/.test(
    domain || '',
  );

const version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
const ws = new WebSocket(version.webSocketDebuggerUrl);
let nextId = 0;
const pending = new Map();

function send(method, params = {}) {
  const id = ++nextId;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    setTimeout(() => {
      if (pending.delete(id)) reject(new Error(`timeout: ${method}`));
    }, 15000);
  });
}

ws.addEventListener('message', (event) => {
  const msg = JSON.parse(
    typeof event.data === 'string' ? event.data : event.data.toString(),
  );
  if (msg.id && pending.has(msg.id)) {
    const p = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? p.reject(new Error(JSON.stringify(msg.error))) : p.resolve(msg.result);
  }
});

await new Promise((resolve) => ws.addEventListener('open', resolve));
const { cookies = [] } = await send('Storage.getCookies', {});
const filtered = cookies
  .filter((c) => isAdobe(c.domain))
  .map((c) => ({
    name: c.name,
    value: c.value,
    domain: c.domain,
    path: c.path,
    expires: c.expires,
    httpOnly: c.httpOnly,
    secure: c.secure,
    sameSite: c.sameSite,
    session: c.session,
  }));

fs.mkdirSync(path.dirname(outPath), { recursive: true });
fs.writeFileSync(
  outPath,
  JSON.stringify({ at: new Date().toISOString(), cookies: filtered }, null, 2),
);
console.log(`saved ${filtered.length} Adobe cookies -> ${outPath}`);
process.exit(0);
