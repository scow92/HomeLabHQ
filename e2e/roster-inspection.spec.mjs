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

const clients = [
  { mac: "00:00:00:00:00:03", hostname: "Zulu laptop", ip: "192.0.2.20", signal: -70, lastSeen: 20 },
  { mac: "00:00:00:00:00:02", hostname: "Alpha camera", ip: "192.0.2.10", signal: -50, lastSeen: 30 },
  { mac: "00:00:00:00:00:01", hostname: "Guest tablet", ip: "192.0.2.25", lastSeen: 10 },
].map(client => ({ ...client, online: true, nac: "approved", kind: "wifi" }));

test("M01 all advertised sorts share table/card order and preserve the saved sort", async ({ page }) => {
  await mockRoster(page, { ...roster, clients, nac: { configured: false } });
  await signIn(page); await page.getByRole("tab", { name: /^Access/ }).click();
  await page.getByLabel("Client presentation").selectOption("table");
  const orders = { hostname: ["Alpha camera", "Guest tablet", "Zulu laptop"],
    ip: ["Alpha camera", "Zulu laptop", "Guest tablet"],
    mac: ["Guest tablet", "Alpha camera", "Zulu laptop"],
    signal: ["Alpha camera", "Zulu laptop", "Guest tablet"],
    lastseen: ["Alpha camera", "Zulu laptop", "Guest tablet"] };
  for (const [sort, order] of Object.entries(orders)) {
    await page.getByLabel("Sort clients", { exact: true }).selectOption(sort);
    await expect.poll(() => page.locator(".clients-table tbody tr:not(.client-detail-row) td:first-child")
      .evaluateAll(cells => cells.map(cell => cell.querySelector(".client-name")?.textContent || cell.textContent))).toEqual(order);
    await page.getByLabel("Client presentation").selectOption("cards");
    await expect(page.locator(".cc-name")).toHaveText(order);
    await page.getByLabel("Client presentation").selectOption("table");
  }
  await page.reload();
  await expect(page.getByLabel("Sort clients", { exact: true })).toHaveValue("lastseen");
});

test("M01 address families, missing values and ties have deterministic ordering", async ({ page }) => {
  await signIn(page);
  const order = await page.evaluate(async () => {
    const { sortClients } = await import("/js/clients/filters.js");
    const rows = ["", "2001:db8::10", "192.0.2.20", "2001:db8::2", "192.0.2.2", "bad", "2001:0db8:0:0:0:0:0:2", "::ffff:192.0.2.1"]
      .map((ip, i) => ({ ip, hostname: "Same", mac: String(i) }));
    return { ip: sortClients(rows, "ip").map(row => row.mac),
      ties: sortClients(rows.slice().reverse(), "hostname").map(row => row.mac) };
  });
  expect(order.ip).toEqual(["4", "2", "7", "3", "6", "1", "0", "5"]);
  expect(order.ties).toEqual(["0", "1", "2", "3", "4", "5", "6", "7"]);
});

for (const viewport of [{ width: 1440, height: 900 }, { width: 768, height: 1024 }, { width: 390, height: 844 }]) {
  test(`M01 read-only history, recovery and navigation at ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockRoster(page, { ...roster, clients, nac: { configured: false } });
    let scans = 0, histories = 0, fail = true;
    await page.route("**/api/clients/refresh", route => { scans++; return json(route, {}); });
    await page.route("**/api/clients/history**", route => {
      histories++;
      return json(route, fail ? { error: "Fictional unavailable history" } : { events: [{ ev: "up", via: "Fictional AP", ts: 1_700_000_000 }] }, fail ? 503 : 200);
    });
    await signIn(page); await page.getByRole("tab", { name: /^Access/ }).click();
    await page.getByLabel("Client presentation").selectOption("table");
    const details = page.getByRole("button", { name: "Details for Alpha camera", exact: true });
    await details.focus(); await page.keyboard.press("Enter");
    await expect(page.locator(".cc-history")).toContainText("Couldn't load history");
    fail = false;
    await page.keyboard.press("Space"); await page.keyboard.press("Enter");
    await expect(page.locator(".cc-history")).toContainText("Connected via Fictional AP");
    if (viewport.width === 390) await page.screenshot({ path: "/tmp/hlhq-m01-mobile.png" });
    await expect(details).toBeFocused();
    expect(histories).toBe(2); expect(scans).toBe(0);
    await expect(page.getByRole("button", { name: /^(Approve|Revoke access|Edit —)/ })).toHaveCount(0);
    await page.getByLabel("Client presentation").selectOption("cards");
    await details.focus(); await page.keyboard.press("Enter");
    await expect(page.locator(".client-card.expanded .cc-history")).toContainText("Connected via Fictional AP");
    await page.getByRole("tab", { name: "Devices", exact: true }).click();
    await page.goBack(); await expect(page).toHaveURL(/#\/access$/);
    await expect(page.getByLabel("Client presentation")).toHaveValue("cards");
    await page.goForward(); await expect(page).toHaveURL(/#\/devices$/);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test("M01 configured table exposes existing actions and export retains whole-roster scope", async ({ page }) => {
  await mockRoster(page);
  await signIn(page); await page.getByRole("tab", { name: /^Access/ }).click();
  await page.getByLabel("Client presentation").selectOption("table");
  await expect(page.getByRole("button", { name: "Revoke access", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /Edit — rename/ })).toHaveCount(2);
  await page.locator("#clients-search").fill("camera");
  await page.locator("#clients-menu").click();
  await expect(page.getByRole("button", { name: /Forget offline shown \(1\)/ })).toBeVisible();
  // Native downloads use the isolated server's owner-scoped export endpoint.
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: /Export roster as CSV/ }).click();
  expect(new URL((await download).url()).search).toBe("?format=csv");
});

test("M01 late history cannot expire a newer route or cross session disposal", async ({ page }) => {
  await mockRoster(page, { ...roster, nac: { configured: false } });
  await signIn(page); await page.getByRole("tab", { name: /^Access/ }).click();
  await page.evaluate(() => {
    const original = window.fetch;
    window.fetch = (path, options) => String(path).includes("/clients/history")
      ? new Promise(resolve => { window.releaseHistory = () => resolve({ status: 401, ok: false, json: async () => ({}) }); })
      : original(path, options);
  });
  await page.getByRole("button", { name: "Details for Laptop Alice" }).click();
  await expect(page.locator(".cc-history")).toHaveText("Loading…");
  await page.waitForFunction(() => !!window.releaseHistory);
  await page.getByRole("tab", { name: "Devices", exact: true }).click();
  await page.evaluate(() => window.releaseHistory());
  await expect(page.locator("#app")).toBeVisible();
  await page.locator("#logout-btn").click();
  await expect(page.locator("#clients-body")).toBeEmpty();
  expect(await page.evaluate(async () => (await import("/js/clients/filters.js")).getFilters().view)).toBeNull();
});
