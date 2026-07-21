import { expect, test } from "@playwright/test";

const credentials = { username: "browser-admin", password: "correct-horse-battery-staple" };
const roster = {
  clients: [
    {
      mac: "00:11:22:33:44:55", hostname: "Laptop Alice", ip: "192.0.2.10",
      kind: "wifi", signal: -55, online: true, nac: "approved",
      seen: [{ via: "Office AP", kind: "wifi", signal: -55 }],
    },
    {
      mac: "00:11:22:33:44:66", hostname: "Camera Garage", ip: "192.0.2.20",
      // Legacy NAC scans could misclassify AP clients as wired while retaining RSSI.
      kind: "wired", signal: -73, online: false, nac: "blocked", lastSeen: 1_700_000_000,
      seen: [{ via: "Garage AP", kind: "wifi", signal: -73 }],
    },
  ],
  sources: [{ name: "Office AP" }],
  nac: { configured: true, deviceId: "firewall-1", managedAliases: [] },
};

const device = {
  id: "router-1", name: "Edge gateway", host: "192.0.2.1", transport: "http",
  driverId: "generic.http", state: { online: true }, order: 0,
};

function json(route, data, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
}

async function signIn(page) {
  await page.goto("/");
  await expect(page.locator("#auth-form")).toHaveAttribute("data-mode", "login");
  await page.locator("#auth-user").fill(credentials.username);
  await page.locator("#auth-pass").fill(credentials.password);
  await page.locator("#auth-submit").click();
  await expect(page.locator("#app")).toBeVisible();
}

async function mockRoster(page, data = roster) {
  await page.route("**/api/clients", (route) => json(route, data));
  await page.route("**/api/clients/history**", (route) => json(route, { events: [] }));
  await page.route("**/api/clients/forget", (route) => json(route, { ok: true }));
  await page.route("**/api/nac/client/membership", (route) => json(route, { configured: false }));
}

test.describe.configure({ mode: "serial" });

test("initial setup creates an admin and that admin can log in", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#auth-form")).toHaveAttribute("data-mode", "setup");
  await page.locator("#auth-user").fill(credentials.username);
  await page.locator("#auth-pass").fill(credentials.password);
  await page.locator("#auth-confirm").fill(credentials.password);
  await page.locator("#auth-submit").click();
  await expect(page.locator("#whoami")).toContainText("browser-admin");

  await page.locator("#logout-btn").click();
  await expect(page.locator("#auth-form")).toHaveAttribute("data-mode", "login");
  await page.locator("#auth-user").fill(credentials.username);
  await page.locator("#auth-pass").fill(credentials.password);
  await page.locator("#auth-submit").click();
  await expect(page.locator("#whoami")).toContainText("browser-admin");
});

test("a failed device refresh retains the last-known device state", async ({ page }) => {
  let deviceReads = 0;
  await page.route("**/api/dashboards", (route) => json(route, { dashboards: [] }));
  await page.route("**/api/devices", (route) => {
    deviceReads += 1;
    return deviceReads === 1
      ? json(route, { devices: [device] })
      : json(route, { error: "device refresh unavailable" }, 503);
  });

  await signIn(page);
  await expect(page.getByText("Edge gateway", { exact: true })).toBeVisible();

  await page.getByRole("tab", { name: "Access" }).click();
  await page.getByRole("tab", { name: "Devices" }).click();
  await expect(page.locator("#toasts")).toContainText("Couldn't refresh devices: device refresh unavailable");
  await expect(page.getByText("Edge gateway", { exact: true })).toBeVisible();
});

test("the Access badge counts new devices but not reconnects", async ({ page }) => {
  let eventSummary = { since: 1, count: 4, newCount: 0 };
  await page.route("**/api/clients/events**", (route) => json(route, eventSummary));
  await signIn(page);

  const seenKey = await page.evaluate(() =>
    Object.keys(localStorage).find((key) => key.startsWith("hlhq-access-seen:")));
  expect(seenKey).toBeTruthy();
  await page.evaluate((key) => localStorage.setItem(key, "1"), seenKey);
  await page.reload();
  await expect(page.locator('.tab[data-tab="clients"] .tab-badge')).toHaveCount(0);

  eventSummary = { since: 1, count: 5, newCount: 1 };
  await page.reload();
  const badge = page.locator('.tab[data-tab="clients"] .tab-badge');
  await expect(badge).toHaveText("1");
  await expect(badge).toHaveAttribute("title", "1 new device since you last looked");
});

test("client filters constrain bulk actions to the visible roster", async ({ page }) => {
  await signIn(page);
  await mockRoster(page);
  await page.getByRole("tab", { name: "Access" }).click();
  await expect(page.getByText("Laptop Alice", { exact: true })).toBeVisible();
  await expect(page.getByText("Camera Garage", { exact: true })).toBeVisible();
  const onlineSignal = page.locator(".client-card").filter({ hasText: "Laptop Alice" }).locator(".cc-signal");
  const offlineSignal = page.locator(".client-card").filter({ hasText: "Camera Garage" }).locator(".cc-signal");
  await expect(onlineSignal).toBeVisible();
  await expect(onlineSignal).toContainText("-55 dBm");
  await expect(offlineSignal).toBeHidden();

  await page.locator("#clients-search").fill("camera");
  await expect(page.getByText("Laptop Alice", { exact: true })).toBeHidden();
  await expect(page.getByText("Camera Garage", { exact: true })).toBeVisible();

  await page.locator("#clients-menu").click();
  await page.getByRole("button", { name: "Forget offline shown (1)" }).click();
  const request = page.waitForRequest((candidate) =>
    candidate.url().endsWith("/api/clients/forget") && candidate.method() === "POST");
  await page.locator("#dialog-ok").click();
  expect(JSON.parse((await request).postData() ?? "{}")).toEqual({ macs: ["00:11:22:33:44:66"] });
});

test("the client table hides retained RSSI for offline clients", async ({ page }) => {
  await signIn(page);
  await mockRoster(page, { ...roster, nac: { configured: false } });
  await page.getByRole("tab", { name: "Access" }).click();

  const onlineRow = page.locator(".clients-table tbody tr").filter({ hasText: "Laptop Alice" });
  const offlineRow = page.locator(".clients-table tbody tr").filter({ hasText: "Camera Garage" });
  await expect(onlineRow.locator("td").nth(5)).toHaveText("-55 dBm");
  await expect(offlineRow.locator("td").nth(5)).toHaveText("–");
});

test("Escape closes the client modal and hash navigation follows the selected tab", async ({ page }) => {
  await signIn(page);
  await mockRoster(page);
  await page.getByRole("tab", { name: "Access" }).click();
  await expect(page).toHaveURL(/#\/access$/);
  const edit = page.getByRole("button", { name: /Edit — rename/ }).first();
  await edit.click();
  await expect(page.locator("#client-modal")).toBeVisible();
  await expect(page.locator("#ce-host")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.locator("#client-modal")).toBeHidden();
  await expect(edit).toBeFocused();

  await page.getByRole("tab", { name: "Add device" }).click();
  await expect(page).toHaveURL(/#\/add$/);
  await page.goBack();
  await expect(page).toHaveURL(/#\/access$/);
  await expect(page.getByRole("tab", { name: "Access" })).toHaveAttribute("aria-selected", "true");
});

test("device presets show only their relevant connection fields", async ({ page }) => {
  await signIn(page);
  await page.getByRole("tab", { name: "Add device" }).click();

  const cases = [
    ["opnsense", ["cred-apiKey", "cred-apiSecret", "cred-scheme", "cred-verifyTls"], ""],
    ["pfsense", ["cred-apiKey", "cred-scheme", "cred-verifyTls"], ""],
    ["unifi", ["cred-apiKey", "cred-scheme", "cred-verifyTls"], "443"],
    ["proxmox", ["cred-tokenId", "cred-tokenSecret", "cred-verifyTls"], "8006"],
    ["truenas", ["cred-apiKey", "cred-scheme", "cred-verifyTls"], ""],
    ["firewalla", ["cred-token"], ""],
    ["mikrotik", ["cred-username", "cred-password", "cred-scheme", "cred-verifyTls"], ""],
    ["openwrt", ["cred-username", "cred-password", "cred-scheme", "cred-verifyTls", "cred-metricsPath"], "80"],
    ["synology", ["cred-username", "cred-password", "cred-scheme", "cred-verifyTls"], "5000"],
    ["qnap", ["cred-username", "cred-password", "cred-scheme", "cred-verifyTls"], "8080"],
    ["keeplink", ["cred-username", "cred-password", "cred-scheme", "cred-verifyTls"], "80"],
    ["zyxel", ["cred-username", "cred-password", "cred-verifyTls"], "443"],
  ];

  for (const [preset, fields, port] of cases) {
    await page.locator("#wiz-preset").selectOption(preset);
    expect(await page.locator("#wiz-creds [id]").evaluateAll(
      (elements) => elements.map((element) => element.id))).toEqual(fields);
    await expect(page.locator("#wiz-port")).toHaveValue(port);
  }

  let submitted;
  await page.route("**/api/devices/detect", async (route) => {
    submitted = JSON.parse(route.request().postData() ?? "{}");
    await json(route, { candidates: [{
      driverId: "firewalla.msp", displayName: "Firewalla", confidence: 0.9,
    }] });
  });
  await page.locator("#wiz-preset").selectOption("firewalla");
  await page.locator("#wiz-host").fill("example.firewalla.net");
  await page.locator("#cred-token").fill("secret-token");
  await page.locator("#wiz-detect").click();
  await expect(page.locator("#wiz-candidates").getByText("Firewalla", { exact: true })).toBeVisible();
  expect(submitted).toEqual({
    transport: "api", host: "example.firewalla.net", port: null,
    credentials: {
      apiKey: "Token secret-token", scheme: "https", verifyTls: true,
      authStyle: "header", keyHeader: "Authorization",
    },
  });
});

test("the service worker refreshes the shell online and serves it offline", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await signIn(page);
  await page.evaluate(() => navigator.serviceWorker.ready);
  await page.reload();
  await page.waitForFunction(async () => (await caches.open("hlhq-shell-v1")).keys().then((keys) => keys.length > 0));

  const manifest = await page.evaluate(async () => {
    const cache = await caches.open("hlhq-shell-v1");
    await cache.put("/manifest.webmanifest", new Response("stale shell"));
    const live = await fetch("/manifest.webmanifest").then((response) => response.text());
    const cached = await cache.match("/manifest.webmanifest").then((response) => response.text());
    return { live, cached };
  });
  expect(manifest.live).not.toBe("stale shell");
  expect(manifest.cached).toBe(manifest.live);

  await context.setOffline(true);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator("#auth-screen")).toBeVisible();
  await expect(page.locator("#auth-form")).toBeVisible();
  await context.close();
});
