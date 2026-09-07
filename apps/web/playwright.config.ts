import { defineConfig, devices } from "@playwright/test"

const webPort = 3310
const apiPort = 3311

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    // Drives `MotionConfig reducedMotion="user"` and the CSS reduced-motion
    // override so assertions never race an in-flight animation.
    contextOptions: { reducedMotion: "reduce" },
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command: `node e2e/mock-api.mjs --port ${apiPort}`,
      url: `http://127.0.0.1:${apiPort}/__e2e/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
    {
      command: `API_INTERNAL_URL=http://127.0.0.1:${apiPort} pnpm dev --host 127.0.0.1 --port ${webPort}`,
      url: `http://127.0.0.1:${webPort}/en/login`,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
})
