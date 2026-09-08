import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { json, mockRoster, roster, signIn } from "./support/fixtures.mjs";

test.use({ serviceWorkers: "block" });
test.beforeEach(async ({ page }) => {
  if (!process.env.HLHQ_BASELINE) return;
  for (const file of ["grid.js", "filters.js"]) {
    const body = execFileSync("git", ["show", `${process.env.HLHQ_BASELINE}:web/js/clients/${file}`], { encoding: "utf8" });
    await page.route(`**/js/clients/${file}`, route => route.fulfill({ contentType: "text/javascript", body }));
  }
});

test("Access defaults to Cards without storing an implicit choice", async ({ page }) => {
  await mockRoster(page, { ...roster, nac: { configured: false } });
  await signIn(page); await page.getByRole("tab", { name: /^Access/ }).click();
  await expect(page.getByLabel("Client presentation")).toHaveValue("cards");
  await expect(page.locator(".client-card")).toHaveCount(2);
  expect(await page.evaluate(() => localStorage.getItem("hlhq-clients-view"))).toBeNull();
});

for (const [width, height, saved, configured] of [[1440, 900, "table", false], [768, 1024, "cards", true], [390, 844, "table", true], [320, 740, "cards", false]]) {
  test.describe(`Access preference ${width}`, () => {
    test.use({ viewport: { width, height }, isMobile: width < 1000, hasTouch: width < 1000 });
    test(`saved ${saved} survives history, reload and account replacement`, async ({ page }) => {
      await mockRoster(page, { ...roster, nac: { ...roster.nac, configured } });
      await page.goto("/");
      await page.evaluate(saved => localStorage.setItem("hlhq-clients-view", saved), saved);
      await signIn(page); await page.getByRole("tab", { name: /^Access/ }).click();
      const mode = page.getByLabel("Client presentation");
      await expect(mode).toHaveValue(saved);
      await page.locator("#clients-search").fill("laptop");
      await page.locator("#clients-status").selectOption("online");
      await expect(page.getByRole("button", { name: "Details for Laptop Alice" })).toBeVisible();
      await expect(page.getByRole("button", { name: "Details for Camera Garage" })).toHaveCount(0);
      await page.getByRole("tab", { name: "Devices", exact: true }).click();
      await page.goBack(); await expect(mode).toHaveValue(saved);
      await page.goForward(); await expect(page).toHaveURL(/#\/devices$/);
      await page.goBack(); await page.reload();
      await expect(mode).toHaveValue(saved);
      await page.locator("#clients-search").fill("laptop");
      await page.locator("#clients-status").selectOption("online");
      let releaseAccount;
      const accountReady = new Promise(resolve => { releaseAccount = resolve; });
      await page.route("**/api/clients", async route => {
        await accountReady;
        return json(route, { ...roster, nac: { ...roster.nac, configured }, clients: [{ ...roster.clients[0], hostname: "Fictional replacement client" }] });
      });
      await page.evaluate(async () => {
        const { setSession } = await import("/js/api.js");
        setSession({ id: "fictional-second-account", username: "Fictional second account", role: "admin" });
      });
      await expect(page.locator("#clients-body")).not.toContainText("Laptop Alice");
      await expect(page.locator("#clients-body")).not.toContainText("Camera Garage");
      await expect(page.locator("#clients-search")).toHaveValue("");
      await expect(page.locator("#clients-status")).toHaveValue("all");
      releaseAccount();
      await expect(page.getByRole("button", { name: "Details for Fictional replacement client" })).toBeVisible();
      await expect(mode).toHaveValue(saved);
      await expect(page.locator("#clients-refresh-state")).toBeVisible();
      await expect(page.locator("#clients-refresh-state")).toHaveAttribute("data-state", "current");
      const other = saved === "cards" ? "table" : "cards";
      await mode.selectOption(other);
      expect(await page.evaluate(() => localStorage.getItem("hlhq-clients-view"))).toBe(other);
      await page.reload(); await expect(mode).toHaveValue(other);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    });
  });
}

test("invalid or unavailable storage falls back to Cards and explicit choices still work", async ({ page }) => {
  await page.addInitScript(() => {
    const get = Storage.prototype.getItem;
    Storage.prototype.getItem = function(key) { if (key === "hlhq-clients-view") return "invalid"; return get.call(this, key); };
    const set = Storage.prototype.setItem;
    Storage.prototype.setItem = function(key, value) { if (key === "hlhq-clients-view") throw new DOMException("Unavailable", "SecurityError"); return set.call(this, key, value); };
  });
  await mockRoster(page, { ...roster, nac: { configured: false } });
  await signIn(page); await page.getByRole("tab", { name: /^Access/ }).click();
  const mode = page.getByLabel("Client presentation");
  await expect(mode).toHaveValue("cards");
  await mode.selectOption("table"); await expect(page.locator(".clients-table")).toBeVisible();
});

for (const mode of ["cards", "table"]) {
  test(`${mode} retains loading, empty, source error, stale and recovery feedback`, async ({ page }) => {
    await page.goto("/"); await page.evaluate(mode => localStorage.setItem("hlhq-clients-view", mode), mode);
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    let fail = false, payload = { ...roster, clients: [], nac: { configured: false } };
    await page.route("**/api/clients", async route => { await gate; return json(route, fail ? { error: "Fictional unavailable" } : payload, fail ? 503 : 200); });
    await signIn(page); await page.getByRole("tab", { name: /^Access/ }).click();
    await expect(page.locator("#clients-refresh-state")).toHaveAttribute("data-state", "refreshing");
    await expect(page.getByLabel("Client presentation")).toHaveValue(mode);
    fail = true; release();
    await expect(page.locator("#clients-refresh-state")).toHaveAttribute("data-state", "error");
    fail = false;
    await page.locator("#clients-refresh-state").getByRole("button", { name: "Retry" }).click();
    await expect(page.locator("#clients-refresh-state")).toHaveAttribute("data-state", "current");
    await expect(page.locator("#clients-body")).toContainText("No clients");
    payload = { ...roster, nac: { configured: false }, sources: [{ name: "Fictional AP", error: "Fictional source failure" }] };
    await page.evaluate(async () => (await import("/js/clients/index.js")).loadClients());
    await expect(page.getByLabel("Client presentation")).toHaveValue(mode);
    await expect(page.locator("#clients-summary")).toContainText("Fictional AP");
    fail = true;
    await page.evaluate(async () => (await import("/js/clients/index.js")).loadClients());
    await expect(page.locator("#clients-refresh-state")).toHaveAttribute("data-state", "stale");
    await expect(page.getByRole("button", { name: "Details for Laptop Alice" })).toBeVisible();
    fail = false;
    await page.locator("#clients-refresh-state").getByRole("button", { name: "Retry" }).click();
    await expect(page.locator("#clients-refresh-state")).toHaveAttribute("data-state", "current");
    await expect(page.locator("#clients-refresh-state")).toBeVisible();
    await expect(page.getByLabel("Client presentation")).toHaveValue(mode);
  });
}
