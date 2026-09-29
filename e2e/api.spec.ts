import { expect, test } from '@playwright/test';
import { apiContext, newWorkspaceId } from './helpers';

test.describe('api', () => {
  test('workspace header is required', async () => {
    const anon = await apiContext();
    for (const path of ['/api/runs', '/api/workspace', '/api/config', '/api/memory']) {
      const r = await anon.get(path);
      expect(r.status(), path).toBe(400);
      expect((await r.json()).detail).toBeTruthy();
    }
    const bad = await apiContext('x');
    expect((await bad.get('/api/runs')).status()).toBe(400);
    const post = await anon.post('/api/runs', { data: { request: 'hi' } });
    expect(post.status()).toBe(400);
  });

  test('config shape', async () => {
    const api = await apiContext(newWorkspaceId());
    const r = await api.get('/api/config');
    expect(r.ok()).toBeTruthy();
    const cfg = await r.json();
    expect(cfg.autonomy_levels).toEqual(expect.arrayContaining(['cautious', 'balanced', 'autonomous']));
    expect(cfg).toHaveProperty('policy');
    expect(Array.isArray(cfg.integrations)).toBe(true);
    expect(cfg).toHaveProperty('budget_defaults');
    expect(cfg.templates.length).toBeGreaterThanOrEqual(4);
    for (const t of cfg.templates) {
      expect(t).toEqual(expect.objectContaining({ id: expect.any(String), title: expect.any(String), prompt: expect.any(String) }));
      expect(Array.isArray(t.apps)).toBe(true);
    }
    const tools = await api.get('/api/tools');
    expect(tools.ok()).toBeTruthy();
    expect((await tools.json()).length).toBeGreaterThan(5);
  });

  test('create run, events, SSE framing, foreign workspace 404', async () => {
    const ws = newWorkspaceId();
    const api = await apiContext(ws);
    await api.post('/api/workspace/reset');

    const created = await api.post('/api/runs', { data: { request: 'Summarize the Northwind QBR meeting', autonomy: 'cautious' } });
    expect(created.status(), await created.text()).toBe(200);
    const run = await created.json();
    expect(run.id).toBeTruthy();
    expect(run.workspace_id).toBe(ws);
    expect(run.autonomy).toBe('cautious');

    const list = await (await api.get('/api/runs')).json();
    expect(list.map((r: any) => r.id)).toContain(run.id);

    const detail = await (await api.get(`/api/runs/${run.id}`)).json();
    expect(detail).toHaveProperty('run');
    expect(detail).toHaveProperty('effects');
    expect(detail).toHaveProperty('approval');

    // events poll: seq monotonic, run_id matches
    await expect.poll(async () => (await (await api.get(`/api/runs/${run.id}/events?after=0`)).json()).length, { timeout: 60_000 }).toBeGreaterThan(0);
    const events = await (await api.get(`/api/runs/${run.id}/events?after=0`)).json();
    expect(events[0]).toEqual(expect.objectContaining({ run_id: run.id, type: expect.any(String), agent: expect.any(String) }));
    const seqs = events.map((e: any) => e.seq);
    expect([...seqs].sort((a, b) => a - b)).toEqual(seqs);

    // SSE framing: read the first bytes of the stream
    const abort = new AbortController();
    const res = await fetch(`${process.env.E2E_API_BASE ?? 'http://localhost:8000'}/api/runs/${run.id}/stream?after=0&workspace_id=${ws}`, {
      signal: abort.signal,
      headers: { Accept: 'text/event-stream' },
    });
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type') ?? '').toContain('text/event-stream');
    const reader = res.body!.getReader();
    let buf = '';
    const deadline = Date.now() + 30_000;
    while (Date.now() < deadline && !/event: run_event\r?\ndata: .+/s.test(buf)) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += new TextDecoder().decode(value);
    }
    abort.abort();
    expect(buf).toMatch(/id: \d+/);
    expect(buf).toMatch(/event: run_event/);
    const dataLine = buf.split(/\r?\n/).find((l) => l.startsWith('data: '))!;
    expect(JSON.parse(dataLine.slice(6))).toEqual(expect.objectContaining({ run_id: run.id }));

    // foreign workspace cannot see the run
    const other = await apiContext(newWorkspaceId());
    expect((await other.get(`/api/runs/${run.id}`)).status()).toBe(404);
    expect((await other.get(`/api/runs/${run.id}/events`)).status()).toBe(404);
    expect((await other.post(`/api/runs/${run.id}/cancel`)).status()).toBe(404);
    expect((await other.get('/api/runs')).ok()).toBeTruthy();
    expect((await (await other.get('/api/runs')).json()).map((r: any) => r.id)).not.toContain(run.id);

    // cleanup: do not leave the agent running
    await api.post(`/api/runs/${run.id}/cancel`);
  });
});
