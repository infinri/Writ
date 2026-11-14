// Minimal Chrome DevTools Protocol client over Node 22's built-in WebSocket (no packages).
// One browser-level connection; page sessions use flattened `sessionId` routing.

export class Cdp {
  static async connect(url, timeoutMs = 10000) {
    const ws = new WebSocket(url);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("CDP connect timeout")), timeoutMs);
      ws.addEventListener("open", () => { clearTimeout(timer); resolve(); }, { once: true });
      ws.addEventListener("error", (e) => { clearTimeout(timer); reject(new Error(`CDP connect: ${e.message || "error"}`)); }, { once: true });
    });
    return new Cdp(ws);
  }

  constructor(ws) {
    this.ws = ws;
    this.next = 1;
    this.pending = new Map();
    this.listeners = new Set();
    ws.addEventListener("message", (event) => {
      const msg = JSON.parse(typeof event.data === "string" ? event.data : Buffer.from(event.data).toString());
      if (msg.id !== undefined) {
        const p = this.pending.get(msg.id);
        if (!p) return;
        this.pending.delete(msg.id);
        if (msg.error) p.reject(new Error(`${p.method}: ${msg.error.message}`));
        else p.resolve(msg.result);
        return;
      }
      for (const l of [...this.listeners]) l(msg);
    });
    ws.addEventListener("close", () => {
      for (const p of this.pending.values()) p.reject(new Error("CDP connection closed"));
      this.pending.clear();
    });
  }

  send(method, params = {}, sessionId = undefined, timeoutMs = 30000) {
    const id = this.next++;
    const msg = { id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${method}: timeout`));
      }, timeoutMs);
      this.pending.set(id, {
        method,
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
      this.ws.send(JSON.stringify(msg));
    });
  }

  /** Subscribe to events; returns an unsubscribe function. */
  on(handler) {
    this.listeners.add(handler);
    return () => this.listeners.delete(handler);
  }

  /** The next event `method` (on `sessionId`) matching `pred`. */
  waitFor(method, sessionId, pred = () => true, timeoutMs = 15000) {
    return new Promise((resolve, reject) => {
      const off = this.on((msg) => {
        if (msg.method === method && msg.sessionId === sessionId && pred(msg.params)) {
          clearTimeout(timer);
          off();
          resolve(msg.params);
        }
      });
      const timer = setTimeout(() => { off(); reject(new Error(`waiting for ${method}: timeout`)); }, timeoutMs);
    });
  }

  close() {
    try { this.ws.close(); } catch { /* already closed */ }
  }
}

/** A page target with a CDP WebAuthn virtual authenticator (simulated user verification). */
export class Page {
  static async open(cdp) {
    const { targetId } = await cdp.send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await cdp.send("Target.attachToTarget", { targetId, flatten: true });
    const page = new Page(cdp, targetId, sessionId);
    await page.send("Page.enable");
    await page.send("Runtime.enable");
    return page;
  }

  constructor(cdp, targetId, sessionId) {
    this.cdp = cdp;
    this.targetId = targetId;
    this.sessionId = sessionId;
  }

  send(method, params = {}, timeoutMs) {
    return this.cdp.send(method, params, this.sessionId, timeoutMs);
  }

  /**
   * Enable the WebAuthn domain and add a CTAP2 platform-like virtual authenticator that
   * reports user verification (CDP virtual authenticator, simulated UV; no real user).
   */
  async addVirtualAuthenticator() {
    await this.send("WebAuthn.enable", { enableUI: false });
    const { authenticatorId } = await this.send("WebAuthn.addVirtualAuthenticator", {
      options: {
        protocol: "ctap2",
        ctap2Version: "ctap2_1",
        transport: "internal",
        hasResidentKey: true,
        hasUserVerification: true,
        isUserVerified: true,
        automaticPresenceSimulation: true,
      },
    });
    this.authenticatorId = authenticatorId;
    return authenticatorId;
  }

  /** Simulated user verification on or off for this page's virtual authenticator. */
  setUserVerified(isUserVerified) {
    return this.send("WebAuthn.setUserVerified", { authenticatorId: this.authenticatorId, isUserVerified });
  }

  async credentials() {
    const { credentials } = await this.send("WebAuthn.getCredentials", { authenticatorId: this.authenticatorId });
    return credentials;
  }

  async navigate(url) {
    const loaded = this.cdp.waitFor("Page.loadEventFired", this.sessionId);
    const { errorText } = await this.send("Page.navigate", { url });
    if (errorText) throw new Error(`navigate ${url}: ${errorText}`);
    await loaded;
  }

  /** Evaluate `expression` in the page (awaiting a promise); returns the value. */
  async evaluate(expression, timeoutMs = 30000) {
    const r = await this.send("Runtime.evaluate", {
      expression, awaitPromise: true, returnByValue: true, userGesture: true,
    }, timeoutMs);
    if (r.exceptionDetails) {
      const d = r.exceptionDetails;
      throw new Error(`page exception: ${(d.exception && d.exception.description) || d.text}`);
    }
    return r.result.value;
  }

  /** Call `fn` (a function source) in the page with JSON-serializable `args`. */
  call(fn, ...args) {
    return this.evaluate(`(${fn})(...${JSON.stringify(args)})`);
  }

  async close() {
    try { await this.cdp.send("Target.closeTarget", { targetId: this.targetId }); } catch { /* gone */ }
  }
}
