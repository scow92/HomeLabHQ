import { expect, test } from "@playwright/test";
import { device, json, roster, signIn } from "./support/fixtures.mjs";

const ownerA = { ...device, id: "owner-a-device", name: "Owner A fictional device", host: "192.0.2.41" };
const ownerB = { ...device, id: "owner-b-device", name: "Owner B fictional device", host: "192.0.2.42" };
const member = { username: "session-member", password: "fictional-member-password", role: "member" };

test.beforeEach(async ({ page }) => {
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.exposeFunction("assertNoPageErrors", () => expect(errors).toEqual([]));
});
test.afterEach(async ({ page }) => { await page.evaluate(() => window.assertNoPageErrors()); });

async function loginHere(page, user = member) {
  await page.locator("#auth-user").fill(user.username);
  await page.locator("#auth-pass").fill(user.password);
  await page.locator("#auth-submit").click();
  await expect(page.locator("#whoami")).toContainText(user.username);
  await expect(page.locator("#app")).toBeVisible();
}

async function prepare(page) {
  await page.route("**/api/dashboards", route => json(route, { dashboards: [] }));
  await page.route("**/api/devices", route => json(route, { devices: [ownerA] }));
  await signIn(page);
  const users = await (await page.request.get("/api/users")).json();
  if (!users.users.some(user => user.username === member.username)) {
    expect((await page.request.post("/api/users", { data: member })).ok()).toBeTruthy();
  }
  await expect(page.locator("#devices-list")).toContainText(ownerA.name);
  await page.evaluate(({ oldName, newUser }) => {
    window.crossAccountLeaks = [];
    new MutationObserver(() => {
      if (document.querySelector("#whoami").textContent.includes(newUser) &&
          document.body.textContent.includes(oldName)) window.crossAccountLeaks.push(oldName);
    }).observe(document.body, { childList: true, subtree: true, characterData: true });
  }, { oldName: ownerA.name, newUser: member.username });
}

async function noOwnerA(page) {
  expect(await page.evaluate(() => window.crossAccountLeaks)).toEqual([]);
  // Include hidden DOM, data attributes and form values, not just visible cards.
  const dom = await page.locator("body").evaluate(body => body.outerHTML +
    [...body.querySelectorAll("input, textarea, select")].map(el => el.value).join("\n"));
  for (const value of [ownerA.id, ownerA.name, ownerA.host, "Owner A private detail"]) {
    expect(dom).not.toContain(value);
  }
}

test("logout removes device snapshots and DOM while preserving theme", async ({ page }) => {
  await prepare(page);
  await page.evaluate(async () => (await import("/js/theme.js")).applyTheme("light"));
  await page.locator("#logout-btn").click();
  await expect(page.locator("#auth-screen")).toBeVisible();
  await noOwnerA(page);
  expect(await page.evaluate(async () => (await import("/js/devices.js")).ALL_DEVICES)).toEqual([]);
  expect(await page.evaluate(() => localStorage.getItem("hlhq-theme"))).toBe("light");
});

for (const outcome of ["success", "error", "empty"]) {
  test(`account switch cannot retain A cards during B ${outcome}`, async ({ page }) => {
    await prepare(page);
    await page.locator("#logout-btn").click();
    await expect(page.locator("#auth-screen")).toBeVisible();
    let release;
    const pending = new Promise(resolve => { release = resolve; });
    await page.route("**/api/devices", async route => { await pending; return outcome === "error"
      ? json(route, { error: "Fictional B unavailable" }, 503)
      : json(route, { devices: outcome === "empty" ? [] : [ownerB] }); });
    await loginHere(page);
    await noOwnerA(page);
    release();
    if (outcome === "success") await expect(page.locator("#devices-list")).toContainText(ownerB.name);
    else await expect(page.locator("#devices-empty")).toContainText(
      outcome === "error" ? "Couldn't load devices." : "No devices yet.");
    await noOwnerA(page);
  });
}

for (const transition of ["logout", "replacement", "reauthentication"]) {
  test(`late response is discarded after ${transition} even if transport ignores abort`, async ({ page }) => {
    await prepare(page);
    // Hold body decoding after a successful fetch: AbortController cannot undo
    // an already buffered response. This tests generation rejection separately.
    await page.evaluate(() => {
      const original = window.fetch;
      window.restoreFetch = () => { window.fetch = original; };
      window.fetch = async (path, opts) => {
        if (path === "/api/devices") {
          window.heldSignal = opts.signal;
          return { ok: true, json: () => new Promise(resolve => { window.releaseDeviceBody = resolve; }) };
        }
        return original(path, opts);
      };
      import("/js/devices.js").then(async mod => {
        window.pendingDeviceLoad = mod.loadDevices();
      });
    });
    await page.waitForFunction(() => !!window.releaseDeviceBody);
    await page.route("**/api/devices", route => json(route, { devices: [] }));
    await page.evaluate(async transition => {
      const api = await import("/js/api.js");
      const release = window.releaseDeviceBody;
      window.restoreFetch();
      api.setSession(transition === "logout" ? null : transition === "reauthentication"
        ? { ...api.SESSION } : { id: "fictional-replacement", username: "replacement", role: "member" });
      release({ devices: [{ id: "owner-a-device", name: "Owner A fictional device", host: "192.0.2.41" }] });
      await window.pendingDeviceLoad;
    }, transition);
    await noOwnerA(page);
    expect(await page.evaluate(() => window.heldSignal.aborted)).toBe(true);
  });
}

test("unauthorized device refresh expires the UI session", async ({ page }) => {
  await prepare(page);
  // Revoke the real cookie session outside the UI, then use the real 401 path.
  expect((await page.request.post("/api/logout")).ok()).toBeTruthy();
  await page.unroute("**/api/devices");
  await page.evaluate(async () => (await import("/js/devices.js")).loadDevices());
  await expect(page.locator("#auth-screen")).toBeVisible();
  await noOwnerA(page);
});

test("history cannot restore A device detail after B signs in", async ({ page }) => {
  await prepare(page);
  await page.route("**/api/devices/owner-a-device/detail", route => json(route, {
    device: ownerA, detail: { info: { Identity: "Owner A private detail" } },
  }));
  await page.locator("#devices-list").getByRole("button", { name: "Details" }).click();
  await expect(page.locator("#dm-body")).toContainText("Owner A private detail");
  await page.locator("#device-modal").getByRole("button", { name: "Close", exact: true }).click();
  await page.getByRole("tab", { name: "Access", exact: true }).click();
  await page.locator("#logout-btn").click();
  await page.route("**/api/devices", route => json(route, { devices: [ownerB] }));
  await loginHere(page);
  await page.goBack();
  await expect(page.locator("#devices-list")).toContainText(ownerB.name);
  await noOwnerA(page);
  await page.goForward();
  await noOwnerA(page);
});

test("A body completing after a real logout and B login cannot overwrite B", async ({ page }) => {
  await prepare(page);
  await page.evaluate(() => {
    const original = window.fetch;
    window.fetch = async (path, opts) => {
      if (path !== "/api/devices") return original(path, opts);
      window.fetch = original;
      return { ok: true, json: () => new Promise(resolve => { window.releaseA = resolve; }) };
    };
    import("/js/devices.js").then(mod => { window.oldLoad = mod.loadDevices(); });
  });
  await page.waitForFunction(() => !!window.releaseA);
  await page.locator("#logout-btn").click();
  await expect(page.locator("#auth-screen")).toBeVisible();
  await noOwnerA(page);
  await page.route("**/api/devices", route => json(route, { devices: [ownerB] }));
  await loginHere(page);
  await expect(page.locator("#devices-list")).toContainText(ownerB.name);
  await page.evaluate(async ownerA => {
    window.releaseA({ devices: [ownerA] });
    await window.oldLoad;
  }, ownerA);
  await noOwnerA(page);
  await expect(page.locator("#devices-list")).toContainText(ownerB.name);
});

test("late detail data cannot repopulate a logged-out modal", async ({ page }) => {
  await prepare(page);
  await page.evaluate(() => {
    const original = window.fetch;
    window.detailLoads = [];
    window.releaseDetails = [];
    window.fetch = async (path, opts) => {
      if (!String(path).endsWith("/detail")) return original(path, opts);
      return { ok: true, json: () => new Promise(resolve => window.releaseDetails.push(resolve)) };
    };
  });
  await page.evaluate(async ownerA => {
    const detail = await import("/js/detail/index.js");
    window.detailLoads.push(detail.openDevice(ownerA));
  }, ownerA);
  await page.waitForFunction(() => window.releaseDetails.length > 0);
  await expect(page.locator("#dm-body .skeleton-line").first()).toBeVisible();
  await page.locator("#logout-btn").dispatchEvent("click");
  await expect(page.locator("#auth-screen")).toBeVisible();
  await page.evaluate(async ownerA => {
    for (const release of window.releaseDetails) release({ device: ownerA,
      detail: { info: { Identity: "Owner A private detail" } } });
    await Promise.all(window.detailLoads);
  }, ownerA);
  await noOwnerA(page);
  await expect(page.locator("#device-modal")).toBeHidden();
});

test("a stale 401 cannot expire a replacement session", async ({ page }) => {
  await prepare(page);
  await page.evaluate(() => {
    const original = window.fetch;
    window.fetch = async (path, opts) => {
      if (path !== "/api/devices") return original(path, opts);
      window.fetch = original;
      return new Promise(resolve => {
        window.release401 = () => resolve({ ok: false, status: 401,
          json: async () => ({ error: "unauthenticated" }) });
      });
    };
    import("/js/devices.js").then(mod => { window.oldLoad = mod.loadDevices(); });
  });
  await page.waitForFunction(() => !!window.release401);
  await page.locator("#logout-btn").click();
  await expect(page.locator("#auth-screen")).toBeVisible();
  await page.route("**/api/devices", route => json(route, { devices: [ownerB] }));
  await loginHere(page);
  await page.evaluate(async () => { window.release401({ error: "unauthenticated" }); await window.oldLoad; });
  await expect(page.locator("#app")).toBeVisible();
  await expect(page.locator("#whoami")).toContainText(member.username);
  await noOwnerA(page);
});

test("same-account refresh and polling work and logout stops private reads", async ({ page }) => {
  await page.clock.install();
  await page.clock.pauseAt(new Date());
  await prepare(page);
  let reads = 0;
  await page.route("**/api/devices", route => {
    reads += 1;
    return json(route, { devices: [{ ...ownerA, name: `Fictional refresh ${reads}` }] });
  });
  await page.clock.runFor(15001);
  await expect(page.locator("#devices-list")).toContainText("Fictional refresh 1");
  await page.route("**/api/devices/owner-a-device/state", route => json(route, { online: true, values: { cpu: 37 } }));
  await page.locator("#devices-list").getByRole("button", { name: "Sync now", exact: true }).click();
  await expect(page.locator("#devices-list")).toContainText("updated just now");
  await page.clock.runFor(15001);
  await expect(page.locator("#devices-list")).toContainText("Fictional refresh 2");
  await page.locator("#logout-btn").click();
  await expect(page.locator("#auth-screen")).toBeVisible();
  await page.clock.runFor(60001);
  expect(reads).toBe(2);
  await noOwnerA(page);
});

test("page-cache restoration revalidates authentication before restoring device data", async ({ page }) => {
  await prepare(page);
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: true })));
  await noOwnerA(page);
  expect((await page.request.post("/api/logout")).ok()).toBeTruthy();
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true })));
  await expect(page.locator("#auth-screen")).toBeVisible();
  await noOwnerA(page);
});

test("device references in other feature caches and drafts are cleared at logout", async ({ page }) => {
  await prepare(page);
  await page.route("**/api/clients", route => json(route, { ...roster,
    clients: [{ ...roster.clients[0], hostname: ownerA.name, ip: ownerA.host }] }));
  await page.getByRole("tab", { name: "Access", exact: true }).click();
  await expect(page.locator("#clients-body")).toContainText(ownerA.name);
  await page.locator("#clients-sort").selectOption("ip");
  await page.locator("#clients-search").fill(ownerA.host);
  await page.route("**/api/compute", route => json(route, {
    instances: [], hosts: [{ id: "fictional-host", parentDevice: ownerA }], ansibleEnabled: false,
  }));
  await page.getByRole("tab", { name: "Compute", exact: true }).click();
  await expect(page.locator("#compute-list")).toContainText(ownerA.name);
  await page.route("**/api/logs", route => json(route, { logs: [{ message: ownerA.name, source: "fixture" }] }));
  await page.getByRole("tab", { name: "Logs", exact: true }).click();
  await expect(page.locator("#logs-table")).toContainText(ownerA.name);
  await page.getByRole("tab", { name: "Settings", exact: true }).click();
  await page.locator("#ans-host").fill(ownerA.host);
  await page.getByRole("tab", { name: "Add device", exact: true }).click();
  await page.locator("#wiz-host").fill(ownerA.host);
  await page.locator("#logout-btn").click();
  await expect(page.locator("#auth-screen")).toBeVisible();
  await noOwnerA(page);
  expect(await page.evaluate(async () => (await import("/js/clients/store.js")).getClients())).toBeNull();
  expect(await page.evaluate(() => localStorage.getItem("hlhq-clients-sort"))).toBe("ip");
  await expect(page.locator("#clients-sort")).toHaveValue("ip");
});

test("401 headers clear protected UI without waiting for an error body", async ({ page }) => {
  await prepare(page);
  await page.evaluate(async () => {
    const original = window.fetch;
    window.readUnauthorizedBody = false;
    window.fetch = async (path, opts) => path === "/api/devices"
      ? { status: 401, ok: false, json: () => {
        window.readUnauthorizedBody = true;
        return new Promise(() => {});
      } } : original(path, opts);
    await (await import("/js/devices.js")).loadDevices();
  });
  await expect(page.locator("#auth-screen")).toBeVisible();
  await noOwnerA(page);
  expect(await page.evaluate(() => window.readUnauthorizedBody)).toBe(false);
});

test("logout clears immediately and serializes login behind the logout response", async ({ page }) => {
  await prepare(page);
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  await page.route("**/api/logout", async route => {
    const response = await route.fetch();
    await pending;
    await route.fulfill({ response });
  });
  await page.locator("#logout-btn").click();
  await expect(page.locator("#auth-screen")).toBeVisible();
  await expect(page.locator("#auth-submit")).toBeDisabled();
  await noOwnerA(page);
  release();
  await expect(page.locator("#auth-submit")).toBeEnabled();
  await page.route("**/api/devices", route => json(route, { devices: [ownerB] }));
  await loginHere(page);
  await expect(page.locator("#devices-list")).toContainText(ownerB.name);
  await noOwnerA(page);
});
