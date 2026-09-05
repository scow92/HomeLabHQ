import { expect, test } from "@playwright/test";
import { device, json, signIn } from "./support/fixtures.mjs";

const widths = [{ width: 1440, height: 900 }, { width: 768, height: 1024 }, { width: 390, height: 844 }];
async function prepare(page) {
  await page.route("**/api/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (["/api/session", "/api/login", "/api/logout", "/api/setup"].includes(path)) return route.continue();
    return json(route, path === "/api/devices" ? { devices: [device] }
      : path.endsWith("/detail") ? { device, detail: { info: { Identity: "Fictional gateway" } } }
      : { dashboards: [], drivers: [], instances: [], clients: [], events: [], notifications: [], controller: null });
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
