/**
 * M3 Real Browser E2E Verification Script (v2.2.1 · I-05, S-04, S-05, S-06, E-02)
 * Verifies against a REAL external `app.runner.daemon` OS process + isolated loopback HTTP Chat Completions gateway:
 * 1. Login
 * 2. /eval/execution:
 *    - docker_group_attached: false & live external isolated_runner_ready badge (with PID)
 *    - dataset-first routing and disabled suites without a compatible ready runner
 *    - ready VLMEvalKit + --task hard lock to mode="infer" (VLMEVALKIT.md:44) + --plan CLI preview
 *    - Direct harmless single-task execution (`direct-smoke-v1` / `smoke-arith-001`) via DEFAULT Web submit,
 *      real SSE stream -> disconnect and replay -> reconciled snapshot (`gen_...`) -> clicks batch drilldown
 *      `/eval/batches/{batch_id}` and verifies HTTP 200 OK (ZERO 404!) + exact score & generation_id!
 * 3. /training/opd (real 197 empty production state, 0 fake runs, train.py --backend opd --plan CLI preview)
 * 4. /admin/health (live external isolated_runner_ready=true, runner_pid>0, docker_group_attached=false, managed_framework ok)
 * Layout shift budget (< 0.05), using the sum of non-input shifts, and Event Timing INP budget (< 100ms).
 */
import { execFile } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import { promisify } from 'node:util';
import { chromium } from 'playwright';

const execFileAsync = promisify(execFile);

const BASE_URL = process.env.BASE_URL || 'http://127.0.0.1:18685';
const TEST_USER = process.env.TEST_USER || 'e2e_admin';
const TEST_PASS = process.env.TEST_PASS || '';
if (!TEST_PASS) {
  console.error('TEST_PASS env var is required');
  process.exit(2);
}

function assert(cond, msg) {
  if (!cond) throw new Error(`ASSERTION FAILED: ${msg}`);
}

async function sampleInp(page, action, fn) {
  await page.evaluate(() => {
    window.__lrmStepInp = 0;
  });
  await fn();
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => setTimeout(r, 40))));
  const ms = await page.evaluate(() => window.__lrmStepInp || 0);
  return { action, ms };
}

async function main() {
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  try {
    await verify(browser);
  } finally {
    await browser.close();
  }
}

async function verify(browser) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  if (process.env.E2E_FETCH_FONTS_WITH_CURL === '1') {
    // Keep the real web fonts in environments where only curl can reach the CDN.
    const fontAssets = new Map();
    await context.route(/^https:\/\/(fonts\.googleapis\.com|fonts\.gstatic\.com)\//, async (route) => {
      const request = route.request();
      const url = request.url();
      if (!fontAssets.has(url)) {
        fontAssets.set(url, execFileAsync('curl', [
          '--fail', '--silent', '--show-error', '--max-time', '20',
          '--user-agent', request.headers()['user-agent'], '--', url,
        ], { encoding: 'buffer' }));
      }
      const { stdout } = await fontAssets.get(url);
      await route.fulfill({
        body: stdout,
        contentType: request.resourceType() === 'stylesheet' ? 'text/css' : 'font/woff2',
        headers: { 'access-control-allow-origin': '*' },
      });
    });
  }
  const page = await context.newPage();

  const consoleErrors = [];
  const pageErrors = [];
  const failed5xx = [];
  const batchDetail404 = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      consoleErrors.push([msg.text(), msg.location().url].filter(Boolean).join(' '));
    }
  });
  page.on('pageerror', (err) => pageErrors.push(String(err)));
  page.on('response', (res) => {
    if (res.status() >= 500) failed5xx.push(`${res.status()} ${res.url()}`);
    if (res.status() === 404 && res.url().includes('/api/v1/batches/')) {
      batchDetail404.push(`${res.status()} ${res.url()}`);
    }
  });

  await page.addInitScript(() => {
    const NativeEventSource = window.EventSource;
    window.__lrmExecutionStreams = [];
    // Observe real EventSource traffic and retain the connection for a controlled mid-job disconnect.
    window.EventSource = class extends NativeEventSource {
      constructor(url, options) {
        super(url, options);
        if (!new URL(this.url).pathname.match(/^\/api\/v1\/execution\/jobs\/[^/]+\/stream$/)) return;
        const stream = { url: this.url, source: this, events: [] };
        window.__lrmExecutionStreams.push(stream);
        for (const type of ['job_queued', 'job_started', 'trial_finished', 'reconciled', 'notification']) {
          this.addEventListener(type, (event) => {
            stream.events.push({ type, id: Number(event.lastEventId), data: JSON.parse(event.data) });
          });
        }
      }
    };

    window.__lrmCls = 0;
    window.__lrmStepInp = 0;
    window.__lrmMaxEventInp = 0;
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (!e.hadRecentInput) window.__lrmCls += e.value;
      }
    }).observe({ type: 'layout-shift', buffered: true });
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (e.duration > window.__lrmStepInp) window.__lrmStepInp = e.duration;
        if (e.duration > window.__lrmMaxEventInp) window.__lrmMaxEventInp = e.duration;
      }
    }).observe({ type: 'event', durationThreshold: 16, buffered: true });
  });

  const clsSamples = [];
  const inpSamples = [];
  const recordCls = async (route) => {
    await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => setTimeout(r, 120))));
    const cls = await page.evaluate(() => {
      if (!PerformanceObserver.supportedEntryTypes.includes('layout-shift')) {
        throw new Error('Chromium does not support layout-shift observations');
      }
      return window.__lrmCls;
    });
    assert(Number.isFinite(cls), `No CLS measurement for ${route}`);
    clsSamples.push({ route, cls });
  };

  // =========================================================================
  // 1. Login
  // =========================================================================
  await page.goto(`${BASE_URL}/login`, { waitUntil: 'networkidle' });
  await page.fill('input[type="text"]', TEST_USER);
  await page.fill('input[type="password"]', TEST_PASS);
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes('/login'), { timeout: 15000 });
  console.log('[OK] 1. Login succeeded');

  // =========================================================================
  // 2. /eval/execution (Real external Runner daemon + Direct harmless task +
  //    DEFAULT submit reconciled batch drilldown HTTP 200!)
  // =========================================================================
  await page.goto(`${BASE_URL}/eval/execution`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('text=受控评测执行与实时监控 (Benchmark Execution)', { timeout: 15000 });
  await page.waitForSelector('[aria-label="未接入评测题库列表"]', { timeout: 15000 });
  await recordCls('/eval/execution');

  // 2a) The evaluation form only displays operational readiness.
  await page.getByText('评测可启动', { exact: true }).waitFor({ timeout: 15000 });
  assert(await page.locator('[aria-label^="docker_group_attached:"]').count() === 0, 'Execution form must omit host diagnostics');

  // 2b) All 4 runner modes + disabled unintegrated suites
  const runnerRadios = page.locator('[role="radiogroup"][aria-label="评测运行器模式选择"] [role="radio"]');
  assert((await runnerRadios.count()) === 4, 'Expected 4 runner mode radios');
  const capabilities = await context.request.get('/api/v1/execution/capabilities');
  assert(capabilities.ok(), 'Expected execution capabilities');
  const { runner_environment: environment, suites } = await capabilities.json();
  const readyRunners = environment.supported_runners ?? [];
  const suitePicker = page.getByRole('combobox', { name: '选择已登记评测题库' });
  for (const suite of suites) {
    const kinds = suite.runner === 'harbor' ? ['harbor', 'harbor_gateway'] : [suite.runner];
    const runnable = suite.selectable && suite.task_count > 0 && kinds.some((kind) => readyRunners.includes(kind));
    assert(
      await suitePicker.evaluate((select, suiteId) =>
        Array.from(select.options).find((option) => option.value === suiteId)?.disabled, suite.suite_id) === !runnable,
      `Unexpected execution availability for ${suite.suite_id}`
    );
  }
  const unintStrip = page.locator('[aria-label="未接入评测题库列表"]');
  assert((await unintStrip.locator('button[disabled]').count()) >= 3, 'Expected >= 3 disabled unintegrated suites');
  const unintText = await unintStrip.innerText();
  for (const expectedName of ['MMLU-Pro', 'BFCL-v4', 'Global-MMLU-Lite']) {
    assert(unintText.includes(expectedName), `Missing disabled unintegrated suite: ${expectedName}`);
  }

  // 2c) VLMEvalKit + --task hard lock to mode="infer" (VLMEVALKIT.md:44)
  const vlmSuite = suites.find((suite) => suite.runner === 'vlmevalkit' && suite.selectable && suite.task_count > 0);
  if (vlmSuite && readyRunners.includes('vlmevalkit')) {
    inpSamples.push(await sampleInp(page, 'exec-select-vision-suite', async () => {
      await suitePicker.selectOption(vlmSuite.suite_id);
    }));
    await page.getByRole('button', { name: /--task 局部抽测/ }).click();
    const modeAllBtn = page.getByRole('button', { name: /mode=all（推理\+官方评分）/ });
    assert(await modeAllBtn.isDisabled(), 'VLMEvalKit mode=all MUST be disabled when --task is selected');
    const modeInferBtn = page.getByRole('button', { name: /mode=infer（仅推理 · unscored）/ });
    assert((await modeInferBtn.getAttribute('aria-pressed')) === 'true', 'VLMEvalKit mode=infer MUST be locked active');
    await page.waitForSelector('text=VLMEVALKIT.md:44 硬约束已触发', { timeout: 10000 });
  }

  // 2d) Switch to Direct 原生直测 (real harmless direct-smoke-v1 / smoke-arith-001)
  inpSamples.push(
    await sampleInp(page, 'exec-select-direct', async () => {
      await suitePicker.selectOption('direct-smoke-v1');
    })
  );
  await page.getByRole('button', { name: /--task 局部抽测/ }).click();
  await page.locator('input[aria-label="指定任务 ID 列表"]').fill('smoke-arith-001');
  const uniqueBatchName = `m3-e2e-direct-${Date.now()}`;
  await page.locator('input[aria-label="批次名 batch_name"]').fill(uniqueBatchName);

  // 2e) Generate --plan CLI preview (validated against real direct-smoke-v1 cases.jsonl!)
  inpSamples.push(
    await sampleInp(page, 'exec-plan-preview', async () => {
      await page.locator('button[aria-label="生成 --plan 预检与 CLI 命令"]').click();
    })
  );
  await page.waitForSelector('text=服务端 --plan 已校验', { timeout: 15000 });
  const configText = await page.locator('section[aria-label="Test Configuration 评测配置卡"]').innerText();
  assert(configText.includes('framework/run.py'), 'CLI preview must include framework/run.py');
  assert(configText.includes('--task smoke-arith-001'), 'CLI preview must include real --task smoke-arith-001');
  assert(configText.includes('<RUNNER_WORK_0700>'), 'CLI preview must use redacted 0700 runner work token');

  // 2f) Submit real Direct single-task job via DEFAULT Web path -> SSE -> Reconciled -> Click Batch Drilldown (200 OK!)
  const jobSubmitted = page.waitForResponse((response) =>
    new URL(response.url()).pathname === '/api/v1/execution/jobs' && response.request().method() === 'POST'
  );
  inpSamples.push(
    await sampleInp(page, 'exec-submit-isolated-job', async () => {
      await page.locator('button[aria-label="启动隔离执行作业"]').click();
    })
  );

  const submitResponse = await jobSubmitted;
  assert(submitResponse.ok(), `Job submission returned HTTP ${submitResponse.status()}`);
  const { job_id: jobId } = await submitResponse.json();
  const submittedPayload = submitResponse.request().postDataJSON();
  assert(submittedPayload.kind === 'direct' && submittedPayload.suite_id === 'direct-smoke-v1',
    'The selected Direct suite must be submitted through its compatible runner');
  assert(typeof jobId === 'string' && jobId.length > 0, 'Expected the submitted job ID');
  const detailPath = `/api/v1/execution/jobs/${encodeURIComponent(jobId)}`;
  const detailUrl = new URL(detailPath, BASE_URL).href;
  const streamPath = `${detailPath}/stream`;

  const progressSec = page.locator('section[aria-label="Test Progress 实时进度监控卡"]');
  const resultSec = page.locator('section[aria-label="Test Result 实时回收表"]');

  await progressSec.locator('text=⚡ Runner 实时事件流').waitFor({ timeout: 15000 });
  await page.waitForFunction((path) => {
    const stream = window.__lrmExecutionStreams.findLast((item) => new URL(item.url).pathname === path);
    return stream?.source.readyState === EventSource.OPEN && stream.events.some((event) => event.type === 'job_started');
  }, streamPath);
  const cursor = await page.evaluate((path) => {
    const stream = window.__lrmExecutionStreams.findLast((item) => new URL(item.url).pathname === path);
    if (stream.source.readyState !== EventSource.OPEN || stream.events.some((event) => event.type === 'trial_finished')) {
      throw new Error('Expected an open SSE stream before the first trial. Use a delayed test gateway.');
    }
    const cursor = stream.events.at(-1).id;
    stream.source.close();
    stream.source.dispatchEvent(new Event('error'));
    return cursor;
  }, streamPath);
  assert(cursor > 0, 'Expected a positive SSE cursor from the first connection');
  const disconnected = progressSec.getByText(/SSE: disconnected/);
  await disconnected.waitFor({ timeout: 15000 });

  // Read from the backend outside the page so the browser cursor stays at the point of disconnect.
  let missedTrial;
  const replayDeadline = Date.now() + 25000;
  while (!missedTrial && Date.now() < replayDeadline) {
    const detail = await context.request.get(detailUrl, { timeout: 5000 });
    assert(detail.ok(), `Job detail returned HTTP ${detail.status()} while disconnected`);
    missedTrial = (await detail.json()).events.find((event) => event.event_type === 'trial_finished' && event.seq_id > cursor);
    if (!missedTrial) await delay(200);
  }
  assert(missedTrial, 'Expected a trial result to be recorded while disconnected');
  assert(await disconnected.isVisible(), 'The page must still be disconnected when the missed event is recorded');

  const reconnect = progressSec.getByRole('button', { name: `断线重连补读事件流 last_event_id=${cursor}` });
  const resumedStream = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return url.pathname === streamPath && url.searchParams.get('last_event_id') === String(cursor);
  });
  await reconnect.click();
  const resumedResponse = await resumedStream;
  assert(resumedResponse.status() === 200, `SSE resume returned HTTP ${resumedResponse.status()}`);
  assert(resumedResponse.headers()['content-type']?.includes('text/event-stream'), 'SSE resume must return an event stream');
  await page.waitForFunction(({ streamPath, cursor }) => window.__lrmExecutionStreams.some((stream) => {
    const url = new URL(stream.url);
    return url.pathname === streamPath && url.searchParams.get('last_event_id') === String(cursor)
      && stream.events.some((event) => event.type === 'reconciled');
  }), { streamPath, cursor }, { timeout: 25000 });

  const replay = await page.evaluate((resumedUrl) => window.__lrmExecutionStreams.findLast((stream) =>
    stream.url === resumedUrl && stream.events.some((event) => event.type === 'reconciled')
  ).events, resumedResponse.url());
  for (const [offset, event] of replay.entries()) {
    assert(event.id === cursor + offset + 1, `SSE resume has a gap or duplicate at event ${event.id}`);
  }
  const trials = replay.filter((event) => event.type === 'trial_finished');
  assert(
    trials.length === 1 && trials[0].id === missedTrial.seq_id
      && trials[0].data.task_id === 'smoke-arith-001' && trials[0].data.reward === 1,
    'SSE resume must replay the missed scored arithmetic trial exactly once'
  );
  const { batch_id: batchId, generation_id: generationId } = replay.find((event) => event.type === 'reconciled').data;
  assert(typeof batchId === 'string' && batchId.length > 0 && /^gen_/.test(generationId),
    'SSE reconciliation must return a batch ID and generation ID');
  console.log(`[OK] SSE disconnected at ID ${cursor} and delivered IDs ${cursor + 1}–${replay.at(-1).id} after reconnect`);

  await progressSec.locator('text=已完成 finished').waitFor({ timeout: 25000 });
  await resultSec.locator('text=权威快照已对账').waitFor({ timeout: 25000 });
  await progressSec.locator('text=已发布共享快照 (gen_').waitFor({ timeout: 15000 });
  await resultSec.locator('text=已回收 1 条 Trial').waitFor({ timeout: 15000 });

  const progressText = (await progressSec.innerText()).replace(/\s+/g, ' ');
  assert(progressText.includes(`已发布共享快照 (${generationId}`), 'Snapshot pill must show the reconciled generation ID');
  assert(
    progressText.includes('普通 stdout/stderr（仅脱敏展示，绝不驱动进度或状态机）'),
    'Zero stdout spoofing notice must be displayed'
  );
  const resultText = await resultSec.innerText();
  assert(resultText.includes('smoke-arith-001'), 'Real task smoke-arith-001 must appear in Test Result table');
  assert(!resultText.includes('SPOOFED_TASK'), 'No spoofed task may appear');

  const stdioLogBox = page.locator('[aria-label="脱敏 stdout 与 stderr 日志输出"]');
  await stdioLogBox.getByText('[direct-worker]', { exact: false }).waitFor({ timeout: 15000 });
  const stdioLogText = await stdioLogBox.innerText();
  assert(!/\/home\/[A-Za-z0-9._-]+\//.test(stdioLogText), 'No unredacted /home/<user>/ path allowed in stdio log');
  assert(await stdioLogBox.evaluate((node) => {
    const style = getComputedStyle(node);
    return style.overflowY === 'auto' && node.clientHeight > 0 && node.clientHeight < 250;
  }), 'Expected a bounded, scrollable stdio viewer beside the redaction notice');
  const detail = await context.request.get(detailUrl);
  assert(detail.status() === 200, `Job detail returned HTTP ${detail.status()}`);
  const stdio = await detail.json();
  assert(stdio.stdio_log_cap_bytes === 2 * 1024 * 1024 && typeof stdio.stdio_log_truncated === 'boolean',
    'Job detail must report the 2 MiB stdio cap and truncation status');
  assert(stdio.redacted_stdio_logs.length > 0 && stdio.redacted_stdio_logs.length <= 200,
    'Job detail must return at most 200 lines of redacted stdio');

  const batchPath = `/eval/batches/${batchId}`;
  const batchLink = resultSec.locator(`a[href="${batchPath}"]`);
  await batchLink.waitFor({ timeout: 10000 });
  const batchDetail = page.waitForResponse((response) => new URL(response.url()).pathname === `/api/v1/batches/${batchId}`);
  await batchLink.click();
  const batchResponse = await batchDetail;
  assert(batchResponse.status() === 200, `Reconciled batch drilldown returned HTTP ${batchResponse.status()}`);
  const batch = await batchResponse.json();
  assert(batch.batch_id === batchId && batch.generation_id === generationId,
    'Batch drilldown must return the exact reconciled batch and generation IDs');
  assert(batch.totals.recorded === 1 && batch.totals.scored === 1 && batch.totals.valid_mean === 1,
    'Batch drilldown must return one scored trial with an exact mean of 1');
  await page.waitForURL((url) => url.pathname === batchPath, { timeout: 15000 });
  await page.getByRole('heading', { name: uniqueBatchName, exact: true }).waitFor({ timeout: 15000 });
  console.log('[OK] 2. /eval/execution verified (external Runner PID, 4 runners, disabled unintegrated suites, VLMEvalKit infer lock, real Direct harmless single-task SSE + default reconciled batch drilldown HTTP 200)');

  await recordCls('/eval/execution → batch');

  // =========================================================================
  // 3. /training/opd (real 197 empty production state + 0 fake runs + CLI plan)
  // =========================================================================
  await page.goto(`${BASE_URL}/training/opd`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('text=Training & 多专家 OPD 监控', { timeout: 15000 });
  await page.waitForSelector('section[aria-label="当前 training-runs 目录空态声明"]', { timeout: 15000 });
  await recordCls('/training/opd');

  const opdBodyText = await page.innerText('body');
  assert(opdBodyText.includes('0 Real Runs · 零假数据污染'), 'OPD page must show honest 0 Real Runs empty-state badge');
  assert(!opdBodyText.includes('opd-sample-run'), 'Fixture opd-sample-run MUST NOT leak into production /training/opd');

  inpSamples.push(
    await sampleInp(page, 'opd-generate-plan', async () => {
      await page.getByRole('button', { name: /生成 --plan 预检与训练 CLI/ }).click();
    })
  );
  await page.waitForSelector('text=步骤 1 · 路由与参数预检命令 (--plan)', { timeout: 15000 });
  const cliGenText = await page.locator('section[aria-label="OPD 预检与训练命令生成器"]').innerText();
  assert(cliGenText.includes('train.py --backend opd --plan'), 'Generated OPD CLI must include train.py --backend opd --plan');
  console.log('[OK] 3. /training/opd verified (real 197 empty production state, 0 fake runs, train.py --backend opd --plan CLI preview)');

  // =========================================================================
  // 4. /admin/health (live external isolated_runner_ready=true, runner_pid>0,
  //    docker_group_attached=false, managed_framework probe ok=true)
  // =========================================================================
  await page.goto(`${BASE_URL}/admin/health`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: '系统监控', exact: true }).waitFor({ timeout: 15000 });
  await page.waitForSelector('text=managed_framework', { timeout: 15000 });
  await page.getByRole('heading', { name: '隔离 Runner', exact: true }).waitFor({ timeout: 15000 });
  await recordCls('/admin/health');

  const healthJson = await page.evaluate(async () => {
    const resp = await fetch('/api/v1/admin/health', { credentials: 'include' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  });
  assert(healthJson?.runner_status?.isolated_runner_ready === true, `Expected isolated_runner_ready=true, got ${JSON.stringify(healthJson?.runner_status)}`);
  assert(typeof healthJson?.runner_status?.runner_pid === 'number' && healthJson.runner_status.runner_pid > 0, 'Expected live external runner_pid > 0');
  assert(healthJson?.runner_status?.docker_group_attached === false, 'Expected docker_group_attached=false');
  assert(healthJson?.storage?.isolation_probes?.managed_framework?.ok === true, 'Expected managed_framework probe ok=true');
  assert(healthJson?.storage?.local_budget_mb === 750, 'Expected local_budget_mb=750');
  console.log('[OK] 4. /admin/health verified (external isolated_runner_ready=true, runner_pid=' + healthJson.runner_status.runner_pid + ', docker_group_attached=false, managed_framework ok=true)');

  // =========================================================================
  // Final Metrics & Hard Budgets
  // =========================================================================
  const maxCls = Math.max(0, ...clsSamples.map((x) => x.cls));
  const maxInp = Math.max(0, ...inpSamples.map((x) => x.ms));
  console.log('=== M3 E2E METRICS SUMMARY ===');
  console.log('CLS samples:', JSON.stringify(clsSamples));
  console.log('Event Timing INP samples (ms):', JSON.stringify(inpSamples));
  console.log(`max_cls=${maxCls.toFixed(4)} (< 0.05), max_event_timing_inp=${maxInp.toFixed(1)}ms (< 100ms)`);
  console.log(`console_errors=${consoleErrors.length}, page_errors=${pageErrors.length}, http_5xx=${failed5xx.length}, batch_404=${batchDetail404.length}`);

  assert(maxCls < 0.05, `CLS budget exceeded: ${maxCls}`);
  assert(maxInp < 100, `INP budget exceeded: ${maxInp}`);
  assert(consoleErrors.length === 0, `Console errors: ${consoleErrors.join('; ')}`);
  assert(pageErrors.length === 0, `Page errors: ${pageErrors.join('; ')}`);
  assert(failed5xx.length === 0, `HTTP 5xx: ${failed5xx.join('; ')}`);
  assert(batchDetail404.length === 0, `Batch 404s: ${batchDetail404.join('; ')}`);
  console.log('ALL M3 PLAYWRIGHT E2E ASSERTIONS PASSED!');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
