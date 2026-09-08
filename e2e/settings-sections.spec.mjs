import { expect, test } from "@playwright/test";
import { json, signIn } from "./support/fixtures.mjs";

async function prepare(page) {
  let controller = null;
  const state = { fail: false, calls: [] };
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (["/api/session", "/api/login", "/api/logout", "/api/setup"].includes(path)) return route.continue();
    state.calls.push([path, route.request().method()]);
    if (path === "/api/settings/ansible") {
      if (route.request().method() === "POST") {
        controller = { ...route.request().postDataJSON(), credentialConfigured: true };
        delete controller.privateKey;
      }
      return json(route, { controller });
    }
    if (path.startsWith("/api/settings/ansible/")) {
      if (state.fail) return json(route, { error: "fictional failure" }, 503);
      if (path.endsWith("/test")) return json(route, { status: Object.fromEntries(
        ["controller", "project", "ansiblePlaybook", "ansibleInventory", "inventory"].map(key => [key, { ok: true }])) });
      if (path.endsWith("/inventory")) controller.inventory = { discoveredAt: 1700000000, hosts: ["fictional"], groups: [] };
      if (path.endsWith("/playbooks")) controller.discoveredPlaybooks = ["checks.yml"];
      if (path.endsWith("/approve")) controller.playbooks = { os_check: { approved: true, playbook: "checks.yml" } };
      return json(route, { ok: true });
    }
    if (path === "/api/compute") return json(route, { instances: [], hosts: [], ansibleEnabled: false });
    return json(route, { devices: [], dashboards: [], clients: [], drivers: [], notifications: [], events: [], configured: false });
  });
  await signIn(page);
  return state;
}

for (const [width, height] of [[1440, 900], [768, 1024], [390, 844], [320, 740]]) {
  test(`M06 section deep links, keyboard and history at ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height }); await prepare(page);
    await page.goto("/#/settings?section=ansible");
    await expect(page.locator("#settings-navigation").getByRole("link", { name: "Ansible", exact: true })).toHaveAttribute("aria-current", "location");
    await expect(page.locator("#ansible-settings-card")).toBeInViewport();
    await expect(page.locator("#ans-prerequisites")).toContainText("Save");
    if (width === 320) await page.screenshot({ path: testInfo.outputPath("m06-phone.png") });
    await page.locator("#ans-name").fill("Fictional draft");
    const account = page.locator("#settings-navigation").getByRole("link", { name: "Account", exact: true });
    await account.focus(); await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/section=account$/);
    await page.goBack();
    await expect(page.locator("#ans-name")).toHaveValue("Fictional draft");
    await page.goForward();
    await expect(account).toHaveAttribute("aria-current", "location");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test("M06 saved setup, explicit approvals and local failure retain drafts", async ({ page }) => {
  const state = await prepare(page);
  await page.goto("/#/settings?section=ansible");
  await page.locator("#ans-name").fill("Fictional controller");
  for (const [id, value] of [["host", "192.0.2.90"], ["user", "fixture"], ["project", "/fixture"], ["inventory", "hosts.yml"], ["playbooks", "playbooks"], ["secret", "fictional-test-key"]]) await page.locator(`#ans-${id}`).fill(value);
  await page.locator("#ans-save").click();
  await expect(page.locator("#ans-secret")).toHaveValue("");
  await expect(page.locator("#ans-prerequisites")).toContainText("Saved");
  state.fail = true;
  await page.locator("#ans-name").fill("Unsaved controller draft");
  await page.locator("#ans-test").click();
  await expect(page.locator("#ans-test-result")).toContainText("Try Test Connection again");
  await expect(page.locator("#ans-name")).toHaveValue("Unsaved controller draft");
  await page.locator("#ans-discover").click();
  await expect(page.locator("#ans-inventory-summary")).toContainText("Try Discover / Refresh Inventory again");
  state.fail = false;
  await page.locator("#ans-test").click();
  await expect(page.locator("#ans-prerequisites")).toContainText("Tested");
  await page.locator("#ans-discover").click();
  await page.locator("#ans-find-playbooks").click();
  const operation = page.locator(".ans-operation").filter({ has: page.getByRole("combobox", { name: "OS update check playbook", exact: true }) });
  await operation.getByRole("combobox").selectOption("checks.yml");
  state.fail = true;
  await operation.getByRole("button", { name: "Save approval" }).click();
  await expect(operation).toContainText("Try Save approval again");
  await expect(operation.getByRole("combobox")).toHaveValue("checks.yml");
  state.fail = false;
  await operation.getByRole("button", { name: "Save approval" }).click();
  await expect(page.locator("#ans-prerequisites")).toContainText("1 approved");
});

test("M06 Compute link, loading recovery and account transition", async ({ page }) => {
  const state = await prepare(page);
  await expect(page.locator("#compute-ansible-setup a")).toHaveAttribute("href", "#/settings?section=ansible");
  let release;
  await page.route("**/api/settings/ansible", async route => {
    await new Promise(resolve => { release = resolve; });
    return json(route, { error: "fixture" }, 503);
  });
  await page.goto("/#/settings?section=ansible");
  await expect(page.locator("#ans-prerequisites")).toContainText("Loading");
  release();
  await expect(page.locator("#ans-prerequisites")).toContainText("Configuration unavailable");
  await page.unroute("**/api/settings/ansible");
  await page.locator("#ansible-settings-refresh-state").getByRole("button", { name: "Retry" }).click();
  await expect(page.locator("#ans-prerequisites")).toContainText("Save");
  await page.locator("#ans-secret").fill("fictional-private-draft");
  await page.evaluate(async () => {
    const api = await import("/js/api.js");
    api.setSession({ ...api.SESSION, role: "member" });
  });
  await page.evaluate(() => { location.hash = "#/settings?section=account"; });
  await expect(page.locator("#ans-secret")).toHaveValue("");
  await expect(page.locator("#settings-navigation").getByRole("link", { name: "Ansible", exact: true })).toBeHidden();
  expect(state.calls.filter(([path, method]) => method === "POST" && path.startsWith("/api/settings/ansible"))).toEqual([]);
});

test("M06 late test failure cannot populate a later Settings visit", async ({ page }) => {
  await prepare(page); await page.goto("/#/settings?section=ansible");
  let release;
  await page.route("**/api/settings/ansible/test", async route => {
    await new Promise(resolve => { release = resolve; });
    await json(route, { error: "fictional failure" }, 503);
  });
  const pending = page.waitForRequest("**/api/settings/ansible/test");
  await page.locator("#ans-test").click(); await pending;
  await page.getByRole("tab", { name: "Devices", exact: true }).click();
  await page.getByRole("tab", { name: "Settings", exact: true }).click();
  const response = page.waitForResponse("**/api/settings/ansible/test");
  release(); await response;
  await expect(page.locator("#ans-test")).toBeEnabled();
  await expect(page.locator("#ans-test-result")).not.toContainText("Try Test Connection again");
});
