import { defineConfig, devices } from '@playwright/test';

/**
 * One browser, one scenario, run against a real gateway.
 *
 * `webServer` starts both the gateway and Vite, so `pnpm test:e2e` needs no
 * setup — which matters because a test suite you have to prepare for is a test
 * suite that stops being run.
 */
// Built separately so `webServer` can be omitted entirely rather than set to
// undefined, which `exactOptionalPropertyTypes` rejects.
const servers = [
  {
    command: 'uv run uvicorn distillserve_gateway.main:app --host 127.0.0.1 --port 8000',
    cwd: '../..',
    url: 'http://127.0.0.1:8000/healthz',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: {
      DISTILLSERVE_MODE: 'sandbox',
      DISTILLSERVE_ENV: 'test',
      DISTILLSERVE_LOG_JSON: 'true',
    },
  },
  {
    command: 'pnpm dev --host 127.0.0.1 --port 5173',
    url: 'http://127.0.0.1:5173',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
];

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  // The scenario makes several live LLM calls and a cold Vite dev server
  // compiles the whole app on first navigation. Playwright's 30s default
  // is not enough for either, and a timeout there looks like a product bug.
  timeout: 180_000,
  expect: { timeout: 30_000 },
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : [['list']],

  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://127.0.0.1:5173',
    // Traces and screenshots only for failures: retaining them for every run
    // fills CI artifacts with megabytes nobody reads.
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    actionTimeout: 15_000,
  },

  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],

  ...(process.env.E2E_BASE_URL ? {} : { webServer: servers }),
});
