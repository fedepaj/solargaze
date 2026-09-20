/** Help, settings and the first-run key prompt — all in one sheet element. */

import { state, setPref, setIonToken } from '../state.js';
import { applyShadowSettings } from '../scene.js';
import { ION_CLIENT_ID, resetAll } from '../config.js';
import * as ionAuth from '../ion-auth.js';
import { toast } from './toast.js';

const $ = id => document.getElementById(id);
let onSaved = null;
let onSkipped = null;

export function initModals({ onCredentialSaved, onSkip } = {}) {
  onSaved = onCredentialSaved;
  onSkipped = onSkip;
  $('modal-x').addEventListener('click', closeModal);
  $('modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });
  window.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

  $('btn-help').addEventListener('click', openHelp);
  $('btn-settings').addEventListener('click', openSettings);
}

export function closeModal() {
  $('modal').hidden = true;
  $('sheet-back')?.remove();
}

/**
 * Render a sheet. Passing `back` adds a return arrow instead of making the
 * close button the only way out — a guide opened from the sign-in panel should
 * hand you back to it, not dump you on the map.
 */
function openSheet(html, wire, back = null) {
  const body = $('modal-body');
  body.innerHTML = html;
  $('modal').hidden = false;

  const existing = $('sheet-back');
  if (existing) existing.remove();

  if (back) {
    const button = document.createElement('button');
    button.id = 'sheet-back';
    button.className = 'sheet-back';
    button.innerHTML =
      '<svg viewBox="0 0 24 24"><path d="M15 5.5 8.5 12l6.5 6.5"/></svg><span>Back</span>';
    button.addEventListener('click', back);
    body.parentElement.insertBefore(button, body);
  }

  wire?.(body);
}

/* ── first run: pick a way in ──────────────────────────────────────── */

export function openKeyPrompt({ reason = '' } = {}) {
  const canSignIn = ionAuth.isConfigured(ION_CLIENT_ID);
  const intro = reason || (
    'SolarGaze casts shadows onto the photorealistic 3D mesh that Google Earth renders. ' +
    'That data is Google\'s, and Cesium ion brokers it — so all you need is a free ion ' +
    'account. No Google Cloud project, no billing details, nothing to pay.'
  );

  openSheet(
    `
    <h2>Connect the 3D mesh</h2>
    <p>${intro}</p>

    ${canSignIn ? `
    <button class="cta" id="ion-signin">Sign in with Cesium ion</button>
    <p style="margin-top:12px"><small style="color:rgba(255,255,255,.5)">You approve once on
       Cesium's own page and come back signed in. The tiles then draw on your own free quota,
       and the sign-in stays only in this browser.</small></p>

    <h3 style="margin-top:26px">OR PASTE A TOKEN</h3>
    <p>If you would rather not sign in, copy an access token from the
       <i>Access Tokens</i> tab of your
       <a href="https://ion.cesium.com/signup" target="_blank" rel="noopener">ion account</a>.</p>` : `
    <h3>CESIUM ION TOKEN</h3>
    <p>Sign up free at <a href="https://ion.cesium.com/signup" target="_blank" rel="noopener">Cesium ion</a>,
       copy the access token from the <i>Access Tokens</i> tab, and paste it here.</p>`}
    <div class="field">
      <input id="ion-input" type="text" placeholder="eyJhbGciOi…" autocomplete="off" spellcheck="false" value="${escapeHtml(state.ionToken)}">
      <button id="ion-save">Save</button>
    </div>

    <button class="ghost-cta" id="open-guide2">
      <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M9.6 9.3a2.5 2.5 0 1 1 3.3 2.4c-.6.2-.9.8-.9 1.4v.4"/><path d="M12 16.8h.01"/></svg>
      First time here? Three steps, about a minute
    </button>
    <p style="margin-top:8px">Or <a href="#" id="key-skip">carry on without</a> — you keep the
       full sun model and every control, drawn over a plain 2D map instead of the 3D mesh.</p>
    `,
    root => {
      const ion = root.querySelector('#ion-input');
      const commit = () => {
        const value = ion.value.trim();
        if (!value) return toast('Paste a token first.', { error: true });
        setIonToken(value);
        closeModal();
        onSaved?.();
      };

      const signIn = root.querySelector('#ion-signin');
      if (signIn) {
        signIn.addEventListener('click', async () => {
          signIn.disabled = true;
          signIn.textContent = 'Redirecting to Cesium ion…';
          try {
            await ionAuth.beginSignIn(ION_CLIENT_ID);
          } catch (err) {
            signIn.disabled = false;
            signIn.textContent = 'Sign in with Cesium ion';
            toast(String(err.message || err), { error: true, ms: 6000 });
          }
        });
      }

      root.querySelector('#open-guide2')?.addEventListener('click', e => {
        e.preventDefault();
        openGuide({ back: () => openKeyPrompt({ reason }) });
      });
      root.querySelector('#ion-save').addEventListener('click', commit);
      ion.addEventListener('keydown', e => { if (e.key === 'Enter') commit(); });

      root.querySelector('#key-skip').addEventListener('click', e => {
        e.preventDefault();
        closeModal();
        onSkipped?.();
      });
      setTimeout(() => ion.focus(), 60);
    },
  );
}

/* ── the three-step guide ─────────────────────────────────────────── */

/**
 * Written for the person using the site, not the person forking it: they need
 * a free account and one button, nothing about client ids or redirect URIs.
 * That audience is served by the README instead.
 *
 * Drop the screenshots into docs/ with these names and they appear; until then
 * each step shows a dashed placeholder saying which shot is missing.
 */
const STEPS = [
  {
    title: 'Open Cesium ion and start a sign-up',
    body: 'Cesium ion is what hands Google\'s 3D buildings to this page. The account is free, ' +
          'and there is no Google Cloud project and no card involved. Use one of the ' +
          'third-party buttons, or the sign-up link at the bottom.',
    link: ['ion.cesium.com/signup', 'https://ion.cesium.com/signup'],
    shot: 'ion-login-signup.jpg',
    arrow: [{ x: 46, y: 64.8, label: 'Sign up' }],
  },
  {
    title: 'Pick a username and accept the terms',
    body: 'The data centre can stay on its default — it only decides where Cesium stores ' +
          'assets you upload yourself, and this app uploads nothing.',
    shot: 'ion-signup-google-username.jpg',
    arrow: [{ x: 34.5, y: 57.8, label: 'Sign up' }],
  },
  {
    title: 'Verify your email',
    body: 'Cesium mails you a six-digit code. Type it in and confirm; the account is live ' +
          'straight away, with nothing left to configure.',
    shot: 'ion-email-verification.jpg',
    blur: [
      { x: 87.6, y: 1.4, w: 7.0, h: 4.6 },   // account name in the header bar
      { x: 53.4, y: 42.2, w: 10.8, h: 3.2 }, // the address the code went to
    ],
    arrow: [{ x: 34.7, y: 55.2, label: 'Code from the email' }],
  },
  {
    title: 'Come back here and press Allow',
    body: 'SolarGaze asks for exactly two permissions: reading map assets, which is the ' +
          'buildings, and geocoding, which is the search box. Nothing else, and no write ' +
          'access to your account.',
    shot: 'ion-authorize.jpg',
    blur: [
      { x: 34, y: 20.4, w: 5.8, h: 3.4 },    // "Hello <name>,"
      { x: 55, y: 46.6, w: 5.8, h: 3.4 },    // "Not <name>? Switch accounts"
    ],
    arrow: [{ x: 34.1, y: 46.8, label: 'Allow' }],
  },
];

const overlay = step => [
  ...(step.blur || []).map(r =>
    `<span class="mask" style="left:${r.x}%;top:${r.y}%;width:${r.w}%;height:${r.h}%"></span>`),
  // Label above, arrowhead below: the whole marker is pulled up by its own
  // height, so whatever sits last is what actually lands on the coordinate.
  ...(step.arrow || []).map(a =>
    `<span class="point" style="left:${a.x}%;top:${a.y}%">
       ${a.label ? `<b>${a.label}</b>` : ''}
       <svg viewBox="0 0 24 24"><path d="M12 3v15M6.5 12.5 12 18l5.5-5.5"/></svg>
     </span>`),
].join('');

export function openGuide({ back = null } = {}) {
  const steps = STEPS.map(step => `
    <li>
      <h4>${step.title}</h4>
      <p>${step.body}${step.link ? ` <a href="${step.link[1]}" target="_blank" rel="noopener">${step.link[0]}</a>` : ''}</p>
      <span class="shot-wrap">
        <img class="shot" src="./docs/${step.shot}" alt=""
             onerror="this.closest('.shot-wrap').classList.add('is-missing')">
        ${overlay(step)}
        <span class="shot-missing">screenshot pending — drop <code>docs/${step.shot}</code> in the repo</span>
      </span>
    </li>`).join('');

  openSheet(`
    <h2>Getting the 3D buildings</h2>
    <p>Three steps, about a minute. Everything stays in this browser — the sign-in is
       between you and Cesium, and this page never sees a password.</p>
    <ol class="steps">${steps}</ol>
    <p><small style="color:rgba(255,255,255,.5)">Prefer not to sign in? The app works without
       it over a plain 2D map: you keep the whole sun model, just not the buildings that
       cast the shadows.</small></p>
  `, null, back);
}

/* ── settings ─────────────────────────────────────────────────────── */

function openSettings() {
  const p = state.prefs;
  openSheet(
    `
    <h2>Settings</h2>

    <h3>SHADOWS</h3>
    <div class="rowopt">
      <div>Cast shadows<small>Turn off for a faster, flat-lit view.</small></div>
      <button class="switch" data-pref="shadows" aria-pressed="${p.shadows}"></button>
    </div>
    <div class="rowopt">
      <div>Soft edges<small>Percentage-closer filtering. Prettier, slightly slower.</small></div>
      <button class="switch" data-pref="softShadows" aria-pressed="${p.softShadows}"></button>
    </div>
    <div class="rowopt">
      <div>Shadow resolution<small>Higher is sharper but costs GPU memory.</small></div>
      <div class="seg" id="seg-quality">
        ${[1024, 2048, 4096].map(v => `<button data-q="${v}" class="${p.shadowQuality === v ? 'is-on' : ''}">${v}</button>`).join('')}
      </div>
    </div>

    <h3>MESH</h3>
    <div class="rowopt">
      <div>Tile detail<small>Finer tiles are sharper and spend the ion quota faster.</small></div>
      <div class="seg" id="seg-mesh">
        ${[['Fine', 16], ['Balanced', 24], ['Light', 32]].map(([label, v]) =>
          `<button data-sse="${v}" class="${p.meshDetail === v ? 'is-on' : ''}">${label}</button>`).join('')}
      </div>
    </div>

    <h3>OVERLAY</h3>
    <div class="rowopt">
      <div>Sun path &amp; compass<small>The ring, the day arc and the readouts.</small></div>
      <button class="switch" data-pref="sunPath" aria-pressed="${p.sunPath}"></button>
    </div>
    <div class="rowopt">
      <div>Full 24-hour slider<small>Off, the slider spans sunrise to sunset like a sun study.</small></div>
      <button class="switch" data-pref="fullDayRange" aria-pressed="${p.fullDayRange}"></button>
    </div>
    <div class="rowopt">
      <div>Weather badge<small>Current conditions from Open-Meteo.</small></div>
      <button class="switch" data-pref="weather" aria-pressed="${p.weather}"></button>
    </div>

    <h3>CESIUM ION</h3>
    ${state.ionSignedIn ? `
    <div class="rowopt">
      <div>Signed in<small>${ionAuth.sessionCanGeocode()
        ? 'Mesh and place search are both drawing on your own ion quota.'
        : 'Mesh only — this sign-in has no geocoding permission, so search falls back to OpenStreetMap. Sign out and back in to grant it.'}</small></div>
      <button class="seg-btn" id="ion-signout">Sign out</button>
    </div>` : ionAuth.isConfigured(ION_CLIENT_ID) ? `
    <button class="cta" id="ion-signin2" style="margin-bottom:14px">Sign in with Cesium ion</button>` : ''}
    <p><small style="color:rgba(255,255,255,.55)">Mesh source:
      <b>${state.tileSource === 'ion' ? 'Cesium ion' : 'none — flat basemap'}</b>.
      A pasted token overrides a sign-in; saving reloads the page.</small></p>
    <div class="field">
      <input id="ion-input2" type="text" placeholder="Paste an ion token instead" autocomplete="off" spellcheck="false" value="${escapeHtml(state.ionToken)}">
      <button id="ion-save2">Save</button>
    </div>

    <h3>RESET</h3>
    <div class="rowopt">
      <div>Start over<small>Forgets the sign-in, any pasted token and every preference, then reloads. Same as opening the page with <code>?reset</code>.</small></div>
      <button class="seg-btn" id="btn-reset">Reset</button>
    </div>
    `,
    root => {
      root.querySelectorAll('.switch[data-pref]').forEach(btn => {
        btn.addEventListener('click', () => {
          const key = btn.dataset.pref;
          const next = btn.getAttribute('aria-pressed') !== 'true';
          btn.setAttribute('aria-pressed', String(next));
          setPref(key, next);
          applyShadowSettings();
        });
      });

      root.querySelector('#seg-quality').addEventListener('click', e => {
        const btn = e.target.closest('button[data-q]');
        if (!btn) return;
        root.querySelectorAll('#seg-quality button').forEach(b => b.classList.remove('is-on'));
        btn.classList.add('is-on');
        setPref('shadowQuality', Number(btn.dataset.q));
        applyShadowSettings();
      });

      root.querySelector('#seg-mesh').addEventListener('click', e => {
        const btn = e.target.closest('button[data-sse]');
        if (!btn) return;
        root.querySelectorAll('#seg-mesh button').forEach(b => b.classList.remove('is-on'));
        btn.classList.add('is-on');
        setPref('meshDetail', Number(btn.dataset.sse));
      });

      root.querySelector('#ion-save2').addEventListener('click', () => {
        setIonToken(root.querySelector('#ion-input2').value.trim());
        location.reload();
      });

      root.querySelector('#ion-signout')?.addEventListener('click', () => {
        ionAuth.signOut();
        setIonToken('');
        location.reload();
      });
      root.querySelector('#btn-reset')?.addEventListener('click', () => {
        ionAuth.signOut();
        resetAll();
      });
      root.querySelector('#ion-signin2')?.addEventListener('click', () => {
        ionAuth.beginSignIn(ION_CLIENT_ID).catch(err =>
          toast(String(err.message || err), { error: true, ms: 6000 }));
      });
    },
  );
}

/* ── help ─────────────────────────────────────────────────────────── */

function openHelp() {
  openSheet(`
    <h2>SolarGaze</h2>
    <p>An open sun-and-shadow simulator. Pick a place, drag the two sliders — time of day and day of year — and watch real shadows fall across Google's photorealistic 3D mesh. Sun positions come from the NOAA solar equations; the shadows are cast by CesiumJS's shadow map against the actual building geometry.</p>

    <button class="cta" id="open-guide" style="margin:4px 0 6px">How do I get the 3D buildings?</button>

    <h3>CONTROLS</h3>
    <ul>
      <li><b>Drag the view</b> — with the padlock open, the point follows the centre of the screen.</li>
      <li><b>Padlock</b> (left rail) — lock the point, then <b>drag it</b> on the map to place it exactly.</li>
      <li><b>± metres</b> — raise the point off the ground to study a balcony or a roof.</li>
      <li><b>← →</b> — step time by 10 minutes (hold <b>Shift</b> for an hour).</li>
      <li><b>↑ ↓</b> — step the date by a day (<b>Shift</b> for a month).</li>
      <li><b>Space</b> — play the day through.</li>
      <li><b>N</b> — jump to the current time where the pin is.</li>
      <li><b>Drag</b> to orbit, <b>right-drag</b> or <b>scroll</b> to zoom, <b>middle-drag</b> to tilt.</li>
    </ul>

    <h3>READING THE OVERLAY</h3>
    <p>The ring on the ground is a compass card graduated in degrees. The glowing arc is the sun's track for the selected day; the dot on it is where the sun is right now, labelled with its <b>△ elevation</b> above the horizon and its <b>azimuth</b> on the ring. The pale line points along the shadow.</p>

    <h3>ACCURACY</h3>
    <p>Sun geometry is good to well under a tenth of a degree. What limits the result is the mesh: Google's tiles are photogrammetry, so trees, awnings and thin structures are approximate, and shading baked into the imagery is not removed. Treat it as a very good study, not a survey.</p>

    <h3>CREDITS</h3>
    <p>Built on <a href="https://cesium.com/platform/cesiumjs/" target="_blank" rel="noopener">CesiumJS</a> (Apache-2.0), with Google Photorealistic 3D Tiles served through <a href="https://cesium.com/platform/cesium-ion/" target="_blank" rel="noopener">Cesium ion</a>. Weather by <a href="https://open-meteo.com" target="_blank" rel="noopener">Open-Meteo</a>. Timezone boundaries by <code>tz-lookup</code>. SolarGaze itself is MIT licensed.</p>
  `, wireHelp);
}

function wireHelp(root) {
  root.querySelector('#open-guide')?.addEventListener('click', () =>
    openGuide({ back: openHelp }));
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}
