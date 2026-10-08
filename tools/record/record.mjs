// The README's animations and the app's own clips, recorded frame by frame
// at the Colosseum on the real 3D mesh: set the state, wait for the mesh to
// be sharp, shoot. Deterministic, so a change to the interface is a re-run.
//
//   npm run dev                                  # the app on :5173
//   npm i --no-save playwright-core              # once
//   for c in hero time date pin analyze heat blur; do node tools/record/record.mjs $c /tmp/clips; done
//   tools/record/encode.sh /tmp/clips docs /tmp/gifs   # videos and posters into docs/, GIFs aside
//
// The GIFs go on the orphan `assets` branch (see the README). Needs
// CESIUM_ION_TOKEN in pipeline/.env, and Chrome for Testing
// (`npx playwright install chromium`) for a GPU; $CHROME and $SG_URL override.
import fs from 'node:fs';
import { open, settle, mod } from './lib.mjs';

const [clip, outRoot] = process.argv.slice(2);
const out = `${outRoot}/${clip}`;
fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(out, { recursive: true });

const DATE = { y: 2026, m: 7, d: 15 };
const size = clip === 'hero' ? { width: 1200, height: 694 } : clip === 'blur' ? { width: 1360, height: 788 } : { width: 1200, height: 720 };
// The heat clip looks from higher up: at the Colosseum's own height a 90 m pixel or two fill the
// frame; from 2.5 km the centre's stone and the parks around it tell the story.
const hash = clip === 'heat' ? '#ll=41.890498,12.492392&cam=41.8640,12.4924,2600&hp=0,-52' : '';
const { browser, page, errors } = await open({ ...size, hash });
await page.addStyleTag({ content: `
  #ion-usage{display:none!important}
  #fakecursor{position:fixed;z-index:99999;width:22px;height:22px;pointer-events:none;transform:translate(-3px,-2px);transition:none}
  #fakecursor.down path{fill:#e2673f}
` });
await page.evaluate(() => {
  const c = document.createElement('div');
  c.id = 'fakecursor';
  c.hidden = true;
  c.innerHTML = '<svg viewBox="0 0 24 24"><path d="M3 2l7.5 19 2.6-7.9L21 10.5Z" fill="#fff" stroke="#111" stroke-width="1.4" stroke-linejoin="round"/></svg>';
  document.body.appendChild(c);
});

let n = 0;
const shot = async (ms = 250) => {
  await settle(page, ms);
  await page.screenshot({ path: `${out}/f${String(n++).padStart(3, '0')}.png` });
};
const cursor = (x, y, down = false) => page.evaluate(([x, y, d]) => {
  const c = document.getElementById('fakecursor'); c.hidden = false; c.style.left = `${x}px`; c.style.top = `${y}px`; c.classList.toggle('down', d);
}, [x, y, down]);
const hideCursor = () => page.evaluate(() => { document.getElementById('fakecursor').hidden = true; });
const centre = async sel => { const b = await page.locator(sel).first().boundingBox(); return [b.x + b.width / 2, b.y + b.height / 2]; };
/** Where a slider's thumb is, for a value between its min and max. */
const thumb = async (sel) => page.evaluate(s => {
  const el = document.querySelector(s); const r = el.getBoundingClientRect();
  const f = (el.value - el.min) / (el.max - el.min); return [r.left + 9 + f * (r.width - 18), r.top + r.height / 2];
}, sel);
const setTime = m => mod(page, '/js/state.js', 'm.setTime(a)', m);
const setDate = d => mod(page, '/js/state.js', 'm.setDate(a)', d);
const setDoy = d => mod(page, '/js/state.js', 'm.setDayOfYear(a)', d);
const glide = async (from, to, frames, fn) => { for (let i = 0; i < frames; i++) await fn(from + (to - from) * (i / (frames - 1))); };
const moveTo = async ([x0, y0], [x1, y1], frames, ms = 60) => { for (let i = 1; i <= frames; i++) { const t = i / frames; const e = t * t * (3 - 2 * t); await cursor(x0 + (x1 - x0) * e, y0 + (y1 - y0) * e); await shot(ms); } };
const events = () => page.evaluate(() => window.solargaze.state.events);
/** Until every reading in ANALYZE has its number (the weather comes from the network). */
const readingsReady = () => page.waitForFunction(() => [...document.querySelectorAll('#layer-rows .lcard-read b, #layer-rows .lcard-sub b')].every(b => b.textContent !== '…'),
  null, { timeout: 45000, polling: 300 }).catch(() => {});

await setDate(DATE);
await setTime(10 * 60 + 3);
await readingsReady();
await settle(page, 1500, 40000);

if (clip === 'hero' || clip === 'blur') {
  const ev = await events();
  const [a, b] = clip === 'blur' ? [16 * 60, 16 * 60] : [ev.sunrise + 25, ev.sunset - 20];
  await glide(a, b, clip === 'blur' ? 1 : 22, async m => { await setTime(Math.round(m)); await shot(500); });
} else if (clip === 'time') {
  await setTime(10 * 60 + 3);
  let p = await thumb('#time-slider');
  await cursor(p[0] + 60, p[1] + 40); await shot();
  await moveTo([p[0] + 60, p[1] + 40], p, 5);
  const ev = await events();
  await glide(10 * 60 + 3, ev.sunset - 25, 34, async m => { await setTime(Math.round(m)); p = await thumb('#time-slider'); await cursor(...p, true); await shot(300); });
  for (let i = 0; i < 4; i++) { await cursor(...p); await shot(); }
} else if (clip === 'date') {
  await setTime(10 * 60 + 3);
  const s2 = '#date-slider';
  await setDoy(25);
  let p = await thumb(s2);
  await cursor(p[0] + 50, p[1] + 40); await shot();
  await moveTo([p[0] + 50, p[1] + 40], p, 5);
  await glide(25, 182, 34, async d => { await setDoy(Math.round(d)); p = await thumb(s2); await cursor(...p, true); await shot(300); });
  for (let i = 0; i < 4; i++) { await cursor(...p); await shot(); }
} else if (clip === 'pin') {
  const lock = await centre('#btn-pin');
  await cursor(lock[0] + 120, lock[1] + 60); await shot();
  await moveTo([lock[0] + 120, lock[1] + 60], lock, 6);
  await cursor(...lock, true); await page.evaluate(() => document.getElementById('btn-pin').click()); await shot(400);
  await cursor(...lock); await shot(); await shot();
  const screen = () => page.evaluate(() => {
    const { viewer, state } = window.solargaze; const C = window.Cesium;
    const p = C.Cartesian3.fromDegrees(state.lon, state.lat, (state.groundHeight ?? 20) + (state.altitude || 0));
    const w = C.SceneTransforms.worldToWindowCoordinates?.(viewer.scene, p) ?? C.SceneTransforms.wgs84ToWindowCoordinates(viewer.scene, p);
    return [w.x, w.y];
  });
  // Picked up, and dropped on the arena's south side: a real drag, the camera still.
  let p = await screen();
  await moveTo(lock, p, 8);
  await page.mouse.move(p[0], p[1]);
  await page.mouse.down();
  const to = [p[0] - 70, p[1] + 150];
  for (let i = 1; i <= 26; i++) {
    const t = i / 26; const e = t * t * (3 - 2 * t);
    const q = [p[0] + (to[0] - p[0]) * e, p[1] + (to[1] - p[1]) * e];
    await page.mouse.move(q[0], q[1], { steps: 2 });
    await cursor(...q, true); await shot(300);
  }
  await page.mouse.up();
  for (let i = 0; i < 6; i++) { await cursor(...to); await shot(); }
} else if (clip === 'analyze') {
  const run = await centre('#btn-run-analyze');
  await cursor(run[0] - 260, run[1] + 160); await shot();
  await moveTo([run[0] - 260, run[1] + 160], run, 9);
  await cursor(...run, true);
  await page.evaluate(() => document.getElementById('btn-run-analyze').click());
  for (let i = 0; i < 12; i++) { await cursor(...run); await page.waitForTimeout(250); await shot(50); }
  await page.waitForFunction(() => !document.getElementById('analyze-out').hidden, null, { timeout: 120000 });
  for (let i = 0; i < 16; i++) { await hideCursor(); await shot(); }
} else if (clip === 'heat') {
  await page.evaluate(() => { if (window.solargaze.state.prefs.sunPath) document.getElementById('chip-sun').click(); });
  await setTime(10 * 60 + 30);
  await settle(page, 800);
  const card = await centre('#chip-temperature');
  await cursor(card[0] - 300, card[1] + 120); await shot();
  await moveTo([card[0] - 300, card[1] + 120], card, 7);
  await cursor(...card, true); await page.evaluate(() => document.getElementById('chip-temperature').click());
  await page.waitForFunction(() => !document.querySelector('#card-temperature .lcard-legend').hidden, null, { timeout: 60000 });
  await shot(1500);
  await cursor(...card);
  for (let i = 0; i < 12; i++) await shot(150);
  // Into the night: the hour, dragged past sunset — the clock decides.
  let p = await thumb('#time-slider');
  await moveTo(card, p, 8);
  const ev = await events();
  await glide(10 * 60 + 30, 22 * 60 + 30, 16, async m => {
    await setTime(Math.round(m)); p = await thumb('#time-slider'); await cursor(...p, true);
    if (m > ev.sunset) await page.waitForTimeout(600);
    await shot(400);
  });
  await readingsReady();
  await page.waitForTimeout(2500);
  await cursor(...p);
  for (let i = 0; i < 16; i++) await shot(150);
  await page.evaluate(() => { document.getElementById('chip-temperature').click(); document.getElementById('chip-sun').click(); });
}
console.log(JSON.stringify({ clip, frames: n, errors: errors.slice(0, 5) }));
await browser.close();
