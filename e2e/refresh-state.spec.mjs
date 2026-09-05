import { expect, test } from "@playwright/test";
import { prepare, tab } from "./support/polling.mjs";
import { json, mockRoster, roster, signIn, device } from "./support/fixtures.mjs";

for (const viewport of [{ width: 1440, height: 900 }, { width: 768, height: 1024 }, { width: 390, height: 844 }]) {
  test(`H05 passive failure is durable, independent and recovers at ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport); await prepare(page);
    const state = page.locator("#devices-refresh-state");
    await expect(state).toHaveAttribute("data-state", "current");
    const success = await state.locator("time").getAttribute("datetime");
    await page.evaluate(() => { window.pollFailure = "devices"; });
    await page.clock.runFor(15000);
    await expect(state).toHaveAttribute("data-state", "stale");
    await expect(state).toContainText("Last-known data");
    await expect(page.locator("#dashboards-refresh-state")).toHaveAttribute("data-state", "current");
    await expect(page.locator("#devices-list")).toContainText("Fictional sample 1");
    await page.clock.runFor(30000);
    await expect(state).toHaveCount(1);
    expect(await state.locator("time").getAttribute("datetime")).toBe(success);
    await expect(page.locator("#toasts")).toBeEmpty();
    await page.evaluate(() => { window.pollFailure = null; });
    await page.clock.runFor(15000);
    await expect(state).toHaveAttribute("data-state", "current");
    expect(await state.locator("time").getAttribute("datetime")).not.toBe(success);
    await expect(page.locator("#devices-list")).toContainText("Fictional sample 5");
    await page.locator("#devices-list").getByRole("button", { name: "Details", exact: true }).click();
    const detail = page.locator("#detail-refresh-state");
    await expect(detail).toHaveAttribute("data-state", "current");
    await page.evaluate(() => { window.pollFailure = "detail"; });
    await page.clock.runFor(20000); await expect(detail).toHaveAttribute("data-state", "stale");
    await page.evaluate(() => { window.pollFailure = null; });
    await page.clock.runFor(20000); await expect(detail).toHaveAttribute("data-state", "current");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test("H05 boot service failure and offline hints are distinct from credentials, retry recovers", async ({ page, context }) => {
  await page.route("**/api/session", route => json(route, { error: "secret=do-not-display" }, 503));
  await page.goto("/");
  await expect(page.locator("#auth-form")).toBeHidden();
  await expect(page.locator("#boot-refresh-state")).toContainText("Service unavailable");
  await expect(page.locator("body")).not.toContainText("do-not-display");
  await context.setOffline(true);
  await page.locator("#boot-refresh-state").getByRole("button", { name: "Retry" }).click();
  await expect(page.locator("#boot-refresh-state")).toContainText("Browser reports offline");
  await context.setOffline(false); await page.unroute("**/api/session");
  await page.locator("#boot-refresh-state").getByRole("button", { name: "Retry" }).click();
  await expect(page.locator("#auth-form")).toBeVisible();
});

test("H05 initial clients failure and explicit scan failure restore controls and retry", async ({ page }) => {
  const errors = []; page.on("pageerror", e => errors.push(e.message));
  await signIn(page);
  await page.route("**/api/clients", route => json(route, { error: "password=never-show" }, 503));
  await tab(page, "Access");
  await expect(page.locator("#clients-refresh-state")).toHaveAttribute("data-state", "error");
  await expect(page.locator("#clients-refresh-state")).not.toContainText("Last-known data");
  await mockRoster(page);
  await page.locator("#clients-refresh-state").getByRole("button", { name: "Retry" }).click();
  await expect(page.locator("#clients-body")).toContainText("Laptop Alice");
  await page.route("**/api/clients/refresh", route => json(route, { error: "password=never-show" }, 503));
  await page.locator("#clients-refresh").click();
  await expect(page.locator("#clients-refresh")).toBeEnabled();
  await expect(page.locator("#clients-scan-state")).toHaveAttribute("data-state", "error");
  await expect(page.locator("#clients-body")).toContainText("Laptop Alice");
  await expect(page.locator("body")).not.toContainText("never-show");
  await page.route("**/api/clients/refresh", route => json(route, roster));
  await page.locator("#clients-scan-state").getByRole("button", { name: "Retry" }).click();
  await expect(page.locator("#clients-scan-state")).toHaveAttribute("data-state", "current");
  expect(errors).toEqual([]);
});

test("H05 cancellation stays quiet, timeout and current 401 have distinct feedback", async ({ page }) => {
  await prepare(page);
  await page.evaluate(async () => {
    const { api } = await import("/js/api.js");
    const ctrl = new AbortController(); ctrl.abort();
    window.cancelError = await api("/api/never", { signal: ctrl.signal }).catch(e => e.name);
  });
  expect(await page.evaluate(() => window.cancelError)).toBe("AbortError");
  await expect(page.locator("#toasts")).toBeEmpty();
  await page.evaluate(async () => {
    const { api } = await import("/js/api.js");
    const original = window.fetch;
    window.fetch = (path, opts) => path === "/api/timeout" ? new Promise((resolve, reject) => {
      opts.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    }) : original(path, opts);
    window.timeoutResult = api("/api/timeout", { timeoutMs: 1000 }).catch(e => { window.timeoutError = e.name; });
  });
  await page.clock.runFor(1000);
  await page.evaluate(() => window.timeoutResult);
  expect(await page.evaluate(() => window.timeoutError)).toBe("TimeoutError");
  await page.route("**/api/expired", route => json(route, {}, 401));
  await page.evaluate(async () => { await (await import("/js/api.js")).api("/api/expired").catch(() => {}); });
  await expect(page.locator("#auth-sub")).toContainText("Session expired");
  await expect(page.locator("#devices-list")).toBeEmpty();
});

test("H05 Settings and notification regions fail independently and recover", async ({ page }) => {
  await prepare(page);
  await page.route("**/api/settings/ansible", route => json(route, { error: "private-token" }, 503));
  await tab(page, "Settings");
  await expect(page.locator("#ansible-settings-refresh-state")).toHaveAttribute("data-state", "error");
  await expect(page.locator("#morning-settings-refresh-state")).toHaveAttribute("data-state", "current");
  await page.route("**/api/settings/ansible", route => json(route, { controller: null }));
  await page.locator("#ansible-settings-refresh-state").getByRole("button", { name: "Retry" }).click();
  await expect(page.locator("#ansible-settings-refresh-state")).toHaveAttribute("data-state", "current");
  await page.route("**/api/notifications?limit=50", route => json(route, { error: "private-token" }, 503));
  await page.locator("#notification-toggle").click();
  await expect(page.locator("#notifications-refresh-state")).toHaveAttribute("data-state", "stale");
  await expect(page.locator("#notification-toggle")).toHaveAccessibleName(/refresh unavailable/);
  await page.route("**/api/notifications?limit=50", route => json(route, { notifications: [], unreadCount: 0 }));
  await page.locator("#notifications-refresh-state").getByRole("button", { name: "Retry" }).click();
  await expect(page.locator("#notifications-refresh-state")).toHaveAttribute("data-state", "current");
  await expect(page.locator("body")).not.toContainText("private-token");
});

test("H05 initial detail failure automatically retries without overlapping ownership", async ({ page }) => {
  await prepare(page);
  await page.evaluate(() => { window.pollFailure = "detail"; });
  await page.locator("#devices-list").getByRole("button", { name: "Details", exact: true }).click();
  await expect(page.locator("#detail-refresh-state")).toHaveAttribute("data-state", "error");
  await page.evaluate(() => { window.pollFailure = null; });
  await page.clock.runFor(20000);
  await expect(page.locator("#detail-refresh-state")).toHaveAttribute("data-state", "current");
  await expect(page.locator("#dm-body")).toContainText("Fictional detail 2");
});
