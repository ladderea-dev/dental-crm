import { expect, test } from '@playwright/test';
import { allowDbWrites, e2ePhone, hasCredentials, login, openPanel } from './helpers';

test.describe('authentication', () => {
  test('login page loads and protected API fails logged out', async ({ page, request }) => {
    await page.goto('/');
    await expect(page.locator('#login-form')).toBeVisible();
    expect((await request.get('/api/crm/contacts')).status()).toBe(401);
  });

  test('invalid login is rejected', async ({ page }) => {
    await page.goto('/');
    await page.locator('#login-username').fill('E2E_TEST_INVALID');
    await page.locator('#login-password').fill('invalid');
    await page.locator('#login-form button[type="submit"]').click();
    await expect(page.locator('#login-error')).toContainText(/invalid|failed/i);
  });

  test('valid login and logout work', async ({ page }) => {
    test.skip(!hasCredentials, 'test credentials not configured');
    await login(page);
    await page.getByRole('button', { name: /logout/i }).click();
    await expect(page.locator('#login-form')).toBeVisible();
  });
});

test.describe('CRM contact lifecycle', () => {
  test('creates, searches, edits, persists, and cleans up one E2E contact', async ({ page }) => {
    test.skip(!hasCredentials, 'test credentials not configured');
    test.skip(!allowDbWrites, 'Set AIRZEN_E2E_DB_WRITES=true only for a dedicated local/staging database');
    await login(page);
    const phone = e2ePhone();
    const name = `E2E_TEST_CONTACT_${Date.now()}`;
    try {
      const created = await page.request.post('/api/crm/contacts', { data: { phone_number: phone, lead_name: name, source: 'E2E_TEST', service_type: 'E2E_TEST_SERVICE', crm_notes: 'E2E_TEST_CREATED' } });
      expect(created.ok()).toBeTruthy();
      await openPanel(page, 'crm');
      await page.locator('#crm-q').fill(phone);
      await page.getByRole('button', { name: 'Apply Filter' }).click();
      await expect(page.locator('#crm-list')).toContainText(name);

      const updated = await page.request.put(`/api/crm/contacts/${encodeURIComponent(phone)}`, { data: { lead_name: `${name}_EDITED`, crm_status: 'Contacted', crm_notes: 'E2E_TEST_UPDATED' } });
      expect(updated.ok()).toBeTruthy();
      await page.reload();
      await openPanel(page, 'crm');
      await page.locator('#crm-q').fill(phone);
      await page.getByRole('button', { name: 'Apply Filter' }).click();
      await expect(page.locator('#crm-list')).toContainText(`${name}_EDITED`);
    } finally {
      const removed = await page.request.delete(`/api/crm/contacts/${encodeURIComponent(phone)}`);
      expect([200, 404]).toContain(removed.status());
    }
  });
});
