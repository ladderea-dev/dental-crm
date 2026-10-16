import { expect, test } from '@playwright/test';
import { login, openPanel } from './helpers';

test('groups one customer journey and keeps history collapsed', async ({ page }) => {
  await login(page);
  const rows = [
    { id: 'E2E_TEST_ACTIVE', name: 'E2E_TEST_CUSTOMER', phone: '+6500000000', date: '2026-12-20', time: '15:00', timezone: 'Asia/Singapore', service: 'E2E_TEST_SERVICE', staff_id: 'E2E_TEST_STAFF', staff_name: 'E2E_TEST_STAFF', status: 'booked', staff_notified: true },
    { id: 'E2E_TEST_HISTORY', name: 'E2E_TEST_CUSTOMER', phone: '+6500000000', date: '2026-12-19', time: '15:00', timezone: 'Asia/Singapore', service: 'E2E_TEST_SERVICE', staff_name: 'E2E_TEST_STAFF', status: 'rescheduled' },
  ];
  await page.route('**/api/appointments?**', route => route.fulfill({ json: rows }));
  await page.route('**/api/appointment-staff', route => route.fulfill({ json: { staff: [{ id: 'E2E_TEST_STAFF', name: 'E2E_TEST_STAFF', active: true }] } }));
  await page.route('**/api/appointments/settings', route => route.fulfill({ json: { timezone: 'Asia/Singapore', appointment_name: 'Appointment', duration_minutes: 60 } }));
  await openPanel(page, 'appointments');
  await expect(page.locator('.appt-customer-card')).toHaveCount(1);
  await expect(page.locator('.appt-customer-card')).toContainText('1 active');
  await expect(page.locator('.appt-customer-card')).toContainText('Asia/Singapore');
  await expect(page.locator('.appt-history')).not.toHaveAttribute('open', '');
});

test('reschedule submits once and reports staff notification result', async ({ page }) => {
  await login(page);
  const row = { id: 'E2E_TEST_ACTIVE', name: 'E2E_TEST_CUSTOMER', phone: '+6500000000', date: '2026-12-20', time: '15:00', timezone: 'Asia/Singapore', service: 'E2E_TEST_SERVICE', staff_name: 'E2E_TEST_STAFF', status: 'booked', staff_notified: false };
  await page.route('**/api/appointments?**', route => route.fulfill({ json: [row] }));
  await page.route('**/api/appointment-staff', route => route.fulfill({ json: { staff: [] } }));
  await page.route('**/api/appointments/settings', route => route.fulfill({ json: { timezone: 'Asia/Singapore', duration_minutes: 60 } }));
  let reschedules = 0;
  await page.route('**/api/appointments/E2E_TEST_ACTIVE/reschedule', route => { reschedules += 1; return route.fulfill({ json: { success: true, warning: '', staff_notification: { success: true } } }); });
  await openPanel(page, 'appointments');
  await page.getByRole('button', { name: 'Reschedule' }).click();
  await page.locator('#resch-date').fill('2026-12-21');
  await page.locator('#resch-time').fill('16:00');
  await page.getByRole('button', { name: 'Reschedule', exact: true }).last().click();
  await expect.poll(() => reschedules).toBe(1);
});
