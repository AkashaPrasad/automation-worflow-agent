import { expect, test } from '@playwright/test';
import { API_BASE, apiContext, freshWorkspace, tid, trackConsoleErrors } from './helpers';

test.describe('smoke', () => {
  test('landing loads and templates render from the API', async ({ page }) => {
    const errors = trackConsoleErrors(page);
    const { api } = await freshWorkspace(page);
    const cfg = await (await api.get('/api/config')).json();
    expect(cfg.templates.length).toBeGreaterThanOrEqual(4);

    await page.goto('/');
    await expect(tid(page, 'composer-input')).toBeVisible();
    await expect(tid(page, 'composer-submit')).toBeVisible();
    await expect(tid(page, 'autonomy-select')).toBeVisible();
    const options = await tid(page, 'autonomy-select').locator('option').evaluateAll((os) => os.map((o) => (o as HTMLOptionElement).value));
    expect(options).toEqual(expect.arrayContaining(['cautious', 'balanced', 'autonomous']));

    const cards = page.getByTestId('template-card');
    await expect(cards.first()).toBeVisible();
    expect(await cards.count()).toBe(cfg.templates.length);
    for (const t of cfg.templates) {
      await expect(page.locator(`[data-testid="template-card"][data-template-id="${t.id}"]`)).toBeVisible();
    }
    expect(errors, errors.join('\n')).toEqual([]);
  });

  test('health and config are OK', async () => {
    const anon = await apiContext();
    const h = await anon.get('/api/health');
    expect(h.ok()).toBeTruthy();
    const health = await h.json();
    expect(health.ok).toBe(true);
    expect(health).toHaveProperty('models');
    expect(health).toHaveProperty('llm_configured');
    expect(health).toHaveProperty('judge_configured');
    console.log(`health @ ${API_BASE}: ${JSON.stringify(health)}`);
  });

  test('all routes render without console errors', async ({ page }) => {
    const errors = trackConsoleErrors(page);
    await freshWorkspace(page);

    await page.goto('/workspace');
    for (const app of ['mail', 'calendar', 'docs', 'sheets', 'notion', 'slack', 'meetings', 'outbox']) {
      const tab = tid(page, `workspace-tab-${app}`);
      await expect(tab, `tab ${app}`).toBeVisible();
      await tab.click();
    }
    await expect(tid(page, 'workspace-reset')).toBeVisible();

    await page.goto('/memory');
    await expect(page.locator('body')).not.toBeEmpty();
    await expect(page.getByText(/memor/i).first()).toBeVisible();

    await page.goto('/architecture');
    await expect(page.getByText(/muse|jev|policy|calibrated/i).first()).toBeVisible();

    // SPA fallback: deep link to an unknown run must not white-screen
    await page.goto('/runs/does-not-exist');
    await expect(page.locator('body')).not.toBeEmpty();

    // known-benign: a 404 for the missing run is expected to log a resource error
    const unexpected = errors.filter((e) => !/404|not found/i.test(e));
    expect(unexpected, unexpected.join('\n')).toEqual([]);
  });
});
