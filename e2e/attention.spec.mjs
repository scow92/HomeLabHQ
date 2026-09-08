import { expect, test } from "@playwright/test";
import { device, json, signIn } from "./support/fixtures.mjs";

test.use({ serviceWorkers: "block" });
const now = 1800000000;
async function prepare(page) {
  await page.clock.install({ time: now * 1000 });
  const devices = [
    { ...device, id: "fresh", name: "Fictional current", state: { online: true, ts: now } },
    { ...device, id: "old", name: "Fictional old", state: { online: true, ts: now - 1000 } },
    { ...device, id: "new", name: "Fictional unpolled", state: null },
    { ...device, id: "down", name: "Fictional offline", state: { online: false, ts: now } },
  ].map(d => ({ ...d, monitoringStaleAfterSeconds: 90 }));
  const instances = [
    { id: "one", name: "Fictional database", node: "alpha", ipAddresses: ["192.0.2.70"], discoveryState: "current" },
    { id: "two", name: "Fictional web", node: "beta", ipAddresses: ["192.0.2.71"], discoveryState: "stale" },
  ].map(i => ({ ...i, type: "vm", status: "running", parentDeviceId: "fresh", parentDevice: devices[0], ansible: { enabled: false } }));
  const state = { devices, instances, fail: false, calls: [] };
  await page.route("**/api/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (["/api/session", "/api/login", "/api/logout", "/api/setup"].includes(path)) return route.continue();
    state.calls.push([path, route.request().method()]);
    if (path === "/api/devices") return json(route, state.fail ? { error: "fixture" } : { devices }, state.fail ? 503 : 200);
    if (/\/api\/devices\/[^/]+\/detail$/.test(path)) return json(route, { device: devices.find(d => path.includes(d.id)), entities: [], detail: {}, history: {} });
    if (path === "/api/compute") return json(route, { instances, hosts: [], ansibleEnabled: false });
    if (["/api/compute/one", "/api/compute/two"].includes(path)) return json(route, { instance: instances.find(i => path.endsWith(i.id)) });
    if (path === "/api/clients") return json(route, { clients: [], sources: [{ device: "Fictional old", error: "private diagnostic omitted" }], nac: { configured: false } });
    return json(route, { drivers: [], dashboards: [], notifications: [], events: [], controller: null, jobs: [] });
  });
  await signIn(page);
  return state;
}

for (const [width, height] of [[1440, 900], [768, 1024], [390, 844], [320, 740]]) {
  test(`M05 status sets and source destinations at ${width}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height }); const state = await prepare(page);
    const summary = page.locator("#devices-summary");
    if (width === 320) await page.evaluate(() => document.documentElement.dataset.theme = "dark");
    await expect(summary.getByRole("button", { name: "1 unknown", exact: true })).toBeVisible();
    await expect(summary.getByRole("button", { name: "1 stale", exact: true })).toBeVisible();
    for (const [status, id] of [["unknown", "new"], ["stale", "old"], ["online", "fresh"], ["offline", "down"]]) {
      const button = summary.getByRole("button", { name: `1 ${status}`, exact: true });
      await button.focus(); await page.keyboard.press("Enter");
      await expect(page.locator("#devices-list > .card")).toHaveCount(1);
      await expect(page.locator("#devices-list > .card")).toHaveAttribute("data-device-id", id);
    }
    await page.goBack(); await expect(page.locator("#dev-status")).toHaveValue("online");
    await page.goForward(); await expect(page.locator("#dev-status")).toHaveValue("offline");
    await page.getByRole("tab", { name: "Access", exact: true }).click();
    const source = page.locator("#clients-summary").getByRole("link", { name: "Fictional old", exact: true });
    await expect(source).toBeVisible();
    await expect(page.locator("#clients-summary")).not.toContainText("private diagnostic");
    await source.click();
    await expect(page.locator("#devices-list > .card")).toHaveCount(1);
    await expect(page.locator("#devices-list")).toContainText("Fictional old");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (width === 390) await page.screenshot({ path: info.outputPath("m05-phone.png") });
    expect(state.calls.some(([path, method]) => method !== "GET" || path.includes("/api/v1/status"))).toBe(false);
  });
}

test("M05 Compute search retains host context, route history and M06 destination", async ({ page }) => {
  await prepare(page);
  await page.getByRole("tab", { name: "Compute", exact: true }).click();
  const search = page.getByRole("searchbox", { name: "Search workloads" });
  for (const term of ["database", "192.0.2.70", "alpha", "Fictional current"]) {
    await search.fill(term);
    await expect(page.locator(".compute-host").first()).toContainText("alpha");
    await expect(page.locator(".compute-card").first()).toContainText("Fictional database");
  }
  await search.fill("192.0.2.71");
  await expect(page.locator(".compute-card")).toHaveCount(1);
  await expect(page.locator(".compute-host")).toContainText("beta");
  await page.reload(); await expect(search).toHaveValue("192.0.2.71");
  await page.getByRole("button", { name: "Clear workload search" }).click();
  await expect(page.locator(".compute-card")).toHaveCount(2);
  await page.getByRole("button", { name: "Stale 1", exact: true }).click();
  await expect(page.locator(".compute-card")).toHaveCount(1);
  await expect(page.locator(".compute-card")).toContainText("Fictional web");
  await page.locator("#compute-ansible-setup").getByRole("link").click();
  await expect(page).toHaveURL(/settings\?section=ansible$/);
  await expect(page.locator("#ansible-settings-card")).toBeInViewport();
  await page.goBack();
  await expect(page.locator("[data-compute-filter=stale]")).toHaveClass(/active/);
});

test("M05 observation age advances through retained failure and recovers without extra polling", async ({ page }) => {
  const state = await prepare(page);
  const initialReads = state.calls.filter(([path]) => path === "/api/devices").length;
  const total = page.locator("#devices-summary").getByRole("button", { name: "4 devices", exact: true });
  await total.focus();
  state.fail = true;
  for (let tick = 1; tick <= 7; tick++) {
    await page.clock.runFor(15000);
    await expect.poll(() => state.calls.filter(([path]) => path === "/api/devices").length).toBe(initialReads + tick);
    await expect(page.locator("#devices-refresh-state")).toHaveAttribute("data-state", "stale");
    await expect(page.locator("#dashboards-refresh-state")).toHaveAttribute("data-state", "current");
  }
  await expect(page.locator("#devices-refresh-state")).toHaveAttribute("data-state", "stale");
  await expect(page.locator("#devices-summary").getByRole("button", { name: "2 stale", exact: true })).toBeVisible();
  state.fail = false; state.devices[0].state.ts = now + 105;
  await page.clock.runFor(15000);
  await expect(page.locator("#devices-summary").getByRole("button", { name: "1 online", exact: true })).toBeVisible();
  await expect(page.locator("#devices-refresh-state")).toHaveAttribute("data-state", "current");
  expect(state.calls.filter(([path]) => path === "/api/devices")).toHaveLength(initialReads + 8);
  await expect(total).toBeFocused();
  await page.evaluate(async () => { const api = await import("/js/api.js"); api.setSession(null); });
  await expect(page.locator("#devices-summary")).toBeEmpty();
  await expect(page.locator("#clients-summary")).toBeEmpty();
});

test("M05 observation policy distinguishes missing age, boundary, debounce and clock skew", async ({ page }) => {
  await prepare(page);
  const results = await page.evaluate(async now => {
    const { deviceObservation, observedAt } = await import("/js/observation-status.js");
    const classify = (state, policy = 90) => deviceObservation({ state, monitoringStaleAfterSeconds: policy }, now);
    return [classify(null), classify({ online: true }), classify({ online: true, ts: now - 90 }),
      classify({ online: true, ts: now - 91 }), classify({ online: true, ts: now + 30 }),
      classify({ online: false, confirmedOnline: true, ts: now }), classify({ online: false, ts: now }),
      classify({ online: true, ts: now }, null), observedAt({ ts: true }),
      observedAt({ reachabilityCheckedAt: "invalid", sourceCheckedAt: "2027-01-15T08:00:00Z", ts: 1 })];
  }, now);
  expect(results).toEqual(["unknown", "unknown", "online", "stale", "online", "degraded", "offline", "unknown", null, now]);
});

test("M05 detail destinations keep resource history and keyboard focus", async ({ page }) => {
  const state = await prepare(page);
  state.devices[0].driverId = "opnsense.firewall";
  await page.locator('[data-device-id="fresh"]').getByRole("button", { name: "Details", exact: true }).click();
  const route = page.url();
  await page.locator("#dm-body").getByRole("link", { name: "VPN", exact: true }).click();
  await expect(page.locator("#dm-body").getByRole("heading", { name: "VPN Endpoint", exact: true })).toBeFocused();
  const alerts = page.locator("#dm-body").getByRole("link", { name: "Alerts", exact: true });
  await alerts.focus(); await page.keyboard.press("Enter");
  await expect(page).toHaveURL(route);
  await expect(page.locator("#dm-body").getByRole("heading", { name: "Alerts", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.locator("#devices-list")).toBeVisible();
  await page.getByRole("tab", { name: "Compute", exact: true }).click();
  await page.getByRole("button", { name: "View Fictional database details" }).click();
  const history = page.locator("#cm-body").getByRole("link", { name: "Maintenance history", exact: true });
  await history.focus(); await page.keyboard.press("Enter");
  await expect(page.locator(".compute-history")).toHaveAttribute("open", "");
  await expect(page.locator(".compute-history > summary")).toBeFocused();
});
