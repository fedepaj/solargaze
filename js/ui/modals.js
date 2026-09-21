/** Help, settings and the first-run key prompt — all in one sheet element. */

import { state, setPref, setIonToken } from '../state.js';
import { applyShadowSettings } from '../scene.js';
import { ION_CLIENT_ID, resetAll } from '../config.js';
import * as ionAuth from '../ion-auth.js';
import { toast } from './toast.js';
import { working } from './sunloader.js';
import { escapeHtml } from '../util.js';

const $ = id => document.getElementById(id);
let onSaved = null;
let onRetried = null;

/**
 * True while the credential gate owns the screen.
 *
 * The gate is deliberately inescapable: without Cesium ion there is no mesh,
 * without a mesh there is nothing to cast a shadow, and an app that cannot do
 * the one thing it exists for is worse than an honest closed door. So the X,
 * the backdrop and Escape are all inert until a credential actually works.
 */
let gated = false;

export function initModals({ onCredentialSaved, onRetry } = {}) {
  onSaved = onCredentialSaved;
  onRetried = onRetry;
  $('modal-x').addEventListener('click', closeModal);
  $('modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });
  window.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

  $('btn-help').addEventListener('click', openHelp);
  $('btn-settings').addEventListener('click', openSettings);
}

export function closeModal() {
  if (gated) return;
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

/* ── the credential gate ─────────────────────────────────────────── */

/**
 * The way in, and the only one.
 *
 * Two shapes, chosen by whether we are here because nothing has been tried yet
 * or because something failed. A first visit gets the hero and the pitch: the
 * visitor is being asked to make an account before they have seen a single
 * shadow move, so show them the shadows first. A failure gets the reason and a
 * retry instead — they know what the app does, they want it back.
 */
export function openGate({ reason = '', kind = '' } = {}) {
  gated = true;
  document.body.classList.add('is-gated');
  $('modal').classList.add('is-gate');

  const canSignIn = ionAuth.isConfigured(ION_CLIENT_ID);
  const failed = !!reason;
  // A rejected credential is the one failure retrying cannot mend, so there the
  // sign-in leads and the retry steps back.
  const retryFirst = failed && kind !== 'credential';

  // Whichever action actually helps is the yellow one, and it goes first: a
  // demoted button sitting above the primary reads as the thing to press.
  const retryBtn = failed
    ? `<button class="${retryFirst ? 'cta' : 'ghost-cta gate-signin-alt'}" id="gate-retry">Try again</button>`
    : '';
  const signInBtn = canSignIn
    ? `<button class="${retryFirst ? 'ghost-cta gate-signin-alt' : 'cta'}" id="ion-signin">${
        failed ? 'Sign in with a different account' : 'Sign in with Cesium ion'}</button>`
    : '';

  openSheet(
    `
    ${failed ? '' : `
    <figure class="gate-hero">
      <video autoplay loop muted playsinline width="640" height="370"
             poster="./docs/colosseum-poster.webp"
             aria-label="A day of sun and shadow moving across the Colosseum">
        <source src="./docs/colosseum-day.webm" type="video/webm">
        <source src="./docs/colosseum-day.mp4" type="video/mp4">
      </video>
    </figure>`}

    <h2>${failed ? 'The 3D mesh did not load' : 'Connect the 3D mesh to begin'}</h2>

    ${failed
      ? `<p class="gate-alert">${reason}</p>`
      : `<p>SolarGaze casts real shadows across the photorealistic 3D mesh that Google Earth
           renders. That mesh is Google's, and Cesium ion is what hands it to this page — so
           the one thing it needs from you is a free ion account. No Google Cloud project,
           no billing details, nothing to pay.</p>`}

    ${retryFirst ? retryBtn + signInBtn : signInBtn + retryBtn}
    ${failed || !canSignIn ? '' : `
    <p class="gate-fine">You approve once on Cesium's own page and come back signed in. The
       tiles then draw on your own free quota, and the sign-in never leaves this browser.</p>`}

    ${failed ? '' : `
    <button class="ghost-cta" id="open-guide2">
      <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M9.6 9.3a2.5 2.5 0 1 1 3.3 2.4c-.6.2-.9.8-.9 1.4v.4"/><path d="M12 16.8h.01"/></svg>
      First time here? Four steps, about a minute
    </button>`}

    <details class="gate-alt" ${!canSignIn || failed ? 'open' : ''}>
      <summary>${canSignIn ? 'Already have a token? Paste it instead' : 'Paste a Cesium ion token'}</summary>
      <p>Copy an access token from the <i>Access Tokens</i> tab of your
         <a href="https://ion.cesium.com/signup" target="_blank" rel="noopener">ion account</a>.
         It needs the <code>assets:read</code> scope.</p>
      <div class="field">
        <input id="ion-input" type="text" placeholder="eyJhbGciOi…" autocomplete="off"
               spellcheck="false" value="${escapeHtml(state.ionToken)}">
        <button id="ion-save">Save</button>
      </div>
    </details>
    `,
    root => {
      const busy = (btn, label) => working(btn, label);

      const ion = root.querySelector('#ion-input');
      const save = root.querySelector('#ion-save');
      const commit = () => {
        const value = ion.value.trim();
        if (!value) return toast('Paste a token first.', { error: true });
        setIonToken(value);
        // The gate stays up until tiles actually draw: a token that ion turns
        // down must not buy even a second of the app.
        busy(save, 'Checking…');
        onSaved?.();
      };
      save.addEventListener('click', commit);
      ion.addEventListener('keydown', e => { if (e.key === 'Enter') commit(); });

      root.querySelector('#gate-retry')?.addEventListener('click', e => {
        busy(e.currentTarget, 'Retrying…');
        onRetried?.();
      });

      const signIn = root.querySelector('#ion-signin');
      signIn?.addEventListener('click', async () => {
        const restore = busy(signIn, 'Redirecting to Cesium ion…');
        try {
          await ionAuth.beginSignIn(ION_CLIENT_ID);
        } catch (err) {
          restore();
          toast(String(err.message || err), { error: true, ms: 6000 });
        }
      });

      root.querySelector('#open-guide2')?.addEventListener('click', e => {
        e.preventDefault();
        openGuide({ back: () => openGate({ reason, kind }) });
      });

      if (!canSignIn || failed) setTimeout(() => ion.focus(), 60);
    },
  );
}

/** Is the gate currently holding the screen? */
export const isGated = () => gated;

/** Tiles are up: hand the app over. The only way the gate ever lifts. */
export function closeGate() {
  if (!gated) return;
  gated = false;
  document.body.classList.remove('is-gated');
  $('modal').classList.remove('is-gate');
  $('modal').hidden = true;
  $('sheet-back')?.remove();
}

/* ── the three-step guide ─────────────────────────────────────────── */

/**
 * Written for the person using the site, not the person forking it: they need
 * a free account and one button, nothing about client ids or redirect URIs.
 * That audience is served by the README instead.
 *
 * Drop the screenshots into docs/ with these names and they appear; until then
 * each step shows a dashed placeholder saying which shot is missing.
 *
 * Replacing one? Redact it before it lands in docs/. The account names in
 * these shots are burned into the pixels, not covered by the page — a
 * browser-side blur would still ship the original bytes to every visitor.
 */
const STEPS = [
  {
    title: 'Open Cesium ion and start a sign-up',
    body: 'Cesium ion is what hands Google\'s 3D buildings to this page. The account is free, ' +
          'with no Google Cloud project and no card involved. The quickest way in is one of ' +
          'the buttons under <i>Third-party authentication</i> — Google, GitHub, Bentley, ' +
          'Epic or Sketchfab — which is the route these screenshots follow. The sign-up link ' +
          'at the bottom does the same with an email and a password instead.',
    link: ['ion.cesium.com/signup', 'https://ion.cesium.com/signup'],
    shot: 'ion-login-signup.jpg',
    box: [{ x: 30.6, y: 26.2, w: 38.6, h: 15.8 }],
    arrow: [
      { x: 49.6, y: 25.9, label: 'Any of these' },
      { x: 46, y: 66.6, label: 'Or email' },
    ],
  },
  {
    title: 'Pick a username and accept the terms',
    body: 'Google hands you straight back here. Choose any free username and tick the terms ' +
          'box — it is required, and a missing tick is the usual reason the button appears ' +
          'to do nothing. The data centre can stay on its default: it only decides where ' +
          'Cesium stores assets you upload yourself, and this app uploads nothing.',
    shot: 'ion-signup-google-username.jpg',
    arrow: [{ x: 34.5, y: 57.8, label: 'Sign up' }],
  },
  {
    title: 'Verify your email',
    body: 'This happens even when you came in through Google: ion confirms the address on ' +
          'the account whichever way you signed up. A six-digit code arrives by mail — type ' +
          'it in, press Verify, and the account is live with nothing left to configure.',
    shot: 'ion-email-verification.jpg',
    arrow: [{ x: 34.7, y: 55.2, label: 'Code from the email' }],
  },
  {
    title: 'Come back here and press Allow',
    body: 'SolarGaze asks for exactly two permissions: reading map assets, which is the ' +
          'buildings, and geocoding, which is the search box. Nothing else, and no write ' +
          'access to your account.',
    shot: 'ion-authorize.jpg',
    // dx slides the label clear of the heading it would otherwise sit on; the
    // arrow itself stays on the button.
    arrow: [{ x: 34.1, y: 43.0, label: 'Allow', dx: -9 }],
  },
];

/**
 * Annotations laid over a guide screenshot.
 *
 * Redactions are NOT here. A blurred box drawn by the browser hides nothing:
 * the file in the repo would still carry the account name in plain pixels for
 * anyone who opened it directly. Those are burned into the images themselves,
 * before they are committed.
 */
const overlay = step => [
  // A box marks a whole group. An arrow would pick one button out of five and
  // read as a recommendation, which is not ours to make.
  ...(step.box || []).map(r =>
    `<span class="markbox" style="left:${r.x}%;top:${r.y}%;width:${r.w}%;height:${r.h}%"></span>`),
  // Label above, arrowhead below: the whole marker is pulled up by its own
  // height, so whatever sits last is what actually lands on the coordinate.
  // The arrowhead therefore has to reach the bottom of its own viewBox — an
  // arrow that stops at 75% of it points a quarter of its height too high,
  // which on a shot this size is a whole row of the table.
  ...(step.arrow || []).map(a =>
    `<span class="point" style="left:${a.x}%;top:${a.y}%${a.dx ? `;--dx:${a.dx}%` : ''}">
       ${a.label ? `<b>${a.label}</b>` : ''}
       <svg viewBox="0 0 24 24"><path d="M12 2v13.4M5.8 15.4 12 21.6l6.2-6.2"/></svg>
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
    <p>Four steps, about a minute. Everything stays in this browser — the sign-in is
       between you and Cesium, and this page never sees a password.</p>
    <ol class="steps">${steps}</ol>
    <p><small style="color:rgba(255,255,255,.5)">There is no way round this one: the shadows
       are cast by Google's building geometry, and ion is the only route to it. Without an
       account there is nothing for the sun to fall on.</small></p>
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
    <p><small style="color:rgba(255,255,255,.55)">A pasted token overrides a sign-in; saving
      reloads the page. Signing out leaves SolarGaze with no way to reach the mesh, so it
      returns you to the connect screen.</small></p>
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

/**
 * The short clips that carry the guide.
 *
 * Video, not animated GIF: the same four seconds costs about 480 KB as WebM
 * against 4.3 MB as a GIF, and the GIF has to quantise a photorealistic mesh
 * down to a 180-colour palette to get even that. The one place a GIF is still
 * the only option is the README, because GitHub strips <video> out of Markdown.
 *
 * Sources are attached by the observer below rather than written here, so
 * opening this sheet fetches the clip you are looking at and nothing else.
 */
const CLIPS = [
  {
    id: 'guide-time',
    heading: 'Time of day',
    body: 'The upper slider is the hour. Drag it and the sun walks its arc while every shadow ' +
          'in the mesh swings with it — the clock, the compass bearing and the elevation ' +
          'readout all follow. The ends of the slider are that day\'s sunrise and sunset, ' +
          'unless you ask for the full 24 hours in Settings.',
    caption: 'Mid-morning to sunset over the Colosseum.',
  },
  {
    id: 'guide-date',
    heading: 'Time of year',
    body: 'The lower slider is the day of the year, and it is the one that answers the ' +
          'questions worth asking. Hold the hour still and sweep it: the same balcony that ' +
          'takes full sun in June can sit in shadow all morning in December, because the sun ' +
          'rises further south and never climbs as high.',
    caption: 'The same hour of the morning, late January to July.',
  },
  {
    id: 'guide-pin',
    heading: 'Putting the point somewhere exact',
    body: 'The studied point follows the middle of the view while the padlock is open, which ' +
          'is what you want while you are still looking around. Close the padlock and it ' +
          'stays put — and then you can pick it up and drop it exactly where you mean, on a ' +
          'doorway, a terrace, a particular window.',
    caption: 'Lock the padlock, then drag the point.',
  },
  {
    id: 'guide-analyze',
    heading: 'How many hours of sun',
    body: 'The ANALYZE tab ray-casts against the buildings actually around the point and ' +
          'counts the hours of direct sun it gets on the selected day, sampling every ten ' +
          'minutes. It can only test geometry that is loaded, so zoom in until the ' +
          'surroundings are sharp before you trust the number.',
    caption: 'A day\'s worth of direct sun, in about a second.',
  },
];

const clipFigure = clip => `
  <figure class="guide-clip">
    <video data-clip="${clip.id}" loop muted playsinline preload="none"
           poster="./docs/${clip.id}-poster.webp" aria-label="${clip.caption}"></video>
    <figcaption>${clip.caption}</figcaption>
  </figure>`;

const section = clip => `
  <h3>${clip.heading.toUpperCase()}</h3>
  <p>${clip.body}</p>
  ${clipFigure(clip)}`;

/** The same bindings the corner card prints, in the same order. */
const MOUSE_KEYS = [
  ['Orbit around the point', ['drag']],
  ['Look around from where you are', ['shift', '+', 'drag']],
  ['Tilt towards the horizon', ['ctrl', '+', 'drag', 'or', 'middle']],
  ['Zoom in and out', ['wheel', 'or', 'right', '+', 'drag']],
  ['Move the point, once the padlock is closed', ['drag']],
];

const BOARD_KEYS = [
  ['Time, ∓10 minutes', ['←', '→']],
  ['Time, ∓1 hour', ['shift', '+', '←', '→']],
  ['Date, ∓1 day', ['↑', '↓']],
  ['Date, ∓30 days', ['shift', '+', '↑', '↓']],
  ['Start or stop the playback', ['space']],
  ['Jump to the current time at the point', ['n']],
];

/** '+' and 'or' are words between keys, not keys themselves. */
const keyBits = bits => bits.map(bit =>
  bit === '+' ? '<i>+</i>'
    : bit === 'or' ? '<i class="alt">or</i>'
      : `<kbd>${bit}</kbd>`).join('');

const keyTable = rows => `
  <dl class="keys">
    ${rows.map(([what, bits]) => `<div><dt>${what}</dt><dd>${keyBits(bits)}</dd></div>`).join('')}
  </dl>`;

function openHelp() {
  openSheet(`
    <h2>Guide &amp; shortcuts</h2>
    <p>An open sun-and-shadow simulator. Pick a place, drag the two sliders — time of day and
       day of year — and watch real shadows fall across Google's photorealistic 3D mesh. Sun
       positions come from the NOAA solar equations; the shadows are cast by CesiumJS's shadow
       map against the actual building geometry.</p>

    <button class="cta" id="open-guide" style="margin:4px 0 6px">How do I get the 3D buildings?</button>

    ${CLIPS.map(section).join('')}

    <h3>MOUSE</h3>
    <p>These are CesiumJS's own camera bindings. The card in the bottom-right corner of the map
       carries the short version; close it once and it stays closed.</p>
    ${keyTable(MOUSE_KEYS)}

    <h3>KEYBOARD</h3>
    <p>The map has focus by default, and keys are ignored while you are typing in a field.</p>
    ${keyTable(BOARD_KEYS)}

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
  wireClips(root);
}

/**
 * Load and play a clip when it is scrolled to, and pause it when it is not.
 *
 * Four autoplaying videos would fetch two megabytes the moment this sheet
 * opens, most of it for sections nobody has reached yet — and four decoders
 * running at once on a page that is already streaming a 3D mesh is a visible
 * stutter on a modest machine. So each one is attached on its first approach
 * and only ever one or two are actually running.
 */
function wireClips(root) {
  const clips = [...root.querySelectorAll('video[data-clip]')];
  if (!clips.length) return;

  const attach = video => {
    if (video.dataset.attached) return;
    video.dataset.attached = '1';
    for (const [file, type] of [['webm', 'video/webm'], ['mp4', 'video/mp4']]) {
      const source = document.createElement('source');
      source.src = `./docs/${video.dataset.clip}.${file}`;
      source.type = type;
      video.appendChild(source);
    }
    video.load();
  };

  // No IntersectionObserver is not worth a fallback path: attach the lot.
  if (!('IntersectionObserver' in window)) return clips.forEach(attach);

  const io = new IntersectionObserver(entries => {
    for (const { target, isIntersecting } of entries) {
      if (!isIntersecting) { target.pause(); continue; }
      attach(target);
      // A play() on a video the user has scrolled straight past rejects; that
      // is the browser doing its job, not an error worth surfacing.
      target.play().catch(() => {});
    }
  }, { root: root.closest('.sheet'), rootMargin: '200px 0px' });

  clips.forEach(clip => io.observe(clip));
}
