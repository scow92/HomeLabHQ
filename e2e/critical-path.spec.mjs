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
  const mode = await page.locator("#auth-form").getAttribute("data-mode");
  await page.locator("#auth-user").fill(credentials.username);
  await page.locator("#auth-pass").fill(credentials.password);
  if (mode === "setup") await page.locator("#auth-confirm").fill(credentials.password);
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

test("Compute workflow filters workloads and runs approved maintenance", async ({ page }) => {
  const instance = {
    id: "compute-1", parentDeviceId: "proxmox-1", provider: "proxmox",
    providerInstanceId: "301", type: "lxc", name: "Synthetic workload",
    status: "running", node: "node-example", cpuCores: 2,
    memoryBytes: 4294967296, diskBytes: 34359738368,
    ipAddresses: ["192.0.2.60"], uptimeSeconds: 3600,
    discoveryState: "current", lastDiscoveredAt: Math.floor(Date.now() / 1000),
    parentDevice: { id: "proxmox-1", name: "Synthetic hypervisor",
      host: "192.0.2.50", driverId: "proxmox.ve",
      state: { online: true, confirmedOnline: true } },
    ansible: {
      enabled: true, controllerId: "primary", inventoryHost: "synthetic-workload",
      updateCheckEligible: true, updateEligible: true,
      dockerDiscoveryEligible: true, dockerUpdateCheckEligible: true,
      dockerUpdateModes: [],
    },
    updateState: { state: "updates_available", updateCount: 2 },
    dockerDiscoveryState: { state: "successful" },
    dockerUpdateState: { state: "updates_available", updateCount: 1,
      summary: "One image update is available" },
    docker: { available: true, version: "99.1.0", composeAvailable: true,
      composeVersion: "9.8.0", summary: "5 containers across one project",
      projects: [{ id: "project-1", name: "Synthetic project", path: "/srv/synthetic",
        configFiles: ["/srv/synthetic/compose.yml"], status: "running(4)",
        updateStrategy: "pull", containers: [
          { name: "web", state: "running", health: "healthy", image: "example/web:1" },
          { name: "worker", state: "running", health: "healthy", image: "example/worker:1" },
          { name: "queue", state: "running", health: "healthy", image: "example/queue:1" },
          { name: "database", state: "running", health: "healthy", image: "example/database:1" },
        ] }], containers: [
          { name: "direct-agent", state: "running", health: null,
            hasHealthcheck: false,
            image: "example/direct-agent:1", labels: {}, networks: ["host"] },
        ] },
    suggestedMappings: [],
  };
  const controller = {
    id: "primary", enabled: true, displayName: "Synthetic controller",
    credentialConfigured: true, inventory: { hosts: [
      { name: "synthetic-workload", address: "192.0.2.60", groups: ["compute"] },
    ], groups: [{ name: "compute", hosts: ["synthetic-workload"] }] },
    discoveredPlaybooks: [], playbooks: {
      os_check: { approved: true }, os_update: { approved: true },
      docker_discovery: { approved: true },
      docker_check: { approved: true, projectVariable: "docker_project" },
    },
  };
  let operation = null;
  let updatePayload = null;
  let refreshRequested = false;
  const refreshJobs = [
    { jobId: "refresh-docker-discovery", operation: "docker_discovery" },
    { jobId: "refresh-os-check", operation: "os_check" },
    { jobId: "refresh-docker-check", operation: "docker_check",
      projectName: "Synthetic project" },
  ];
  let dockerCheckPayload = null;
  let dockerCheckPolls = 0;
  await page.route("**/api/compute**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/compute/refresh") {
      refreshRequested = true;
      return json(route, { providers: [], ansibleInventory: { ok: true },
        maintenanceJobs: [{ computeInstanceId: "compute-1", queued: true,
          operations: refreshJobs.map((job) => job.operation), jobs: refreshJobs }] });
    }
    if (url.pathname === "/api/compute") return json(route, { instances: [instance], ansibleEnabled: true, summary: {
      workloads: 1, running: 1, stopped: 0, containers: 5,
      healthyContainers: 4, needsUpdates: 1,
    } });
    if (url.pathname === "/api/compute/compute-1") return json(route, { instance });
    if (url.pathname === "/api/compute/compute-1/jobs") return json(route, { jobs: operation ? [operation] : [] });
    if (url.pathname.startsWith("/api/compute/jobs/")) {
      const refreshJob = refreshJobs.find((job) => url.pathname.endsWith(job.jobId));
      if (operation?.id === "job-docker-check" && url.pathname.endsWith(operation.id) &&
          dockerCheckPolls++ === 0) {
        return json(route, { job: { ...operation, state: "running" } });
      }
      return json(route, { job: {
        ...(refreshJob || operation), state: "successful",
        summary: "Maintenance completed successfully",
        finishedAt: Math.floor(Date.now() / 1000),
      } });
    }
    if (url.pathname === "/api/compute/compute-1/updates/check") {
      operation = { id: "job-check", operation: "os_check", state: "queued",
        createdAt: Math.floor(Date.now() / 1000), recap: {} };
      return json(route, { job: operation }, 202);
    }
    if (url.pathname === "/api/compute/compute-1/updates") {
      updatePayload = request.postDataJSON();
      operation = { id: "job-update", operation: "os_update", state: "queued",
        createdAt: Math.floor(Date.now() / 1000), recap: {} };
      return json(route, { job: operation }, 202);
    }
    if (url.pathname === "/api/compute/compute-1/docker/check") {
      dockerCheckPayload = request.postDataJSON();
      operation = { id: "job-docker-check", operation: "docker_check",
        projectName: dockerCheckPayload.projectName, state: "queued",
        createdAt: Math.floor(Date.now() / 1000), recap: {} };
      return json(route, { job: operation }, 202);
    }
    return json(route, { error: "unhandled compute route" }, 404);
  });
  await page.route("**/api/settings/ansible", (route) => json(route, { controller }));

  await signIn(page);
  await page.getByRole("tab", { name: "Compute" }).click();
  await expect(page.getByText("Synthetic workload", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Need Attention 1" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Refresh All" })).toBeVisible();
  await expect(page.locator("#compute-update-all")).toBeEnabled();
  await expect(page.locator(".compute-host-header")).toContainText("Synthetic hypervisor");
  await expect(page.locator(".compute-host-header")).toContainText("Docker · Operational");
  await expect(page.locator(".compute-card")).toContainText(
    "Docker5 containers · Operational · Updates: 1 available");
  await expect(page.locator(".compute-container-preview")).toContainText("web");
  await expect(page.locator(".compute-card button")).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  const mobileLabelBoxes = await page.locator(".compute-card .dev-state .k").evaluateAll(
    (labels) => labels.map((label) => {
      const box = label.getBoundingClientRect();
      const range = document.createRange();
      range.selectNodeContents(label);
      return { width: box.width, lines: range.getClientRects().length };
    }));
  expect(mobileLabelBoxes.every((box) => box.width >= 80 && box.lines === 1)).toBe(true);
  await expect.poll(() => page.evaluate(() =>
    document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.getByRole("button", { name: "Refresh All" }).click();
  await expect.poll(() => refreshRequested).toBe(true);
  await expect(page.locator("#toasts")).toContainText(
    "Compute, OS updates, and Docker updates refreshed.");

  await page.getByRole("button", { name: "VMs" }).click();
  await expect(page.locator("#compute-empty")).toContainText("No matching workloads");
  await page.getByRole("button", { name: "Docker", exact: true }).click();
  await page.locator(".compute-card").click();
  await expect(page.locator("#compute-modal")).toBeVisible();
  await expect(page.locator("#compute-modal .workload-details")).toBeVisible();
  await expect(page.locator("#compute-modal .info-chip")).toHaveCount(0);
  await expect(page.getByText("Synthetic project", { exact: true })).toBeVisible();
  await expect(page.locator("#compute-modal").getByText("web", { exact: true })).toBeVisible();
  await expect(page.locator("#compute-modal")).toContainText("Docker 99.1.0 · Compose 9.8.0");
  await expect(page.locator("#compute-modal")).toContainText("5 containers");
  await expect(page.locator("#compute-modal")).toContainText("4 healthy");
  await expect(page.locator("#compute-modal")).toContainText("1 no healthcheck");
  await expect(page.getByText("Compose projects", { exact: true })).toBeVisible();
  await expect(page.getByText("Other containers", { exact: true })).toBeVisible();
  await expect(page.locator("#compute-modal").getByText("direct-agent", { exact: true })).toBeVisible();
  await expect(page.getByText("No healthcheck", { exact: true })).toBeVisible();
  await expect(page.locator("#compute-modal")).not.toContainText("/srv/synthetic");
  await expect(page.locator(".maintenance-summary").first()).toContainText(
    "Docker updatesOne image update is availableAvailable · 1 update");

  await page.locator("#compute-modal").getByRole(
    "button", { name: "Check updates", exact: true }).click();
  await expect.poll(() => dockerCheckPayload).toEqual({ projectName: "Synthetic project" });
  await expect(page.locator("#compute-modal .maintenance-progress")).toContainText(
    "Checking Synthetic project…");
  await expect(page.locator("#toasts")).toContainText("Maintenance completed successfully");

  await page.locator("#compute-modal").getByRole(
    "button", { name: "Check Updates", exact: true }).click();
  await expect(page.locator("#toasts")).toContainText("Maintenance completed successfully");
  await page.locator("#compute-modal").getByRole("button", { name: "Update", exact: true }).click();
  await expect(page.locator("#dialog-msg")).toContainText("Reboot permission is OFF");
  await page.locator("#dialog-ok").click();
  await expect.poll(() => updatePayload).toEqual({ allowReboot: false, rebootConfirmed: false });

  await page.setViewportSize({ width: 390, height: 844 });
  const modalCard = page.locator("#compute-modal .modal-card");
  expect(await modalCard.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  for (const control of await page.locator("#compute-modal .maintenance-actions > *").all()) {
    const box = await control.boundingBox();
    expect(box).not.toBeNull();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(390);
  }
});

test("Compute bulk updates eligible workloads and reports partial failures", async ({ page }) => {
  const parent = (id, online = true) => ({
    id: `host-${id}`, name: `Host ${id}`, host: `192.0.2.${id}`,
    driverId: "proxmox.ve",
    state: { online, confirmedOnline: online },
  });
  const workload = (id, overrides = {}) => ({
    id, parentDeviceId: `host-${id}`, parentDevice: parent(id),
    provider: "proxmox", providerInstanceId: id, type: "vm",
    name: `Workload ${id}`, status: "running", discoveryState: "current",
    ansible: { enabled: true, controllerId: "primary", inventoryHost: `host-${id}`,
      updateEligible: true, maintenanceActive: false },
    updateState: { state: "updates_available", updateCount: 2 },
    ...overrides,
  });
  const refreshedInstances = [
    workload("eligible-success", {
      discoveryState: "stale",
      dockerDiscoveryState: { state: "failed" },
      docker: { available: true, containers: [
        { name: "one-warning", state: "stopped" },
        { name: "another-warning", state: "stopped" },
      ] },
    }),
    workload("eligible-failure"),
    workload("offline", { parentDevice: parent("offline", false) }),
    workload("unsupported", { ansible: { enabled: true, controllerId: "primary",
      inventoryHost: "host-unsupported", updateEligible: false, maintenanceActive: false } }),
    workload("busy", { ansible: { enabled: true, controllerId: "primary",
      inventoryHost: "host-busy", updateEligible: true, maintenanceActive: true } }),
    workload("current", { updateState: { state: "up_to_date", updateCount: 0 } }),
  ];
  const finalInstances = refreshedInstances.map((instance) => {
    if (instance.id === "eligible-success") return {
      ...instance, discoveryState: "current", dockerDiscoveryState: { state: "successful" },
      docker: null, updateState: { state: "up_to_date", updateCount: 0 },
    };
    if (instance.id === "eligible-failure") return {
      ...instance, updateState: { state: "failed" },
    };
    return instance;
  });
  let refreshed = false;
  let completedJobs = 0;
  let computeReads = 0;
  const updateRequests = [];
  const jobPolls = new Map();

  await page.route("**/api/compute**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/compute/refresh") {
      refreshed = true;
      return json(route, { providers: [], ansibleInventory: { ok: true }, maintenanceJobs: [] });
    }
    if (url.pathname === "/api/compute") {
      computeReads += 1;
      const instances = !refreshed ? [] : completedJobs === 2
        ? finalInstances : refreshedInstances;
      return json(route, { instances, ansibleEnabled: true, summary: {} });
    }
    if (url.pathname.startsWith("/api/compute/jobs/bulk-")) {
      const jobId = url.pathname.split("/").at(-1);
      const polls = jobPolls.get(jobId) || 0;
      jobPolls.set(jobId, polls + 1);
      if (!polls) return json(route, { job: { id: jobId, state: "running" } });
      completedJobs += 1;
      const failed = jobId === "bulk-eligible-failure";
      return json(route, { job: { id: jobId, state: failed ? "failed" : "successful",
        summary: failed ? "Synthetic update failure" : "Updated" } });
    }
    const match = url.pathname.match(/^\/api\/compute\/([^/]+)\/updates$/);
    if (match && request.method() === "POST") {
      updateRequests.push(match[1]);
      expect(request.postDataJSON()).toEqual({ allowReboot: false, rebootConfirmed: false });
      return json(route, { job: { id: `bulk-${match[1]}`, operation: "os_update",
        state: "queued" } }, 202);
    }
    return json(route, { error: "unhandled compute route" }, 404);
  });

  await signIn(page);
  await page.getByRole("tab", { name: "Compute" }).click();
  await expect(page.getByRole("button", { name: "Need Attention 0" })).toBeVisible();
  await expect(page.locator("#compute-update-all")).toBeDisabled();
  await expect(page.getByRole("button", { name: "Refresh All" })).toBeVisible();

  await page.getByRole("button", { name: "Refresh All" }).click();
  await expect(page.getByRole("button", { name: "Need Attention 5" })).toBeVisible();
  await expect(page.locator("#compute-update-all")).toBeEnabled();
  await page.getByRole("button", { name: "Need Attention 5" }).click();
  await expect(page.locator(".compute-card")).toHaveCount(5);

  await page.locator("#compute-update-all").click();
  await expect(page.locator("#dialog-title")).toHaveText("Update 2 Compute devices?");
  await expect(page.locator("#dialog-msg")).toContainText(
    "3 other devices with available updates will be skipped");
  await expect(page.locator("#compute-update-all")).toBeDisabled();
  await page.locator("#dialog-ok").click();
  await expect(page.locator("#compute-update-all")).toHaveAttribute("aria-busy", "true");
  await page.locator("#compute-update-all-progress").evaluate((element) => {
    document.querySelector("#compute-update-all").dispatchEvent(
      new MouseEvent("click", { bubbles: true }));
    document.querySelector("#compute-update-all").dispatchEvent(
      new MouseEvent("click", { bubbles: true }));
    return !element.hidden;
  });

  await expect(page.locator("#toasts")).toContainText(
    "Bulk update complete: 1 succeeded, 1 failed, 3 skipped.");
  expect(updateRequests.sort()).toEqual(["eligible-failure", "eligible-success"]);
  expect(computeReads).toBeGreaterThanOrEqual(3);
  await expect(page.getByRole("button", { name: "Need Attention 4" })).toBeVisible();
  await expect(page.locator("#compute-update-all")).toBeDisabled();
});

test("Compute separates Docker lifecycle, healthchecks, and parent summaries", async ({ page }) => {
  const parent = { id: "host-mixed", name: "Rack hypervisor", host: "192.0.2.70",
    driverId: "proxmox.ve", state: { online: true, confirmedOnline: true } };
  const uncheckedParent = { id: "host-unchecked", name: "Utility hypervisor",
    host: "192.0.2.71", driverId: "proxmox.ve",
    state: { online: true, confirmedOnline: true } };
  const unavailableParent = { id: "host-offline", name: "Offline hypervisor",
    host: "192.0.2.72", driverId: "proxmox.ve",
    state: { online: false, confirmedOnline: false } };
  const mixed = {
    id: "compute-mixed", parentDeviceId: parent.id, parentDevice: parent,
    provider: "proxmox", providerInstanceId: "501", type: "vm", name: "Docker host",
    status: "running", node: "rack-a", cpuCores: 4, memoryBytes: 8589934592,
    discoveryState: "current", dockerDiscoveryState: { state: "successful" },
    updateState: { state: "up_to_date" }, ansible: { enabled: false },
    docker: { available: true, version: "29.0", composeAvailable: true,
      composeVersion: "2.39", projects: [{ name: "Mixed project", status: "running(5)",
        containers: [
          { name: "healthy", state: "running", hasHealthcheck: true, health: "healthy" },
          { name: "unhealthy", state: "running", hasHealthcheck: true,
            health: "unhealthy", healthDetails: { output: "probe failed" } },
          { name: "starting", state: "running", hasHealthcheck: true, health: "starting" },
          { name: "unchecked", state: "running", hasHealthcheck: false, health: null },
          { name: "restarting", state: "restarting", hasHealthcheck: true, health: "starting" },
          { name: "exited", state: "exited", hasHealthcheck: false, health: null },
          { name: "paused", state: "paused", hasHealthcheck: false, health: null },
          { name: "missing-data", state: "running", hasHealthcheck: null, health: "unknown" },
        ], images: [] }], containers: [], images: [] },
  };
  const unchecked = {
    id: "compute-unchecked", parentDeviceId: uncheckedParent.id,
    parentDevice: uncheckedParent, provider: "proxmox", providerInstanceId: "502",
    type: "lxc", name: "Unchecked Docker host", status: "running", node: "rack-b",
    discoveryState: "current", dockerDiscoveryState: { state: "successful" },
    updateState: { state: "up_to_date" }, ansible: { enabled: false },
    docker: { available: true, projects: [{ name: "No-check project", containers: [
      { name: "worker", state: "running", hasHealthcheck: false, health: null },
    ], images: [] }], containers: [], images: [] },
  };
  const unavailable = {
    id: "compute-unavailable", parentDeviceId: unavailableParent.id,
    parentDevice: unavailableParent, provider: "proxmox", providerInstanceId: "503",
    type: "vm", name: "Stale Docker host", status: "running", node: "rack-c",
    discoveryState: "unavailable", dockerDiscoveryState: { state: "failed" },
    updateState: { state: "unknown" }, ansible: { enabled: false },
    docker: { available: true, projects: [{ name: "Stale project", containers: [
      { name: "formerly-healthy", state: "running", hasHealthcheck: true,
        health: "healthy" },
    ], images: [] }], containers: [], images: [] },
  };
  const instances = [mixed, unchecked, unavailable];
  await page.route("**/api/compute**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/compute") return json(route, { instances, ansibleEnabled: true });
    if (path.endsWith("/jobs")) return json(route, { jobs: [] });
    const instance = instances.find((item) => path === `/api/compute/${item.id}`);
    return instance ? json(route, { instance }) : json(route, { error: "not found" }, 404);
  });
  await page.route("**/api/settings/ansible", (route) => json(route, { controller: null }));

  await signIn(page);
  await page.getByRole("tab", { name: "Compute" }).click();

  await expect(page.locator("#compute-summary")).toContainText("2 online · 1 offline");
  await expect(page.locator("#compute-summary")).toContainText(
    "1 healthy · 1 unhealthy · 1 starting");
  await expect(page.locator("#compute-summary")).toContainText(
    "4 no healthcheck · 2 unknown");
  const mixedHost = page.locator(".compute-host").filter({ hasText: "Rack hypervisor" });
  await expect(mixedHost.locator(".compute-host-header")).toContainText("Docker · 1 unhealthy");
  const uncheckedHost = page.locator(".compute-host").filter({ hasText: "Utility hypervisor" });
  await expect(uncheckedHost.locator(".compute-host-header")).toContainText("Docker · Running");
  await expect(uncheckedHost.locator(".compute-host-header")).not.toContainText("Healthy");
  const unavailableHost = page.locator(".compute-host").filter({ hasText: "Offline hypervisor" });
  await expect(unavailableHost.locator(".compute-host-header")).toContainText("Offline");
  await expect(unavailableHost.locator(".compute-host-header")).toContainText("Docker · Unknown");

  await mixedHost.locator(".compute-card").click();
  const modal = page.locator("#compute-modal");
  await expect(modal.getByText("Mixed project", { exact: true })).toBeVisible();
  await expect(modal.locator(".compose-project-header")).toContainText("1 unhealthy");
  for (const [name, status] of [["healthy", "Healthy"], ["unhealthy", "Unhealthy"],
    ["starting", "Starting"],
    ["restarting", "Restarting"], ["exited", "Exited"], ["paused", "Paused"],
    ["missing-data", "Unknown"]]) {
    const row = modal.locator(".container-row").filter({
      has: page.getByText(name, { exact: true }),
    });
    await expect(row).toContainText(status);
  }
  const uncheckedRow = modal.locator(".container-row").filter({ hasText: "unchecked" });
  await expect(uncheckedRow).toContainText("Running");
  await expect(uncheckedRow).toContainText("No healthcheck");
  const noHealthcheck = uncheckedRow.locator(".compute-status").filter({ hasText: "No healthcheck" });
  await expect(noHealthcheck).toHaveAttribute("title",
    "This container is running, but its image or Compose configuration does not define a Docker healthcheck.");
  await page.getByRole("button", { name: "Close" }).click();

  await uncheckedHost.locator(".compute-card").click();
  await expect(modal.locator(".compose-project-header")).toContainText("Running");
  await expect(modal.locator(".compose-project-header")).not.toContainText("Healthy");
  await page.getByRole("button", { name: "Close" }).click();

  await unavailableHost.locator(".compute-card").click();
  await expect(modal.locator(".compose-project-header")).toContainText("Unknown");
  await expect(modal.locator(".container-row")).toContainText("Unknown");
  await expect(modal.locator(".container-row")).not.toContainText("Healthy");

  for (const viewport of [{ width: 1024, height: 600 }, { width: 800, height: 480 },
    { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    expect(await modal.locator(".modal-card").evaluate(
      (element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  }
});

test("Compute renders loading, empty, and error states", async ({ page }) => {
  let release;
  let mode = "loading";
  const waiting = new Promise((resolve) => { release = resolve; });
  await page.route("**/api/compute", async (route) => {
    if (mode === "loading") {
      await waiting;
      mode = "empty";
      return json(route, { instances: [], ansibleEnabled: false });
    }
    return json(route, { error: "inventory unavailable" }, 503);
  });

  await signIn(page);
  await page.getByRole("tab", { name: "Compute" }).click();
  await expect(page.locator("#compute-empty")).toContainText("Loading compute inventory");
  release();
  await expect(page.locator("#compute-empty")).toContainText("No compute workloads discovered");
  await page.getByRole("tab", { name: "Devices" }).click();
  await page.getByRole("tab", { name: "Compute" }).click();
  await expect(page.locator("#compute-empty")).toContainText("Couldn't load Compute");
  await expect(page.locator("#compute-empty")).toContainText("inventory unavailable");
});

test("Compute explains that maintenance requires Ansible setup", async ({ page }) => {
  const instance = {
    id: "compute-unmanaged", parentDeviceId: "proxmox-1", provider: "proxmox",
    providerInstanceId: "101", type: "vm", name: "Unmanaged workload",
    status: "running", cpuCores: 1, memoryBytes: 536870912,
    discoveryState: "current", ansible: { enabled: false },
    updateState: { state: "unknown" },
    parentDevice: { id: "proxmox-1", name: "Configured Proxmox",
      host: "192.0.2.50", driverId: "proxmox.ve" },
  };
  await page.route("**/api/compute", (route) => json(route, {
    instances: [instance], ansibleEnabled: false,
    summary: { workloads: 1, running: 1, stopped: 0, containers: 0,
      healthyContainers: 0, needsUpdates: 0 },
  }));

  await signIn(page);
  await page.getByRole("tab", { name: "Compute" }).click();

  await expect(page.locator("#compute-ansible-setup")).toContainText(
    "Set up Ansible in Settings to check workload updates and discover Docker.");
  await expect(page.locator(".compute-card")).toContainText("UpdatesSet up Ansible");
  await expect(page.locator(".compute-card")).not.toContainText("Docker");
  await expect(page.getByRole("button", { name: "Docker", exact: true })).toBeHidden();
  await page.locator("#compute-ansible-setup").getByRole("link", { name: "Open Settings" }).click();
  await expect(page.getByRole("tab", { name: "Settings" })).toHaveAttribute("aria-selected", "true");
});

test("Compute mapping persists and refreshes managed unknown card actions", async ({ page }) => {
  let instance = {
    id: "compute-mapping", parentDeviceId: "proxmox-1", provider: "proxmox",
    providerInstanceId: "202", type: "lxc", name: "immich",
    status: "running", cpuCores: 2, memoryBytes: 2147483648,
    discoveryState: "current", updateState: { state: "unknown" },
    ansible: { enabled: false, controllerId: null, inventoryHost: null,
      updateCheckEligible: false, updateEligible: false,
      dockerDiscoveryEligible: false, dockerUpdateCheckEligible: false,
      dockerUpdateModes: [] },
    parentDevice: { id: "proxmox-1", name: "Configured Proxmox",
      host: "192.0.2.50", driverId: "proxmox.ve" },
  };
  const controller = {
    id: "primary", enabled: true, displayName: "Ansible controller",
    inventory: { hosts: [
      { name: "immich", address: "192.0.2.60", groups: ["containers"] },
    ], groups: [] },
  };
  let mappingPayload = null;
  await page.route("**/api/compute**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/compute/compute-mapping/ansible") {
      mappingPayload = request.postDataJSON();
      instance = { ...instance, ansible: {
        enabled: true, controllerId: "primary", inventoryHost: "immich",
        updateCheckEligible: false, updateEligible: false,
        dockerDiscoveryEligible: false, dockerUpdateCheckEligible: false,
        dockerUpdateModes: [],
      } };
      return json(route, { instance });
    }
    if (path === "/api/compute/compute-mapping/jobs") return json(route, { jobs: [] });
    if (path === "/api/compute/compute-mapping") return json(route, {
      instance: { ...instance, suggestedMappings: instance.ansible.enabled ? [] : [
        { controllerId: "primary", inventoryHost: "immich", signals: ["exact_hostname"] },
      ] },
    });
    if (path === "/api/compute") return json(route, {
      instances: [instance], ansibleEnabled: true,
      summary: { workloads: 1, running: 1, stopped: 0, containers: 0,
        healthyContainers: 0, needsUpdates: 0 },
    });
    return json(route, { error: "unhandled compute route" }, 404);
  });
  await page.route("**/api/settings/ansible", (route) => json(route, { controller }));

  await signIn(page);
  await page.getByRole("tab", { name: "Compute" }).click();
  const card = page.locator(".compute-card").filter({ hasText: "immich" });
  await expect(card).toContainText("UpdatesNot managed");
  await expect(card).not.toContainText("Docker");

  await card.click();
  const mappingSelect = page.getByRole("combobox", { name: "Ansible inventory host" });
  await expect(mappingSelect).toHaveValue("immich");
  await expect(page.locator("#compute-modal")).toContainText(
    "Ansible management is off. Confirm an inventory host to enable update and Docker checks.");
  await page.getByRole("button", { name: "Manage with Ansible as immich" }).click();

  await expect.poll(() => mappingPayload).toEqual({
    enabled: true, controllerId: "primary", inventoryHost: "immich",
  });
  await expect(page.locator("#compute-modal")).toContainText("Managed by Ansible as immich");
  await expect(page.locator("#compute-modal")).toContainText(
    "Mapping saved. To enable checks, approve OS update check and Docker discovery playbooks");
  await expect(page.locator("#compute-modal").getByRole("button", { name: "Check Updates" })).toBeDisabled();
  await expect(page.locator("#compute-modal").getByRole("button", { name: "Discover Docker" })).toBeDisabled();
  await expect(page.locator("#compute-modal").getByRole("button", { name: "Open Ansible settings" })).toBeVisible();
  await expect(card).toContainText("UpdatesUnknown");
  await expect(card).not.toContainText("Docker");
  await expect(card.locator("button")).toHaveCount(0);

  await page.reload();
  await expect(page.locator("#app")).toBeVisible();
  await expect(page.locator("#compute-modal")).toContainText("Managed by Ansible as immich");
  await expect(page.getByRole("combobox", { name: "Ansible inventory host" })).toHaveValue("immich");
  const reloadedCard = page.locator(".compute-card").filter({ hasText: "immich" });
  await expect(reloadedCard).toContainText("UpdatesUnknown");
  await expect(reloadedCard).not.toContainText("Docker");
  await expect(reloadedCard.locator("button")).toHaveCount(0);
  await page.locator("#compute-modal").getByRole("button", { name: "Open Ansible settings" }).click();
  await expect(page.locator("#compute-modal")).toBeHidden();
  await expect(page.getByRole("tab", { name: "Settings" })).toHaveAttribute("aria-selected", "true");
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
    id: "uk", name: "United Kingdom",
    enabled: true, peerUuid: "peer-1", instanceUuid: "instance-1", gatewayUuid: "",
    country: "United Kingdom", city: "London", maxCandidates: 20,
    discoveryIntervalSeconds: 3600, preferredOwners: ["Example Hosting"],
    excludedOwners: [], includeUnknownOwners: false, handshakeWarningSeconds: 300,
    compatibilityTargets: [{ ...target }], notes: "",
  };
  let includeTargets = true;
  let partialDiscovery = false;
  let profiles = [profile];
  let switchResult = { ok: true, rollback: null, message: "Endpoint switched and verified." };
  let delaySwitch = false;
  let discoveryRefreshes = 0;
  const snapshot = (selectedProfile = profiles[0]) => ({
    profileConfigured: true,
    profile: { ...selectedProfile, compatibilityTargets: includeTargets ? [{ ...target }] : [] },
    current: {
      configured: true, endpointIp: "192.0.2.10", endpointPort: 51820,
      serverId: 956247,
      hostname: "uk-current.nordvpn.com", owner: "Example Hosting", asn: "64500",
      classification: "Eligible", health: "Healthy",
      peerUuid: "peer-1", instanceUuid: "instance-1", candidateId: "current",
      status: { latestHandshake: Math.floor(Date.now() / 1000) - 42, handshakeAge: 42,
        receivedBytes: 1200, transmittedBytes: 800, status: "online" },
      utilization: { percent: 23, observedAt: Math.floor(Date.now() / 1000) - 30,
        source: "NordVPN", history: [
          [Math.floor(Date.now() / 1000) - 30, 23],
        ] },
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
      peers: [
        { uuid: "peer-1", name: "NordVPN UK peer" },
        { uuid: "peer-2", name: "NordVPN Netherlands peer" },
      ],
      instances: [
        { uuid: "instance-1", name: "WireGuard UK tunnel" },
        { uuid: "instance-2", name: "WireGuard Netherlands tunnel" },
      ],
      locations: [
        { id: 1, name: "Netherlands", cities: [{ id: 11, name: "Amsterdam" }] },
        { id: 2, name: "United Kingdom", cities: [
          { id: 21, name: "London" }, { id: 22, name: "Manchester" },
        ] },
      ],
    });
    if (url.pathname.endsWith("/compatibility")) return json(route, { ok: true });
    if (url.pathname.endsWith("/switch")) {
      if (delaySwitch) await new Promise((resolve) => setTimeout(resolve, 1500));
      return json(route, switchResult);
    }
    if (request.method() === "POST" && url.pathname.endsWith("/vpn-endpoints")) {
      savedPayload = request.postDataJSON();
      const created = { ...profile, ...savedPayload, id: "nl" };
      profiles.push(created);
      return json(route, { profile: created }, 201);
    }
    if (request.method() === "PATCH") {
      savedPayload = request.postDataJSON();
      const profileId = url.pathname.split("/").pop();
      const index = profiles.findIndex((item) => item.id === profileId);
      const targetIndex = index >= 0 ? index : 0;
      profiles[targetIndex] = { ...profiles[targetIndex], ...savedPayload };
      profile = profiles[0];
      return json(route, { profile: profiles[targetIndex] });
    }
    if (url.searchParams.get("refresh") === "1") discoveryRefreshes += 1;
    const profileId = url.pathname.split("/").pop();
    const selected = profiles.find((item) => item.id === profileId);
    if (selected) return json(route, snapshot(selected));
    const first = snapshot(profiles[0]);
    return json(route, { ...first, profiles: profiles.map((item) => snapshot(item)) });
  });

  await signIn(page);
  await page.locator(".card").filter({ hasText: "OPNsense firewall" }).click();
  const section = page.locator(".vpn-endpoint-section");
  const currentCard = section.locator(".vpn-current-card");
  const serverHeading = currentCard.locator(".vpn-server-head");
  await expect(serverHeading.getByText("uk-current.nordvpn.com", { exact: true })).toBeVisible();
  await expect(currentCard.getByText("Healthy", { exact: true })).toBeVisible();
  const utilization = serverHeading.getByRole(
    "button", { name: "Server utilization 23%. View history" });
  await expect(utilization).toHaveText("23%");
  await expect(currentCard.locator("canvas")).toHaveCount(0);
  await utilization.click();
  const utilizationDialog = page.locator(".vpn-utilization-dialog");
  await expect(utilizationDialog.getByRole("heading", { name: "Server utilization" })).toBeVisible();
  await expect(utilizationDialog.locator(".c-now")).toHaveText("23 %");
  await expect(utilizationDialog.locator("canvas"))
    .toHaveAttribute("aria-label", /now 23 %.*min 23 %.*peak 23 %/);
  await expect.poll(() => utilizationDialog.locator("canvas").evaluate((canvas) => {
    const pixels = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
    return Array.from(pixels).some((_value, index) => index % 4 === 3 && pixels[index] > 0);
  })).toBe(true);
  await expect(utilizationDialog.getByText(
    "One observation recorded. The trend line will appear after the next provider observation."))
    .toBeVisible();
  await utilizationDialog.getByRole("button", { name: "Close" }).click();
  await expect(utilizationDialog).toHaveCount(0);
  await expect(currentCard.locator(".vpn-pill")).toHaveCount(1);
  await expect(currentCard.getByText("Eligible", { exact: true })).toHaveCount(0);
  await expect(currentCard.getByText("Endpoint state", { exact: true })).toHaveCount(0);
  await expect(currentCard.getByText("Discovery timestamp", { exact: true })).toHaveCount(0);
  await expect(section.getByText("1 verified", { exact: true })).toBeVisible();
  await expect(section.locator(".vpn-details")).not.toHaveAttribute("open", "");
  await expect(section.locator(".vpn-candidates")).toHaveCount(0);

  await expect(section.getByRole("button", { name: "More" })).toHaveCount(0);
  const settingsButton = section.getByRole("button", { name: "Settings" });
  const historyButton = section.getByRole("button", { name: "History", exact: true });
  await expect(settingsButton).toBeVisible();
  await expect(settingsButton.locator("svg")).toHaveCount(1);
  await expect(historyButton).toBeVisible();
  await expect(historyButton.locator("svg")).toHaveCount(1);
  await historyButton.click();
  const historyDialog = page.locator(".vpn-history-dialog");
  await expect(historyDialog.getByRole("heading", { name: "VPN endpoint history" })).toBeVisible();
  await expect(historyDialog.getByText("No endpoint history has been recorded.")).toBeVisible();
  await historyDialog.getByRole("button", { name: "Close" }).click();
  await settingsButton.evaluate((button) => { button.click(); button.click(); });
  await expect(page.locator(".vpn-settings-dialog")).toHaveCount(1);
  await expect(page.getByText("Network preferences", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Compatibility targets", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Country")).toHaveValue("United Kingdom");
  await expect(page.getByLabel("City")).toHaveValue("London");
  await expect(page.getByLabel("City").locator("option")).toHaveText([
    "Any city", "London", "Manchester",
  ]);
  await page.getByLabel("Maximum candidates").fill("9");
  await page.getByLabel("Discovery interval (seconds)").fill("900");
  await page.getByLabel("Handshake warning threshold (seconds)").fill("240");
  const patchRequest = page.waitForRequest((request) =>
    request.url().endsWith("/api/devices/opnsense-1/vpn-endpoints/uk") && request.method() === "PATCH");
  await page.getByRole("button", { name: "Save settings" }).click();
  await patchRequest;
  await expect(page.locator(".vpn-settings-dialog")).toHaveCount(0);
  expect(savedPayload.maxCandidates).toBe(9);
  expect(savedPayload.discoveryIntervalSeconds).toBe(900);
  expect(savedPayload.handshakeWarningSeconds).toBe(240);
  expect(savedPayload).not.toHaveProperty("preferredOwners");
  expect(savedPayload).not.toHaveProperty("excludedOwners");
  expect(savedPayload).not.toHaveProperty("compatibilityTargets");

  const reopenedSettingsButton = section.getByRole("button", { name: "Settings" });
  await reopenedSettingsButton.click();
  await expect(page.locator(".vpn-settings-dialog")).toHaveCount(1);
  await expect(page.getByLabel("Maximum candidates")).toHaveValue("9");
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(reopenedSettingsButton).toBeFocused();

  await section.getByRole("button", { name: "Add VPN endpoint" }).click();
  await expect(page.getByRole("heading", { name: "Add VPN endpoint" })).toBeVisible();
  await page.getByLabel("Profile name").fill("Netherlands");
  await page.getByLabel("Country").selectOption("Netherlands");
  await expect(page.getByLabel("City").locator("option")).toHaveText(["Any city", "Amsterdam"]);
  await page.getByLabel("City").selectOption("Amsterdam");
  await page.getByLabel("WireGuard instance").selectOption("instance-2");
  await page.getByLabel("WireGuard peer").selectOption("peer-2");
  const createRequest = page.waitForRequest((request) =>
    request.url().endsWith("/api/devices/opnsense-1/vpn-endpoints") && request.method() === "POST");
  await page.getByRole("button", { name: "Save settings" }).click();
  await createRequest;
  await expect(page.locator(".vpn-settings-dialog")).toHaveCount(0);
  await expect(section.getByRole("tab", { name: "United Kingdom" })).toBeVisible();
  const netherlandsTab = section.getByRole("tab", { name: "Netherlands" });
  await expect(netherlandsTab).toHaveAttribute("aria-selected", "true");
  await netherlandsTab.focus();
  await page.keyboard.press("ArrowLeft");
  await expect(section.getByRole("tab", { name: "United Kingdom" }))
    .toHaveAttribute("aria-selected", "true");

  const find = section.getByRole("button", { name: "Find replacement" });
  const panel = section.locator(".vpn-candidates");
  await find.click();
  await expect.poll(() => discoveryRefreshes).toBe(1);
  await expect(panel.locator(":scope > .vpn-candidate-list > .vpn-candidate-card")).toHaveCount(3);
  await find.click();
  await expect.poll(() => discoveryRefreshes).toBe(2);
  await expect(panel).toHaveCount(1);
  await expect(panel.locator(":scope > .vpn-candidate-list > .vpn-candidate-card")).toHaveCount(3);
  await expect(panel.getByText("Eligible", { exact: true })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Show all candidates" })).toBeVisible();
  const others = panel.locator(".vpn-other-candidates");
  await expect(others).not.toHaveAttribute("open", "");
  await expect(others.getByText("uk1005.nordvpn.com", { exact: true })).toBeHidden();

  await panel.locator(":scope > .vpn-candidate-list > .vpn-candidate-card").first()
    .getByRole("button", { name: "Use" }).click();
  await expect(page.locator("#dialog-title")).toHaveText("Change VPN endpoint?");
  await expect(page.locator("#dialog-msg")).toContainText("Current");
  await expect(page.locator("#dialog-msg")).toContainText("Replacement");
  await expect(page.locator("#dialog-msg")).toContainText("regenerate its WireGuard configuration");
  await expect(page.locator("#dialog-msg")).toContainText("restart the selected instance");
  await expect(page.locator("#dialog-ok")).toHaveText("Apply and verify");
  await page.locator("#dialog-cancel").click();
  await expect(page.locator("#dialog")).toBeHidden();

  switchResult = { ok: false, rollback: null,
    message: "Endpoint change was not applied, so the existing OPNsense configuration was left unchanged." };
  delaySwitch = true;
  await panel.locator(":scope > .vpn-candidate-list > .vpn-candidate-card").first()
    .getByRole("button", { name: "Use" }).click();
  await page.locator("#dialog-ok").click();
  await expect(page.locator("#toasts")).toContainText("left unchanged");
  await expect(section.locator(".vpn-operation")).toHaveCount(0);

  await panel.locator(":scope > .vpn-candidate-list > .vpn-candidate-card").first()
    .getByRole("button", { name: "View checks" }).click();
  await expect(page.getByRole("heading", { name: "Update validation" })).toBeVisible();
  await expect(page.getByLabel("Validation target")).toHaveValue("corporate-portal");
  await page.getByRole("button", { name: "Cancel" }).click();

  switchResult = { ok: true, rollback: null, message: "Endpoint switched and verified." };
  delaySwitch = false;
  await panel.locator(":scope > .vpn-candidate-list > .vpn-candidate-card").first()
    .getByRole("button", { name: "Use" }).click();
  await page.locator("#dialog-ok").click();
  await expect(page.locator("#toasts")).toContainText("Endpoint switched and verified.");
  await expect(panel).toHaveCount(0);

  const details = section.locator(".vpn-details");
  await details.locator("summary").focus();
  await page.keyboard.press("Enter");
  await expect(details).toHaveAttribute("open", "");
  const serverIdLabel = details.locator("dt", { hasText: "Server ID" });
  await expect(serverIdLabel).toHaveCount(1);
  await expect(serverIdLabel.locator("xpath=following-sibling::dd[1]")).toHaveText("956247");
  for (const label of ["WireGuard peer", "WireGuard instance", "Received bytes", "Sent bytes"]) {
    await expect(details.locator("dt", { hasText: label })).toHaveCount(0);
  }
  await expect(details.locator("dt", { hasText: "Ownership classification" })).toHaveCount(0);
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
