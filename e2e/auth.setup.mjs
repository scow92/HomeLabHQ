import { expect, test } from "@playwright/test";
import {
  credentials, device, json, mockRoster, roster, signIn,
} from "./support/fixtures.mjs";

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
