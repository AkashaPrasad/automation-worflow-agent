import { expect, test } from '@playwright/test';
import {
  approveAllNonBlock, driveToTerminal, freshWorkspace, outboxText, startRunFromTemplate, tid,
  waitForApprovalOrTerminal, waitForStatus, answerClarificationIfAny, getRun,
} from './helpers';

test('demo 1: meeting -> Notion -> email -> follow-up (balanced)', async ({ page }) => {
  const { api } = await freshWorkspace(page);
  const runId = await startRunFromTemplate(page, 'qbr-recap', 'balanced');

  await expect(tid(page, 'run-status')).toBeVisible();
  await expect(tid(page, 'plan-tree')).toBeVisible({ timeout: 3 * 60_000 });
  await expect.poll(() => page.getByTestId('plan-node').count(), { timeout: 3 * 60_000 }).toBeGreaterThanOrEqual(4);

  let d = await waitForApprovalOrTerminal(api, runId);
  if (d.run.status === 'clarifying') {
    await answerClarificationIfAny(page, api, runId, 'Yes, all attendees, next week, 30 minutes.');
    d = await waitForApprovalOrTerminal(api, runId);
  }

  if (d.run.status === 'awaiting_approval') {
    const panel = tid(page, 'approval-panel');
    await expect(panel).toBeVisible();
    // the external email must not be auto-approved
    const items = await panel.getByTestId('approval-item').evaluateAll((els) =>
      els.map((e) => ({ id: e.getAttribute('data-node-id'), verdict: (e.getAttribute('data-verdict') ?? '').toLowerCase(), text: e.textContent ?? '' })));
    expect(items.length).toBeGreaterThan(0);
    const emailItem = items.find((i) => /gmail\.(send|draft)|@northwind\.com|email/i.test(i.text));
    if (emailItem) expect(['ask', 'block']).toContain(emailItem.verdict);
    const asked = d.approval?.items?.find((i: any) => i.tool === 'gmail.send');
    if (asked) expect(String(asked.gate?.verdict ?? 'ask').toLowerCase()).toBe('ask');

    await approveAllNonBlock(page);
  }

  d = await driveToTerminal(page, api, runId);
  await expect(tid(page, 'run-status')).toHaveText(/completed/i, { timeout: 60_000 });
  expect(d.run.status).toBe('completed');

  await expect(tid(page, 'effects-ledger')).toBeVisible();
  const applied = d.effects.filter((e: any) => e.status === 'applied');
  expect(applied.length).toBeGreaterThan(0);
  await expect(page.locator('[data-testid="effect-item"][data-status="applied"]').first()).toBeVisible();

  // sandbox outbox has the recap email, Notion page and calendar event
  const tools = applied.map((e: any) => e.tool);
  expect(tools.some((t: string) => t.startsWith('gmail.'))).toBeTruthy();
  expect(tools).toContain('notion.create_page');
  expect(tools).toContain('calendar.create_event');

  await page.goto('/workspace');
  await tid(page, 'workspace-tab-outbox').click();
  const { text } = await outboxText(api);
  expect(text).toMatch(/northwind/);
  expect(text).toMatch(/notion/);
  expect(text).toMatch(/event|calendar|follow/);
  await expect(page.locator('body')).toContainText(/northwind|recap/i);
});
