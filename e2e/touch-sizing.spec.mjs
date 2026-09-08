import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { device, json, mockRoster, signIn } from "./support/fixtures.mjs";

test.use({ serviceWorkers: "block" });
test.beforeEach(async ({ page }) => {
  if (process.env.HLHQ_BASELINE) {
    for (const file of ["base.css", "views.css"]) {
      const body = execFileSync("git", ["show", `${process.env.HLHQ_BASELINE}:web/styles/${file}`], { encoding: "utf8" });
      await page.route(`**/styles/${file}`, route => route.fulfill({ contentType: "text/css", body }));
    }
  }
});

test.describe("coarse fields", () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  test("M04 touch fields cover auth, Settings, wizard and collection controls", async ({ page }) => {
    await mockRoster(page);
    await page.goto("/");
    expect(await page.evaluate(() => matchMedia("(pointer: coarse)").matches)).toBe(true);
    await expect(page.locator("#auth-user")).toHaveCSS("font-size", "16px");
    await expect(page.locator("#auth-pass")).toHaveCSS("font-size", "16px");
    await signIn(page);
    for (const name of [/^Access/, "Settings", "Add device"]) {
      await page.getByRole("tab", { name, exact: typeof name === "string" }).click();
      const fields = await page.locator('.panel:not([hidden]) input:not([type="checkbox"]):not([type="radio"]), .panel:not([hidden]) select, .panel:not([hidden]) textarea')
        .evaluateAll(elements => elements.filter(el => el.getClientRects().length).map(el => ({ id: el.id, font: parseFloat(getComputedStyle(el).fontSize) })));
      expect(fields.length).toBeGreaterThan(0);
      expect(fields.filter(field => field.font < 16)).toEqual([]);
    }
    await page.locator("#wiz-host").fill("192.0.2.99");
    await expect(page.locator("#wiz-host")).toHaveValue("192.0.2.99");
    await page.locator("#logout-btn").click();
    await expect(page.locator("#auth-pass")).toHaveValue("");
    expect(await page.locator('meta[name="viewport"]').getAttribute("content")).not.toMatch(/user-scalable\s*=\s*no|maximum-scale\s*=\s*1/);
  });
});

const identity = "Fictional-branch-gateway-with-a-long-unbroken-resource-identity-0123456789";
for (const [width, height, touch] of [[1440, 900, false], [768, 1024, true], [390, 844, true], [320, 740, true]]) {
  test.describe(`viewport ${width}`, () => {
    test.use({ viewport: { width, height }, isMobile: touch, hasTouch: touch });
    test(`M04 long identity, independent lock and detail targets at ${width}`, async ({ page }) => {
      await page.route("**/api/devices", route => json(route, { devices: [device] }));
      await signIn(page);
      await page.evaluate(async ({ device, identity }) => {
        const { clientsList } = await import("/js/detail/tables.js");
        const { interfacesSection } = await import("/js/detail/interfaces.js");
        const { chartCard } = await import("/js/detail/metrics.js");
        const { openOverlay } = await import("/js/ui.js");
        const now = Math.floor(Date.now() / 1000);
        const dm = { device, entities: [], history: {}, ifHistory: { wan: { rx: [[now - 60, 1], [now, 10]], tx: [[now - 60, 1], [now, 5]] } } };
        const { body } = openOverlay({ title: identity });
        body.append(interfacesSection({ columns: [{ key: "device", label: "Interface" }], rows: [{ device: "wan" }] }, dm));
        body.append(clientsList({ bindable: true, columns: [{ key: "client", label: "Client" }, { key: "mac", label: "MAC" }], rows: [{ client: identity, mac: "00:11:22:33:44:55" }, { client: "Other AP client", mac: "00:11:22:33:44:66", lock: "elsewhere" }] }, dm));
        body.append(chartCard({ key: "temperature", name: "Fictional temperature", unit: "°C" }, [[now - 60, 20], [now, 21]], dm));
      }, { device, identity });
      const title = page.locator(".series-modal .modal-head h2 span");
      await expect(title).toHaveText(identity);
      expect(await title.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
      const history = page.getByRole("button", { name: "History for wan", exact: true });
      await history.focus(); await page.keyboard.press("Enter");
      await expect(history).toHaveAttribute("aria-expanded", "true");
      await page.getByRole("button", { name: "Edit", exact: true }).click();
      const remove = page.getByRole("button", { name: "Remove interface wan" });
      const lock = page.getByRole("button", { name: `Bind ${identity} to ${device.name}` });
      const sizes = [];
      for (const target of [remove, lock, page.getByRole("button", { name: "Close", exact: true }), ...await page.locator(".c-range-btn").all()]) {
        const box = await target.boundingBox();
        sizes.push({ label: await target.getAttribute("aria-label") || await target.textContent(), width: box.width, height: box.height });
        expect(box.width).toBeGreaterThanOrEqual(touch ? 44 : 24);
        expect(box.height).toBeGreaterThanOrEqual(touch ? 44 : 24);
      }
      expect(await lock.evaluate(el => el.parentElement.closest("button"))).toBeNull();
      const expectDot = async (button, token) => {
        const dot = await button.evaluate((el, token) => {
          const style = getComputedStyle(el, "::before");
          const probe = document.createElement("span");
          probe.style.color = `var(${token})`; el.append(probe);
          const expected = getComputedStyle(probe).color; probe.remove();
          return { color: style.backgroundColor, expected, width: style.width, height: style.height, content: style.content };
        }, token);
        expect(dot.color).toBe(dot.expected);
        expect(dot.width).toBe("10px"); expect(dot.height).toBe("10px");
        expect(dot.content).toBe('""');
      };
      await expectDot(lock, "--muted");
      let bindingCalls = 0, release;
      await page.route("**/api/devices/router-1/bind-client", async route => {
        bindingCalls++;
        if (width === 1440 && bindingCalls === 1) await new Promise(resolve => { release = resolve; });
        return json(route, { ok: true });
      });
      await lock.focus(); await page.keyboard.press("Space");
      if (width === 1440) {
        await expect(lock).toHaveAttribute("aria-disabled", "true");
        await expect(lock).toHaveAttribute("aria-busy", "true");
        await expect(lock).toBeFocused();
        await expect.poll(() => bindingCalls).toBe(1);
        await page.keyboard.press("Enter"); expect(bindingCalls).toBe(1);
        release();
      }
      await expect(lock).toHaveAttribute("aria-pressed", "true");
      await expect(lock).toBeFocused();
      await expect(page.locator(".client-head").first()).toHaveAttribute("aria-expanded", "false");
      expect(await lock.evaluate(el => getComputedStyle(el).outlineStyle)).not.toBe("none");
      for (const theme of ["light", "dark"]) {
        await page.evaluate(async theme => (await import("/js/theme.js")).applyTheme(theme), theme);
        await expectDot(lock, "--green");
        await expectDot(page.getByRole("button", { name: `Bind Other AP client to ${device.name}` }), "--amber");
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        if (width === 320) await page.screenshot({ path: `/tmp/hlhq-m04-320-${theme}.png`, animations: "disabled" });
      }
      console.log("M04_TARGETS " + JSON.stringify({ viewport: { width, height }, coarse: touch, sizes }));
      await lock.click();
      await expect(lock).toHaveAttribute("aria-pressed", "false");
      await expectDot(lock, "--muted");
      await page.keyboard.press("Escape"); await expect(page.locator(".series-modal")).toHaveCount(0);
    });
  });
}
