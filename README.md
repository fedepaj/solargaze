# solargaze assets

Binary artwork for the [SolarGaze](https://github.com/fedepaj/solargaze) README,
kept on an orphan branch so it never lands on `main`.

Two reasons it lives here. The Pages workflow uploads the repository root
verbatim, so anything committed on `main` is also published and served to every
visitor — and these GIFs are only ever read by GitHub's README renderer, not by
the app, which plays the far smaller WebM versions in `docs/`. And a GIF is
rewritten wholesale every time it is regenerated, so on `main` each revision
would be another few megabytes in the history forever.

| File | Shows |
| --- | --- |
| `colosseum-day.gif` | Sunrise to sunset over the Colosseum — the README hero |
| `guide-time.gif` | Dragging the time-of-day slider |
| `guide-date.gif` | Dragging the day-of-year slider at a fixed hour |
| `guide-pin.gif` | Locking the padlock, then dragging the point |
| `guide-analyze.gif` | The ANALYZE tab computing hours of direct sun |

Referenced from the README by absolute `raw.githubusercontent.com` URL on this
branch. Regenerating one means committing it here and nothing on `main` changes.

Sources are the same frame captures the `docs/*.webm` clips are built from.
