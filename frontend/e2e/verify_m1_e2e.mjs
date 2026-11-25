/**
 * M1 Real Browser E2E Verification Script (v2.2.1 07-2: E-01, E-03, E-04, A-02, A-03, A-07, A-09, A-10, A-14, A-15, S-03)
 * Zero skippable assertions: verifies every Gate journey end-to-end against live 197 hot + cold data
 * and measures genuine W3C Event Timing (INP) & Layout Shift (CLS).
 *
 * Usage:
 *   BASE_URL=http://127.0.0.1:18680 TEST_USER=e2e_admin TEST_PASS=... node verify_m1_e2e.mjs
 */
import { chromium } from 'playwright';

const BASE_URL = process.env.BASE_URL || 'http://127.0.0.1:18680';
const TEST_USER = process.env.TEST_USER || 'e2e_admin';
const TEST_PASS = process.env.TEST_PASS || '';
if (!TEST_PASS) {
  console.error('TEST_PASS env var is required');
  process.exit(2);
}

function assert(cond, msg) {
  if (!cond) {
    throw new Error(`ASSERTION FAILED: ${msg}`);
  }
}

async function sampleEventTimingInp(page, action, fn) {
  await page.evaluate(() => {
    window.__lrmStepInp = 0;
  });
  await fn();
  // Allow Event Timing entry to flush after paint
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
  const failed404Media = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') consoleErrors.push(msg.text());
  });
  page.on('pageerror', (err) => pageErrors.push(String(err)));
  page.on('response', (res) => {
    if (res.status() >= 500) failed5xx.push(`${res.status()} ${res.url()}`);
    if (res.status() === 404 && res.url().includes('/api/v1/media/')) {
      failed404Media.push(`${res.status()} ${res.url()}`);
    }
  });

  // Inject genuine W3C CLS & Event Timing (INP) observers on every navigation
  await page.addInitScript(() => {
    window.__lrmCls = 0;
    window.__lrmStepInp = 0;
    window.__lrmMaxEventInp = 0;
    try {
      const po = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          if (!entry.hadRecentInput) window.__lrmCls += entry.value;
        }
      });
      po.observe({ type: 'layout-shift', buffered: true });
    } catch {
      /* ignore */
    }
    try {
      const eo = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          if (entry.interactionId && entry.duration > 0) {
            if (entry.duration > window.__lrmStepInp) window.__lrmStepInp = entry.duration;
            if (entry.duration > window.__lrmMaxEventInp) window.__lrmMaxEventInp = entry.duration;
          }
        }
      });
      eo.observe({ type: 'event', durationThreshold: 16, buffered: true });
    } catch {
      /* ignore */
    }
  });

  const clsSamples = [];
  const inpSamples = [];

  // 1. Auth Redirect + Login + Home A-14 Threshold Flip -> AA-LCR Same-Task Evidence (S-01 / E-01 / A-14)
  await page.goto(`${BASE_URL}/`, { waitUntil: 'networkidle' });
  assert(page.url().includes('/login'), `Expected redirect to /login, got ${page.url()}`);
  await page.fill('#login-username', TEST_USER);
  await page.fill('#login-password', TEST_PASS);
  await page.click('button[type="submit"]');
  await page.waitForURL((u) => u.pathname === '/', { timeout: 10000 });
  await page.waitForSelector('text=三线提分与防退化护栏裁决', { timeout: 10000 });
  await page.waitForSelector('text=aa-lcr', { timeout: 10000 });
  await page.waitForTimeout(300);
  clsSamples.push({ route: '/', cls: await page.evaluate(() => window.__lrmCls || 0) });

  const homeBodyBefore = await page.textContent('body');
  assert(homeBodyBefore.includes('aa-lcr'), 'Home verdict missing aa-lcr');
  assert(homeBodyBefore.includes('mmlu-pro') && homeBodyBefore.includes('无法判定'), 'Home verdict missing undeterminable mmlu-pro');

  inpSamples.push(
    await sampleEventTimingInp(page, 'home-threshold-toggle', async () => {
      await page.click('button[role="tab"]:has-text("严苛 (-0.5 pp)")');
    })
  );
  await page.waitForSelector('text=退步 2', { timeout: 10000 });

  // A-14 mandatory evidence entry: click AA-LCR on Home -> lands on /eval/compare carrying -0.5pp + paired AA-LCR diff!
  await page.locator('a:has-text("aa-lcr")').first().click();
  await page.waitForURL(/\/eval\/compare.*focus_benchmark=aa-lcr/, { timeout: 10000 });
  await page.waitForSelector('text=同题双栏历史样本证据 · AA-LCR-v1.1', { timeout: 15000 });
  const aalcrCmpBody = await page.textContent('body');
  assert(aalcrCmpBody.includes('-0.5 pp'), 'Home threshold -0.5pp was not carried to Compare');
  await page.locator('#compare-task-diff tbody tr').first().click();
  await page.waitForSelector('[role="dialog"]', { timeout: 8000 });
  await page.click('button[aria-label="关闭抽屉"]');
  console.log('[OK] 1. Login + Home A-14 (-1.0pp -> -0.5pp) -> AA-LCR Paired Side-by-Side Evidence');

  // 2. Global Ctrl+K Search -> Model Profile (E-01)
  await page.keyboard.press('Control+k');
  await page.waitForSelector('input[placeholder*="搜索模型"]', { timeout: 5000 });
  await page.fill('input[placeholder*="搜索模型"]', 'Qwen3_8-27B');
  await page.waitForSelector('button:has-text("模型档案")', { timeout: 5000 });
  await page.click('button:has-text("模型档案")');
  await page.waitForURL(/\/models\/Qwen3_8-27B/, { timeout: 10000 });
  await page.waitForSelector('text=已验证 Checkpoint 趋势', { timeout: 10000 });
  await page.click('button[role="tab"]:has-text("已验证 Checkpoint 趋势")');
  await page.waitForTimeout(300);
  const modelText = await page.textContent('body');
  assert(!/License:|Country:|Hardware:|Price:/.test(modelText), 'Forbidden fake commercial field on Model profile');
  console.log('[OK] 2. CommandPalette (Ctrl+K) -> Model Profile + Checkpoint Trend');

  // 3. Leaderboard 4-View Switcher + ARC Scatter N=71 + Multi-Select + Tooltip XSS Escape + Theme + L2 Drawer (E-03, A-15, S-03)
  await page.goto(`${BASE_URL}/leaderboard`, { waitUntil: 'networkidle' });
  await page.waitForSelector('text=综合主榜', { timeout: 10000 });
  await page.waitForTimeout(400);
  clsSamples.push({ route: '/leaderboard', cls: await page.evaluate(() => window.__lrmCls || 0) });

  inpSamples.push(
    await sampleEventTimingInp(page, 'lb-track-agentic', async () => {
      await page.click('button[role="tab"]:has-text("Agentic")');
    })
  );
  await page.waitForTimeout(300);

  assert((await page.getAttribute('html', 'data-theme')) === 'dark', 'Theme must default to dark');
  inpSamples.push(
    await sampleEventTimingInp(page, 'theme-toggle', async () => {
      await page.click('button[aria-label="切换明暗主题"]');
    })
  );
  assert((await page.getAttribute('html', 'data-theme')) === 'light', 'Theme did not switch to light');
  await page.click('button[aria-label="切换明暗主题"]');
  assert((await page.getAttribute('html', 'data-theme')) === 'dark', 'Theme did not switch back to dark');

  await page.click('button[role="tab"]:has-text("Trend")');
  await page.waitForSelector('canvas', { timeout: 5000 });
  await page.click('button[role="tab"]:has-text("Grouped Bar")');
  await page.waitForSelector('canvas', { timeout: 5000 });
  await page.click('button[role="tab"]:has-text("Efficiency Scatter")');
  await page.waitForSelector('text=N = 71 common valid tasks', { timeout: 10000 });
  await page.waitForSelector('canvas', { timeout: 5000 });

  // A-15 multi-select scatter subset via Shift+Click on canvas + chip toggle -> Compare Dock (mandatory, non-skippable!)
  const shiftPoint = await page.evaluate(() => {
    const el = document.querySelector('[_echarts_instance_]');
    const chart = el && el.__lrmChart;
    if (!chart) return null;
    const opt = chart.getOption();
    const sIdx = (opt.series || []).findIndex((s) => s.type === 'scatter' && s.data && s.data.length > 0);
    if (sIdx < 0) return null;
    const pt = opt.series[sIdx].data[0];
    const px = chart.convertToPixel({ seriesIndex: sIdx }, pt.value);
    const rect = el.getBoundingClientRect();
    return { x: rect.left + px[0], y: rect.top + px[1], batchId: pt.point?.batch_id };
  });
  assert(shiftPoint && shiftPoint.batchId, 'Failed to locate scatter point coordinates on canvas');
  await page.keyboard.down('Shift');
  await page.mouse.click(shiftPoint.x, shiftPoint.y);
  await page.keyboard.up('Shift');
  await page.waitForSelector('text=已选 1 组', { timeout: 5000 });
  const scatterSelectBtns = page.getByText('框选 / Shift+点选送对比：', { exact: true })
    .locator('..').getByRole('button', { pressed: false });
  assert((await scatterSelectBtns.count()) >= 1, 'Expected >=1 remaining selectable scatter point on TB2.1');
  await scatterSelectBtns.nth(0).click();
  await page.waitForSelector('text=已选 2 组', { timeout: 5000 });
  await page.getByRole('button', { name: '选中项加入对比', exact: true }).click();
  await page.waitForSelector('[aria-label="悬浮对比停靠栏"]', { timeout: 5000 });

  // Partial cost observations can be shown, but cannot enter the frontier.
  await page.selectOption('select[aria-label="切换 X 轴指标"]', 'recorded_cost_usd');
  await page.getByText(/空心点使用部分试题|共同有效题缺少/).waitFor({ timeout: 8000 });
  const hasCostFrontier = await page.evaluate(() => {
    const element = Array.from(document.querySelectorAll('div')).find((el) => el.__lrmChart && el.querySelector('canvas'));
    return Boolean(element?.__lrmChart.getOption().series?.some((series) => series.name === 'Pareto Frontier'));
  });
  assert(!hasCostFrontier, 'Partial recorded cost cannot contribute to the Pareto frontier');
  await page.selectOption('select[aria-label="切换 X 轴指标"]', 'duration_p50_sec');
  await page.waitForSelector('canvas', { timeout: 8000 });

  // Back to Table & mandatory L2 SlideOverDrawer check
  await page.click('button[role="tab"]:has-text("Table")');
  await page.waitForSelector('table tbody tr', { timeout: 10000 });
  const scoreBtn = page.locator('table tbody tr button[aria-label^="得分"]').first();
  assert((await scoreBtn.count()) > 0, 'Expected at least 1 interactive score cell on Leaderboard');
  await scoreBtn.click();
  await page.waitForSelector('[role="dialog"]', { timeout: 5000 });
  await page.click('button[aria-label="关闭抽屉"]');

  // S-03 XSS escaping & punctuation regression test (non-vacuous: reloads with fresh cache & verifies rendered tooltip DOM text!)
  await page.route('**/api/v1/leaderboard/efficiency-scatter*', async (route) => {
    const resp = await route.fetch();
    const json = await resp.json();
    if (json.points && json.points.length > 0) {
      json.points[0].model_canonical = 'Qwen & "Test" <img src=x onerror="window.__xssFired=1">';
      json.points[0].harness = "harbor'<script>window.__xssFired=2</script>";
      json.points[0].batch_name = 'batch&<>test (v1.2: a/b)';
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(json) });
  });
  await page.goto(`${BASE_URL}/leaderboard?view=scatter`, { waitUntil: 'networkidle' });
  await page.waitForSelector('canvas', { timeout: 10000 });
  await page.waitForTimeout(300);
  const tooltipCheck = await page.evaluate(() => {
    const el = document.querySelector('[_echarts_instance_]');
    const chart = el && el.__lrmChart;
    if (!chart) return { ok: false, reason: 'chart missing' };
    const opt = chart.getOption();
    let targetSIdx = -1;
    let targetDIdx = -1;
    for (let si = 0; si < (opt.series || []).length; si++) {
      const s = opt.series[si];
      if (s.type !== 'scatter' || !Array.isArray(s.data)) continue;
      for (let di = 0; di < s.data.length; di++) {
        if (String(s.data[di]?.point?.batch_name || '').includes('batch&<>test')) {
          targetSIdx = si;
          targetDIdx = di;
          break;
        }
      }
      if (targetSIdx >= 0) break;
    }
    if (targetSIdx < 0) return { ok: false, reason: 'injected point not found in series' };
    chart.dispatchAction({ type: 'showTip', seriesIndex: targetSIdx, dataIndex: targetDIdx });
    return { ok: true, targetSIdx, targetDIdx };
  });
  assert(tooltipCheck.ok, `Failed to open scatter tooltip for XSS test: ${JSON.stringify(tooltipCheck)}`);
  await page.waitForTimeout(150);
  const tooltipText = await page.evaluate(() => {
    const el = document.querySelector('[_echarts_instance_]');
    return el ? el.innerText : '';
  });
  assert(
    tooltipText.includes('Qwen & "Test" <img src=x onerror="window.__xssFired=1">'),
    `Escaped model_canonical text did not render literally in tooltip! Got: ${tooltipText}`
  );
  assert(
    tooltipText.includes("harbor'<script>window.__xssFired=2</script>"),
    `Escaped harness text did not render literally in tooltip! Got: ${tooltipText}`
  );
  assert(
    tooltipText.includes('batch&<>test (v1.2: a/b)'),
    `Normal punctuation + HTML reserved chars in batch_name did not render literally! Got: ${tooltipText}`
  );
  const xssFired = await page.evaluate(() => Boolean(window.__xssFired));
  assert(!xssFired, 'XSS payload executed inside scatter tooltip!');
  const injectedImgCount = await page.locator('img[src="x"]').count();
  assert(injectedImgCount === 0, 'Unescaped <img src=x> rendered inside scatter tooltip!');
  await page.unroute('**/api/v1/leaderboard/efficiency-scatter*');
  console.log('[OK] 3. Leaderboard 4-View + ARC Scatter N=71 + Shift+Click Subset + Non-Vacuous Tooltip XSS Escape + Theme + L2 Drawer');

  // 4. Datasets 3-Track Tree + A-09 Legacy Multimodal Browser (CMMMU / MMMU_Pro_10c / MM-IFEval / RealWorldQA)
  await page.goto(`${BASE_URL}/datasets`, { waitUntil: 'networkidle' });
  await page.waitForSelector('text=多模态指令遵循', { timeout: 10000 });
  clsSamples.push({ route: '/datasets', cls: await page.evaluate(() => window.__lrmCls || 0) });

  await page.click('button[role="tab"]:has-text("2026-11-20 样本")');
  await page.waitForSelector('select[aria-label="选择历史基准目录"]', { timeout: 8000 });

  // 4a. CMMMU original image loads cleanly (naturalWidth > 0)
  await page.selectOption('select[aria-label="选择历史基准目录"]', 'CMMMU');
  await page.waitForSelector('text=张已核验图像', { timeout: 10000 });
  await page.locator('table tbody tr:has-text("张已核验图像")').first().click();
  await page.waitForSelector('[role="dialog"] img', { timeout: 10000 });
  await page.waitForFunction(() => {
    const img = document.querySelector('[role="dialog"] img');
    return Boolean(img && img.complete && img.naturalWidth > 0);
  }, { timeout: 10000 });
  await page.click('button[aria-label="关闭抽屉"]');

  // 4b. MMMU_Pro_10c conflict placeholder (ambiguous_multi_hit -> zero <img> & zero 404)
  await page.selectOption('select[aria-label="选择历史基准目录"]', 'MMMU_Pro_10c');
  await page.waitForSelector('text=多版本冲突（已拒绝猜测）', { timeout: 10000 });
  await page.locator('table tbody tr:has-text("多版本冲突")').first().click();
  await page.waitForSelector('[role="dialog"]:has-text("ambiguous_multi_hit")', { timeout: 8000 });
  assert((await page.locator('[role="dialog"] img').count()) === 0, 'MMMU_Pro ambiguous sample must NOT render <img>');
  await page.click('button[aria-label="关闭抽屉"]');

  // 4c. MM-IFEval TSV inline original image loads cleanly (naturalWidth > 0)
  await page.selectOption('select[aria-label="选择历史基准目录"]', 'MM-IFEval');
  await page.waitForSelector('text=张已核验图像', { timeout: 10000 });
  await page.locator('table tbody tr:has-text("张已核验图像")').first().click();
  await page.waitForSelector('[role="dialog"] img', { timeout: 10000 });
  await page.waitForFunction(() => {
    const img = document.querySelector('[role="dialog"] img');
    return Boolean(img && img.complete && img.naturalWidth > 0);
  }, { timeout: 10000 });
  await page.click('button[aria-label="关闭抽屉"]');

  // 4d. RealWorldQA unregistered_source placeholder (MUST NOT say "已核验图像" and zero 404!)
  await page.selectOption('select[aria-label="选择历史基准目录"]', 'RealWorldQA');
  await page.waitForSelector('text=未登记数据源', { timeout: 10000 });
  const rwTableText = await page.locator('table').first().textContent();
  assert(!rwTableText.includes('已核验图像'), 'RealWorldQA must NOT be mislabeled as 已核验图像');
  await page.locator('table tbody tr').first().click();
  await page.waitForSelector('[role="dialog"]:has-text("unregistered_source")', { timeout: 8000 });
  assert((await page.locator('[role="dialog"] img').count()) === 0, 'RealWorldQA unregistered sample must NOT render <img>');
  await page.click('button[aria-label="关闭抽屉"]');
  // 4e. Missing media placeholder check (missing -> renders 媒体文件缺失 and zero <img> on fresh page)
  await page.route('**/api/v1/legacy/RealWorldQA/samples*page=2*', async (route) => {
    const resp = await route.fetch();
    const json = await resp.json();
    if (json.items && json.items.length > 0) {
      json.items[0].media_match_status = 'missing';
      json.items[0].media_refs = [{ kind: 'missing', reason: 'file_missing', is_image: false }];
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(json) });
  });
  await page.click('button:has-text("下一页")');
  await page.waitForSelector('text=媒体文件缺失', { timeout: 10000 });
  await page.unroute('**/api/v1/legacy/RealWorldQA/samples*page=2*');
  console.log('[OK] 4. Datasets 3-Track Tree + A-09 CMMMU / MMMU_Pro_10c / MM-IFEval TSV Image / RealWorldQA Unregistered & Missing Placeholders');

  // 5a. A-03 GAIA Clickable 118/165 Intersection Denominator -> Opens Full 118 Common Valid Tasks
  await page.goto(`${BASE_URL}/eval/compare?preset=gaia-2way`, { waitUntil: 'networkidle' });
  const gaiaDenomBtn = page.locator('button[aria-label="查看全部 118 道共同有效题"]');
  await gaiaDenomBtn.waitFor({ timeout: 10000 });
  await gaiaDenomBtn.click();
  await page.waitForFunction(
    () => document.querySelectorAll('#compare-task-diff tbody tr').length === 118,
    { timeout: 10000 }
  );
  console.log('[OK] 5a. A-03 GAIA 118/165 Clickable Intersection -> Loaded All 118 Common Valid Tasks');

  // 5a-extra. A-02 3-way DeepSWE matrix & task-diff intersection lock (39 valid / 48 covered, NOT 60 / 78!)
  await page.goto(`${BASE_URL}/eval/compare?preset=deepswe-3way`, { waitUntil: 'networkidle' });
  await page.waitForSelector('button[aria-label="查看全部 39 道共同有效题"]', { timeout: 10000 });
  await page.waitForSelector('#compare-task-diff:has-text("共同有效 39 / 共同覆盖 48")', { timeout: 10000 });
  console.log('[OK] 5a-extra. A-02 DeepSWE 3-way Matrix & Task-Diff Strictly Locked to 39/48');

  // 5b. E-04 DeepSWE 2-way -> "B 独胜" (5 tasks) -> Split Diff Drawer -> Trial Microscope -> Return Restores Exact State!
  await page.goto(`${BASE_URL}/eval/compare?preset=deepswe-2way`, { waitUntil: 'networkidle' });
  await page.waitForSelector('text=45.00%', { timeout: 10000 });
  const cmpText = await page.textContent('body');
  assert(cmpText.includes('23.33%'), 'DeepSWE OpenClaw 23.33% missing');
  assert(cmpText.includes('39.74%') && cmpText.includes('31/78'), 'DeepSWE faults_as_zero 39.74% (31/78) missing');
  assert(cmpText.includes('17.95%') && cmpText.includes('14/78'), 'DeepSWE faults_as_zero 17.95% (14/78) missing');

  // Capture exact comparison_id before switching filter
  await page.waitForFunction(() => new URL(window.location.href).searchParams.get('cmp'), { timeout: 8000 });
  const origCmpId = await page.evaluate(() => new URL(window.location.href).searchParams.get('cmp'));
  assert(origCmpId && origCmpId.startsWith('cmp_'), `Expected cmp_ ID in URL, got ${origCmpId}`);

  await page.click('button:has-text("B 独胜 (5)")');
  await page.waitForFunction(
    () => document.querySelectorAll('#compare-task-diff tbody tr').length === 5,
    { timeout: 10000 }
  );
  const firstBWinTaskId = (await page.locator('#compare-task-diff tbody tr td').first().textContent()).trim();
  await page.locator('#compare-task-diff tbody tr').first().click();
  await page.waitForSelector('[role="dialog"]:has-text("展开双栏 ATIF 轨迹 Diff")', { timeout: 8000 });

  await page.locator('[role="dialog"] a:has-text("打开显微镜")').first().click();
  await page.waitForURL(/\/eval\/batches\/.*\/trials\//, { timeout: 10000 });
  await page.waitForSelector('text=总览 / 4阶段耗时 / 故障', { timeout: 10000 });
  await page.click('a:has-text("返回来源页")');
  await page.waitForURL(/\/eval\/compare/, { timeout: 10000 });
  await page.waitForSelector('[role="dialog"]', { timeout: 10000 });

  const restoredCmpId = await page.evaluate(() => new URL(window.location.href).searchParams.get('cmp'));
  const restoredFilter = await page.evaluate(() => new URL(window.location.href).searchParams.get('diff_filter'));
  assert(restoredCmpId === origCmpId, `E-04 comparison_id changed on return: ${origCmpId} -> ${restoredCmpId}`);
  assert(restoredFilter === 'b_wins', `E-04 diff_filter lost on return: ${restoredFilter}`);
  await page.waitForFunction(
    () => document.querySelectorAll('#compare-task-diff tbody tr').length === 5,
    { timeout: 10000 }
  );
  const restoredDrawerTitle = await page.locator('[role="dialog"]').textContent();
  assert(restoredDrawerTitle.includes(firstBWinTaskId), `E-04 Split Diff Drawer lost task ${firstBWinTaskId}`);
  await page.click('button[aria-label="关闭抽屉"]');
  console.log('[OK] 5b. E-04 DeepSWE B 独胜 (5) -> Split Diff -> Trial Microscope -> Return Restored Exact comparison_id + Filter + Drawer');

  // 5c. E-01 Save Frozen Snapshot Short Link -> Reopen /compare/s/:shortId -> Matrix + HTML Export + Task Diff + Split Diff!
  const snapTitle = `E2E Frozen DeepSWE ${Date.now()}`;
  await page.fill('input[placeholder*="保存短链"]', snapTitle);
  await page.selectOption('select[aria-label="保存目标项目"]', 'default');
  await page.selectOption('select[aria-label="保存模式"]', 'snapshot');
  await page.click('button:has-text("保存短链")');
  await page.waitForSelector(`button:has-text("${snapTitle}")`, { timeout: 10000 });
  await page.locator(`button:has-text("${snapTitle}")`).first().click();
  await page.waitForURL(/\/compare\/s\/s_/, { timeout: 10000 });
  await page.waitForSelector('text=45.00%', { timeout: 10000 });
  await page.waitForSelector('#compare-task-diff tbody tr', { timeout: 10000 });
  await page.locator('#compare-task-diff tbody tr').first().click();
  await page.waitForSelector('[role="dialog"]:has-text("展开双栏 ATIF 轨迹 Diff")', { timeout: 8000 });
  await page.click('button[aria-label="关闭抽屉"]');
  const [download] = await Promise.all([
    page.waitForEvent('download', { timeout: 10000 }),
    page.click('button:has-text("html")'),
  ]);
  assert(download.suggestedFilename().endsWith('.html'), `Expected .html download, got ${download.suggestedFilename()}`);
  console.log('[OK] 5c. E-01 Frozen Snapshot Short Link Reopen Preserves Matrix + Task Diff + Split Diff + Standalone HTML Export');

  // 6. Reports Center + Batch Detail (A-07 round1 40/320 & A-10 CMMMU official + VLMEvalKitInferenceError)
  await page.goto(`${BASE_URL}/eval/reports`, { waitUntil: 'networkidle' });
  await page.waitForSelector('table tbody tr', { timeout: 10000 });
  const round1Row = page.locator('tr:has-text("synth-v1-qwen-opencode-round1")').first();
  assert((await round1Row.count()) > 0, 'round1 row missing in Reports');
  await round1Row.click();
  await page.waitForSelector('text=40 题 / 320 尝试', { timeout: 10000 });
  await page.waitForSelector('text=19/40', { timeout: 10000 });
  console.log('[OK] 6a. Batch Detail A-07 round1 (40 tasks / 320 attempts, pass@8 19/40)');

  await page.goto(`${BASE_URL}/eval/reports?runner=vlmevalkit`, { waitUntil: 'networkidle' });
  const cmmmuRow = page.locator('tr:has-text("cmmmu-vlmkit-qwen38-27b-20261124-nonthinking")').first();
  assert((await cmmmuRow.count()) > 0, 'CMMMU nonthinking row missing');
  await cmmmuRow.click();
  await page.waitForSelector('text=VLMEvalKit 官方分项指标', { timeout: 10000 });
  console.log('[OK] 6b. Batch Detail A-10 VLMEvalKit official summary + subject breakdown');

  // A-10 failed VLMEvalKit trial MUST display VLMEvalKitInferenceError explicitly!
  await page.goto(`${BASE_URL}/eval/reports?runner=vlmevalkit`, { waitUntil: 'networkidle' });
  const vlmFailRow = page.locator('tr:has-text("cmmmu-vlmkit-qwen38-27b-20261124-v2")').first();
  assert((await vlmFailRow.count()) > 0, 'Failed VLMEvalKit batch missing');
  await vlmFailRow.click();
  await page.waitForSelector('text=VLMEvalKitInferenceError', { timeout: 10000 });
  await page.locator('table tbody tr').first().click();
  await page.waitForURL(/\/eval\/batches\/.*\/trials\//, { timeout: 10000 });
  await page.waitForSelector('text=VLMEvalKitInferenceError', { timeout: 10000 });
  console.log('[OK] 6c. Batch & Trial Detail A-10 VLMEvalKitInferenceError Explicitly Displayed');

  // 7. Benchmarks, Harnesses, Recommend, Methodology, Admin Health
  for (const route of ['/harnesses', '/benchmarks/terminal-bench', '/recommend', '/toolkit/methodology', '/admin/health']) {
    await page.goto(`${BASE_URL}${route}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(250);
    clsSamples.push({ route, cls: await page.evaluate(() => window.__lrmCls || 0) });
  }
  const healthText = await page.textContent('body');
  assert(healthText.includes('本地运行目录占用') && healthText.includes('存储隔离安全探针'), 'Admin health missing budget or probes');
  console.log('[OK] 7. Harnesses, Benchmarks, Recommend, Methodology, Admin Health verified');

  await browser.close();

  const maxCls = Math.max(...clsSamples.map((x) => x.cls), 0);
  const maxInp = Math.max(...inpSamples.map((x) => x.ms), 0);
  console.log('=== E2E METRICS SUMMARY ===');
  console.log('CLS samples:', JSON.stringify(clsSamples));
  console.log('Event Timing INP samples (ms):', JSON.stringify(inpSamples));
  console.log(`max_cls=${maxCls.toFixed(4)} (< 0.05), max_event_timing_inp=${maxInp.toFixed(1)}ms (< 100ms)`);
  console.log(`console_errors=${consoleErrors.length}, page_errors=${pageErrors.length}, http_5xx=${failed5xx.length}, media_404=${failed404Media.length}`);

  if (consoleErrors.length) console.error('Console errors:', consoleErrors);
  if (pageErrors.length) console.error('Page errors:', pageErrors);
  if (failed5xx.length) console.error('HTTP 5xx:', failed5xx);
  if (failed404Media.length) console.error('Media 404:', failed404Media);

  assert(consoleErrors.length === 0, `Console errors detected: ${consoleErrors.join('; ')}`);
  assert(pageErrors.length === 0, `Page errors detected: ${pageErrors.join('; ')}`);
  assert(failed5xx.length === 0, `HTTP 5xx detected: ${failed5xx.join('; ')}`);
  assert(failed404Media.length === 0, `Media 404s detected: ${failed404Media.join('; ')}`);
  assert(maxCls < 0.05, `CLS ${maxCls} >= 0.05`);
  assert(maxInp < 100, `W3C Event Timing INP ${maxInp}ms >= 100ms`);
  console.log('ALL M1 PLAYWRIGHT E2E ASSERTIONS PASSED!');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
