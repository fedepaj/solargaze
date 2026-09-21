<div align="center">

<h1>SolarGaze</h1>

<img src="docs/logo.svg" width="84" height="84" alt="">

<p><strong>Sun and shadow, cast across Google's photorealistic 3D tiles.</strong></p>

<p>
  <a href="https://fedepaj.github.io/solargaze/">Live</a> ·
  <a href="#getting-the-3d-mesh">Setup</a> ·
  <a href="#running-it-locally">Develop</a> ·
  <a href="LICENSE">MIT</a>
</p>

</div>

---

Pick a place, drag two sliders — time of day and day of year — and watch real
shadows move across the same mesh Google Earth renders. No build step, no
server: a folder of static files that runs on GitHub Pages.

[![A day of light and shadow over the Colosseum](docs/colosseum-poster.webp)](docs/colosseum-day.webm)

<sub>Sunrise to sunset over the Colosseum, 8 September — <a href="docs/colosseum-day.webm">play
the loop</a>. The ring is a compass card lying on the ground, the glowing arc is the sun's track
for that day, and the beam arrives from the sun's direction into the studied point.</sub>

## What it does

- **Google Photorealistic 3D Tiles** as the world, via Cesium ion — real
  buildings, real geometry, shadows cast by the actual mesh.
- **Two sliders**: time of day (sunrise→sunset, or a full 24 h) and day of
  year, plus a date picker and a playback that sweeps the span in about half a
  minute.
- **Sun path overlay**: a graduated compass card on the ground, the day's arc,
  sunrise/sunset markers, live azimuth and elevation, and the shadow direction.
- **Local wall clock** at the place you are looking at, DST included.
- **Sun-hours probe** (ANALYZE tab): ray-casts against the loaded geometry to
  estimate hours of direct sun at the pin.
- **Shareable links** that restore the exact view, date and time.

Out of scope: drawing, placing objects, editing the map.

## Controls

The map has focus by default; keys are ignored while you type in a field.

| Key | Does |
| --- | --- |
| `←` `→` | Time, ∓10 minutes (`Shift` for an hour) |
| `↑` `↓` | Date, ∓1 day (`Shift` for 30) |
| `Space` | Start or stop the playback |
| `N` | Jump to the current local time at the pin |

Everything after `#` is written back to the address bar as you move, so the URL
is always a link to what you are looking at.

| Parameter | Means |
| --- | --- |
| `#ll=lat,lon` | The studied point |
| `#t=YYYY-MM-DDThh:mm` | Date and time, as the **local** wall clock there |
| `#cam=lat,lon,height` | Camera position, height in metres |
| `#hp=heading,pitch` | Camera heading and pitch, in degrees |
| `?ion=TOKEN` | Save a Cesium ion token, then scrub it from the URL |
| `?reset` | Forget everything this app stored, and reload |

Combine the hash ones with `&`. Malformed fields are ignored, not fatal.

## Getting the 3D mesh

SolarGaze will not open without a Cesium ion credential — the whole app is
shadows cast by real geometry, and ion is the only route to it. A free account
is all it takes: **no Google Cloud project, no card on file.** Three ways in,
differing in whose quota gets spent.

**Sign in with Cesium ion.** Visitors approve once on Cesium's own page and the
tiles draw on *their* quota. OAuth 2.0 with PKCE — no client secret, no
backend. Register the app at ion → your username → *Developer Settings* → *Add
Application*, list every URL you serve from as a redirect URI, and put the
numeric client id in `ION_CLIENT_ID` in [`js/config.js`](js/config.js).

> **Forking?** `ION_CLIENT_ID` ships with the id of the fedepaj.github.io
> deployment, whose redirect URIs do not include yours. Replace it, or set it
> to `''` to hide the button and leave the paste-a-token route.

**Paste a token.** Sign up at [ion](https://ion.cesium.com/signup), copy an
access token from the *Access Tokens* tab, paste it in. `?ion=TOKEN` works too.

**Ship a token.** Set `DEMO_ION_TOKEN` in [`js/config.js`](js/config.js) and
visitors need nothing at all. Read the notes above that constant first: a token
in a static site is public, and every visitor's tiles are billed to you.

*Settings → Reset*, or `?reset`, forgets the sign-in, any pasted token and
every preference.

## Running it locally

```bash
npm run dev     # npx serve on http://localhost:5173
npm test        # node --test over the solar equations
```

Any static server works — `python3 -m http.server` is fine. It must be http(s),
not `file://`, because the app is ES modules, and the browser needs WebGL.

CesiumJS comes from a CDN, pinned in [`index.html`](index.html) in three places
that move together. `CESIUM_VERSION` in `js/config.js` is a readable copy for
anything that reports it; editing it alone changes nothing.

## Deploying

```bash
# in js/config.js: point REPO_URL at your fork, replace ION_CLIENT_ID with your
# own registered application (or set it to ''), and set DEFAULTS to the place
# you want the app to open on
git commit -am "Point at my fork" && git push
```

In **Settings → Pages**, set *Source* to **Deploy from a branch** (`main`, root)
or to **GitHub Actions** and let
[`.github/workflows/pages.yml`](.github/workflows/pages.yml) run. There is
nothing to build.

## How it is put together

```
index.html            overlay markup
css/app.css           the whole visual design
docs/                 logo, the loops the connect screen plays, and the
                      screenshots the in-app ion walkthrough loads
test/                 node --test over solar.js
js/
  solar.js            NOAA solar equations — pure, dependency-free, tested
  timezone.js         IANA zone for a lat/lon, DST-correct wall-clock maths
  state.js            single source of truth + a small pub/sub bus
  scene.js            Cesium viewer, 3D tiles, shadow map, camera
  sunpath.js          the 3D overlay: compass card, day arc, readouts
  analyze.js          direct-sunlight-hours ray casting
  ion-auth.js         Cesium ion sign-in (OAuth2 + PKCE, no backend)
  util.js             helpers with no home of their own
  app.js              bootstrap and wiring
  ui/                 timepanel, analyzepane, search, compass, weather,
                      usage meter, modals, toast, tooltip, sunloader,
                      offlinedemo
```

Two sun calculations run side by side. CesiumJS derives the light direction
from `clock.currentTime` with its own ephemeris — that is what the shadow map
uses — while `solar.js` computes the numbers the interface prints. Feed the
clock the right UTC instant and both agree. Everything drawn is derived from
three inputs held in `state.js`: where the pin is, which day, what time; a
change to any of them recomputes once and the modules that care are notified.

`window.solargaze` exposes `state`, `viewer`, `setLocation` and `setPref` for
embedding or console work.

The reasoning behind the less obvious choices lives next to the code that makes
them, not here.

## Accuracy

Sun geometry is good to well under a tenth of a degree, rise/set times to about
half a minute. The spot-check — Turin, 19 Sep 2026, elevation 42.65° and
azimuth 151.18° at 12:00 CEST, sunrise 07:12, sunset 19:33 — is asserted in
[`test/solar.test.mjs`](test/solar.test.mjs).

Rise and set use the standard 90.833° zenith, which allows for the sun's
semidiameter and mean refraction. Elevation and azimuth readouts are geometric,
matching the shadows. The ANALYZE probe samples every 10 minutes from a sensor
1.5 m above the pin and can only test tiles **currently loaded**, so zoom in
before trusting it.

What limits the result is the mesh: Google's tiles are photogrammetry, so
trees, awnings and thin structures are approximate and the lighting baked into
the imagery is not removed. A very good study, not a survey.

## Licence and attribution

SolarGaze is MIT — see [LICENSE](LICENSE). The pieces it stands on are not
yours to relicense:

- **CesiumJS** — Apache-2.0.
- **Google Photorealistic 3D Tiles** — governed by the
  [Google Maps Platform Terms](https://cloud.google.com/maps-platform/terms).
  Two obligations matter: the attributions Cesium renders at the bottom of the
  map **must stay visible**, and while the tiles are displayed the search box
  must use **Google's geocoder**. The app routes search through ion's
  Google-backed geocoder with the mesh on screen, and Nominatim on the flat
  basemap — see [`js/ui/search.js`](js/ui/search.js).
- **Cesium ion** — how the tiles and the geocoder are reached; usage follows
  your ion plan and Google's terms above.
- **OpenStreetMap** (fallback basemap) — ODbL, © OpenStreetMap contributors.
- **Open-Meteo** (weather badge) — CC-BY 4.0.
- **tz-lookup** — timezone boundary data, CC0.

Not affiliated with, or endorsed by, Google or Shadowmap.
