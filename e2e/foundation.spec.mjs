import { expect, test } from "@playwright/test";
import { device, json, signIn, mockRoster, roster } from "./support/fixtures.mjs";

const widths = [{ width: 1440, height: 900 }, { width: 768, height: 1024 }, { width: 390, height: 844 }];
async function prepare(page) {
  await page.route("**/api/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (["/api/session", "/api/login", "/api/logout", "/api/setup"].includes(path)) return route.continue();
    return json(route, path === "/api/devices" ? { devices: [device] }
      : path.endsWith("/detail") ? { device, detail: { info: { Identity: "Fictional gateway" } } }
      : { dashboards: [], drivers: [], instances: [], clients: [], users: [], events: [], notifications: [], controller: null });
  });
  await signIn(page);
}

for (const viewport of widths) test(`M02 stacked dialog ownership at ${viewport.width}`, async ({ page }) => {
  await page.setViewportSize(viewport); await prepare(page);
  const trigger = page.locator("#devices-list").getByRole("button", { name: "Details", exact: true });
  await trigger.click();
  await expect(page.locator("#device-modal")).toBeVisible();
  await page.evaluate(async () => {
    const { promptDialog } = await import("/js/ui.js");
    window.dialogResult = promptDialog({ title: "Rename fictional gateway", value: "Gateway" });
  });
  await expect(page.getByRole("dialog", { name: "Rename fictional gateway" })).toBeVisible();
  expect(await page.locator("#app").evaluate(el => el.inert)).toBe(true);
  expect(await page.locator("#device-modal").evaluate(el => el.inert)).toBe(true);
  await page.locator("#dialog-input").focus();
  await page.keyboard.press("Shift+Tab");
  expect(await page.evaluate(() => document.querySelector("#dialog").contains(document.activeElement))).toBe(true);
  await page.keyboard.press("Escape");
  await expect(page.locator("#dialog")).toBeHidden();
  expect(await page.evaluate(() => document.body.style.overflow)).toBe("hidden");
  expect(await page.locator("#device-modal").evaluate(el => el.inert)).toBe(false);
  await page.evaluate(async () => { window.overlay = (await import("/js/ui.js")).openOverlay({ title: "Fictional series" }); });
  await expect(page.getByRole("dialog", { name: "Fictional series" })).toBeVisible();
  await page.evaluate(async () => {
    const { seriesChartCard } = await import("/js/charts.js");
    window.overlay.body.append(seriesChartCard({ name: "Fictional temperature", unit: "°C" }, [[1788609600, 20], [1788609660, 21]]));
  });
  await page.locator(".series-modal canvas").focus();
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Fictional series" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator(".series-modal")).toHaveCount(0);
  expect(await page.evaluate(() => document.body.style.overflow)).toBe("hidden");
  await page.keyboard.press("Escape");
  await expect(page.locator("#device-modal")).toBeHidden();
  expect(await page.evaluate(() => document.body.style.overflow)).toBe("");
  expect(await page.locator("#app").evaluate(el => el.inert)).toBe(false);
  await expect(page).toHaveURL(/#\/devices$/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

for (const viewport of widths) test(`H06 labels and actionable validation at ${viewport.width}`, async ({ page }) => {
  await page.setViewportSize(viewport); await prepare(page);
  await page.getByRole("tab", { name: "Settings", exact: true }).click();
  await expect(page.getByLabel("Current password", { exact: true })).toBeVisible();
  await page.getByLabel("Current password", { exact: true }).fill("fictional-current");
  await page.getByLabel("New password", { exact: true }).fill("fictional-new-password");
  await page.getByLabel("Confirm new password", { exact: true }).fill("different-fictional-password");
  await page.locator("#pw-form").getByRole("button", { name: "Update", exact: true }).click();
  await expect(page.locator("#pw-confirm")).toBeFocused();
  await expect(page.locator("#pw-confirm")).toHaveAttribute("aria-invalid", "true");
  await expect(page.locator("#pw-confirm")).toHaveAccessibleDescription(/do not match/);
  await page.getByRole("tab", { name: "Users", exact: true }).click();
  await page.locator("#add-user-btn").click();
  await expect(page.locator("#add-user-form").getByLabel("Username", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Role", { exact: true })).toBeVisible();
  await page.locator("#add-user-form").getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.locator("#nu-user")).toBeFocused();
  await expect(page.locator("#nu-user")).toHaveAttribute("aria-invalid", "true");
  await page.getByRole("tab", { name: "Devices", exact: true }).click();
  await page.locator("#devices-list").getByRole("button", { name: "Details", exact: true }).click();
  await page.evaluate(async () => {
    const { alertsSection } = await import("/js/detail/alerts.js");
    document.querySelector("#dm-body").append(alertsSection({ device: { id: "router-1", name: "Fictional gateway" },
      entities: [{ kind: "sensor", key: "temperature", name: "Temperature", unit: "°C", value: 21 }] }));
  });
  const group = page.getByRole("group", { name: "Alert threshold for Fictional gateway" });
  await expect(group.getByLabel("Sensor", { exact: true })).toBeVisible();
  await expect(group.getByLabel("Comparison", { exact: true })).toBeVisible();
  await group.getByRole("button", { name: "Add alert" }).click();
  await expect(group.getByLabel("Threshold", { exact: true })).toBeFocused();
  await expect(group.getByLabel("Threshold", { exact: true })).toHaveAccessibleDescription(/°C.*threshold value/);
  await page.keyboard.press("Escape");
  await mockRoster(page, { ...roster, nac: { ...roster.nac, deviceName: "Fictional firewall", alias: "Trusted" } });
  await page.getByRole("tab", { name: /^Access/ }).click();
  await expect(page.getByRole("switch", { name: "Enforce network access on Fictional firewall (Trusted)" })).toHaveAttribute("aria-checked", "false");
  await page.getByLabel("Search clients", { exact: true }).fill("Laptop");
  await expect(page.locator('label[for="clients-search"]')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
