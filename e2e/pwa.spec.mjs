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
  await page.waitForFunction(async () => (await caches.open("hlhq-shell-v9")).keys().then((keys) => keys.length > 0));

  const cachedPaths = await page.evaluate(async () => {
    const cache = await caches.open("hlhq-shell-v9");
    return (await cache.keys()).map((request) => new URL(request.url).pathname);
  });
  expect(cachedPaths).toEqual(expect.arrayContaining([
    "/styles/base.css", "/styles/components.css", "/styles/views.css",
    "/js/app.js", "/js/router.js",
  ]));

  const manifest = await page.evaluate(async () => {
    const cache = await caches.open("hlhq-shell-v9");
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
  const offlineStyles = await page.evaluate(async () => Promise.all(
    [...document.querySelectorAll('link[rel="stylesheet"]')]
      .map(async (link) => ({ href: link.getAttribute("href"), ok: (await fetch(link.href)).ok })),
  ));
  expect(offlineStyles).toEqual([
    { href: "/styles/base.css", ok: true },
    { href: "/styles/components.css", ok: true },
    { href: "/styles/views.css", ok: true },
  ]);
  await context.close();
});
