import { expect, test } from "@playwright/test";
import {
  credentials, device, json, mockRoster, roster, signIn,
} from "./support/fixtures.mjs";

test.describe.configure({ mode: "serial" });

test("the service worker refreshes the shell online and serves it offline", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await signIn(page);
  await page.evaluate(() => navigator.serviceWorker.ready);
  await page.reload();
  await page.waitForFunction(async () => (await caches.open("hlhq-shell-v6")).keys().then((keys) => keys.length > 0));

  const manifest = await page.evaluate(async () => {
    const cache = await caches.open("hlhq-shell-v6");
    await cache.put("/manifest.webmanifest", new Response("stale shell"));
    const live = await fetch("/manifest.webmanifest").then((response) => response.text());
    const cached = await cache.match("/manifest.webmanifest").then((response) => response.text());
    return { live, cached };
  });
  expect(manifest.live).not.toBe("stale shell");
  expect(manifest.cached).toBe(manifest.live);

  await context.setOffline(true);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator("#auth-screen")).toBeVisible();
  await expect(page.locator("#auth-form")).toBeVisible();
  await context.close();
});
