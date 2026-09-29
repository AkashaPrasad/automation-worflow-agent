import { expect, test } from '@playwright/test';
import {
  driveToTerminal, freshWorkspace, getRun, pollRun, startRunFromTemplate, tid, waitForStatus, TERMINAL,
} from './helpers';

const ACTIVE = ['planning', 'shadowing', 'executing', 'verifying', 'awaiting_approval', 'clarifying', 'created'];

test.describe('controls', () => {
  test('pause and resume a running run', async ({ page }) => {
    const { api } = await freshWorkspace(page);
    const runId = await startRunFromTemplate(page, 'qbr-recap', 'balanced');
    await expect(tid(page, 'btn-pause')).toBeVisible({ timeout: 60_000 });

    // pause as early as possible while the run is still active
    const before = await getRun(api, runId);
    test.skip(TERMINAL.includes(before.run.status), 'run already finished before pause');
    await tid(page, 'btn-pause').click();
    await expect(tid(page, 'run-status')).toHaveText(/paused/i, { timeout: 30_000 });
    expect((await getRun(api, runId)).run.status).toBe('paused');

    // stays paused: no progression for a few seconds
    await page.waitForTimeout(4000);
    expect((await getRun(api, runId)).run.status).toBe('paused');

    await tid(page, 'btn-resume').click();
    await expect(tid(page, 'run-status')).not.toHaveText(/paused/i, { timeout: 30_000 });
    const after = await pollRun(api, runId, (x) => x.run.status !== 'paused', { timeout: 30_000, label: 'leave paused' });
    expect([...ACTIVE, ...TERMINAL]).toContain(after.run.status);

    await api.post(`/api/runs/${runId}/cancel`);
  });

  test('cancel stops a run', async ({ page }) => {
    const { api } = await freshWorkspace(page);
    const runId = await startRunFromTemplate(page, 'vendor-review', 'cautious');
    await expect(tid(page, 'btn-cancel')).toBeVisible({ timeout: 60_000 });
    await tid(page, 'btn-cancel').click();
    await expect(tid(page, 'run-status')).toHaveText(/cancelled/i, { timeout: 30_000 });
    expect((await getRun(api, runId)).run.status).toBe('cancelled');
  });

  test('rollback after completion compensates reversible effects', async ({ page }) => {
    const { api } = await freshWorkspace(page);
    const runId = await startRunFromTemplate(page, 'qbr-recap', 'autonomous');
    const d = await driveToTerminal(page, api, runId);
    test.skip(d.run.status !== 'completed', `run ended ${d.run.status}, nothing to roll back`);
    const applied = d.effects.filter((e: any) => e.status === 'applied' && e.compensation);
    test.skip(applied.length === 0, 'no compensable applied effects');

    await expect(tid(page, 'btn-rollback')).toBeVisible();
    await tid(page, 'btn-rollback').click();
    await pollRun(api, runId, (x) => x.effects.some((e: any) => e.status === 'compensated'), { timeout: 90_000, label: 'compensated effects' });
    await expect(page.locator('[data-testid="effect-item"][data-status="compensated"]').first()).toBeVisible({ timeout: 30_000 });
    await expect(tid(page, 'run-status')).toHaveText(/rolled_back|rolled back/i, { timeout: 30_000 });
  });
});
