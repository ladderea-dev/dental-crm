import { expect, test } from '@playwright/test';
import { login, openPanel } from './helpers';

test.beforeEach(async ({ page }) => {
  await login(page);
  const conversation = { id: 'E2E_TEST_CONV', phone_number: '+6500000000', contact_name: 'E2E_TEST_CUSTOMER', status: 'open', ai_enabled: true, assigned_to: '', unread_count: 1, last_message: 'E2E_TEST hello', last_message_at: new Date().toISOString() };
  await page.route('**/api/whatsapp/conversations?**', route => route.fulfill({ json: { conversations: [conversation] } }));
  await page.route('**/api/whatsapp/conversations/E2E_TEST_CONV/messages?**', route => route.fulfill({ json: { messages: [{ id: 'E2E_TEST_MSG', direction: 'inbound', message_type: 'text', message_text: 'E2E_TEST hello', created_at: new Date().toISOString() }] } }));
  await page.route('**/api/whatsapp/conversations/E2E_TEST_CONV', async route => {
    if (route.request().method() === 'PATCH') return route.fulfill({ json: { success: true } });
    return route.continue();
  });
  await page.route('**/api/whatsapp/conversations/E2E_TEST_CONV/send', route => route.fulfill({ status: 418, json: { detail: 'E2E live send blocked by test' } }));
  await openPanel(page, 'whatsapp');
});

test('conversation loads and controls update without a live send', async ({ page }) => {
  await expect(page.locator('#wa-conv-list')).toContainText('E2E_TEST_CUSTOMER');
  await page.getByText('E2E_TEST_CUSTOMER').click();
  await expect(page.locator('#wa-messages')).toContainText('E2E_TEST hello');
  await page.locator('#wa-ai-toggle-btn').click();
  await expect(page.locator('#wa-ai-toggle-btn')).toContainText('OFF');
  await page.getByRole('button', { name: /take over/i }).click();
  await page.locator('#wa-assign-input').fill('E2E_TEST_STAFF');
  await page.getByRole('button', { name: 'Assign', exact: true }).click();
});

test('empty composer does not send and template selector loads', async ({ page }) => {
  await page.getByText('E2E_TEST_CUSTOMER').click();
  let sends = 0;
  page.on('request', req => { if (req.url().endsWith('/send') && req.method() === 'POST') sends += 1; });
  await page.locator('#wa-reply-input').fill('   ');
  await page.getByRole('button', { name: 'Send message' }).click();
  expect(sends).toBe(0);
  await page.getByRole('button', { name: 'Template', exact: true }).click();
  await expect(page.locator('#wa-template-modal')).toBeVisible();
});
