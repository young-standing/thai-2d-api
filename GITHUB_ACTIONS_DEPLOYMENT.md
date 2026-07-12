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
5. Run **Publish Myanmar 2D** manually once, selecting `morning` or `evening`.
6. Confirm the `github-pages` environment deployment succeeds.

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

- `public/latest.json` contains the current verified result.
- `public/history.json` contains at most 100 unique results, newest first.
- `public/index.html` is a dependency-free responsive viewer.

Pages artifacts are immutable per deployment and do not preserve files automatically. Before writing a new artifact, the publisher downloads the live `history.json`, accepts only the exact public schema with string numeric fields, removes duplicate `market_datetime` values, and discards malformed data. If download or validation fails, it safely starts with an empty history.

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

From **Actions -> Publish Myanmar 2D -> Run workflow**, select the desired window. Manual window runs use the same five-minute polling and source-time validation as scheduled runs.

Local debugging without deploying:

```bash
python github_publisher.py --once
```

This writes local `public/latest.json` and `public/history.json`. It does not call the GitHub API or deploy Pages.

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
