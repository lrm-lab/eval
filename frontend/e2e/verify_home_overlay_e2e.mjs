// Run against a built frontend from a Node environment with Playwright and Chromium installed.
import assert from 'node:assert/strict';
import { chromium } from 'playwright';

const origin = new URL(process.env.BASE_URL || 'http://127.0.0.1:5173').origin;
const cellId = 'overlay-check';
const releaseId = 'overlay-check-v1';
const batch = {
  batch_id: 'overlay-batch', generation_id: 'gen-1', batch_name: 'Overlay browser check',
  batch_status: 'finished', run_scope: 'full_split', score_kind: 'trial_valid_mean',
  score: 0.5, scored: 1, recorded: 1, planned: 1, recorded_at: null,
};
const group = {
  dataset_release_ids: [releaseId], registered_task_count: 1,
  published_run_count: 1, cell_ids: [cellId],
};
const evaluationMap = {
  cells: [{
    id: cellId, kind: 'trials', benchmark_slug: 'overlay-check', dataset_release_id: releaseId,
    dataset_version: 'v1', split: 'test', display_name: 'Overlay dataset', registered_task_count: 1,
    model_key: 'model', model_canonical: 'Overlay model', model_route: 'Overlay model',
    harness_key: 'harness', harness: 'Overlay harness', harness_version: 'v1', runner: 'direct',
    scoring_protocol: 'exact', missing_identity: [], historical_scores: [],
    published_run_count: 1, scored_run_count: 1, running_count: 0, has_scored_history_peer: false,
    latest_batch: batch, latest_scored_batch: batch,
  }],
  releases: [{ dataset_release_id: releaseId, registered_task_count: 1 }],
  groups: {
    dataset: [{ ...group, id: 'overlay-check', label: 'Overlay dataset' }],
    model: [{ ...group, id: 'model', label: 'Overlay model' }],
    harness: [{ ...group, id: 'harness', label: 'Overlay harness' }],
  },
};

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
try {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 }, hasTouch: true, reducedMotion: 'no-preference',
  });
  await context.addCookies([{ name: 'lrm_eval_csrf', value: 'browser-fixture', url: origin }]);
  await context.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, (route) => route.abort());
  await context.route('**/api/**', (route) => {
    switch (new URL(route.request().url()).pathname) {
      case '/api/v1/auth/me':
        return route.fulfill({ json: {
          user_id: 'overlay-viewer', username: 'overlay-viewer', display_name: 'Viewer',
          role: 'viewer', projects: ['default'], scopes: [],
        } });
      case '/api/v1/home/evaluation-map':
        return route.fulfill({ json: evaluationMap });
      case '/api/v1/compare/history':
        return route.fulfill({ json: { items: [] } });
      default:
        return route.abort();
    }
  });
  const page = await context.newPage();
  page.setDefaultTimeout(10_000);
  const runtimeErrors = [];
  page.on('pageerror', (error) => runtimeErrors.push(error.message));
  await page.goto(origin, { waitUntil: 'domcontentloaded' });

  const tile = page.locator(`#heatmap-tile-${cellId}`);
  const detail = page.getByRole('dialog', { name: '评测详情' });
  const closeButton = detail.getByRole('button', { name: '关闭评测详情' });
  await tile.waitFor();
  const tileBounds = await tile.boundingBox();
  assert.ok(tileBounds);
  const tapTile = () => page.touchscreen.tap(tileBounds.x + 16, tileBounds.y + 16);
  await tapTile();
  await detail.waitFor();
  assert.equal(new URL(page.url()).searchParams.get('cell'), cellId);

  // The native dialog must release focus and clicks before its visual exit completes.
  await detail.evaluate((dialog) => {
    dialog.addEventListener('close', () => {
      if (dialog.open) return;
      const style = getComputedStyle(dialog);
      const animations = dialog.getAnimations();
      if (!window.lastDetailExit) {
        // Keep the first discrete exit in progress even on a slow test host.
        for (const animation of animations) {
          if (['display', 'overlay'].includes(animation.transitionProperty)) animation.updatePlaybackRate(0.1);
        }
      }
      window.lastDetailExit = {
        display: style.display, pointerEvents: style.pointerEvents, inert: dialog.inert,
        modal: dialog.matches(':modal'), animations: animations.map((animation) => animation.transitionProperty),
      };
    });
  });
  await closeButton.tap();
  await page.waitForURL((url) => !url.searchParams.has('cell'));
  const normalExit = await page.evaluate(() => window.lastDetailExit);
  assert.notEqual(normalExit.display, 'none');
  assert.equal(normalExit.pointerEvents, 'none');
  assert.equal(normalExit.inert, true);
  assert.equal(normalExit.modal, false);
  assert.ok(normalExit.animations.includes('opacity'));

  // Reopen during the exit, then change the live OS preference before the next close.
  await tapTile();
  await page.waitForFunction(() => document.querySelector('.heatmap-dialog')?.matches(':modal'));
  assert.equal(new URL(page.url()).searchParams.get('cell'), cellId);
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.keyboard.press('Escape');
  await page.waitForURL((url) => !url.searchParams.has('cell'));
  const reducedExit = await page.evaluate(() => window.lastDetailExit);
  assert.equal(reducedExit.display, 'none');
  assert.deepEqual(reducedExit.animations, []);
  await page.waitForFunction((id) => document.activeElement?.id === id, `heatmap-tile-${cellId}`);
  assert.deepEqual(runtimeErrors, []);
  console.log('390px mobile detail closes immediately after a fast reopen and a live reduced-motion change.');
} finally {
  await browser.close();
}
