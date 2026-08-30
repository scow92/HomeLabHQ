import { expect } from "@playwright/test";

export const credentials = { username: "browser-admin", password: "correct-horse-battery-staple" };
export const roster = {
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

export const device = {
  id: "router-1", name: "Edge gateway", host: "192.0.2.1", transport: "http",
  driverId: "generic.http", state: { online: true }, order: 0,
};

export function json(route, data, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
}

export async function signIn(page) {
  await page.goto("/");
  const mode = await page.locator("#auth-form").getAttribute("data-mode");
  await page.locator("#auth-user").fill(credentials.username);
  await page.locator("#auth-pass").fill(credentials.password);
  if (mode === "setup") await page.locator("#auth-confirm").fill(credentials.password);
  await page.locator("#auth-submit").click();
  await expect(page.locator("#app")).toBeVisible();
}

export async function mockRoster(page, data = roster) {
  await page.route("**/api/clients", (route) => json(route, data));
  await page.route("**/api/clients/history**", (route) => json(route, { events: [] }));
  await page.route("**/api/clients/forget", (route) => json(route, { ok: true }));
  await page.route("**/api/nac/client/membership", (route) => json(route, { configured: false }));
}
