import { defineConfig, devices } from '@playwright/test';

// E2E_BASE_URL: frontend (default local Vite), E2E_API_BASE: backend (default local uvicorn)
// Production: E2E_BASE_URL=https://aiml.spacesdrive.cc E2E_API_BASE=https://api.aiml.spacesdrive.cc
export default defineConfig({
  testDir: '.',
  testMatch: /.*\.spec\.ts/,
  timeout: 6 * 60_000, // agent runs take minutes
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1, // runs are rate limited per workspace and hit shared LLM quotas
  retries: process.env.CI ? 1 : 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:5173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
