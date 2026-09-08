import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { device, json, mockRoster, signIn } from "./support/fixtures.mjs";

test.use({ serviceWorkers: "block" });
const devices = [0, 1, 2].map(i => ({ ...device, id: `fictional-${i}`, name: `Fictional device ${i}`, order: i }));
const card = (page, id = 0) => page.locator(`[data-device-id="fictional-${id}"]`);
const order = page => page.locator("#devices-list > .card").evaluateAll(els => els.map(el => el.dataset.deviceId));

async function prepare(page) {
  if (process.env.HLHQ_BASELINE) {
    for (const file of ["js/devices.js", "styles/views.css"]) {
      const body = execFileSync("git", ["show", `${process.env.HLHQ_BASELINE}:web/${file}`], { encoding: "utf8" });
      await page.route(`**/${file}`, route => route.fulfill({ contentType: file.endsWith("css") ? "text/css" : "text/javascript", body }));
    }
  }
  await page.clock.install({ time: new Date("2026-09-08T12:00:00Z") });
  await page.clock.pauseAt(new Date("2026-09-08T12:00:00Z"));
  await page.addInitScript(() => {
    window.gestureTimers = new Set(); window.gestureSignals = [];
    const timeout = window.setTimeout, clear = window.clearTimeout;
    window.setTimeout = (fn, ms, ...args) => {
      const id = timeout(() => { window.gestureTimers.delete(id); fn(...args); }, ms);
      if (ms === 450 && new Error().stack.includes("/js/devices.js")) window.gestureTimers.add(id);
      return id;
    };
    window.clearTimeout = id => { window.gestureTimers.delete(id); clear(id); };
    const add = window.addEventListener.bind(window);
    window.addEventListener = (type, fn, options) => {
      if (type === "blur" && options?.signal) window.gestureSignals.push(options.signal);
      add(type, fn, options);
    };
  });
  await mockRoster(page);
  await page.route("**/api/dashboards", route => json(route, { dashboards: [] }));
  let savedDevices = devices.map(d => ({ ...d }));
  await page.route("**/api/devices", route => json(route, { devices: savedDevices }));
  const writes = [];
  await page.route("**/api/devices/reorder", route => {
    const ids = route.request().postDataJSON().ids;
    writes.push(ids);
    savedDevices = ids.map(id => savedDevices.find(d => d.id === id));
    return json(route, { ok: true });
  });
  await signIn(page);
  await expect(card(page)).toBeVisible();
  await expect(page.locator("#devices-refresh-state")).toHaveAttribute("data-state", "current");
  await expect(page.locator("#dashboards-refresh-state")).toHaveAttribute("data-state", "current");
  await page.clock.runFor(16); // deliver the router's queued heading-focus frame
  await expect(page.getByRole("heading", { name: "Devices", exact: true })).toBeFocused();
  await page.evaluate(() => {
    window.openedDevices = [];
    document.addEventListener("hlhq:open-device", e => { window.openedDevices.push(e.detail.id); });
  });
  return writes;
}

async function expectNoGestureWork(page) {
  expect(await page.evaluate(() => ({ timers: window.gestureTimers.size, listeners: window.gestureSignals.filter(signal => !signal.aborted).length })))
    .toEqual({ timers: 0, listeners: 0 });
}

async function touch(page, type, id = 0, point) {
  const el = card(page, id);
  const box = await el.boundingBox();
  await el.dispatchEvent(type, { touches: type === "touchend" || type === "touchcancel" ? [] : [{ identifier: 1, clientX: point?.x ?? box.x + 25, clientY: point?.y ?? box.y + 25 }] });
}
async function hold(page) {
  await touch(page, "touchstart");
  await page.clock.runFor(449);
  await expect(card(page)).not.toHaveClass(/dragging/);
  await page.clock.runFor(1);
  await expect(card(page)).toHaveClass(/dragging/);
}
async function moveLast(page) {
  const box = await card(page, 2).boundingBox();
  await touch(page, "touchmove", 0, { x: box.x + box.width - 5, y: box.y + box.height - 5 });
}

test.describe("touch gestures", () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  test("long press deliberately reorders once and guards activation", async ({ page }) => {
    const writes = await prepare(page);
    await hold(page); await moveLast(page);
    await expect.poll(() => order(page)).toEqual(["fictional-1", "fictional-2", "fictional-0"]);
    await touch(page, "touchend"); await touch(page, "touchend");
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0]).toEqual(["fictional-1", "fictional-2", "fictional-0"]);
    await card(page).dispatchEvent("click");
    expect(await page.evaluate(() => window.openedDevices)).toEqual([]);
    await expect(card(page)).not.toHaveClass(/dragging/);
    await expectNoGestureWork(page);
    await page.reload();
    await expect.poll(() => order(page)).toEqual(writes[0]);
    expect(writes).toHaveLength(1);
  });
});

test.describe("gesture cancellation", () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  for (const interruption of ["movement", "scroll", "touchcancel", "pointercancel", "lostpointercapture", "blur", "navigation", "account", "filter", "multitouch", "escape"]) {
    test(`${interruption} cancels pending and active gestures without saving`, async ({ page }) => {
      const writes = await prepare(page);
      const errors = []; page.on("pageerror", error => errors.push(error.message));
      for (const active of [false, true]) {
        await touch(page, "touchstart");
        if (active) { await page.clock.runFor(450); await moveLast(page); }
        if (interruption === "movement") {
          if (active) await touch(page, "touchcancel");
          else { const box = await card(page).boundingBox(); await touch(page, "touchmove", 0, { x: box.x + 36, y: box.y + 25 }); }
        } else if (interruption === "scroll") {
          if (active) await touch(page, "touchcancel");
          else await page.evaluate(() => window.dispatchEvent(new Event("scroll")));
        } else if (interruption === "escape") await card(page).dispatchEvent("keydown", { key: "Escape" });
        else if (interruption === "blur") await page.evaluate(() => window.dispatchEvent(new Event("blur")));
        else if (interruption === "navigation") {
          await page.getByRole("tab", { name: /^Access/ }).click();
          await page.getByRole("tab", { name: "Devices", exact: true }).click();
        } else if (interruption === "account") {
          await page.evaluate(async () => {
            const api = await import("/js/api.js");
            api.setSession({ id: "fictional-next-account", username: "Fictional replacement", role: "admin" });
          });
          await page.evaluate(async () => (await import("/js/devices.js")).activateDevices());
          await expect(card(page)).toBeVisible();
        } else if (interruption === "filter") {
          await page.locator("#dev-search-input").fill("Fictional");
          await page.locator("#dev-search-input").fill("");
        } else if (interruption === "multitouch") await card(page).dispatchEvent("touchstart", { touches: [{ identifier: 1, clientX: 10, clientY: 10 }, { identifier: 2, clientX: 20, clientY: 20 }] });
        else await card(page).dispatchEvent(interruption);
        await page.clock.runFor(1000);
        await touch(page, "touchend");
        await expect(card(page)).not.toHaveClass(/dragging/);
        expect(await order(page)).toEqual(["fictional-0", "fictional-1", "fictional-2"]);
        expect(writes).toEqual([]);
        await expectNoGestureWork(page);
      }
      // Cleanup must release the render guard as well as the visual state.
      await page.evaluate(async () => (await import("/js/devices.js")).renderDeviceList());
      await expect(card(page)).toBeVisible();
      expect(errors).toEqual([]);
    });
  }
  test("real touch tap opens details; long press through Chromium touch input reorders", async ({ page, context }) => {
    const writes = await prepare(page);
    await page.route("**/api/devices/fictional-0/detail", route => json(route, { device: devices[0], detail: {} }));
    await card(page).evaluate(el => el.scrollIntoView({ block: "start" }));
    await page.clock.runFor(32); // deliver the scroll and two rendering frames before touching
    await expect.poll(async () => (await card(page, 2).boundingBox()).y).toBeLessThan(844);
    const cdp = await context.newCDPSession(page);
    const box = await card(page).boundingBox();
    await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: box.x + 25, y: box.y + 25 }] });
    await page.clock.runFor(450);
    await expect(card(page)).toHaveClass(/dragging/);
    const end = await card(page, 2).boundingBox();
    await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: end.x + end.width - 5, y: end.y + end.height - 5 }] });
    await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await expect.poll(() => writes.length).toBe(1);
    await page.clock.runFor(801);
    await card(page).tap({ position: { x: 25, y: 25 } });
    await expect.poll(() => page.evaluate(() => window.openedDevices)).toEqual(["fictional-0"]);
  });
  test("hold without a move does not save; controls never initiate a hold", async ({ page }) => {
    const writes = await prepare(page);
    await hold(page); await touch(page, "touchend");
    expect(writes).toEqual([]);
    await card(page).getByRole("button", { name: "Move down" }).dispatchEvent("touchstart", { touches: [{ identifier: 1, clientX: 20, clientY: 20 }] });
    await page.clock.runFor(450);
    await expect(card(page)).not.toHaveClass(/dragging/);
    expect(writes).toEqual([]);
  });
});

test("native mouse drag saves only a dropped order and suppresses follow-on actions", async ({ page }) => {
  const writes = await prepare(page);
  const start = await card(page).boundingBox(), end = await card(page, 2).boundingBox();
  await page.mouse.move(start.x + 25, start.y + 25); await page.mouse.down();
  await page.mouse.move(end.x + end.width - 5, end.y + end.height - 5, { steps: 10 });
  await expect(card(page)).toHaveClass(/dragging/);
  await page.mouse.up();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toEqual(["fictional-1", "fictional-2", "fictional-0"]);
  await card(page).getByRole("button", { name: "Details", exact: true }).dispatchEvent("click");
  expect(await page.evaluate(() => window.openedDevices)).toEqual([]);
  // A second native drag ending without a drop rolls back its transient order.
  await page.clock.runFor(801);
  const transfer = await page.evaluateHandle(() => new DataTransfer());
  await card(page).dispatchEvent("dragstart", { dataTransfer: transfer });
  await page.locator("#devices-list").dispatchEvent("dragover", { dataTransfer: transfer, clientX: start.x, clientY: start.y });
  await card(page).dispatchEvent("dragend", { dataTransfer: transfer });
  expect(await order(page)).toEqual(writes[0]);
  expect(writes).toHaveLength(1);
});

for (const [width, height, touchInput] of [[1440, 900, false], [768, 1024, true], [390, 844, true], [320, 740, true]]) {
  test.describe(`device controls ${width}`, () => {
    test.use({ viewport: { width, height }, isMobile: touchInput, hasTouch: touchInput });
    test("keyboard buttons, focus, long titles and successful refresh remain usable", async ({ page }) => {
      const writes = await prepare(page);
      await expect(card(page).getByRole("button", { name: "Move up" })).toBeDisabled();
      const down = card(page).getByRole("button", { name: "Move down" });
      await down.focus(); await page.keyboard.press("Enter");
      await expect.poll(() => writes.length).toBe(1);
      await expect(down).toBeFocused();
      expect(await down.evaluate(el => getComputedStyle(el).outlineStyle)).not.toBe("none");
      await card(page).focus(); await page.keyboard.press("Alt+ArrowUp");
      await expect.poll(() => writes.length).toBe(2);
      await expect(card(page)).toBeFocused();
      await down.focus(); await page.keyboard.press("Space");
      await expect.poll(() => writes.length).toBe(3);
      await page.evaluate(async () => {
        const mod = await import("/js/devices.js");
        mod.ALL_DEVICES[0].name = "Fictional-long-device-title-without-breaks-012345678901234567890123456789";
        mod.renderDeviceList();
      });
      for (const status of ["#devices-refresh-state", "#dashboards-refresh-state"]) {
        await expect(page.locator(status)).toHaveAttribute("data-state", "current");
        await expect(page.locator(status)).toBeVisible();
      }
      if (touchInput) for (const button of await card(page).locator("button:visible").all()) {
        const size = await button.boundingBox();
        expect(size.width).toBeGreaterThanOrEqual(44); expect(size.height).toBeGreaterThanOrEqual(44);
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      // Unrelated form controls retain their original padding and touch contract.
      await page.getByRole("tab", { name: "Add device", exact: true }).click();
      await expect(page.locator("#wiz-host")).toHaveCSS("padding-top", "10px");
      await expect(page.locator("#wiz-detect")).toHaveCSS("padding-top", "9px");
    });
  });
}

for (const active of [false, true]) {
  test(`pagehide disposes ${active ? "active drag" : "pending hold"} before detached events`, async ({ page }) => {
    const writes = await prepare(page);
    await touch(page, "touchstart");
    if (active) { await page.clock.runFor(450); await moveLast(page); }
    await page.evaluate(() => {
      window.detachedDevice = document.querySelector('[data-device-id="fictional-0"]');
      window.dispatchEvent(new PageTransitionEvent("pagehide"));
    });
    await page.clock.runFor(1000);
    await page.evaluate(() => window.detachedDevice.dispatchEvent(new TouchEvent("touchend")));
    await expect(page.locator("#devices-list")).toBeEmpty();
    await expectNoGestureWork(page);
    expect(writes).toEqual([]);
  });
}
