import { expect, Page, test } from '@playwright/test';

export const username = process.env.AIRZEN_TEST_USERNAME || '';
export const password = process.env.AIRZEN_TEST_PASSWORD || '';
export const hasCredentials = Boolean(username && password);
export const allowDbWrites = process.env.AIRZEN_E2E_DB_WRITES === 'true';

export async function login(page: Page) {
  test.skip(!hasCredentials, 'AIRZEN_TEST_USERNAME and AIRZEN_TEST_PASSWORD are required');
  await page.goto('/');
  await page.locator('#login-username').fill(username);
  await page.locator('#login-password').fill(password);
  await page.locator('#login-form button[type="submit"]').click();
  await expect(page.locator('body')).toHaveClass(/authenticated/, { timeout: 20_000 });
}

export async function openPanel(page: Page, panel: string) {
  await page.evaluate((name) => (window as any).switchTab(name), panel);
  await expect(page.locator(`#${panel}`)).toHaveClass(/active/);
}

export function e2ePhone() {
  const suffix = String(Date.now()).slice(-8);
  return `+65${suffix}`;
}
