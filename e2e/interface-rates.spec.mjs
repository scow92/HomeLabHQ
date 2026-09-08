import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { device, json, signIn } from "./support/fixtures.mjs";

test.use({ serviceWorkers: "block" });
test.beforeEach(async ({ page }) => {
  if (process.env.HLHQ_BASELINE) {
    const body = execFileSync("git", ["show", `${process.env.HLHQ_BASELINE}:web/js/detail/interfaces.js`], { encoding: "utf8" });
    await page.route("**/js/detail/interfaces.js", route => route.fulfill({ contentType: "text/javascript", body }));
  }
});
const table = { interfaces: true, columns: [{ key: "device", label: "Interface" }], rows: [{ device: "wan" }] };
const counters = { rx: [[100, 100000], [160, 220000]], tx: [[100, 100000], [160, 160000]] };

for (const viewport of [{ width: 1440, height: 900 }, { width: 768, height: 1024 }, { width: 390, height: 844 }]) {
  test(`M10 first display uses available samples before polling at ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.clock.install(); await page.clock.pauseAt(new Date());
    let reads = 0, fail = false;
    await page.route("**/api/devices", route => json(route, { devices: [device] }));
    await page.route("**/api/devices/router-1/detail", route => {
      reads++;
      return fail ? json(route, { error: "Fictional unavailable detail" }, 503)
        : json(route, { device, detail: { tables: [table] }, ifHistory: { wan: counters } });
    });
    await signIn(page);
    await page.evaluate(() => { location.hash = "#/device/router-1"; });
    await expect(page.locator("#device-modal")).toBeVisible();
    await expect(page.locator(".if-rate .r-dn")).toHaveText("↓ 16.0 Kbps");
    await expect(page.locator(".if-rate .r-up")).toHaveText("↑ 8.0 Kbps");
    expect(reads).toBe(1);
    const history = page.getByRole("button", { name: "History for wan", exact: true });
    await history.focus(); await page.keyboard.press("Enter");
    await expect(history).toHaveAttribute("aria-expanded", "true");
    expect(reads).toBe(1);
    if (viewport.width === 390) await page.screenshot({ path: "/tmp/hlhq-m10-mobile.png" });
    if (viewport.width === 1440) {
      fail = true; await page.clock.runFor(20000);
      await expect(page.locator("#detail-refresh-state")).toHaveAttribute("data-state", "stale");
      await expect(page.locator(".if-rate .r-dn")).toHaveText("↓ 16.0 Kbps");
      expect(reads).toBe(2);
      fail = false; await page.clock.runFor(20000);
      await expect(page.locator("#detail-refresh-state")).toHaveAttribute("data-state", "current");
      expect(reads).toBe(3);
    }
    await page.goBack(); await expect(page.locator("#device-modal")).toBeHidden();
    await page.goForward(); await expect(page).toHaveURL(/#\/device\/router-1$/);
    await expect(page.locator(".if-rate .r-dn")).toHaveText("↓ 16.0 Kbps");
    await page.keyboard.press("Escape"); await expect(page).toHaveURL(/#\/devices$/);
  });
}

test("M10 missing, single, reset and invalid samples; live fills respect presentation ownership", async ({ page }) => {
  await signIn(page);
  const results = await page.evaluate(async ({ table, counters, device }) => {
    const { interfacesSection } = await import("/js/detail/interfaces.js");
    const { refreshCharts, resetCharts } = await import("/js/charts.js");
    const cases = [undefined, { rx: [[100, 1]] }, { rx: [[100, 50], [160, 20]], tx: [[100, 5], [160, 1]] },
      { rx: [[100, 1], [100, 2]], tx: [[160, 2], [100, 3]] },
      { rx: [[100, null], [160, 20]], tx: [[100, 2], [160, "invalid"]] }, counters];
    const sampleResults = cases.map(samples => {
      const dm = { device, ifHistory: { wan: samples } };
      const section = interfacesSection(table, dm);
      return [...section.querySelectorAll(".if-rate span")].map(el => el.textContent);
    });
    resetCharts();
    let current = true;
    const dm = { device, ifHistory: { wan: counters }, current: () => current };
    const section = interfacesSection(table, dm); document.body.append(section);
    dm.ifHistory.wan = { rx: [[100, 0], [160, 240000]], tx: [[100, 0], [160, 120000]] };
    refreshCharts(); const live = section.querySelector(".r-dn").textContent;
    current = false; dm.ifHistory = {}; refreshCharts();
    const obsolete = section.querySelector(".r-dn").textContent;
    current = true; section.remove(); refreshCharts();
    const detached = section.querySelector(".r-dn").textContent;
    resetCharts(); return { sampleResults, live, obsolete, detached };
  }, { table, counters, device });
  const missing = ["↓ Not enough samples", "↑ Not enough samples"];
  expect(results.sampleResults).toEqual([missing, missing, ["↓ 0 bps", "↑ 0 bps"], missing, missing, ["↓ 16.0 Kbps", "↑ 8.0 Kbps"]]);
  expect(results.live).toBe("↓ 32.0 Kbps");
  expect(results.obsolete).toBe(results.live);
  expect(results.detached).toBe(results.live);
});
