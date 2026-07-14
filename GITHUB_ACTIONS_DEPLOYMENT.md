# Serverless GitHub Actions and Pages deployment

This is the primary production deployment. It uses no VM, Docker runtime, SQLite service, or always-running API.

## How it works

The workflow `.github/workflows/publish-2d.yml` runs Monday-Friday at:

- `05:28 UTC`, equivalent to `11:58 Asia/Yangon` for the morning result.
- `09:57 UTC`, equivalent to `16:27 Asia/Yangon` for the evening result.

It installs Python dependencies and matching Playwright Chromium, runs the complete test suite, polls for no more than five minutes, calculates the verified result, downloads and validates the currently published history, writes static JSON, uploads only `public/`, and deploys through the official Pages actions.

Concurrency prevents morning, evening, and manual runs from overlapping. Generated JSON is never committed back to `main`.

## Repository setup

1. Push this repository to GitHub.
2. Open **Settings -> Pages**.
3. Under **Build and deployment**, select **GitHub Actions** as the source.
4. Open **Actions** and enable workflows if the repository policy requires it.
5. Use the default manual `once` mode to smoke-test collection without deploying.
6. To perform an intentional production poll, select `poll` and enable
   `publish_production`. The run must still occur inside the selected result window.

The workflow uses only:

```yaml
permissions:
  contents: read
  pages: write
  id-token: write
```

No repository secret is required. The default published URL is derived as:

```text
https://<owner>.github.io/<repository>/
```

If the Pages URL differs, set the workflow environment variable `PUBLISHED_BASE_URL` to the public HTTPS base URL. The publisher accepts previous history only over HTTPS.

## Static data

- `public/latest.json` contains only the latest captured scheduled result.
- `public/history.json` contains at most 200 unique results, newest first.
- `public/index.html` is a dependency-free responsive viewer.

Pages artifacts are immutable per deployment and do not preserve files automatically. Before writing a new artifact, the publisher downloads the live `history.json`, accepts only the explicit scheduled-result and verified-backfill schemas, and deduplicates by result date and session time. Official scheduled records always take priority over third-party backfills.

Failed collection or calculation occurs before static files are replaced, so a failed run cannot overwrite the currently deployed valid Pages result.

## Cache busting

GitHub Pages or intermediary caches may retain JSON briefly. Browser and mobile clients should request:

```text
latest.json?t=<current timestamp>
```

For example:

```javascript
fetch(`latest.json?t=${Date.now()}`, { cache: "no-store" })
```

## Manual operation

From **Actions -> Publish Myanmar 2D -> Run workflow**, select the desired session.
The default `once` mode is a smoke test: it fetches and calculates the current data,
writes only a runner-temporary artifact, and skips all GitHub Pages deployment steps.

Manual production recovery requires all of the following:

- select `poll` mode;
- set `publish_production` to `true`;
- run inside the selected weekday session window;
- capture a changed source timestamp on that date at or after the target.

Local debugging without deploying:

```bash
python github_publisher.py --once
```

This prints a normalized smoke result and never writes `public/latest.json` or
`public/history.json`. An optional non-public artifact can be written with:

```bash
python github_publisher.py --window morning --once --artifact-path .tmp/smoke.json
```

## Optional historical backfill

The **Backfill Myanmar 2D History** workflow is manual-only. It requests each of
the last 30 calendar dates separately from `api.thaistock2d.com`, rate limits the
requests, and accepts only records whose supplied 2D result matches the local
SET-hundredths and displayed-value-units calculation.

This API is an untrusted secondary source. It is never used for current live
results, never changes the scheduled `latest.json` record, and cannot replace an
official record with the same result date and session time. To run it, open the
manual workflow and enable `publish_production` explicitly.

The workflow logs only safe request diagnostics: date, endpoint host, HTTP
status, response content type, record counts, and categorized rejection reasons.
It never logs headers, cookies, authorization values, or response bodies. An
`always()` summary step reports the failure category and confirms that published
JSON remained unchanged even when the import command exits unsuccessfully.

Local artifact generation uses a separate directory:

```bash
python historical_backfill.py --days 30 --output-dir backfill-public
```

## Security and artifacts

Only the `public/` directory is uploaded. Never put any of these files under it:

- `.env` or credentials
- cookies or browser profiles
- SQLite databases or WAL files
- logs or raw response bodies
- Playwright traces, screenshots, or videos

The workflow does not push commits and has no `contents: write` permission.

## Troubleshooting

- Review the workflow test step before investigating collection failures.
- A five-minute expiry leaves the prior Pages deployment intact.
- If Chromium fails, verify `requirements.txt` remains pinned to the version installed by `python -m playwright install --with-deps chromium`.
- If history resets, verify the Pages site is public and its `history.json` is reachable over HTTPS.
- If schedules appear shifted, remember GitHub cron is UTC; Myanmar remains UTC+06:30 year-round.

The optional GCE/Docker/systemd approach remains documented in `DEPLOYMENT.md` but is not part of this production workflow.
