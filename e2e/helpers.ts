import { APIRequestContext, expect, Locator, Page, request as pwRequest } from '@playwright/test';
import { randomUUID } from 'crypto';

export const API_BASE = (process.env.E2E_API_BASE ?? 'http://localhost:8000').replace(/\/$/, '');

export const TERMINAL = ['completed', 'failed', 'cancelled', 'rolled_back'];

export const tid = (page: Page, id: string): Locator => page.getByTestId(id);

export function newWorkspaceId(): string {
  return `e2e-${randomUUID()}`; // 8-64 chars of [A-Za-z0-9-]
}

export async function apiContext(workspaceId?: string): Promise<APIRequestContext> {
  return pwRequest.newContext({
    baseURL: API_BASE,
    extraHTTPHeaders: workspaceId ? { 'X-Workspace-Id': workspaceId } : {},
  });
}

/** Fresh workspace: inject the id into localStorage before the app loads and reset the sandbox via API. */
export async function freshWorkspace(page: Page): Promise<{ ws: string; api: APIRequestContext }> {
  const ws = newWorkspaceId();
  await page.addInitScript((id) => {
    try { window.localStorage.setItem('adjutant.workspace', id); } catch { /* ignore */ }
  }, ws);
  const api = await apiContext(ws);
  const r = await api.post('/api/workspace/reset');
  expect(r.ok(), `workspace reset failed: ${r.status()}`).toBeTruthy();
  return { ws, api };
}

export async function getTemplates(api: APIRequestContext): Promise<Array<{ id: string; title: string; prompt: string; apps: string[] }>> {
  const r = await api.get('/api/config');
  expect(r.ok()).toBeTruthy();
  return (await r.json()).templates;
}

/** Pick a template card, set autonomy and submit; resolves once the run console URL is reached. */
export async function startRunFromTemplate(page: Page, templateId: string, autonomy = 'balanced'): Promise<string> {
  await page.goto('/');
  const card = page.locator(`[data-testid="template-card"][data-template-id="${templateId}"]`);
  await expect(card).toBeVisible();
  await card.click();
  await expect(tid(page, 'composer-input')).not.toHaveValue('');
  await tid(page, 'autonomy-select').selectOption(autonomy);
  await tid(page, 'composer-submit').click();
  await page.waitForURL(/\/runs\/[^/?#]+/, { timeout: 30_000 });
  return runIdFromUrl(page);
}

export function runIdFromUrl(page: Page): string {
  const m = page.url().match(/\/runs\/([^/?#]+)/);
  if (!m) throw new Error(`not on a run page: ${page.url()}`);
  return m[1];
}

export async function getRun(api: APIRequestContext, runId: string): Promise<any> {
  const r = await api.get(`/api/runs/${runId}`);
  expect(r.ok(), `GET run ${runId}: ${r.status()}`).toBeTruthy();
  return r.json();
}

/** Poll the API until predicate(detail) is truthy. */
export async function pollRun(
  api: APIRequestContext,
  runId: string,
  predicate: (detail: any) => boolean,
  opts: { timeout?: number; interval?: number; label?: string } = {},
): Promise<any> {
  const timeout = opts.timeout ?? 5 * 60_000;
  const start = Date.now();
  let last: any;
  while (Date.now() - start < timeout) {
    last = await getRun(api, runId);
    if (predicate(last)) return last;
    await new Promise((r) => setTimeout(r, opts.interval ?? 1500));
  }
  throw new Error(`timed out waiting for ${opts.label ?? 'condition'}; last status=${last?.run?.status}`);
}

/** Wait until the run is terminal, or awaiting approval / clarifying (returns the state reached). */
export async function waitForApprovalOrTerminal(api: APIRequestContext, runId: string, timeout = 5 * 60_000) {
  const d = await pollRun(
    api, runId,
    (x) => TERMINAL.includes(x.run.status) || x.run.status === 'awaiting_approval' || x.run.status === 'clarifying',
    { timeout, label: 'approval or terminal status' },
  );
  return d;
}

export async function waitForStatus(api: APIRequestContext, runId: string, statuses: string[], timeout = 5 * 60_000) {
  return pollRun(api, runId, (x) => statuses.includes(x.run.status), { timeout, label: `status in ${statuses.join('|')}` });
}

/** Answer a clarification prompt if the agent asks one (UI). */
export async function answerClarificationIfAny(page: Page, api: APIRequestContext, runId: string, answer: string) {
  const d = await getRun(api, runId);
  if (d.run.status !== 'clarifying') return false;
  await tid(page, 'clarify-input').fill(answer);
  await tid(page, 'clarify-submit').click();
  return true;
}

/** Approve every non-block item through the UI and submit. Returns the verdicts seen. */
export async function approveAllNonBlock(page: Page): Promise<string[]> {
  const panel = tid(page, 'approval-panel');
  await expect(panel).toBeVisible({ timeout: 30_000 });
  const items = panel.getByTestId('approval-item');
  const n = await items.count();
  const verdicts: string[] = [];
  for (let i = 0; i < n; i++) {
    const item = items.nth(i);
    const verdict = ((await item.getAttribute('data-verdict')) ?? '').toLowerCase();
    const nodeId = (await item.getAttribute('data-node-id')) ?? '';
    verdicts.push(verdict);
    if (verdict === 'block') continue;
    await page.getByTestId(`approval-approve-${nodeId}`).click();
  }
  await tid(page, 'approval-submit').click();
  return verdicts;
}

/** Drive a run to a terminal state through the UI: clarifications answered, approvals approved (non-block). */
export async function driveToTerminal(page: Page, api: APIRequestContext, runId: string, timeout = 5 * 60_000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const d = await waitForApprovalOrTerminal(api, runId, timeout - (Date.now() - start));
    if (TERMINAL.includes(d.run.status)) return d;
    if (d.run.status === 'clarifying') {
      await answerClarificationIfAny(page, api, runId, 'Yes, go ahead with sensible defaults.');
    } else {
      await approveAllNonBlock(page);
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  throw new Error('run did not reach a terminal state in time');
}

export async function workspaceSnapshot(api: APIRequestContext): Promise<any> {
  const r = await api.get('/api/workspace');
  expect(r.ok()).toBeTruthy();
  return r.json();
}

/** Outbox as lowercase JSON, for resilient substring assertions independent of item shape. */
export async function outboxText(api: APIRequestContext): Promise<{ raw: any[]; text: string }> {
  const snap = await workspaceSnapshot(api);
  const raw = snap.outbox ?? [];
  return { raw, text: JSON.stringify(raw).toLowerCase() };
}

/** Collect console errors and failed same-origin/API requests for a page. */
export function trackConsoleErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on('console', (m) => {
    if (m.type() !== 'error') return;
    const t = m.text();
    if (/favicon|Failed to load resource.*(favicon|manifest)/i.test(t)) return;
    errors.push(t);
  });
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  return errors;
}

export async function expectNoHorizontalOverflow(page: Page) {
  const { sw, cw } = await page.evaluate(() => ({
    sw: document.documentElement.scrollWidth,
    cw: document.documentElement.clientWidth,
  }));
  expect(sw, `horizontal overflow: scrollWidth ${sw} > clientWidth ${cw}`).toBeLessThanOrEqual(cw + 1);
}
