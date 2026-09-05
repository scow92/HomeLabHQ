import { expect } from "@playwright/test";
import { device, json, signIn } from "./fixtures.mjs";

export async function prepare(page) {
  await page.clock.install({ time: new Date("2026-09-05T12:00:00Z") });
  await page.clock.pauseAt(new Date("2026-09-05T12:00:00Z"));
  await page.route("**/api/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (["/api/session", "/api/login", "/api/logout", "/api/setup"].includes(path)) return route.continue();
    return json(route, { dashboards: [], drivers: [], instances: [], clients: [], events: [], notifications: [], controller: null });
  });
  await page.addInitScript(resource => {
    window.pollReads = [];
    window.pollHold = null;
    window.pollFailure = null;
    const original = window.fetch;
    window.fetch = async (path, opts) => {
      if (path === "/api/dashboards") return { ok: true, status: 200, json: async () => ({ dashboards: [] }) };
      const kind = path === "/api/devices" ? "devices" : path === "/api/logs" ? "logs"
        : path === `/api/devices/${resource.id}/detail` ? "detail"
        : String(path).startsWith("/api/clients/events?") ? "badge" : null;
      if (!kind) return original(path, opts);
      const read = { kind, at: Date.now(), signal: opts.signal, settled: false };
      window.pollReads.push(read);
      const n = window.pollReads.filter(r => r.kind === kind).length;
      const currentDevice = { ...resource, name: `Fictional sample ${n}`, state: { online: true, ts: Date.now() / 1000 } };
      const body = kind === "devices" ? { devices: [currentDevice] }
        : kind === "logs" ? { logs: [{ ts: Date.now() / 1000, message: `Fictional log ${n}` }] }
        : kind === "badge" ? { newCount: n }
        : { device: currentDevice, detail: { info: { Identity: `Fictional detail ${n}` } } };
      const failure = window.pollFailure === kind;
      return { ok: !failure, status: failure ? 503 : 200, json: async () => {
        // Like H02 fixtures, allow decoded bodies to complete after abort.
        if (window.pollHold === kind) await new Promise(resolve => { read.release = resolve; });
        read.settled = true;
        return failure ? { error: "Fictional refresh failure" } : body;
      } };
    };
    window.pollTimeouts = new Set();
    const timeout = window.setTimeout, clearTimeout = window.clearTimeout;
    window.setTimeout = (fn, ms, ...args) => {
      const id = timeout(() => { window.pollTimeouts.delete(id); fn(...args); }, ms);
      if (new Error().stack.includes("/js/api.js")) window.pollTimeouts.add(id);
      return id;
    };
    window.clearTimeout = id => { window.pollTimeouts.delete(id); clearTimeout(id); };
    window.pollTimers = new Map(); window.pollListeners = new Set();
    const interval = window.setInterval, clear = window.clearInterval;
    window.setInterval = (fn, ms, ...args) => {
      const id = interval(fn, ms, ...args);
      if (new Error().stack.includes("/js/ui.js") && ms !== 30000) window.pollTimers.set(id, ms);
      return id;
    };
    window.clearInterval = id => { window.pollTimers.delete(id); clear(id); };
    const add = document.addEventListener.bind(document), remove = document.removeEventListener.bind(document);
    document.addEventListener = (name, fn, opts) => {
      if (name === "visibilitychange" && new Error().stack.includes("/js/ui.js")) window.pollListeners.add(fn);
      add(name, fn, opts);
    };
    document.removeEventListener = (name, fn, opts) => { window.pollListeners.delete(fn); remove(name, fn, opts); };
  }, device);
  await signIn(page);
  await page.evaluate(() => history.replaceState(null, "", "#/devices"));
  await expect(page.locator("#devices-list")).toContainText("Fictional sample 1");
}
export async function reads(page, kind = "devices") {
  return page.evaluate(kind => window.pollReads.filter(r => r.kind === kind).map(r => ({ at: r.at, aborted: r.signal.aborted, settled: r.settled })), kind);
}
export async function tick(page, ms, kind = "devices", expected) {
  await page.clock.runFor(ms);
  if (expected !== undefined) expect((await reads(page, kind)).length).toBe(expected);
}
export async function tab(page, name) { await page.getByRole("tab", { name, exact: true }).click(); }
