// Shared by record.mjs: a GPU-backed headless Chrome on the app, signed in
// with the Cesium ion token from pipeline/.env, and a way to wait until the
// 3D mesh has nothing left to load.
import { chromium } from 'playwright-core';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
export const BASE = process.env.SG_URL || 'http://localhost:5173';

/** Chrome for Testing as Playwright installs it (`npx playwright install chromium`), or $CHROME. */
export const CHROME = process.env.CHROME || (() => {
  const cache = path.join(os.homedir(), 'Library/Caches/ms-playwright');
  const dir = fs.existsSync(cache) && fs.readdirSync(cache).filter(d => /^chromium-\d+$/.test(d)).sort().pop();
  const app = dir && path.join(cache, dir, 'chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing');
  return app && fs.existsSync(app) ? app : undefined;   // undefined: Playwright's own default
})();

export function token() {
  const env = fs.readFileSync(path.join(REPO, 'pipeline/.env'), 'utf8');
  const m = /^CESIUM_ION_TOKEN=(.*)$/m.exec(env);
  if (!m || !m[1].trim()) throw new Error('CESIUM_ION_TOKEN is not set in pipeline/.env');
  return m[1].trim().replace(/^['"]|['"]$/g, '');
}

export async function open({ width = 1200, height = 720, hash = '' } = {}) {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true,
    args: ['--use-angle=metal', '--enable-gpu', '--ignore-gpu-blocklist', '--enable-webgl', '--hide-scrollbars'] });
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(`${BASE}/?ion=${token()}${hash}`, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.solargaze?.diag?.meshReady && (window.solargaze.diag.initialViewSettled
    || window.solargaze.diag.urlPlacedView || window.solargaze.diag.settleSkipped), null, { timeout: 120000, polling: 500 });
  await page.addStyleTag({ content: '.mousecard{display:none!important} .toast{display:none!important}' });
  return { browser, page, errors };
}

/** Wait until the 3D tiles and the globe have nothing left to load, then a beat. */
export async function settle(page, ms = 400, timeout = 20000) {
  await page.evaluate(() => window.solargaze.viewer.scene.requestRender());
  await page.waitForFunction(() => {
    const v = window.solargaze.viewer; v.scene.requestRender();
    const ts = [...Array(v.scene.primitives.length).keys()].map(i => v.scene.primitives.get(i)).filter(p => p && 'tilesLoaded' in p);
    return ts.every(t => t.tilesLoaded) && (!v.scene.globe?.show || v.scene.globe.tilesLoaded);
  }, null, { timeout, polling: 200 }).catch(() => {});
  await page.waitForTimeout(ms);
}

/** Run `fn` (a function body, given the module as `m` and an argument `a`) against one of the app's modules. */
export const mod = (page, file, fn, arg) => page.evaluate(async ([p, f, a]) => {
  const m = await import(p); return new Function('m', 'a', f)(m, a);
}, [file, fn, arg]);
