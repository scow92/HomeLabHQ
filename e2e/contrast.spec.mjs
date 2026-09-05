import { expect, test } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { signIn } from "./support/fixtures.mjs";

test("M03 text pairs across themes, surfaces and severity consumers", async ({ page }) => {
  await signIn(page);
  const results = [];
  for (const theme of ["light", "dark", "auto"]) {
    await page.emulateMedia({ colorScheme: "light" });
    await page.evaluate(async theme => {
      (await import("/js/theme.js")).applyTheme(theme);
      document.querySelector("#contrast-fixture")?.remove();
      const fixture = document.createElement("section"); fixture.id = "contrast-fixture";
      fixture.style.cssText = "padding:16px;display:flex;gap:12px;flex-wrap:wrap";
      document.querySelector('[data-panel="devices"]').prepend(fixture);
      for (const surface of ["--bg", "--bg-elev", "--bg-elev-2"]) {
        const panel = document.createElement("div"); panel.style.cssText = `padding:16px;background:var(${surface});display:grid;gap:12px`;
        for (const [cls, label] of [["c-range-btn active", "24h"], ["client-rssi sev-good", "Connected"], ["client-rssi sev-warn", "Attention"],
          ["client-rssi sev-bad", "Failed"], ["compute-status compute-status-good", "✓ Running"], ["compute-status compute-status-warn", "! Unknown"],
          ["compute-status compute-status-bad", "! Failed"], ["muted", "Last observed"], ["btn btn-primary", "Save"], ["btn btn-danger", "Remove"], ["dash-tab active", "All"]]) {
          const el = document.createElement(cls.includes("btn") ? "button" : "span"); el.className = cls; el.textContent = label;
          el.dataset.surface = surface; panel.append(el);
        }
        fixture.append(panel);
      }
    }, theme);
    await page.locator("#contrast-fixture .c-range-btn").first().focus();
    const pairs = await page.evaluate(() => {
      const ctx = document.createElement("canvas").getContext("2d");
      const rgba = css => { ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = css; ctx.fillRect(0, 0, 1, 1); return [...ctx.getImageData(0, 0, 1, 1).data].map(n => n / 255); };
      const over = (fg, bg) => fg.slice(0, 3).map((v, i) => v * fg[3] + bg[i] * (1 - fg[3])).concat(1);
      const background = el => !el ? [1, 1, 1, 1] : over(rgba(getComputedStyle(el).backgroundColor), background(el.parentElement));
      const lum = rgb => rgb.slice(0, 3).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4).reduce((n, v, i) => n + v * [.2126, .7152, .0722][i], 0);
      const pairs = [...document.querySelectorAll("#contrast-fixture [data-surface]")].map(el => {
        const bg = background(el), fg = over(rgba(getComputedStyle(el).color), bg);
        const [a, b] = [lum(fg), lum(bg)].sort((a, b) => b - a);
        return { class: el.className, surface: el.dataset.surface, ratio: (a + .05) / (b + .05), minimum: 4.5 };
      });
      const control = document.querySelector("#contrast-fixture .c-range-btn");
      for (const property of ["outlineColor", "borderTopColor"]) {
        const fg = rgba(getComputedStyle(control)[property]), bg = background(control.parentElement);
        const [a, b] = [lum(fg), lum(bg)].sort((a, b) => b - a);
        pairs.push({ class: property, surface: "control surrounding surface", ratio: (a + .05) / (b + .05), minimum: 3 });
      }
      return pairs;
    });
    results.push({ theme, pairs });
    if (theme !== "auto") await page.screenshot({ path: `/tmp/hlhq-m03-${theme}.png` });
    for (const pair of pairs) expect(pair.ratio, `${theme} ${pair.class} on ${pair.surface}`).toBeGreaterThanOrEqual(pair.minimum);
    for (const width of [768, 390]) {
      await page.setViewportSize({ width, height: 900 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
    await page.setViewportSize({ width: 1440, height: 900 });
  }
  await test.info().attach("contrast-pairs", { body: JSON.stringify(results), contentType: "application/json" });
  writeFileSync("/tmp/hlhq-m03-pairs.json", JSON.stringify(results, null, 2));
});
