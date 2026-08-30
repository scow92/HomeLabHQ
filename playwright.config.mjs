import { defineConfig } from "@playwright/test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const dataDir = mkdtempSync(join(tmpdir(), "homelabhq-e2e-"));

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:8877",
    browserName: "chromium",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "setup",
      testMatch: /auth\.setup\.mjs/,
    },
    {
      name: "chromium",
      dependencies: ["setup"],
      testIgnore: /auth\.setup\.mjs/,
    },
  ],
  webServer: {
    command: `${process.env.PYTHON ?? "python3"} -m backend.run`,
    url: "http://127.0.0.1:8877/healthz",
    reuseExistingServer: false,
    env: {
      ...process.env,
      HLHQ_DATA_DIR: dataDir,
      HLHQ_ICON_HTTP_PORT: "0",
      HLHQ_PORT: "8877",
    },
  },
});
