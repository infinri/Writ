// Process and loopback plumbing of the browser suite: disposable Chrome, the fixture, the
// real `writ-approval-server` binary, loopback HTTP and the WebAuthn calls run in the page.
// Everything binds or connects to 127.0.0.1 only.

import { spawn } from "node:child_process";
import http from "node:http";
import net from "node:net";
import readline from "node:readline";

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** A free loopback port (bound and released). */
export function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.on("error", reject);
    s.listen(0, "127.0.0.1", () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}

/** Wait for `child` to exit (or kill it after `ms`). */
export function waitExit(child, ms = 10000) {
  if (child.exitCode !== null || child.signalCode !== null) return Promise.resolve(child.exitCode);
  return new Promise((resolve) => {
    const timer = setTimeout(() => { child.kill("SIGKILL"); }, ms);
    child.once("exit", (code) => { clearTimeout(timer); resolve(code); });
  });
}

export async function stop(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  child.kill("SIGTERM");
  await waitExit(child);
}

/**
 * Headless Chrome with a disposable profile (`userDataDir`, inside the suite's temporary
 * directory). Returns the child and the browser DevTools WebSocket URL.
 */
export function launchChrome(chromePath, userDataDir, tmpDir) {
  const child = spawn(chromePath, [
    "--headless=new",
    "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=0",
    `--user-data-dir=${userDataDir}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--password-store=basic",
    "--use-mock-keychain",
    "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"], env: { ...process.env, TMPDIR: tmpDir } });
  return new Promise((resolve, reject) => {
    let seen = "";
    const timer = setTimeout(() => reject(new Error(`chrome did not start: ${seen.slice(-500)}`)), 20000);
    child.on("exit", (code) => { clearTimeout(timer); reject(new Error(`chrome exited ${code}: ${seen.slice(-500)}`)); });
    child.stderr.on("data", (d) => {
      seen += d.toString();
      const m = seen.match(/DevTools listening on (ws:\/\/\S+)/);
      if (m) { clearTimeout(timer); child.removeAllListeners("exit"); resolve({ child, wsUrl: m[1] }); }
    });
  });
}

/** A static loopback page server (the setup page during enrollment, the wrong-origin page). */
export function staticServer(port, title) {
  const server = http.createServer((req, res) => {
    res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
    res.end(`<!doctype html><html><head><meta charset="utf-8"><title>${title}</title></head><body>${title}</body></html>`);
  });
  return new Promise((resolve, reject) => {
    server.on("error", reject);
    server.listen(port, "127.0.0.1", () => resolve(server));
  });
}

/**
 * Close a static server deterministically: its open (keep-alive) connections are destroyed
 * first, so `close` never waits on a browser that keeps one open; bounded by a timeout.
 */
export const closeServer = (server) => new Promise((r) => {
  if (!server) return r();
  const timer = setTimeout(r, 5000);
  server.close(() => { clearTimeout(timer); r(); });
  server.closeAllConnections();
});

/** Line-oriented child process (JSON lines on stdout). */
export function spawnLines(cmd, args, onStderr) {
  const child = spawn(cmd, args, { stdio: ["pipe", "pipe", "pipe"] });
  const lines = [];
  const waiters = [];
  readline.createInterface({ input: child.stdout }).on("line", (line) => {
    const w = waiters.shift();
    if (w) w.resolve(line); else lines.push(line);
  });
  child.stderr.on("data", (d) => onStderr && onStderr(d.toString()));
  child.on("exit", (code) => {
    for (const w of waiters.splice(0)) w.reject(new Error(`${cmd} exited ${code}`));
  });
  child.nextLine = (ms = 60000) => {
    if (lines.length) return Promise.resolve(lines.shift());
    if (child.exitCode !== null) return Promise.reject(new Error(`${cmd} exited ${child.exitCode}`));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`${cmd}: no output`)), ms);
      waiters.push({
        resolve: (l) => { clearTimeout(timer); resolve(l); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
    });
  };
  return child;
}

// Run in the page: WebAuthn create/get with server-provided options; the credential is
// serialized as webauthn-rs expects (base64url fields).
export const PAGE_CREATE = `async (optionsJson) => {
  const un = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
  const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\\+/g, "-").replace(/\\//g, "_").replace(/=+$/, "");
  const pk = JSON.parse(optionsJson).publicKey;
  pk.challenge = un(pk.challenge);
  pk.user.id = un(pk.user.id);
  (pk.excludeCredentials || []).forEach((c) => { c.id = un(c.id); });
  const cred = await navigator.credentials.create({ publicKey: pk });
  return JSON.stringify({ id: cred.id, rawId: b64(cred.rawId), type: cred.type, extensions: {},
    response: { attestationObject: b64(cred.response.attestationObject), clientDataJSON: b64(cred.response.clientDataJSON) } });
}`;

export const PAGE_GET = `async (optionsJson) => {
  const un = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
  const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\\+/g, "-").replace(/\\//g, "_").replace(/=+$/, "");
  const pk = JSON.parse(optionsJson).publicKey;
  pk.challenge = un(pk.challenge);
  (pk.allowCredentials || []).forEach((c) => { c.id = un(c.id); });
  const cred = await navigator.credentials.get({ publicKey: pk });
  const r = cred.response;
  return JSON.stringify({ id: cred.id, rawId: b64(cred.rawId), type: cred.type, extensions: {},
    response: { authenticatorData: b64(r.authenticatorData), clientDataJSON: b64(r.clientDataJSON),
      signature: b64(r.signature), userHandle: r.userHandle ? b64(r.userHandle) : null } });
}`;

/**
 * Drive the fixture: answer each `create`/`get` step in `page` (which must be on the
 * server's origin) until `done`. Returns the `done` message.
 */
export async function driveFixture(fixture, page, log) {
  for (;;) {
    const msg = JSON.parse(await fixture.nextLine());
    if (msg.step === "done") return msg;
    const fn = msg.step === "create" ? PAGE_CREATE : msg.step === "get" ? PAGE_GET : null;
    if (!fn) throw new Error(`fixture step ${msg.step}`);
    log(`fixture: ${msg.step} for ${msg.subject} through the CDP virtual authenticator (simulated UV)`);
    const credential = await page.call(fn, JSON.stringify(msg.options));
    fixture.stdin.write(JSON.stringify({ credential }) + "\n");
  }
}

/** Raw loopback HTTP to 127.0.0.1:`port` with explicit headers (Host included). */
export function request(port, method, path, headers = {}, body = undefined) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: "127.0.0.1", port, method, path, headers, timeout: 15000 }, (res) => {
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString() }));
    });
    req.on("timeout", () => req.destroy(new Error("timeout")));
    req.on("error", reject);
    if (body !== undefined) req.write(body);
    req.end();
  });
}

export const form = (fields) => new URLSearchParams(fields).toString();
