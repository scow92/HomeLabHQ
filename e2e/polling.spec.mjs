import { expect, test } from "@playwright/test";
import { credentials, json } from "./support/fixtures.mjs";
import { prepare, reads, tick, tab } from "./support/polling.mjs";

for (const history of [false, true]) {
  test(`Devices resumes exactly once after ${history ? "Back and Forward" : "internal navigation"}`, async ({ page }) => {
    await prepare(page);
    await tick(page, 15000, "devices", 2);
    await expect(page.locator("#devices-list")).toContainText("Fictional sample 2");
    await tick(page, 15000, "devices", 3);
    expect((await reads(page)).map(r => r.at - 1788609600000)).toEqual([0, 15000, 30000]);
    for (let i = 0; i < 3; i++) {
      await tab(page, "Compute");
      await tick(page, 30000, "devices", 3 + i * 3);
      if (history) { await page.goBack(); await expect(page).toHaveURL(/#\/devices$/); }
      else await tab(page, "Devices");
      await expect(page.locator("#devices-list")).toContainText(`Fictional sample ${4 + i * 3}`);
      await tick(page, 15000, "devices", 5 + i * 3);
      await tick(page, 15000, "devices", 6 + i * 3);
      if (history) {
        await page.goForward(); await expect(page).toHaveURL(/#\/compute$/);
        await tick(page, 30000, "devices", 6 + i * 3);
        // Next iteration's Compute click keeps the same history destination.
      }
    }
  });
}

test("Devices slow poll does not overlap and obsolete completion cannot render", async ({ page }) => {
  await prepare(page);
  await page.evaluate(() => { window.pollHold = "devices"; });
  await tick(page, 15000, "devices", 2);
  await tick(page, 15000, "devices", 2);
  await tab(page, "Compute");
  expect((await reads(page))[1].aborted).toBe(true);
  const before = await page.locator("#devices-list").innerHTML();
  await page.evaluate(() => { window.pollHold = null; window.pollReads.find(r => r.release).release(); });
  expect(await page.locator("#devices-list").innerHTML()).toBe(before);
  await expect(page.locator("#toasts")).toBeEmpty();
  await tab(page, "Devices");
  await expect(page.locator("#devices-list")).toContainText("Fictional sample 3");
  await tick(page, 15000, "devices", 4);
});

async function visibility(page, state) {
  await page.evaluate(state => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: state });
    document.dispatchEvent(new Event("visibilitychange"));
  }, state);
}
async function timers(page, ms) {
  return page.evaluate(ms => [...window.pollTimers.values()].filter(value => value === ms).length, ms);
}
async function release(page) {
  await page.evaluate(() => {
    window.pollHold = null;
    for (const read of window.pollReads) read.release?.();
  });
  // Drain decoded-body/API/render continuations without a wall-clock sleep.
  await page.evaluate(() => new Promise(resolve => {
    const channel = new MessageChannel();
    channel.port1.onmessage = () => { channel.port1.close(); channel.port2.close(); resolve(); };
    channel.port2.postMessage(null);
  }));
}

test.beforeEach(async ({ page }) => {
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.exposeFunction("assertPollingErrors", () => expect(errors).toEqual([]));
});
test.afterEach(async ({ page }) => {
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pagehide")));
  await release(page);
  expect(await page.evaluate(() => ({ timers: window.pollTimers.size, listeners: window.pollListeners.size,
    pending: window.pollReads.filter(r => !r.settled).length }))).toEqual({ timers: 0, listeners: 0, pending: 0 });
  await expect.poll(() => page.evaluate(() => window.pollTimeouts.size)).toBe(0);
  await page.evaluate(() => window.assertPollingErrors());
});

for (const method of ["Close", "Escape", "Back"]) {
  test(`Device detail ${method} restores the parent polling lifecycle`, async ({ page }) => {
    await prepare(page);
    for (let i = 0; i < 2; i++) {
      await page.locator("#devices-list").getByRole("button", { name: "Details", exact: true }).click();
      await expect(page.locator("#dm-body")).toContainText(`Fictional detail ${i * 3 + 1}`);
      expect(await timers(page, 15000)).toBe(0);
      expect(await timers(page, 20000)).toBe(1);
      await tick(page, 20000, "detail", i * 3 + 2);
      await tick(page, 20000, "detail", i * 3 + 3);
      if (method === "Close") await page.locator("#device-modal").getByRole("button", { name: "Close", exact: true }).click();
      else if (method === "Escape") await page.keyboard.press("Escape");
      else await page.goBack();
      await expect(page.locator("#device-modal")).toBeHidden();
      await expect(page).toHaveURL(/#\/devices$/);
      expect(await timers(page, 20000)).toBe(0);
      expect(await timers(page, 15000)).toBe(1);
      await tick(page, 15000, "devices", i * 3 + 3);
      await tick(page, 15000, "devices", i * 3 + 4);
      await expect(page.locator("#devices-list")).toContainText(`Fictional sample ${i * 3 + 4}`);
    }
  });
}

const pollers = [
  { kind: "devices", ms: 15000, selector: "#devices-list" },
  { kind: "logs", ms: 3000, selector: "#logs-table" },
  { kind: "detail", ms: 20000, selector: "#dm-body" },
  { kind: "badge", ms: 60000, selector: '[data-tab="clients"]' },
];
async function enter(page, kind) {
  if (kind === "logs") await tab(page, "Logs");
  if (kind === "detail") {
    await page.locator("#devices-list").getByRole("button", { name: "Details", exact: true }).click();
    await expect(page.locator("#dm-body")).toContainText("Fictional detail 1");
  }
  if (kind === "badge") {
    await tab(page, "Compute");
    await tick(page, 60000, "badge", 1);
  }
}
for (const { kind, ms, selector } of pollers) {
  test(`${kind}: slow polling, visibility pause and clean restoration`, async ({ page }) => {
    await prepare(page); await enter(page, kind);
    const initial = (await reads(page, kind)).length;
    await page.evaluate(kind => { window.pollHold = kind; }, kind);
    await tick(page, ms, kind, initial + 1);
    await tick(page, ms, kind, initial + 1);
    await visibility(page, "hidden");
    expect((await reads(page, kind)).at(-1).aborted).toBe(true);
    expect(await timers(page, ms)).toBe(0);
    await tick(page, ms * 2, kind, initial + 1);
    const before = await page.locator(selector).innerHTML();
    await release(page);
    expect(await page.locator(selector).innerHTML()).toBe(before);
    await expect(page.locator("#toasts")).toBeEmpty();
    await visibility(page, "visible"); await visibility(page, "visible");
    expect(await timers(page, ms)).toBe(1);
    await tick(page, ms, kind, initial + 2);
    await tick(page, ms, kind, initial + 3);
  });
}

test("Logs stops immediately, rejects late reads and resumes once; auto-refresh can be toggled", async ({ page }) => {
  await prepare(page); await tab(page, "Logs");
  await tick(page, 3000, "logs", 2); await tick(page, 3000, "logs", 3);
  await page.evaluate(() => { window.pollHold = "logs"; });
  await tick(page, 3000, "logs", 4);
  await tab(page, "Compute");
  expect(await timers(page, 3000)).toBe(0);
  expect((await reads(page, "logs")).at(-1).aborted).toBe(true);
  const before = await page.locator("#logs-table").innerHTML();
  await release(page);
  expect(await page.locator("#logs-table").innerHTML()).toBe(before);
  await tick(page, 6000, "logs", 4);
  await tab(page, "Logs");
  await tick(page, 3000, "logs", 6); await tick(page, 3000, "logs", 7);
  await page.locator("#logs-auto").uncheck();
  expect(await timers(page, 3000)).toBe(0);
  await tick(page, 6000, "logs", 8); // checkbox preserves the existing immediate read
  await page.locator("#logs-auto").check();
  expect(await timers(page, 3000)).toBe(1);
  await tick(page, 3000, "logs", 10);
});

for (const mode of ["logout", "expiry", "reauthentication", "page-cache"]) {
  test(`Devices ${mode} disposes old polling and authenticates a clean lifecycle`, async ({ page }) => {
    await prepare(page);
    await page.evaluate(() => { window.pollHold = "devices"; });
    await tick(page, 15000, "devices", 2);
    if (mode === "logout") {
      await page.locator("#logout-btn").click();
      await expect(page.locator("#auth-submit")).toBeEnabled();
    } else if (mode === "expiry") {
      await page.route("**/api/h03-expiry", route => json(route, { error: "Fictional expired session" }, 401));
      await page.evaluate(async () => { try { await (await import("/js/api.js")).api("/api/h03-expiry"); } catch (_) {} });
    } else if (mode === "page-cache") {
      await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: true })));
    } else {
      await page.evaluate(async () => {
        const api = await import("/js/api.js");
        api.setSession({ id: "fictional-second-owner", username: "fictional-second-owner", role: "admin" });
      });
    }
    expect((await reads(page))[1].aborted).toBe(true);
    await release(page);
    await expect(page.locator("#devices-list")).not.toContainText("Fictional sample 2");
    if (mode !== "reauthentication") {
      expect(await timers(page, 15000)).toBe(0);
      await tick(page, 30000, "devices", 2);
      if (mode === "page-cache") await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true })));
      else {
        await page.locator("#auth-user").fill(credentials.username);
        await page.locator("#auth-pass").fill(credentials.password);
        await page.locator("#auth-submit").click();
      }
    }
    await expect(page.locator("#devices-list")).toContainText("Fictional sample 3");
    expect(await timers(page, 15000)).toBe(1);
    await tick(page, 15000, "devices", 4); await tick(page, 15000, "devices", 5);
    await expect(page.locator("#toasts")).toBeEmpty();
  });
}

test("Devices failed poll retains values and retries at the same interval", async ({ page }) => {
  await prepare(page);
  await page.evaluate(() => { window.pollFailure = "devices"; });
  await tick(page, 15000, "devices", 2);
  await expect(page.locator("#devices-list")).toContainText("Fictional sample 1");
  await expect(page.locator("#devices-refresh-state")).toHaveAttribute("data-state", "stale");
  await expect(page.locator("#toasts")).toBeEmpty();
  await page.evaluate(() => { window.pollFailure = null; });
  await tick(page, 15000, "devices", 3);
  await expect(page.locator("#devices-list")).toContainText("Fictional sample 3");
  expect(await timers(page, 15000)).toBe(1);
});

test("Rapid route reactivation and detail history never duplicate timers", async ({ page }) => {
  await prepare(page);
  for (let i = 0; i < 5; i++) {
    await tab(page, "Compute"); await tab(page, "Devices");
    expect(await timers(page, 15000)).toBe(1);
  }
  await tick(page, 15000, "devices", 7); await tick(page, 15000, "devices", 8);
  await page.locator("#devices-list").getByRole("button", { name: "Details", exact: true }).click();
  await expect(page.locator("#dm-body")).toContainText("Fictional detail 1");
  await page.goBack(); await expect(page.locator("#device-modal")).toBeHidden();
  await tick(page, 15000, "devices", 10);
  await page.goForward(); await expect(page.locator("#dm-body")).toContainText("Fictional detail 2");
  expect(await timers(page, 15000)).toBe(0); expect(await timers(page, 20000)).toBe(1);
  await tick(page, 20000, "detail", 3); await tick(page, 20000, "detail", 4);
});

for (const resource of ["router-1", "fictional-missing"]) {
  test(`Direct device route ${resource} restores parent polling`, async ({ page }) => {
    await prepare(page);
    await page.goto(`/#/device/${resource}`);
    if (resource === "router-1") {
      await expect(page.locator("#dm-body")).toContainText("Fictional detail 1");
      expect(await timers(page, 15000)).toBe(0);
      await page.locator("#device-modal").getByRole("button", { name: "Close", exact: true }).click();
    }
    await expect(page).toHaveURL(/#\/devices$/);
    expect(await timers(page, 15000)).toBe(1);
    const initial = (await reads(page)).length;
    await tick(page, 15000, "devices", initial + 1);
    await tick(page, 15000, "devices", initial + 2);
  });
}

test("Access badge stays app-scoped and entering Access invalidates a pending summary", async ({ page }) => {
  await prepare(page); await tab(page, "Compute");
  await tick(page, 60000, "badge", 1);
  await page.evaluate(() => { window.pollHold = "badge"; });
  await tick(page, 60000, "badge", 2);
  await page.locator('.tab[data-tab="clients"]').click();
  await expect(page.locator('[data-panel="clients"]')).toBeVisible();
  await expect.poll(async () => (await reads(page, "badge")).at(-1).aborted).toBe(true);
  await release(page);
  await expect(page.locator('[data-tab="clients"] .tab-badge')).toHaveCount(0);
  await tick(page, 120000, "badge", 2);
  await tab(page, "Compute");
  await tick(page, 60000, "badge", 3);
  await expect(page.locator('[data-tab="clients"] .tab-badge')).toHaveText("3");
  expect(await timers(page, 60000)).toBe(1);
});

test("Logs preserves its three-second delay after a completed poll", async ({ page }) => {
  await prepare(page); await tab(page, "Logs");
  await page.evaluate(() => { window.pollHold = "logs"; });
  await tick(page, 3000, "logs", 2);
  await tick(page, 2000, "logs", 2);
  await release(page); // completes at t=5s; the next read must be at t=8s
  await tick(page, 2999, "logs", 2);
  await tick(page, 1, "logs", 3);
  expect((await reads(page, "logs")).map(r => r.at - 1788609600000)).toEqual([0, 3000, 8000]);
});

test("Logs initial and manual reads share the same non-overlapping lifecycle", async ({ page }) => {
  await prepare(page);
  await page.evaluate(() => { window.pollHold = "logs"; });
  await tab(page, "Logs");
  await tick(page, 5000, "logs", 1);
  await release(page);
  await tick(page, 2999, "logs", 1); await tick(page, 1, "logs", 2);
  await page.evaluate(() => { window.pollHold = "logs"; });
  await page.locator("#logs-refresh").click();
  await tick(page, 5000, "logs", 3);
  await release(page);
  await tick(page, 2999, "logs", 3); await tick(page, 1, "logs", 4);
});
