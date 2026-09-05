import { expect, test } from "@playwright/test";
import { device, json, signIn } from "./support/fixtures.mjs";

test.beforeEach(async ({ page }) => {
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.exposeFunction("checkDetailErrors", () => expect(errors).toEqual([]));
});
test.afterEach(async ({ page }) => { await page.evaluate(() => window.checkDetailErrors()); });

const resources = ["a", "b"].map((id, index) => ({
  ...device, id: `fictional-${id}`, name: `Fictional ${id.toUpperCase()}`,
  host: `192.0.2.${51 + index}`, type: "vm", provider: "proxmox",
  providerInstanceId: `${101 + index}`, status: "running", os: `${id} initial`,
}));
const views = [
  { kind: "device", module: "/js/detail/index.js", open: "openDevice", close: "closeDevice", prefix: "dm", modal: "device-modal" },
  { kind: "compute", module: "/js/compute.js", open: "openCompute", close: "closeCompute", prefix: "cm", modal: "compute-modal" },
];

async function prepare(page, view) {
  await page.route("**/api/devices", route => json(route, { devices: resources }));
  await page.route("**/api/dashboards", route => json(route, { dashboards: [] }));
  await page.route("**/api/compute", route => json(route, { instances: resources }));
  await page.route("**/api/settings/ansible", route => json(route, { controller: null }));
  await page.route("**/api/compute/*/jobs", route => json(route, { jobs: [] }));
  await signIn(page);
  await page.evaluate(view => {
    window.detailCalls = [];
    const original = window.fetch;
    window.fetch = async (path, opts) => {
      if (view.kind === "device" ? /^\/api\/devices\/[^/]+\/detail$/.test(path)
        : /^\/api\/compute\/[^/]+$/.test(path)) {
        // Hold body decoding; deliberately complete despite cancellation.
        const call = { path, signal: opts.signal };
        window.detailCalls.push(call);
        const response = { ok: true, json: () => new Promise((resolve, reject) => {
          call.resolve = resolve; call.reject = reject;
        }) };
        call.response = response;
        return response;
      }
      return original(path, opts);
    };
  }, view);
}
async function open(page, view, index) {
  await page.evaluate(async ({ view, resource }) => {
    const mod = await import(view.module);
    // Match the selected URL before opening, so baseline routing cannot add
    // accidental duplicate requests to the response-ordering scenarios.
    history.pushState(null, "", `#/${view.kind}/${encodeURIComponent(resource.id)}`);
    window.openPromises ||= [];
    window.openPromises[window.detailCalls.length] = mod[view.open](resource);
  }, { view, resource: resources[index] });
}
async function count(page, n) {
  await page.waitForFunction(n => window.detailCalls.length >= n && !!window.detailCalls[n - 1].resolve, n);
}
async function release(page, view, call, resource, value, failure = false) {
  await page.evaluate(async ({ view, call, resource, value, failure }) => {
    const pending = window.detailCalls[call];
    if (failure) { pending.response.ok = false; pending.response.status = 503; pending.resolve({ error: value }); }
    else pending.resolve(view.kind === "device"
      ? { device: resource, detail: { info: { Identity: value } }, actions: [{ name: "identify", label: "Identify" }] }
      : { instance: { ...resource, os: value } });
    // Drain the API/coordinator continuations, including stale rejection paths.
    await window.openPromises?.[call];
    await new Promise(resolve => {
      const channel = new MessageChannel();
      channel.port1.onmessage = () => { channel.port1.close(); channel.port2.close(); resolve(); };
      channel.port2.postMessage(null);
    });
  }, { view, call, resource: resources[resource], value, failure });
}
async function current(page, view, index, value, failure = false) {
  await expect(page).toHaveURL(new RegExp(`#/${view.kind}/${resources[index].id}$`));
  await expect(page.locator(`#${view.modal}`)).toBeVisible();
  await expect(page.locator(`#${view.prefix}-title`)).toHaveText(resources[index].name);
  if (failure && view.kind === "device") {
    await expect(page.locator("#detail-refresh-state")).toHaveAttribute("data-state", "error");
    await expect(page.locator("#detail-refresh-state")).toContainText("Service unavailable");
  } else await expect(page.locator(`#${view.prefix}-body`)).toContainText(value);
  await expect(page.locator(`#${view.prefix}-body`)).not.toContainText("obsolete");
  await expect(page.locator("#toasts")).not.toContainText("obsolete");
}
for (const view of views) {
  for (const [oldFails, newFails] of [[false, false], [true, false], [false, true]]) {
    test(`${view.kind}: late ${oldFails ? "failure" : "success"} after current ${newFails ? "failure" : "success"}`, async ({ page }) => {
      await prepare(page, view);
      await open(page, view, 0); await count(page, 1);
      await open(page, view, 1); await count(page, 2);
      await release(page, view, 1, 1, "current result", newFails);
      await release(page, view, 0, 0, "obsolete result", oldFails);
      await current(page, view, 1, "current result", newFails);
    });
  }
  test(`${view.kind}: newest refresh owns the same resource`, async ({ page }) => {
    await prepare(page, view);
    await open(page, view, 0); await count(page, 1);
    await open(page, view, 0); await count(page, 2);
    await release(page, view, 1, 0, "current refresh");
    await release(page, view, 0, 0, "obsolete refresh");
    await current(page, view, 0, "current refresh");
  });
  test(`${view.kind}: close rejects late responses and reopening owns fresh DOM`, async ({ page }) => {
    await prepare(page, view);
    await open(page, view, 0); await count(page, 1);
    await page.evaluate(async view => (await import(view.module))[view.close](), view);
    const before = await page.locator(`#${view.prefix}-body`).innerHTML();
    await release(page, view, 0, 0, "obsolete closed");
    await expect(page.locator(`#${view.modal}`)).toBeHidden();
    expect(await page.locator(`#${view.prefix}-body`).innerHTML()).toBe(before);
    await open(page, view, 1); await count(page, 2);
    await release(page, view, 1, 1, "current reopened");
    await current(page, view, 1, "current reopened");
  });
}

for (const view of views) {
  test(`${view.kind}: abort cannot clear newer loading state or show a failure`, async ({ page }) => {
    await prepare(page, view);
    await open(page, view, 0); await count(page, 1);
    await open(page, view, 1); await count(page, 2);
    expect(await page.evaluate(() => window.detailCalls[0].signal.aborted)).toBe(true);
    await page.evaluate(async () => {
      window.detailCalls[0].reject(new DOMException("Aborted", "AbortError"));
      await window.openPromises[0];
    });
    await expect(page.locator(`#${view.prefix}-body`)).toHaveAttribute("aria-busy", "true");
    await expect(page.locator(`#${view.prefix}-body`)).not.toContainText("Couldn't");
    await expect(page.locator("#toasts")).toBeEmpty();
    await release(page, view, 1, 1, "current after abort");
    await current(page, view, 1, "current after abort");
    await expect(page.locator(`#${view.prefix}-body`)).not.toHaveAttribute("aria-busy", "true");
  });
  test(`${view.kind}: view and session invalidation reject buffered results`, async ({ page }) => {
    await prepare(page, view);
    await open(page, view, 0); await count(page, 1);
    await open(page, view, 1); await count(page, 2);
    await page.evaluate(async () => (await import("/js/api.js")).setSession(null));
    await release(page, view, 1, 1, "obsolete session B");
    await release(page, view, 0, 0, "obsolete session A", true);
    await expect(page.locator("#auth-screen")).toBeVisible();
    await expect(page.locator(`#${view.modal}`)).toBeHidden();
    await expect(page.locator(`#${view.prefix}-body`)).toBeEmpty();
    await expect(page.locator("#toasts")).toBeEmpty();
  });
  test(`${view.kind}: back and forward keep route, title and DOM together`, async ({ page }) => {
    await prepare(page, view);
    await page.evaluate(({ view, resource }) => {
      document.dispatchEvent(new CustomEvent(`hlhq:open-${view.kind}`, { detail: resource }));
    }, { view, resource: resources[0] });
    await count(page, 1);
    await page.evaluate(({ view, resource }) => {
      document.dispatchEvent(new CustomEvent(`hlhq:open-${view.kind}`, { detail: resource }));
    }, { view, resource: resources[1] });
    await count(page, 2);
    await page.goBack(); await count(page, 3);
    await page.goForward(); await count(page, 4);
    await release(page, view, 3, 1, "current forward");
    await current(page, view, 1, "current forward");
    await release(page, view, 2, 0, "obsolete back");
    await release(page, view, 1, 1, "obsolete original B");
    await release(page, view, 0, 0, "obsolete original A");
    await current(page, view, 1, "current forward");
    expect(await page.evaluate(() => window.detailCalls.length)).toBe(4);
    await expect(page.getByRole("tab", { name: view.kind === "device" ? "Devices" : "Compute", exact: true })).toHaveAttribute("aria-selected", "true");
    await page.getByRole("tab", { name: "Access", exact: true }).evaluate(el => el.click());
    await expect(page).toHaveURL(/#\/access$/);
    await expect(page.locator(`#${view.modal}`)).toBeHidden();
  });
  test(`${view.kind}: detached body cannot receive a late result`, async ({ page }) => {
    await prepare(page, view);
    await open(page, view, 0); await count(page, 1);
    await page.evaluate(prefix => {
      window.oldDetailBody = document.querySelector(`#${prefix}-body`);
      window.oldDetailBody.replaceWith(window.oldDetailBody.cloneNode(false));
    }, view.prefix);
    const before = await page.evaluate(() => window.oldDetailBody.innerHTML);
    await release(page, view, 0, 0, "obsolete detached");
    expect(await page.evaluate(() => window.oldDetailBody.innerHTML)).toBe(before);
    await expect(page.locator(`#${view.prefix}-body`)).toBeEmpty();
    await open(page, view, 1); await count(page, 2);
    await release(page, view, 1, 1, "current replacement DOM");
    await current(page, view, 1, "current replacement DOM");
  });
  test(`${view.kind}: delayed route lookup cannot reopen an obsolete selection`, async ({ page }) => {
    await prepare(page, view);
    await page.evaluate(view => {
      const original = window.fetch;
      window.fetch = async (path, opts) => {
        if (path === (view.kind === "device" ? "/api/devices" : "/api/compute")) {
          window.fetch = original;
          return { ok: true, json: () => new Promise(resolve => { window.releaseLookup = resolve; }) };
        }
        return original(path, opts);
      };
      location.hash = `#/${view.kind}/fictional-a`;
    }, view);
    await page.waitForFunction(() => !!window.releaseLookup);
    await page.evaluate(view => { location.hash = `#/${view.kind}/fictional-b`; }, view);
    await count(page, 1);
    await release(page, view, 0, 1, "current lookup B");
    await current(page, view, 1, "current lookup B");
    await page.evaluate(({ view, resources }) => window.releaseLookup(
      view.kind === "device" ? { devices: resources } : { instances: resources }), { view, resources });
    // A browser task barrier delivers both history events without a sleep.
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
    await current(page, view, 1, "current lookup B");
    expect(await page.evaluate(() => window.detailCalls.length)).toBe(1);
  });
}

test("device: visible action targets the winning resource", async ({ page }) => {
  const view = views[0]; await prepare(page, view);
  await open(page, view, 0); await count(page, 1);
  await open(page, view, 1); await count(page, 2);
  await release(page, view, 1, 1, "current action");
  await release(page, view, 0, 0, "obsolete action");
  let target;
  await page.route("**/api/devices/*/action", route => {
    target = new URL(route.request().url()).pathname;
    return json(route, { message: "Fictional identify complete" });
  });
  await page.getByRole("button", { name: "Identify", exact: true }).click();
  await expect(page.locator("#toasts")).toContainText("Fictional identify complete");
  expect(target).toBe("/api/devices/fictional-b/action");
});

async function holdReads(page, pattern) {
  await page.evaluate(pattern => {
    window.heldReads = [];
    const original = window.fetch;
    window.fetch = async (path, opts) => {
      if (!new RegExp(pattern).test(path)) return original(path, opts);
      const held = { path, signal: opts.signal };
      window.heldReads.push(held);
      const response = { ok: true, json: () => new Promise(resolve => { held.resolve = resolve; }) };
      held.response = response;
      return response;
    };
  }, pattern);
}
async function heldCount(page, count) {
  await page.waitForFunction(count => window.heldReads.length === count && !!window.heldReads[count - 1].resolve, count);
}
async function releaseHeld(page, index, data, failure = false) {
  await page.evaluate(({ index, data, failure }) => {
    const held = window.heldReads[index];
    if (failure) { held.response.ok = false; held.response.status = 503; }
    held.resolve(data);
  }, { index, data, failure });
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
}

for (const phase of ["settings", "history"]) {
  for (const outcome of ["success", "failure", "empty"]) {
    test(`compute: obsolete ${phase} ${outcome} cannot append to the current body`, async ({ page }) => {
      const view = views[1]; await prepare(page, view);
      await holdReads(page, phase === "settings" ? "^/api/settings/ansible$" : "^/api/compute/[^/]+/jobs$");
      await open(page, view, 0); await count(page, 1);
      await page.evaluate(resource => window.detailCalls[0].resolve({ instance: resource }), resources[0]);
      await heldCount(page, 1);
      await open(page, view, 1); await count(page, 2);
      await page.evaluate(resource => window.detailCalls[1].resolve({ instance: resource }), resources[1]);
      await heldCount(page, 2);
      await releaseHeld(page, 1, phase === "settings" ? { controller: null } : { jobs: [] });
      await page.evaluate(() => window.openPromises[1]);
      const before = await page.locator("#cm-body").innerHTML();
      await releaseHeld(page, 0, outcome === "failure" ? { error: "obsolete secondary" }
        : phase === "settings" ? { controller: { inventory: { hosts: [{ name: "obsolete host" }] } } }
          : { jobs: outcome === "empty" ? [] : [{ operation: "obsolete_job", state: "successful", summary: "obsolete history" }] }, outcome === "failure");
      await page.evaluate(() => window.openPromises[0]);
      expect(await page.locator("#cm-body").innerHTML()).toBe(before);
      await current(page, view, 1, "b initial");
      await expect(page.locator("#cm-body")).not.toHaveAttribute("aria-busy", "true");
      await expect(page.locator(".compute-history")).toHaveCount(1);
    });
  }
}

for (const outcome of ["success", "failure", "empty"]) {
  test(`series: closed overlay rejects late ${outcome} after another opens`, async ({ page }) => {
    const view = views[0]; await prepare(page, view);
    await open(page, view, 0); await count(page, 1);
    await page.evaluate(resource => window.detailCalls[0].resolve({ device: resource, detail: {
      tables: [{ title: "Fictional disks", columns: [{ key: "id", label: "Disk" }, { key: "temp", label: "Temperature" }],
        rows: [{ id: "disk-a", temp: 30 }, { id: "disk-b", temp: 40 }],
        cellChart: { col: "temp", idKey: "id", metric: "temperature", title: "Disk history", unit: "°C" } }],
    } }), resources[0]);
    await page.evaluate(() => window.openPromises[0]);
    await holdReads(page, "/series\\?");
    await page.locator(".cell-chart").first().click(); await heldCount(page, 1);
    await page.evaluate(() => { window.closedSeries = document.querySelector(".series-body"); });
    await page.locator(".sc-close").click();
    await page.locator(".cell-chart").last().click(); await heldCount(page, 2);
    await releaseHeld(page, 1, { series: [] });
    await expect(page.locator(".series-body")).toHaveText("Not enough history yet to chart.");
    const before = await page.evaluate(() => window.closedSeries.innerHTML);
    await releaseHeld(page, 0, outcome === "failure" ? { error: "obsolete series" }
      : { series: outcome === "empty" ? [] : [[100, 30], [200, 40]] }, outcome === "failure");
    expect(await page.evaluate(() => window.closedSeries.innerHTML)).toBe(before);
    await expect(page.locator(".series-modal h2")).toHaveText("Disk history: disk-b");
    await expect(page.locator(".series-body")).toHaveText("Not enough history yet to chart.");
    await page.getByRole("tab", { name: "Access", exact: true }).evaluate(el => el.click());
    await expect(page.locator(".series-modal")).toHaveCount(0);
    await expect(page.locator("#device-modal")).toBeHidden();
  });
}

const profiles = ["a", "b"].map(id => ({
  profileConfigured: true, profile: { id, name: `VPN ${id.toUpperCase()}`, enabled: true },
  current: { configured: true, hostname: `${id}.example.invalid` },
  discovery: { status: "ok", candidates: [] },
}));
async function prepareVpn(page) {
  const view = views[0]; await prepare(page, view);
  await page.route("**/vpn-endpoints", route => json(route, { profiles }));
  await open(page, view, 0); await count(page, 1);
  await page.evaluate(resource => window.detailCalls[0].resolve({
    device: { ...resource, driverId: "opnsense.firewall" },
  }), resources[0]);
  await page.evaluate(() => window.openPromises[0]);
  await expect(page.getByRole("tab", { name: "VPN A", exact: true })).toBeVisible();
}
for (const [oldOutcome, newOutcome] of [["success", "success"], ["failure", "success"], ["success", "failure"], ["empty", "success"]]) {
  test(`VPN: late profile ${oldOutcome} cannot replace current ${newOutcome}`, async ({ page }) => {
    await prepareVpn(page);
    await holdReads(page, "/vpn-endpoints/[^/]+\\?refresh=1$");
    await page.getByRole("button", { name: "Find replacement", exact: true }).click(); await heldCount(page, 1);
    await page.getByRole("tab", { name: "VPN B", exact: true }).click();
    await page.getByRole("button", { name: "Find replacement", exact: true }).click(); await heldCount(page, 2);
    await releaseHeld(page, 1, newOutcome === "failure" ? { error: "current discovery failure" } : profiles[1], newOutcome === "failure");
    await expect(page.locator(".vpn-endpoint-section")).toContainText("b.example.invalid");
    if (newOutcome === "failure") await expect(page.locator(".vpn-endpoint-section")).toContainText("current discovery failure");
    const before = await page.locator(".vpn-endpoint-section").innerHTML();
    await releaseHeld(page, 0, oldOutcome === "failure" ? { error: "obsolete discovery failure" }
      : oldOutcome === "empty" ? {} : profiles[0], oldOutcome === "failure");
    expect(await page.locator(".vpn-endpoint-section").innerHTML()).toBe(before);
    await expect(page.getByRole("tab", { name: "VPN B", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(page.locator("#toasts")).not.toContainText("obsolete");
  });
}
for (const leave of ["close", "replace", "session"]) {
  test(`VPN: settings choices cannot open a dialog after ${leave}`, async ({ page }) => {
    await prepareVpn(page); await holdReads(page, "/vpn-endpoints/choices$");
    await page.locator(".vpn-endpoint-section").getByRole("button", { name: "Settings", exact: true }).click();
    await heldCount(page, 1);
    if (leave === "close") await page.locator("#device-modal").getByRole("button", { name: "Close", exact: true }).click();
    else if (leave === "session") await page.evaluate(async () => (await import("/js/api.js")).setSession(null));
    else await page.getByRole("tab", { name: "VPN B", exact: true }).click();
    await releaseHeld(page, 0, { peers: [], instances: [], locations: [] });
    await expect(page.locator(".vpn-settings-dialog")).toHaveCount(0);
    await expect(page.locator("#toasts")).toBeEmpty();
  });
}

async function prepareChart(page) {
  const view = views[0]; await prepare(page, view);
  await open(page, view, 0); await count(page, 1);
  await page.evaluate(resource => window.detailCalls[0].resolve({ device: resource,
    entities: [{ key: "cpu", name: "Fictional CPU", enabled: true, kind: "sensor", value: 10, unit: "%" }],
    history: { cpu: [[100, 10], [200, 20]] },
  }), resources[0]);
  await page.evaluate(() => window.openPromises[0]);
  await expect(page.locator(".chart-card .c-title")).toHaveText("Fictional CPU");
}
for (const [oldOutcome, newOutcome] of [["success", "success"], ["failure", "success"], ["success", "failure"], ["empty", "success"]]) {
  test(`chart: late range ${oldOutcome} cannot overwrite current ${newOutcome}`, async ({ page }) => {
    await prepareChart(page); await holdReads(page, "/history\\?");
    await page.getByRole("button", { name: "24h", exact: true }).click(); await heldCount(page, 1);
    await page.getByRole("button", { name: "7d", exact: true }).click(); await heldCount(page, 2);
    await page.getByRole("button", { name: "24h", exact: true }).click(); await heldCount(page, 3);
    await releaseHeld(page, 2, newOutcome === "failure" ? { error: "current chart failure" }
      : { series: [[100, 40], [200, 50]] }, newOutcome === "failure");
    if (newOutcome === "success") await expect(page.locator(".chart-card .hi")).toHaveText("peak 50 %");
    else await expect(page.locator(".chart-card .auth-err")).toHaveText("Couldn't load history: current chart failure");
    const before = await page.locator(".chart-card").innerHTML();
    await releaseHeld(page, 0, oldOutcome === "failure" ? { error: "obsolete chart failure" }
      : { series: oldOutcome === "empty" ? [] : [[100, 70], [200, 80]] }, oldOutcome === "failure");
    await releaseHeld(page, 1, { series: [] });
    expect(await page.locator(".chart-card").innerHTML()).toBe(before);
    await expect(page.getByRole("button", { name: "24h", exact: true })).toHaveAttribute("aria-pressed", "true");
    await expect(page.locator(".chart-card")).not.toHaveAttribute("aria-busy", "true");
    await expect(page).toHaveURL(/#\/device\/fictional-a$/);
  });
}
test("chart: obsolete completion cannot clear loading; close/session invalidate the range view", async ({ page }) => {
  await prepareChart(page); await holdReads(page, "/history\\?");
  await page.getByRole("button", { name: "24h", exact: true }).click(); await heldCount(page, 1);
  await page.getByRole("button", { name: "24h", exact: true }).click(); await heldCount(page, 2);
  expect(await page.evaluate(() => window.heldReads[0].signal.aborted)).toBe(true);
  await releaseHeld(page, 0, { error: "obsolete range" }, true);
  await expect(page.locator(".chart-card")).toHaveAttribute("aria-busy", "true");
  await expect(page.locator(".chart-card .auth-err")).toBeHidden();
  await page.evaluate(() => { window.closedChart = document.querySelector(".chart-card"); });
  await page.locator("#device-modal").getByRole("button", { name: "Close", exact: true }).click();
  const before = await page.evaluate(() => window.closedChart.innerHTML);
  await page.evaluate(async () => (await import("/js/api.js")).setSession(null));
  await releaseHeld(page, 1, { series: [[100, 80], [200, 90]] });
  expect(await page.evaluate(() => window.closedChart.innerHTML)).toBe(before);
  await expect(page.locator("#dm-body")).toBeEmpty();
  await expect(page.locator("#toasts")).toBeEmpty();
});

test("device: an in-flight live detail read cannot repaint the next device", async ({ page }) => {
  const view = views[0]; await prepare(page, view); await page.clock.install();
  await open(page, view, 0); await count(page, 1);
  await release(page, view, 0, 0, "initial live A");
  await page.clock.runFor(20000); await count(page, 2);
  await open(page, view, 1); await count(page, 3);
  await release(page, view, 2, 1, "current live B");
  const before = await page.locator("#dm-body").innerHTML();
  await release(page, view, 1, 0, "obsolete live A");
  expect(await page.locator("#dm-body").innerHTML()).toBe(before);
  await current(page, view, 1, "current live B");
  await page.locator("#device-modal").getByRole("button", { name: "Close", exact: true }).click();
  await page.clock.runFor(20000);
  expect(await page.evaluate(() => window.detailCalls.length)).toBe(3);
});

for (const picker of ["driver", "firewall"]) {
  for (const failure of [false, true]) {
    test(`${picker}: pending choices ${failure ? "failure" : "success"} cannot open over another detail`, async ({ page }) => {
      const view = views[0]; await prepare(page, view);
      await open(page, view, 0); await count(page, 1);
      await page.evaluate(resource => window.detailCalls[0].resolve({ device: resource,
        detail: { firewall: { supported: true, rules: [] } },
      }), resources[0]);
      await page.evaluate(() => window.openPromises[0]);
      await holdReads(page, picker === "driver" ? "^/api/drivers\\?" : "/firewall/all$");
      if (picker === "driver") await page.locator("#dm-sub button").click();
      else await page.getByRole("button", { name: "Add rule", exact: true }).click();
      await heldCount(page, 1);
      await open(page, view, 1); await count(page, 2);
      await release(page, view, 1, 1, "current picker B");
      await releaseHeld(page, 0, failure ? { error: "obsolete choices" }
        : { drivers: [{ id: "fictional.driver", displayName: "obsolete driver" }], rules: [] }, failure);
      await expect(page.locator("#dialog")).toBeHidden();
      await expect(page.locator("#toasts")).toBeEmpty();
      await current(page, view, 1, "current picker B");
    });
  }
}

for (const view of views) {
  test(`${view.kind}: click and encoded deep link activate only one read`, async ({ page }) => {
    await prepare(page, view);
    if (view.kind === "device") await page.locator("#devices-list").getByRole("button", { name: "Details", exact: true }).first().click();
    else {
      await page.getByRole("tab", { name: "Compute", exact: true }).click();
      await page.getByRole("button", { name: "View Fictional A details", exact: true }).click();
    }
    await count(page, 1); await release(page, view, 0, 0, "current click");
    await current(page, view, 0, "current click");
    expect(await page.evaluate(() => window.detailCalls.length)).toBe(1);
    const encoded = { ...resources[0], id: "fictional:encoded%20" };
    let lists = 0, details = 0;
    await page.route(view.kind === "device" ? "**/api/devices" : "**/api/compute", route => {
      lists += 1;
      return json(route, view.kind === "device" ? { devices: [encoded] } : { instances: [encoded] });
    });
    await page.route(view.kind === "device" ? "**/api/devices/*/detail" : "**/api/compute/fictional*", route => {
      if (new URL(route.request().url()).pathname.endsWith("/jobs")) return json(route, { jobs: [] });
      details += 1;
      return json(route, view.kind === "device" ? { device: encoded, detail: { info: { Identity: "encoded detail" } } }
        : { instance: { ...encoded, os: "encoded detail" } });
    });
    await page.goto(`/?detail-entry=1#/${view.kind}/${encodeURIComponent(encoded.id)}`);
    await expect(page.locator(`#${view.prefix}-body`)).toContainText("encoded detail");
    await expect(page).toHaveURL(new RegExp(`#/${view.kind}/${encodeURIComponent(encoded.id)}$`));
    expect(lists).toBe(1); expect(details).toBe(1);
    lists = 0; details = 0;
    await page.reload();
    await expect(page.locator(`#${view.prefix}-body`)).toContainText("encoded detail");
    expect(lists).toBe(1); expect(details).toBe(1);
    await page.locator(`#${view.modal}`).getByRole("button", { name: "Close", exact: true }).click();
    await expect(page.locator(`#${view.modal}`)).toBeHidden();
  });
}

for (const view of views) {
  test(`${view.kind}: stale empty detail cannot clear current content`, async ({ page }) => {
    await prepare(page, view);
    await open(page, view, 0); await count(page, 1);
    await open(page, view, 1); await count(page, 2);
    await release(page, view, 1, 1, "current nonempty");
    await page.evaluate(async () => { window.detailCalls[0].resolve({}); await window.openPromises[0]; });
    await current(page, view, 1, "current nonempty");
    await expect(page.locator(`#${view.prefix}-body`)).not.toHaveAttribute("aria-busy", "true");
  });
}

test("VPN: returning to a profile gives its newest discovery ownership of loading and content", async ({ page }) => {
  await prepareVpn(page); await holdReads(page, "/vpn-endpoints/[^/]+\\?refresh=1$");
  await page.getByRole("button", { name: "Find replacement", exact: true }).click(); await heldCount(page, 1);
  await page.getByRole("tab", { name: "VPN B", exact: true }).click();
  await page.getByRole("tab", { name: "VPN A", exact: true }).click();
  await page.getByRole("button", { name: "Find replacement", exact: true }).click(); await heldCount(page, 2);
  expect(await page.evaluate(() => window.heldReads[0].signal.aborted)).toBe(true);
  await releaseHeld(page, 0, { error: "obsolete discovery" }, true);
  await expect(page.locator(".vpn-candidates")).toContainText("Discovering candidates…");
  await expect(page.locator("#toasts")).toBeEmpty();
  await releaseHeld(page, 1, { ...profiles[0], current: { configured: true, hostname: "new-a.example.invalid" } });
  await expect(page.locator(".vpn-endpoint-section")).toContainText("new-a.example.invalid");
  await expect(page.locator(".vpn-candidates")).not.toContainText("Discovering candidates…");
  await expect(page.getByRole("tab", { name: "VPN A", exact: true })).toHaveAttribute("aria-selected", "true");
});

test("VPN: closing and reopening the same device rejects an obsolete panel load", async ({ page }) => {
  const view = views[0]; await prepare(page, view);
  await holdReads(page, "/vpn-endpoints$");
  const firewall = { ...resources[0], driverId: "opnsense.firewall" };
  await open(page, view, 0); await count(page, 1);
  await page.evaluate(device => window.detailCalls[0].resolve({ device }), firewall);
  await page.evaluate(() => window.openPromises[0]); await heldCount(page, 1);
  await page.locator("#device-modal").getByRole("button", { name: "Close", exact: true }).click();
  await open(page, view, 0); await count(page, 2);
  await page.evaluate(device => window.detailCalls[1].resolve({ device }), firewall);
  await page.evaluate(() => window.openPromises[1]); await heldCount(page, 2);
  await releaseHeld(page, 1, { profiles: [profiles[1]] });
  const before = await page.locator(".vpn-endpoint-section").innerHTML();
  await releaseHeld(page, 0, { profiles: [profiles[0]] });
  expect(await page.locator(".vpn-endpoint-section").innerHTML()).toBe(before);
  await expect(page.locator(".vpn-endpoint-section")).toContainText("b.example.invalid");
});

test("driver: the newest choices request owns the shared picker", async ({ page }) => {
  const view = views[0]; await prepare(page, view);
  await open(page, view, 0); await count(page, 1);
  await release(page, view, 0, 0, "current driver detail");
  await holdReads(page, "^/api/drivers\\?");
  await page.locator("#dm-sub button").click(); await heldCount(page, 1);
  await page.locator("#dm-sub button").click(); await heldCount(page, 2);
  await releaseHeld(page, 1, { drivers: [{ id: "fictional.current", displayName: "Current driver" }] });
  await expect(page.locator("#dialog")).toContainText("Current driver");
  await releaseHeld(page, 0, { drivers: [{ id: "fictional.obsolete", displayName: "Obsolete driver" }] });
  await expect(page.locator("#dialog")).toContainText("Current driver");
  await expect(page.locator("#dialog")).not.toContainText("Obsolete driver");
  await page.keyboard.press("Escape");
  await current(page, view, 0, "current driver detail");
});

test("VPN: a delayed discovery cannot reselect the previous profile", async ({ page }) => {
  await prepareVpn(page); await holdReads(page, "/vpn-endpoints/[^/]+\\?refresh=1$");
  await page.getByRole("button", { name: "Find replacement", exact: true }).click(); await heldCount(page, 1);
  await page.getByRole("tab", { name: "VPN B", exact: true }).click();
  await releaseHeld(page, 0, profiles[0]);
  await expect(page.getByRole("tab", { name: "VPN B", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.locator(".vpn-endpoint-section")).toContainText("b.example.invalid");
  await expect(page.locator(".vpn-endpoint-section")).not.toContainText("a.example.invalid");
});

for (const view of views) {
  test(`${view.kind}: obsolete 401 headers cannot expire the winning view`, async ({ page }) => {
    await prepare(page, view);
    await page.evaluate(view => {
      const original = window.fetch;
      window.fetch = async (path, opts) => {
        window.fetch = original;
        if (path === (view.kind === "device" ? "/api/devices/fictional-a/detail" : "/api/compute/fictional-a")) {
          return new Promise(resolve => { window.releaseHeaders = resolve; });
        }
        return original(path, opts);
      };
    }, view);
    await open(page, view, 0);
    await page.waitForFunction(() => !!window.releaseHeaders);
    await page.evaluate(() => { window.oldOpen = window.openPromises[0]; });
    await open(page, view, 1); await count(page, 1);
    await release(page, view, 0, 1, "current authenticated detail");
    await page.evaluate(async () => {
      window.releaseHeaders({ status: 401, ok: false, json: async () => ({ error: "obsolete unauthorized" }) });
      await window.oldOpen;
    });
    await current(page, view, 1, "current authenticated detail");
    await expect(page.locator("#auth-screen")).toBeHidden();
  });
}
