import { expect, test } from "@playwright/test";
import { device, json, signIn, mockRoster } from "./support/fixtures.mjs";

for (const viewport of [{ width: 1440, height: 900 }, { width: 768, height: 1024 }, { width: 390, height: 844 }]) {
  test(`H04 keyboard setup and disclosures at ${viewport.width}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.route("**/api/devices", route => json(route, { devices: [device], device }));
    await page.route("**/api/drivers", route => json(route, { drivers: [], transports: ["ssh", "http"] }));
    await page.route("**/api/devices/detect", route => json(route, { candidates: [
      { driverId: "generic.ssh", displayName: "Fictional shell", confidence: 0.8 },
      { driverId: "generic.http", displayName: "Fictional web", confidence: 0.7 },
    ] }));
    await page.route("**/api/devices/entities", route => json(route, { entities: [{ key: "temp", kind: "sensor", name: "Temperature" }] }));
    await mockRoster(page); await signIn(page);
    await page.getByRole("tab", { name: "Add device", exact: true }).focus(); await page.keyboard.press("Enter");
    const ssh = page.getByRole("radio", { name: "SSH", exact: true });
    await ssh.focus(); await page.keyboard.press("Space"); await expect(ssh).toBeChecked();
    await page.keyboard.press("ArrowRight"); await expect(page.getByRole("radio", { name: "HTTP web UI", exact: true })).toBeChecked();
    await page.locator("#wiz-host").fill("192.0.2.99");
    await page.locator("#wiz-detect").focus(); await page.keyboard.press("Enter");
    await expect(page.getByRole("radio", { name: "Fictional shell", exact: true })).toBeChecked();
    await page.getByRole("radio", { name: "Fictional shell", exact: true }).focus();
    await page.keyboard.press("ArrowRight"); await expect(page.getByRole("radio", { name: "Fictional web", exact: true })).toBeChecked();
    await page.locator("#wiz-choose").focus(); await page.keyboard.press("Enter");
    await expect(page.locator('[data-wstep="3"]')).toBeVisible();
    if (viewport.width === 390) await page.screenshot({ path: "/tmp/hlhq-h04-mobile.png" });
    await page.locator("#wiz-steps").getByRole("button", { name: "Connect", exact: true }).focus();
    await page.keyboard.press("Space"); await expect(page.locator("#wiz-host")).toHaveValue("192.0.2.99");
    await page.locator("#wiz-detect").focus(); await page.keyboard.press("Enter");
    await expect(page.getByRole("radio", { name: "Fictional shell", exact: true })).toBeChecked();
    await page.locator("#wiz-choose").focus(); await page.keyboard.press("Enter");
    await page.locator("#wiz-save").focus(); await page.keyboard.press("Enter");
    await expect(page.locator('[data-wstep="4"]')).toBeVisible();
    await expect(page.locator("#wiz-done-msg")).toContainText("added with 1 entities");
    await page.getByRole("tab", { name: /^Access/ }).focus(); await page.keyboard.press("Enter");
    const client = page.getByRole("button", { name: "Details for Laptop Alice", exact: true });
    await client.focus(); await page.keyboard.press("Space"); await expect(client).toHaveAttribute("aria-expanded", "true");
    await expect(page.locator(".client-card.expanded .cc-history")).toContainText("No events recorded");
    await page.keyboard.press("Enter"); await expect(client).toHaveAttribute("aria-expanded", "false");
    expect(await client.evaluate(el => getComputedStyle(el).outlineStyle)).not.toBe("none");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}

test("H04 radio, interface and independent AP binding buttons retain native table semantics", async ({ page }) => {
  await page.route("**/api/devices", route => json(route, { devices: [device] }));
  await signIn(page);
  await page.evaluate(async resource => {
    const { radiosTable, clientsList } = await import("/js/detail/tables.js");
    const { interfacesSection } = await import("/js/detail/interfaces.js");
    const { openOverlay } = await import("/js/ui.js");
    const dm = { device: resource, entities: [], history: {}, ifHistory: {} };
    const { body } = openOverlay({ title: "Fictional network controls" });
    body.append(radiosTable({ columns: [{ key: "band", label: "Band" }], rows: [{ band: "5 GHz", historyKey: "clients" }] }, dm));
    body.append(interfacesSection({ columns: [{ key: "device", label: "Interface" }], rows: [{ device: "eth0" }] }, dm));
    body.append(clientsList({ bindable: true, columns: [{ key: "client", label: "Client" }, { key: "mac", label: "MAC" }], rows: [{ client: "Fictional laptop", mac: "00:11:22:33:44:55" }] }, dm));
  }, device);
  const radio = page.getByRole("button", { name: "History for 5 GHz" });
  await radio.focus(); await page.keyboard.press("Enter"); await expect(radio).toHaveAttribute("aria-expanded", "true");
  await page.keyboard.press("Space"); await expect(radio).toHaveAttribute("aria-expanded", "false");
  const iface = page.getByRole("button", { name: "History for eth0" });
  await iface.focus(); await page.keyboard.press("Enter"); await expect(iface).toHaveAttribute("aria-expanded", "true");
  const lock = page.getByRole("button", { name: /Bind Fictional laptop/ });
  expect(await lock.evaluate(el => el.parentElement.closest("button"))).toBeNull();
  await page.route("**/api/devices/router-1/bind-client", route => json(route, { ok: true }));
  await lock.focus(); await page.keyboard.press("Space"); await expect(lock).toHaveAttribute("aria-pressed", "true");
  expect((await lock.boundingBox()).width).toBeGreaterThanOrEqual(24);
  expect((await lock.boundingBox()).height).toBeGreaterThanOrEqual(24);
  await expect(page.locator(".client-head")).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("columnheader", { name: "Band", exact: true })).toBeVisible();
});
