/**
 * "Sign in with Cesium ion" — OAuth 2.0 authorization code flow with PKCE.
 *
 * This is the answer to "my users should not have to go and mint a token".
 * They click a button, approve on ion's own page, and come back with an access
 * token drawn against *their* quota rather than yours. PKCE means there is no
 * client secret, which is what makes it legal — and possible — to run the whole
 * flow from a static page with no backend.
 *
 * What it costs you once: register the app at ion → Settings → Developer
 * Settings → Add Application, list every redirect URI you will serve from
 * (they must match exactly, including the trailing path), and put the numeric
 * client id in `config.js` as ION_CLIENT_ID.
 */

const AUTHORIZE_URL = 'https://ion.cesium.com/oauth';
const TOKEN_URL = 'https://api.cesium.com/oauth/token';
/**
 * `assets:read` fetches the tiles; `geocode` powers the search box. The OAuth
 * documentation lists only the three `assets:*` scopes, yet ion's own default
 * tokens carry `geocode` too — so we ask for it and cope if ion says no,
 * rather than betting either way.
 */
const SCOPES = 'assets:read geocode';
const FALLBACK_SCOPES = 'assets:read';

const PKCE_STORE = 'solargaze.pkce';
const SESSION_STORE = 'solargaze.ionSession';

/* sessionStorage can throw in locked-down browsers; never let that break boot. */
const readSession = key => { try { return sessionStorage.getItem(key); } catch { return null; } };
const writeSession = (key, v) => { try { sessionStorage.setItem(key, v); } catch { /* ignore */ } };
const dropSession = key => { try { sessionStorage.removeItem(key); } catch { /* ignore */ } };

const readLocal = key => { try { return localStorage.getItem(key); } catch { return null; } };
const writeLocal = (key, v) => { try { localStorage.setItem(key, v); } catch { /* ignore */ } };
const dropLocal = key => { try { localStorage.removeItem(key); } catch { /* ignore */ } };

/** RFC 4648 §5 base64url, no padding — what the PKCE spec asks for. */
function base64url(bytes) {
  let binary = '';
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function randomString(byteLength = 32) {
  return base64url(crypto.getRandomValues(new Uint8Array(byteLength)));
}

async function challengeFor(verifier) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

/**
 * The redirect target must match what you registered, character for character.
 * Query and hash are stripped so a shared deep link never breaks sign-in.
 */
export function redirectUri() {
  return `${location.origin}${location.pathname}`;
}

export const isConfigured = clientId => !!clientId;

/** True once we hold a token that has not expired. */
export function currentSession() {
  try {
    const raw = readLocal(SESSION_STORE);
    if (!raw) return null;
    const session = JSON.parse(raw);
    if (!session.accessToken) return null;
    if (session.expiresAt && Date.now() > session.expiresAt - 60_000) return null;
    return session;
  } catch {
    return null;
  }
}

export function signOut() {
  dropLocal(SESSION_STORE);
  dropSession(PKCE_STORE);
}

/** Send the browser to ion's consent page. Does not return. */
export async function beginSignIn(clientId, { scopes = SCOPES } = {}) {
  if (!crypto?.subtle) throw new Error('This browser cannot do PKCE (needs a secure context).');

  const verifier = randomString();
  const stateToken = randomString(16);
  writeSession(PKCE_STORE, JSON.stringify({ verifier, stateToken, scopes }));

  const url = new URL(AUTHORIZE_URL);
  url.searchParams.set('response_type', 'code');
  url.searchParams.set('client_id', String(clientId));
  url.searchParams.set('redirect_uri', redirectUri());
  url.searchParams.set('scope', scopes);
  url.searchParams.set('state', stateToken);
  url.searchParams.set('code_challenge', await challengeFor(verifier));
  url.searchParams.set('code_challenge_method', 'S256');

  location.assign(url.toString());
}

/**
 * Call once on load. If we came back from ion with a code, swap it for a token
 * and scrub the code from the address bar.
 *
 * @returns the access token, or null when this was an ordinary page load.
 */
export async function completeSignIn(clientId) {
  const params = new URLSearchParams(location.search);
  const code = params.get('code');
  const returnedState = params.get('state');
  const error = params.get('error');

  if (error) {
    let attempted = null;
    try { attempted = JSON.parse(readSession(PKCE_STORE) || 'null'); } catch { /* ignore */ }
    scrubUrl();

    // If ion will not grant `geocode`, come straight back asking only for what
    // it will. The user sees one extra consent screen, not a dead end.
    if (error === 'invalid_scope' && attempted?.scopes !== FALLBACK_SCOPES) {
      dropSession(PKCE_STORE);
      await beginSignIn(clientId, { scopes: FALLBACK_SCOPES });
      return null;
    }
    throw new Error(`Cesium ion refused the sign-in (${error}).`);
  }
  if (!code) return null;

  let saved;
  try {
    saved = JSON.parse(readSession(PKCE_STORE) || 'null');
  } catch {
    saved = null;
  }
  dropSession(PKCE_STORE);
  scrubUrl();

  if (!saved?.verifier) throw new Error('Sign-in could not be completed — the browser lost the session.');
  // Without this check a third party could hand the user a crafted redirect.
  if (returnedState !== saved.stateToken) throw new Error('Sign-in state did not match; ignoring the response.');

  const res = await fetch(TOKEN_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({
      grant_type: 'authorization_code',
      client_id: String(clientId),
      code,
      redirect_uri: redirectUri(),
      code_verifier: saved.verifier,
    }),
  });

  if (!res.ok) {
    throw new Error(`Token exchange failed (${res.status}). Check the client id and that this exact URL is a registered redirect URI.`);
  }

  const data = await res.json();
  if (!data.access_token) throw new Error('Cesium ion returned no access token.');

  writeLocal(SESSION_STORE, JSON.stringify({
    accessToken: data.access_token,
    refreshToken: data.refresh_token || null,
    expiresAt: data.expires_in ? Date.now() + data.expires_in * 1000 : null,
    // `data.scope` is what ion actually granted, which may be less than we asked.
    scopes: data.scope || saved.scopes || SCOPES,
  }));

  return data.access_token;
}

/** Did the signed-in token come back with permission to geocode? */
export function sessionCanGeocode() {
  const session = currentSession();
  return !!session && String(session.scopes || '').includes('geocode');
}

/** Quietly renew a token that is close to expiry. Returns null if it cannot. */
export async function refreshSession(clientId) {
  let session;
  try {
    session = JSON.parse(readLocal(SESSION_STORE) || 'null');
  } catch {
    return null;
  }
  if (!session?.refreshToken) return null;

  try {
    const res = await fetch(TOKEN_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({
        grant_type: 'refresh_token',
        client_id: String(clientId),
        refresh_token: session.refreshToken,
      }),
    });
    if (!res.ok) return null;
    const data = await res.json();
    if (!data.access_token) return null;

    writeLocal(SESSION_STORE, JSON.stringify({
      accessToken: data.access_token,
      refreshToken: data.refresh_token || session.refreshToken,
      expiresAt: data.expires_in ? Date.now() + data.expires_in * 1000 : null,
    }));
    return data.access_token;
  } catch {
    return null;
  }
}

function scrubUrl() {
  try {
    const url = new URL(location.href);
    for (const p of ['code', 'state', 'error', 'error_description']) url.searchParams.delete(p);
    history.replaceState(null, '', url);
  } catch { /* some embedding contexts forbid this */ }
}
