import { expect, test } from '@playwright/test';
import { expectNoHorizontalOverflow, freshWorkspace, startRunFromTemplate, tid } from './helpers';

test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

test.describe('mobile 390px', () => {
  test('landing renders without horizontal overflow', async ({ page }) => {
    await freshWorkspace(page);
    await page.goto('/');
    await expect(tid(page, 'composer-input')).toBeVisible();
    await expect(page.getByTestId('template-card').first()).toBeVisible();
    await expectNoHorizontalOverflow(page);
  });

  test('run console renders without horizontal overflow', async ({ page }) => {
    const { api } = await freshWorkspace(page);
    const runId = await startRunFromTemplate(page, 'inbox-triage', 'cautious');
    await expect(tid(page, 'run-status')).toBeVisible({ timeout: 60_000 });
    await expect(tid(page, 'plan-tree')).toBeVisible({ timeout: 3 * 60_000 });
    await page.waitForTimeout(1000);
    await expectNoHorizontalOverflow(page);
    await api.post(`/api/runs/${runId}/cancel`);
  });
});
