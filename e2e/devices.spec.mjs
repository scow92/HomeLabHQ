import { expect, test } from "@playwright/test";
import {
  credentials, device, json, mockRoster, roster, signIn,
} from "./support/fixtures.mjs";

test.describe.configure({ mode: "serial" });

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
