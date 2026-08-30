import { expect, test } from "@playwright/test";
import {
  credentials, device, json, mockRoster, roster, signIn,
} from "./support/fixtures.mjs";

test.describe.configure({ mode: "serial" });

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

test("notification centre and app badge reconcile to backend unread state", async ({ page }) => {
  let unread = 1;
  let readAt = null;
  const notification = {
    id: "notification-1", title: "Device offline", body: "pve1 became unreachable.",
    category: "host_offline", createdAt: Math.floor(Date.now() / 1000),
    readAt: null, dismissedAt: null, data: { deviceId: "proxmox-1" },
  };
  await page.addInitScript(() => {
    window.__badgeCalls = [];
    Object.defineProperty(navigator, "setAppBadge", { configurable: true,
      value: async (count) => window.__badgeCalls.push(["set", count]) });
    Object.defineProperty(navigator, "clearAppBadge", { configurable: true,
      value: async () => window.__badgeCalls.push(["clear"]) });
  });
  await page.route("**/api/notifications**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path.endsWith("/read")) {
      unread = 0; readAt = Math.floor(Date.now() / 1000);
      return json(route, { notification: { ...notification, readAt }, unreadCount: unread });
    }
    if (request.method() === "POST" && path.endsWith("/read-all")) {
      unread = 0; readAt = Math.floor(Date.now() / 1000);
      return json(route, { unreadCount: unread });
    }
    return json(route, {
      notifications: [{ ...notification, readAt }], unreadCount: unread,
    });
  });

  await signIn(page);
  const badge = page.locator("#notification-badge");
  await expect(badge).toHaveText("1");
  await expect.poll(() => page.evaluate(() => window.__badgeCalls.at(-1))).toEqual(["set", 1]);

  await page.locator("#notification-toggle").click();
  await expect(page.locator("#notification-panel")).toBeVisible();
  await expect(page.locator("#notification-list")).toContainText("pve1 became unreachable.");
  await page.getByRole("button", { name: "Mark Device offline read" }).click();
  await expect(badge).toBeHidden();
  await expect.poll(() => page.evaluate(() => window.__badgeCalls.at(-1))).toEqual(["clear"]);
  await expect(page.locator("#notification-list")).toContainText("Device offline");

  await page.evaluate(() => navigator.setAppBadge(9));
  await page.reload();
  await expect(badge).toBeHidden();
  await expect.poll(() => page.evaluate(() => window.__badgeCalls.at(-1))).toEqual(["clear"]);
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
