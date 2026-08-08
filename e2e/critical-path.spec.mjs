import { expect, test } from "@playwright/test";

const credentials = { username: "browser-admin", password: "correct-horse-battery-staple" };
const roster = {
  clients: [
    {
      mac: "00:11:22:33:44:55", hostname: "Laptop Alice", ip: "192.0.2.10",
      kind: "wifi", signal: -55, online: true, nac: "approved",
      seen: [{ via: "Office AP", kind: "wifi", signal: -55 }],
    },
    {
      mac: "00:11:22:33:44:66", hostname: "Camera Garage", ip: "192.0.2.20",
      // Legacy NAC scans could misclassify AP clients as wired while retaining RSSI.
      kind: "wired", signal: -73, online: false, nac: "blocked", lastSeen: 1_700_000_000,
      seen: [{ via: "Garage AP", kind: "wifi", signal: -73 }],
    },
  ],
  sources: [{ name: "Office AP" }],
  nac: { configured: true, deviceId: "firewall-1", managedAliases: [] },
};

const device = {
  id: "router-1", name: "Edge gateway", host: "192.0.2.1", transport: "http",
  driverId: "generic.http", state: { online: true }, order: 0,
};

function json(route, data, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
}

async function signIn(page) {
  await page.goto("/");
  await expect(page.locator("#auth-form")).toHaveAttribute("data-mode", "login");
  await page.locator("#auth-user").fill(credentials.username);
  await page.locator("#auth-pass").fill(credentials.password);
  await page.locator("#auth-submit").click();
  await expect(page.locator("#app")).toBeVisible();
}

async function mockRoster(page, data = roster) {
  await page.route("**/api/clients", (route) => json(route, data));
  await page.route("**/api/clients/history**", (route) => json(route, { events: [] }));
  await page.route("**/api/clients/forget", (route) => json(route, { ok: true }));
  await page.route("**/api/nac/client/membership", (route) => json(route, { configured: false }));
}

test.describe.configure({ mode: "serial" });

test("initial setup creates an admin and that admin can log in", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("#auth-form")).toHaveAttribute("data-mode", "setup");
  await page.locator("#auth-user").fill(credentials.username);
  await page.locator("#auth-pass").fill(credentials.password);
  await page.locator("#auth-confirm").fill(credentials.password);
  await page.locator("#auth-submit").click();
  await expect(page.locator("#whoami")).toContainText("browser-admin");

  await page.locator("#logout-btn").click();
  await expect(page.locator("#auth-form")).toHaveAttribute("data-mode", "login");
  await page.locator("#auth-user").fill(credentials.username);
  await page.locator("#auth-pass").fill(credentials.password);
  await page.locator("#auth-submit").click();
  await expect(page.locator("#whoami")).toContainText("browser-admin");
});

test("a failed device refresh retains the last-known device state", async ({ page }) => {
  let deviceReads = 0;
  await page.route("**/api/dashboards", (route) => json(route, { dashboards: [] }));
  await page.route("**/api/devices", (route) => {
    deviceReads += 1;
    return deviceReads === 1
      ? json(route, { devices: [device] })
      : json(route, { error: "device refresh unavailable" }, 503);
  });

  await signIn(page);
  await expect(page.getByText("Edge gateway", { exact: true })).toBeVisible();

  await page.getByRole("tab", { name: "Access" }).click();
  await page.getByRole("tab", { name: "Devices" }).click();
  await expect(page.locator("#toasts")).toContainText("Couldn't refresh devices: device refresh unavailable");
  await expect(page.getByText("Edge gateway", { exact: true })).toBeVisible();
});

test("the Access badge counts new devices but not reconnects", async ({ page }) => {
  let eventSummary = { since: 1, count: 4, newCount: 0 };
  await page.route("**/api/clients/events**", (route) => json(route, eventSummary));
  await signIn(page);

  const seenKey = await page.evaluate(() =>
    Object.keys(localStorage).find((key) => key.startsWith("hlhq-access-seen:")));
  expect(seenKey).toBeTruthy();
  await page.evaluate((key) => localStorage.setItem(key, "1"), seenKey);
  await page.reload();
  await expect(page.locator('.tab[data-tab="clients"] .tab-badge')).toHaveCount(0);

  eventSummary = { since: 1, count: 5, newCount: 1 };
  await page.reload();
  const badge = page.locator('.tab[data-tab="clients"] .tab-badge');
  await expect(badge).toHaveText("1");
  await expect(badge).toHaveAttribute("title", "1 new device since you last looked");
});

test("client filters constrain bulk actions to the visible roster", async ({ page }) => {
  await signIn(page);
  await mockRoster(page);
  await page.getByRole("tab", { name: "Access" }).click();
  await expect(page.getByText("Laptop Alice", { exact: true })).toBeVisible();
  await expect(page.getByText("Camera Garage", { exact: true })).toBeVisible();
  const onlineSignal = page.locator(".client-card").filter({ hasText: "Laptop Alice" }).locator(".cc-signal");
  const offlineSignal = page.locator(".client-card").filter({ hasText: "Camera Garage" }).locator(".cc-signal");
  await expect(onlineSignal).toBeVisible();
  await expect(onlineSignal).toContainText("-55 dBm");
  await expect(offlineSignal).toBeHidden();

  await page.locator("#clients-search").fill("camera");
  await expect(page.getByText("Laptop Alice", { exact: true })).toBeHidden();
  await expect(page.getByText("Camera Garage", { exact: true })).toBeVisible();

  await page.locator("#clients-menu").click();
  await page.getByRole("button", { name: "Forget offline shown (1)" }).click();
  const request = page.waitForRequest((candidate) =>
    candidate.url().endsWith("/api/clients/forget") && candidate.method() === "POST");
  await page.locator("#dialog-ok").click();
  expect(JSON.parse((await request).postData() ?? "{}")).toEqual({ macs: ["00:11:22:33:44:66"] });
});

test("the client table hides retained RSSI for offline clients", async ({ page }) => {
  await signIn(page);
  await mockRoster(page, { ...roster, nac: { configured: false } });
  await page.getByRole("tab", { name: "Access" }).click();

  const onlineRow = page.locator(".clients-table tbody tr").filter({ hasText: "Laptop Alice" });
  const offlineRow = page.locator(".clients-table tbody tr").filter({ hasText: "Camera Garage" });
  await expect(onlineRow.locator("td").nth(5)).toHaveText("-55 dBm");
  await expect(offlineRow.locator("td").nth(5)).toHaveText("–");
});

test("Escape closes the client modal and hash navigation follows the selected tab", async ({ page }) => {
  await signIn(page);
  await mockRoster(page);
  await page.getByRole("tab", { name: "Access" }).click();
  await expect(page).toHaveURL(/#\/access$/);
  const edit = page.getByRole("button", { name: /Edit — rename/ }).first();
  await edit.click();
  await expect(page.locator("#client-modal")).toBeVisible();
  await expect(page.locator("#ce-host")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.locator("#client-modal")).toBeHidden();
  await expect(edit).toBeFocused();

  await page.getByRole("tab", { name: "Add device" }).click();
  await expect(page).toHaveURL(/#\/add$/);
  await page.goBack();
  await expect(page).toHaveURL(/#\/access$/);
  await expect(page.getByRole("tab", { name: "Access" })).toHaveAttribute("aria-selected", "true");
});

test("Proxmox detail lists packages and follows update installation progress", async ({ page }) => {
  const proxmox = {
    id: "proxmox-1", name: "Proxmox node", host: "192.0.2.30", transport: "api",
    driverId: "proxmox.ve", state: { online: true }, order: 0,
  };
  let installed = false;
  let sshConfigured = false;
  await page.route("**/api/dashboards", (route) => json(route, { dashboards: [] }));
  await page.route("**/api/devices", (route) => json(route, { devices: [proxmox] }));
  await page.route("**/api/devices/proxmox-1/detail", (route) => json(route, {
    device: proxmox, entities: [], detail: {}, history: {}, ifHistory: {},
    actions: [], online: [], supportsBinding: false, supportsUpdates: true,
  }));
  await page.route("**/api/devices/proxmox-1/updates", (route) => json(route, {
    total: installed ? 0 : 1,
    nodes: [{ node: "pve-one", status: "online", packages: installed ? [] : [{
      name: "pve-manager", installed: "8.2.1", available: "8.2.2",
      description: "Proxmox VE management tools",
    }] }],
    sshConfigured,
    operation: installed ? {
      id: "job-1", state: "completed", percent: 100, message: "Reboot required on pve-one.",
      nodes: [{ node: "pve-one", state: "completed", rebootRequired: true,
        message: "Updates installed; reboot required" }],
    } : null,
  }));
  await page.route("**/api/devices/proxmox-1/updates/install", (route) => {
    installed = true;
    return json(route, { operation: {
      id: "job-1", state: "running", percent: 35, message: "Installing updates on pve-one",
      nodes: [{ node: "pve-one", state: "running", message: "Installing updates" }],
    } }, 202);
  });
  await page.route("**/api/devices/proxmox-1/updates/status", (route) => json(route, {
    operation: {
      id: "job-1", state: "completed", percent: 100, message: "Reboot required on pve-one.",
      nodes: [{ node: "pve-one", state: "completed", rebootRequired: true,
        message: "Updates installed; reboot required" }],
    },
  }));
  await page.route("**/api/devices/proxmox-1/updates/credentials", async (route) => {
    expect(await route.request().postDataJSON()).toEqual({
      username: "root", password: "root-password", port: 22,
    });
    sshConfigured = true;
    return json(route, { ok: true });
  });

  await signIn(page);
  await page.locator(".card").filter({ hasText: "Proxmox node" }).click();
  await expect(page.getByRole("heading", { name: "Software updates" })).toBeVisible();
  await expect(page.getByText("pve-manager", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Configure root SSH" }).click();
  await expect(page.locator("#dialog")).toBeVisible();
  await expect(page.locator("#dialog-input")).toBeFocused();
  await page.locator("#dialog-input").fill("root-password");
  await page.locator("#dialog-ok").click();
  await expect(page.locator("#toasts")).toContainText("Root SSH credentials verified and saved.");
  await page.getByRole("button", { name: "Install updates" }).click();
  const installRequest = page.waitForRequest((request) =>
    request.url().endsWith("/api/devices/proxmox-1/updates/install"));
  await page.locator("#dialog-ok").click();
  await installRequest;
  await expect(page.locator(".update-operation")).toContainText("Reboot required on pve-one.");
  await expect(page.locator(".update-operation")).toContainText("Updates installed; reboot required");
  await expect(page.locator(".update-operation progress")).toHaveAttribute("value", "100");
});

test("VPN endpoint manager saves settings and progressively discloses candidates", async ({ page }) => {
  const firewall = {
    id: "opnsense-1", name: "OPNsense firewall", host: "192.0.2.1", transport: "api",
    driverId: "opnsense.firewall", state: { online: true }, order: 0,
  };
  const target = {
    id: "corporate-portal", name: "Corporate portal", description: "Manual sign-in check",
    state: "Unknown", lastValidatedAt: null, note: "",
  };
  const candidates = [
    ["candidate-1", "uk1001.nordvpn.com", "192.0.2.21", "Preferred", "London", 12],
    ["candidate-2", "uk1002.nordvpn.com", "192.0.2.22", "Eligible", "London", 18],
    ["candidate-3", "uk1003.nordvpn.com", "192.0.2.23", "Preferred", "Manchester", 21],
    ["candidate-4", "uk1004.nordvpn.com", "192.0.2.24", "Eligible", "Glasgow", 25],
    ["candidate-5", "uk1005.nordvpn.com", "192.0.2.25", "Excluded", "London", 31],
    ["candidate-6", "uk1006.nordvpn.com", "192.0.2.26", "Unknown", "", null],
  ].map(([candidateId, hostname, endpointIp, classification, city, load]) => ({
    candidateId, hostname, endpointIp, endpointPort: 51820, classification, city, load,
    owner: classification === "Unknown" ? "" : "Example Hosting", asn: "64500",
    lookupStatus: classification === "Unknown" ? "unknown" : "known",
    compatibilityTargets: [{ ...target }], active: false,
  }));
  let profile = {
    enabled: true, peerUuid: "peer-1", instanceUuid: "instance-1", gatewayUuid: "",
    country: "United Kingdom", city: "London", maxCandidates: 20,
    discoveryIntervalSeconds: 3600, preferredOwners: ["Example Hosting"],
    excludedOwners: [], includeUnknownOwners: false, handshakeWarningSeconds: 300,
    compatibilityTargets: [{ ...target }], notes: "",
  };
  let includeTargets = true;
  let partialDiscovery = false;
  const snapshot = () => ({
    profileConfigured: true,
    profile: { ...profile, compatibilityTargets: includeTargets ? [{ ...target }] : [] },
    current: {
      configured: true, endpointIp: "192.0.2.10", endpointPort: 51820,
      hostname: "uk-current.nordvpn.com", owner: "Example Hosting", asn: "64500",
      classification: "Preferred", runtimeClassification: "Active", health: "Healthy",
      peerUuid: "peer-1", instanceUuid: "instance-1", candidateId: "current",
      status: { latestHandshake: Math.floor(Date.now() / 1000) - 42, handshakeAge: 42,
        receivedBytes: 1200, transmittedBytes: 800, status: "online" },
      compatibilityTargets: includeTargets ? [{ ...target, state: "Verified",
        lastValidatedAt: 1_700_000_000, note: "Checked manually" }] : [],
    },
    discovery: partialDiscovery ? {} : { status: "ok", at: 1_700_000_000,
      candidates: candidates.map((candidate) => ({ ...candidate,
        compatibilityTargets: includeTargets ? candidate.compatibilityTargets : [] })) },
    history: [],
  });
  let savedPayload;
  await page.route("**/api/dashboards", (route) => json(route, { dashboards: [] }));
  await page.route("**/api/devices", (route) => json(route, { devices: [firewall] }));
  await page.route("**/api/devices/opnsense-1/detail", (route) => json(route, {
    device: firewall, entities: [], detail: {}, history: {}, ifHistory: {},
    actions: [], online: [], supportsBinding: false, supportsUpdates: false,
  }));
  await page.route("**/api/devices/opnsense-1/vpn-endpoints**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname.endsWith("/choices")) return json(route, {
      peers: [{ uuid: "peer-1", name: "NordVPN peer" }],
      instances: [{ uuid: "instance-1", name: "WireGuard tunnel" }],
    });
    if (url.pathname.endsWith("/compatibility")) return json(route, { ok: true });
    if (url.pathname.endsWith("/switch")) return json(route, { ok: true,
      rollback: null, message: "Endpoint switched and verified." });
    if (request.method() === "PATCH") {
      savedPayload = request.postDataJSON();
      profile = { ...profile, ...savedPayload };
      return json(route, { profile });
    }
    return json(route, snapshot());
  });

  await signIn(page);
  await page.locator(".card").filter({ hasText: "OPNsense firewall" }).click();
  const section = page.locator(".vpn-endpoint-section");
  await expect(section.getByText("uk-current.nordvpn.com", { exact: true })).toBeVisible();
  await expect(section.getByText("Healthy", { exact: true })).toBeVisible();
  await expect(section.getByText("1 verified", { exact: true })).toBeVisible();
  await expect(section.locator(".vpn-details")).not.toHaveAttribute("open", "");
  await expect(section.locator(".vpn-candidates")).toHaveCount(0);

  const moreButton = section.getByRole("button", { name: "More" });
  await expect(moreButton).toHaveAttribute("aria-expanded", "false");
  await moreButton.click();
  await expect(moreButton).toHaveAttribute("aria-expanded", "true");
  const settingsButton = section.getByRole("button", { name: "Settings" });
  await expect(settingsButton).toBeVisible();
  await settingsButton.evaluate((button) => { button.click(); button.click(); });
  await expect(page.locator(".vpn-settings-dialog")).toHaveCount(1);
  await page.getByLabel("Maximum candidates").fill("9");
  await page.getByLabel("Discovery interval (seconds)").fill("900");
  await page.getByLabel("Handshake warning threshold (seconds)").fill("240");
  const patchRequest = page.waitForRequest((request) =>
    request.url().endsWith("/api/devices/opnsense-1/vpn-endpoints") && request.method() === "PATCH");
  await page.getByRole("button", { name: "Save settings" }).click();
  await patchRequest;
  await expect(page.locator(".vpn-settings-dialog")).toHaveCount(0);
  expect(savedPayload.maxCandidates).toBe(9);
  expect(savedPayload.discoveryIntervalSeconds).toBe(900);
  expect(savedPayload.handshakeWarningSeconds).toBe(240);

  await moreButton.click();
  await section.getByRole("button", { name: "Settings" }).click();
  await expect(page.locator(".vpn-settings-dialog")).toHaveCount(1);
  await expect(page.getByLabel("Maximum candidates")).toHaveValue("9");
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(moreButton).toBeFocused();

  const find = section.getByRole("button", { name: "Find replacement" });
  await find.click();
  await find.click();
  const panel = section.locator(".vpn-candidates");
  await expect(panel).toHaveCount(1);
  await expect(panel.locator(":scope > .vpn-candidate-list > .vpn-candidate-card")).toHaveCount(3);
  await expect(panel.getByRole("button", { name: "Show all candidates" })).toBeVisible();
  const others = panel.locator(".vpn-other-candidates");
  await expect(others).not.toHaveAttribute("open", "");
  await expect(others.getByText("uk1005.nordvpn.com", { exact: true })).toBeHidden();

  await panel.locator(":scope > .vpn-candidate-list > .vpn-candidate-card").first()
    .getByRole("button", { name: "Use" }).click();
  await expect(page.locator("#dialog-title")).toHaveText("Change VPN endpoint?");
  await expect(page.locator("#dialog-msg")).toContainText("Current");
  await expect(page.locator("#dialog-msg")).toContainText("Replacement");
  await expect(page.locator("#dialog-ok")).toHaveText("Apply and verify");
  await page.locator("#dialog-cancel").click();
  await expect(page.locator("#dialog")).toBeHidden();

  await panel.locator(":scope > .vpn-candidate-list > .vpn-candidate-card").first()
    .getByRole("button", { name: "View checks" }).click();
  await expect(page.getByRole("heading", { name: "Update validation" })).toBeVisible();
  await expect(page.getByLabel("Validation target")).toHaveValue("corporate-portal");
  await page.getByRole("button", { name: "Cancel" }).click();

  const details = section.locator(".vpn-details");
  await details.locator("summary").focus();
  await page.keyboard.press("Enter");
  await expect(details).toHaveAttribute("open", "");
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await section.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);

  includeTargets = false;
  partialDiscovery = true;
  await page.getByRole("button", { name: "Close" }).click();
  await page.locator(".card").filter({ hasText: "OPNsense firewall" }).click();
  const reopened = page.locator(".vpn-endpoint-section");
  await expect(reopened.getByText("uk-current.nordvpn.com", { exact: true })).toBeVisible();
  await expect(reopened.locator(".vpn-validation-summary")).toHaveCount(0);
  await expect(reopened.locator(".vpn-candidates")).toHaveCount(0);
});

test("device presets show only their relevant connection fields", async ({ page }) => {
  await signIn(page);
  await page.getByRole("tab", { name: "Add device" }).click();

  const cases = [
    ["opnsense", ["cred-apiKey", "cred-apiSecret", "cred-scheme", "cred-verifyTls"], ""],
    ["pfsense", ["cred-apiKey", "cred-scheme", "cred-verifyTls"], ""],
    ["unifi", ["cred-apiKey", "cred-scheme", "cred-verifyTls"], "443"],
    ["proxmox", ["cred-tokenId", "cred-tokenSecret", "cred-verifyTls",
      "cred-sshPassword", "cred-sshPrivateKey", "cred-sshPort"], "8006"],
    ["truenas", ["cred-apiKey", "cred-scheme", "cred-verifyTls"], ""],
    ["firewalla", ["cred-token"], ""],
    ["mikrotik", ["cred-username", "cred-password", "cred-scheme", "cred-verifyTls"], ""],
    ["openwrt", ["cred-username", "cred-password", "cred-scheme", "cred-verifyTls", "cred-metricsPath"], "80"],
    ["synology", ["cred-username", "cred-password", "cred-scheme", "cred-verifyTls"], "5000"],
    ["qnap", ["cred-username", "cred-password", "cred-scheme", "cred-verifyTls"], "8080"],
    ["keeplink", ["cred-username", "cred-password", "cred-scheme", "cred-verifyTls"], "80"],
    ["zyxel", ["cred-username", "cred-password", "cred-verifyTls"], "443"],
  ];

  for (const [preset, fields, port] of cases) {
    await page.locator("#wiz-preset").selectOption(preset);
    expect(await page.locator("#wiz-creds [id]").evaluateAll(
      (elements) => elements.map((element) => element.id))).toEqual(fields);
    await expect(page.locator("#wiz-port")).toHaveValue(port);
  }

  let submitted;
  await page.route("**/api/devices/detect", async (route) => {
    submitted = JSON.parse(route.request().postData() ?? "{}");
    await json(route, { candidates: [{
      driverId: "firewalla.msp", displayName: "Firewalla", confidence: 0.9,
    }] });
  });
  await page.locator("#wiz-preset").selectOption("firewalla");
  await page.locator("#wiz-host").fill("example.firewalla.net");
  await page.locator("#cred-token").fill("secret-token");
  await page.locator("#wiz-detect").click();
  await expect(page.locator("#wiz-candidates").getByText("Firewalla", { exact: true })).toBeVisible();
  expect(submitted).toEqual({
    transport: "api", host: "example.firewalla.net", port: null,
    credentials: {
      apiKey: "Token secret-token", scheme: "https", verifyTls: true,
      authStyle: "header", keyHeader: "Authorization",
    },
  });
});

test("the service worker refreshes the shell online and serves it offline", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await signIn(page);
  await page.evaluate(() => navigator.serviceWorker.ready);
  await page.reload();
  await page.waitForFunction(async () => (await caches.open("hlhq-shell-v1")).keys().then((keys) => keys.length > 0));

  const manifest = await page.evaluate(async () => {
    const cache = await caches.open("hlhq-shell-v1");
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
