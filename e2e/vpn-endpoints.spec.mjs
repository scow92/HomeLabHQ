import { expect, test } from "@playwright/test";
import {
  credentials, device, json, mockRoster, roster, signIn,
} from "./support/fixtures.mjs";

test.describe.configure({ mode: "serial" });

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
