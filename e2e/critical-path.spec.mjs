import { expect, test } from "@playwright/test";

const credentials = { username: "browser-admin", password: "correct-horse-battery-staple" };
const roster = {
  clients: [
    {
      mac: "00:11:22:33:44:55", hostname: "Laptop Alice", ip: "192.0.2.10",
      kind: "wifi", online: true, nac: "approved", seen: [{ via: "Office AP", kind: "wifi" }],
    },
    {
      mac: "00:11:22:33:44:66", hostname: "Camera Garage", ip: "192.0.2.20",
      kind: "wired", online: false, nac: "blocked", lastSeen: 1_700_000_000, seen: [],
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

async function mockRoster(page) {
  await page.route("**/api/clients", (route) => json(route, roster));
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

test("client filters constrain bulk actions to the visible roster", async ({ page }) => {
  await signIn(page);
  await mockRoster(page);
  await page.getByRole("tab", { name: "Access" }).click();
  await expect(page.getByText("Laptop Alice", { exact: true })).toBeVisible();
  await expect(page.getByText("Camera Garage", { exact: true })).toBeVisible();

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

test("the service worker serves the application shell offline", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await signIn(page);
  await page.evaluate(() => navigator.serviceWorker.ready);
  await page.reload();
  await page.waitForFunction(async () => (await caches.open("hlhq-shell-v1")).keys().then((keys) => keys.length > 0));

  await context.setOffline(true);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator("#auth-screen")).toBeVisible();
  await expect(page.locator("#auth-form")).toBeVisible();
  await context.close();
});
