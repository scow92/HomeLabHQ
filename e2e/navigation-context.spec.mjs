import { expect, test } from "@playwright/test";
import { device, json, signIn } from "./support/fixtures.mjs";

const widths = [{ width: 1440, height: 900 }, { width: 768, height: 1024 }, { width: 390, height: 844 }];

async function prepare(page) {
  const devices = Array.from({ length: 5 }, (_, index) => ({
    ...device,
    id: `route-device-${index}`,
    name: index ? `Fictional node ${index}` : "Fictional edge",
    host: `192.0.2.${index + 10}`,
    dashboardId: index < 2 ? "fictional-ops" : null,
    state: { online: index !== 1 },
  }));
  await page.route("**/api/**", route => {
    const path = new URL(route.request().url()).pathname;
    if (["/api/session", "/api/login", "/api/logout", "/api/setup"].includes(path)) return route.continue();
    if (path === "/api/dashboards") return json(route, { dashboards: [{ id: "fictional-ops", name: "Operations" }] });
    if (path === "/api/devices") return json(route, { devices });
    if (/^\/api\/devices\/[^/]+\/detail$/.test(path)) {
      const selected = devices.find(item => path.includes(item.id)) || devices[0];
      return json(route, { device: selected, detail: { info: { Identity: selected.name } } });
    }
    if (path === "/api/compute") return json(route, { instances: [], hosts: [], ansibleEnabled: false });
    return json(route, { drivers: [], clients: [], events: [], notifications: [], users: [], controller: null });
  });
  await signIn(page);
  await expect(page.locator("#devices-list")).toContainText("Fictional edge");
}

test("M07 member keyboard routes skip hidden tabs and wizard draft survives navigation", async ({ page }) => {
  await prepare(page);
  await page.evaluate(async () => {
    const api = await import("/js/api.js");
    api.setSession({ ...api.SESSION, role: "member" });
  });
  await page.getByRole("tab", { name: "Add device" }).click();
  await page.getByLabel("Host / IP").fill("192.0.2.77");
  await page.getByRole("radio", { name: "HTTP web UI", exact: true }).click();
  const wizard = page.locator("[data-panel='add']");
  await wizard.getByLabel("Username", { exact: true }).fill("fictional-user");
  await wizard.getByLabel("Password", { exact: true }).fill("fictional-secret");
  await page.getByRole("tab", { name: "Add device" }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Settings" })).toHaveAttribute("aria-selected", "true");
  await page.getByRole("tab", { name: "Add device" }).click();
  await expect(page.getByLabel("Host / IP")).toHaveValue("192.0.2.77");
  const persisted = await page.evaluate(() => JSON.stringify({
    hash: location.hash, local: { ...localStorage }, session: { ...sessionStorage },
  }));
  expect(persisted).not.toContain("192.0.2.77");
  expect(persisted).not.toContain("fictional-secret");
  let userReads = 0;
  page.on("request", request => { if (new URL(request.url()).pathname === "/api/users") userReads += 1; });
  await page.evaluate(() => { location.hash = "#/users"; });
  await expect(page.locator("#route-feedback")).toContainText("not available for your account");
  await expect(page.locator("[data-panel='users']")).toBeHidden();
  await expect(page.locator("#route-feedback").getByRole("link", { name: "Return to Devices" })).toBeVisible();
  expect(userReads).toBe(0);
});

for (const viewport of widths) test(`M07 route context, history and safe failures at ${viewport.width}`, async ({ page }) => {
  await page.setViewportSize(viewport);
  const errors = []; page.on("pageerror", error => errors.push(error.message));
  await prepare(page);
  await page.getByRole("button", { name: /Operations/ }).click();
  await expect(page).toHaveURL(/dashboard=fictional-ops/);
  await page.getByLabel("Search devices").fill("edge");
  await page.getByLabel("Filter devices by status").selectOption("online");
  await expect(page).toHaveURL(/q=edge/);
  await expect(page).toHaveURL(/status=online/);
  await page.reload();
  await expect(page.getByLabel("Search devices")).toHaveValue("edge");
  await expect(page.getByLabel("Filter devices by status")).toHaveValue("online");
  await expect(page.getByRole("button", { name: /Operations/ })).toHaveClass(/active/);
  await expect(page.locator("#devices-list")).toContainText("Fictional edge");

  await page.getByRole("tab", { name: "Compute" }).click();
  await page.getByRole("button", { name: /Need Attention/ }).click();
  await expect(page).toHaveURL(/filter=attention/);
  await page.goBack();
  await expect(page.getByRole("button", { name: "All", exact: true })).toHaveClass(/active/);
  await page.goBack();
  await expect(page.getByLabel("Search devices")).toHaveValue("edge");
  await expect(page.locator("[data-panel='devices'] h1")).toBeFocused();
  await expect(page).toHaveTitle("Devices · HomelabHQ");
  await page.goForward();
  await expect(page.getByRole("tab", { name: "Compute" })).toHaveAttribute("aria-selected", "true");
  await page.goBack();
  await expect(page.getByLabel("Search devices")).toHaveValue("edge");
  await page.locator("#devices-list").getByRole("button", { name: "Details", exact: true }).click();
  await expect(page).toHaveTitle("Fictional edge · HomelabHQ");
  await page.keyboard.press("Escape");
  await expect(page).toHaveURL(/dashboard=fictional-ops/);
  await expect(page).toHaveURL(/q=edge/);
  await expect(page).toHaveTitle("Devices · HomelabHQ");

  await page.evaluate(() => { location.hash = "#/does-not-exist"; });
  await expect(page.locator("#route-feedback")).toContainText("could not be found");
  await page.evaluate(() => { location.hash = "#/device/fictional-missing"; });
  await expect(page.locator("#route-feedback")).toContainText("could not be found");
  await page.evaluate(() => { location.hash = "#/device/%E0%A4%A"; });
  await expect(page.locator("#route-feedback")).toContainText("not a valid link");
  await expect(page.locator("#route-feedback h1")).toBeFocused();
  await expect(page).toHaveTitle("Page unavailable · HomelabHQ");
  expect(errors).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  if (process.env.CAPTURE_UI_EVIDENCE && viewport.width === 390) {
    await page.screenshot({ path: "docs/ui-review/m07-route-feedback-mobile.png", fullPage: true });
  }
});

test("M07 legacy Access hash is normalized without duplicate activation", async ({ page }) => {
  await signIn(page);
  let clientReads = 0;
  page.on("request", request => { if (new URL(request.url()).pathname === "/api/clients") clientReads += 1; });
  await page.evaluate(() => { location.hash = "#/clients"; });
  await expect(page).toHaveURL(/#\/access$/);
  await expect(page.getByRole("tab", { name: "Access" })).toHaveAttribute("aria-selected", "true");
  expect(clientReads).toBe(1);
});
