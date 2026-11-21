import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  use: { baseURL: process.env.CANCHA_TEST_URL ?? 'http://127.0.0.1:8765',
    channel: process.env.CANCHA_BROWSER || undefined, launchOptions: { args: ['--no-proxy-server'] } },
});
