/**
 * M2 Real Browser E2E Verification Script (v2.2.1 · 02-5, 02-6 §2, 02-7 §1-2, 02-8, A-07, A-08, A-12, E-02)
 * Verifies all 7 M2 pages + upgraded Admin Health on real 197 data with W3C CLS & Event Timing INP budgets.
 */
import { chromium } from 'playwright';

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
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();

  const consoleErrors = [];
  const pageErrors = [];
  const failed5xx = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });
  page.on('pageerror', (err) => pageErrors.push(String(err)));
  page.on('response', (res) => {
    if (res.status() >= 500) failed5xx.push(`${res.status()} ${res.url()}`);
  });

  await page.addInitScript(() => {
    window.__lrmCls = 0;
    window.__lrmStepInp = 0;
    window.__lrmMaxEventInp = 0;
    try {
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
    } catch {}
  });

  const clsSamples = [];
  const inpSamples = [];
  const recordCls = async (route) => {
    const cls = await page.evaluate(() => window.__lrmCls || 0);
    clsSamples.push({ route, cls });
  };

  // 1. Login
  await page.goto(`${BASE_URL}/login`, { waitUntil: 'networkidle' });
  await page.fill('input[type="text"]', TEST_USER);
  await page.fill('input[type="password"]', TEST_PASS);
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => !u.pathname.includes('/login'), { timeout: 15000 });
  console.log('[OK] 1. Login succeeded');

  // 2. Diagnostics 5 Panels (Failures, Submetrics, Efficiency, Tools, Objective Clues)
  await page.goto(`${BASE_URL}/eval/diagnostics`, { waitUntil: 'networkidle' });
  await page.waitForSelector('text=Diagnostics 深度诊断与客观轨迹线索', { timeout: 15000 });
  await page.waitForSelector('text=agent_protocol', { timeout: 15000 });
  await recordCls('/eval/diagnostics');

  for (const [tabLabel, expectedText] of [
    ['子指标热图', '项子指标'],
    ['效率与 Token', 'Reasoning 观测试验'],
    ['工具画像', '工具覆盖分母护栏'],
    ['客观线索', '仅输出 4 项 100% 客观可统计轨迹线索'],
  ]) {
    const s = await sampleInp(page, `diag-${tabLabel}`, async () => {
      await page.getByRole('tab', { name: tabLabel }).click();
    });
    inpSamples.push(s);
    await page.waitForSelector(`text=${expectedText}`, { timeout: 15000 });
  }
  const cluesBody = await page.innerText('body');
  assert(!/cheating|laziness|作弊|摆烂/i.test(cluesBody), 'Zero subjective behavioral labels allowed');
  console.log('[OK] 2. Diagnostics 5 panels verified (zero subjective labels)');

  // 3. Synthetic Studio (real tianxi-synth-v1 40 train / 12 holdout + plan preview)
  await page.goto(`${BASE_URL}/synth/datasets`, { waitUntil: 'networkidle' });
  await page.waitForSelector('text=tianxi-synth-v1', { timeout: 15000 });
  await page.waitForSelector('text=Train: 40 题', { timeout: 15000 });
  await recordCls('/synth/datasets');
  await page.getByRole('button', { name: /生成 CLI 预检/ }).click();
  await page.waitForSelector('text=synthesize.py', { timeout: 15000 });
  console.log('[OK] 3. Synthetic Studio verified (tianxi-synth-v1 40/12 + plan preview)');

  // 4. Selection Board (real round1 40x8, review=21, regression=19, pass@8 = 19/40)
  await page.goto(`${BASE_URL}/synth/selection`, { waitUntil: 'networkidle' });
  await page.waitForSelector('text=RL / SFT 四通道选题板与 pass@k 缺测曲线', { timeout: 15000 });
  await page.waitForSelector('text=GRPO 门槛', { timeout: 15000 });
  await recordCls('/synth/selection');
  console.log('[OK] 4. Selection Board verified (4 lanes + pass@k curve)');

  // 5. Direct Lab (native_direct_v1 + extended_adapter_pending disclosure + validator + companion image upload)
  await page.goto(`${BASE_URL}/eval/direct-lab`, { waitUntil: 'networkidle' });
  await page.waitForSelector('text=native_direct_v1', { timeout: 15000 });
  await page.waitForSelector('text=extended_adapter_pending', { timeout: 15000 });
  await recordCls('/eval/direct-lab');
  const sVal = await sampleInp(page, 'direct-validate', async () => {
    await page.getByRole('button', { name: '校验原生格式' }).click();
  });
  inpSamples.push(sVal);
  await page.waitForSelector('text=cases_sha=', { timeout: 15000 });

  // Verify browser companion PNG upload binds image bytes into cases_sha256!
  const png1x1 = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
    'base64'
  );
  await page.locator('input[aria-label="上传 Direct 配套图像"]').setInputFiles({
    name: 'sample.png',
    mimeType: 'image/png',
    buffer: png1x1,
  });
  await page.locator('textarea').fill('{"id":"mm-1","prompt":"Describe image","images":["sample.png"],"answer":"ok"}\n');
  await page.getByRole('button', { name: '校验原生格式' }).click();
  await page.waitForSelector('text=1 题 · 1 图 · cases_sha=', { timeout: 15000 });
  console.log('[OK] 5. Direct Lab verified (native_direct_v1 + extended_adapter_pending + companion PNG upload)');

  // 6. Toolkit Verify (report.py check + summary_only offline calc -> Offline Report -> Report Management)
  await page.goto(`${BASE_URL}/toolkit/verify`, { waitUntil: 'networkidle' });
  await page.waitForSelector('text=Offline Verify 离线校验与三格式分流算分', { timeout: 15000 });
  await recordCls('/toolkit/verify');
  await page.getByRole('button', { name: '提交离线算分并入库' }).click();
  await page.waitForSelector('text=summary_passthrough', { timeout: 15000 });
  await page.getByRole('tab', { name: 'Report Management' }).click();
  await page.waitForSelector('text=离线验证报告', { timeout: 15000 });
  console.log('[OK] 6. Toolkit Verify 3 tabs + offline calc verified');

  // 7. Toolkit Params + Manuals + Upgraded Admin Health
  await page.goto(`${BASE_URL}/toolkit/params`, { waitUntil: 'networkidle' });
  await page.waitForSelector('text=运行参数跨批次对照台账', { timeout: 15000 });
  await recordCls('/toolkit/params');

  await page.goto(`${BASE_URL}/toolkit/manuals`, { waitUntil: 'networkidle' });
  await page.waitForSelector('text=Manuals 框架与平台手册中心', { timeout: 15000 });
  await page.waitForSelector('text=On this page', { timeout: 15000 });
  await recordCls('/toolkit/manuals');

  await page.goto(`${BASE_URL}/admin/health`, { waitUntil: 'networkidle' });
  await page.waitForSelector('text=服务端 Gateway / Teacher / Judge Profiles', { timeout: 15000 });
  await recordCls('/admin/health');
  console.log('[OK] 7. Toolkit Params, Manuals, and upgraded Admin Health verified');

  const maxCls = Math.max(0, ...clsSamples.map((x) => x.cls));
  const maxInp = Math.max(0, ...inpSamples.map((x) => x.ms));
  console.log('=== M2 E2E METRICS SUMMARY ===');
  console.log('CLS samples:', JSON.stringify(clsSamples));
  console.log('Event Timing INP samples (ms):', JSON.stringify(inpSamples));
  console.log(`max_cls=${maxCls.toFixed(4)} (< 0.05), max_event_timing_inp=${maxInp.toFixed(1)}ms (< 100ms)`);
  console.log(`console_errors=${consoleErrors.length}, page_errors=${pageErrors.length}, http_5xx=${failed5xx.length}`);

  assert(maxCls < 0.05, `CLS budget exceeded: ${maxCls}`);
  assert(maxInp < 100, `INP budget exceeded: ${maxInp}`);
  assert(consoleErrors.length === 0, `Console errors: ${consoleErrors.join('; ')}`);
  assert(pageErrors.length === 0, `Page errors: ${pageErrors.join('; ')}`);
  assert(failed5xx.length === 0, `HTTP 5xx: ${failed5xx.join('; ')}`);
  console.log('ALL M2 PLAYWRIGHT E2E ASSERTIONS PASSED!');
  await browser.close();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
