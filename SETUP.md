# GitHub-only YouTube Shorts setup

This guide configures the daily workflow in `.github/workflows/create-short.yml`. It renders a 20-second English motivational Short entirely on GitHub's `ubuntu-latest` runner and uploads it with the official YouTube Data API. Scheduled and manual uploads are private by default.

## What you need

- A GitHub repository with Actions enabled.
- A YouTube channel on the Google account you will authorize.
- A free Pexels API key.
- Python 3.12 for the one-time local OAuth helper.
- Between one and five or more royalty-free MP3 files that you are permitted to use on YouTube.

Do not place API keys, OAuth credentials, tokens, or `client_secret.json` in the repository.

## 1. Get a free Pexels API key

1. Create or sign in to a Pexels account.
2. Open the [Pexels API page](https://www.pexels.com/api/).
3. Request an API key and accept the API terms.
4. Keep the key available for the GitHub Secret step below.

The renderer uses the official `GET /v1/videos/search` endpoint with portrait and medium-size filters. It records the Pexels creator and source page in every YouTube description.

## 2. Create the Google Cloud OAuth client

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Create a project or select a dedicated project for this automation.
3. Open **APIs & Services > Library**.
4. Search for **YouTube Data API v3** and enable it.
5. Configure **Google Auth Platform / OAuth consent screen**.
6. If the audience is External and the app is in Testing, add your own Google account as a test user.
7. Open **Clients** (or **Credentials > Create credentials > OAuth client ID**).
8. Choose **Desktop app**. Do not choose a service account; YouTube uploads require user OAuth authorization.
9. Download the JSON credentials file and save it in the repository root locally as `client_secret.json`.

`client_secret.json` is ignored by Git and must never be committed.

### Important seven-day Testing limitation

For an External OAuth consent screen whose publishing status remains **Testing**, Google normally issues a refresh token that expires in seven days when non-basic scopes such as YouTube upload are requested. After it expires, scheduled uploads fail until you run the helper again. Move the OAuth app to Production and complete any Google requirements that apply to your app before relying on long-term unattended uploads. See [Google OAuth refresh-token expiration](https://developers.google.com/identity/protocols/oauth2#expiration).

## 3. Generate the refresh token locally

Create a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python scripts/get_refresh_token.py
```

Windows PowerShell activation is:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts\get_refresh_token.py
```

Your browser opens Google's consent page. Sign in with the account that owns the target YouTube channel. The helper requests only `https://www.googleapis.com/auth/youtube.upload` and requests offline access. It prints:

- `YOUTUBE_CLIENT_ID`
- `YOUTUBE_CLIENT_SECRET`
- `YOUTUBE_REFRESH_TOKEN`

Copy them directly to GitHub Secrets. The helper does not save the refresh token. Do not paste its terminal output into an issue, commit, screenshot, or workflow log. Delete the local `client_secret.json` after setup if you no longer need it.

## 4. Add GitHub Secrets and the channel variable

On GitHub, open **Settings > Secrets and variables > Actions**.

Under **Secrets**, create these four repository secrets:

| Secret | Value |
|---|---|
| `PEXELS_API_KEY` | Pexels API key |
| `YOUTUBE_CLIENT_ID` | Value printed by the helper |
| `YOUTUBE_CLIENT_SECRET` | Value printed by the helper |
| `YOUTUBE_REFRESH_TOKEN` | Value printed by the helper |

Under **Variables**, add:

| Variable | Value |
|---|---|
| `CHANNEL_NAME` | The short channel name displayed near the bottom of each video |

Secrets are passed only as environment variables. Upload scripts never print their values.

## 5. Add royalty-free music

Add one to five or more `.mp3` files under `music/`, then commit them. The renderer automatically discovers every MP3 and randomly picks one. No filename configuration is necessary.

Read [music/README.md](music/README.md) before adding files. Confirm that each license allows YouTube use and monetization where relevant. Keep your license evidence and source URL. The repository intentionally contains no music file, and configuration validation intentionally fails until you add one.

## 6. Push the project and check Actions permissions

Commit and push the implementation, quotes, state, and properly licensed MP3 files to the default branch. The scheduled event only runs workflows that exist on the default branch.

The workflow needs permission to update `data/state.json`. Under **Settings > Actions > General > Workflow permissions**, allow **Read and write permissions** if repository or organization policy does not already permit the workflow's declared `contents: write` permission.

The state update uses a temporary branch, merges the rendered state with the latest remote state, and retries conflicts three times. A state-persistence failure is non-fatal and does not undo a completed render or upload.

## 7. Run the first private manual test

1. Open the repository's **Actions** tab.
2. Select **Create daily YouTube Short**.
3. Choose **Run workflow**.
4. Leave the quote and Pexels search fields empty for a catalog-driven test.
5. Keep **privacy status** set to `private`.
6. Run the workflow.

You may optionally enter an original English quote and a Pexels search phrase. Manual input is passed through environment variables rather than inserted into shell commands.

When the run completes, open YouTube Studio and verify that the video is private, vertical, exactly 20 seconds, correctly cropped, readable, and attributed. Review the selected music license again.

## 8. Inspect logs and artifacts

Open the workflow run and expand individual steps for clear validation, Pexels, FFmpeg, state, and YouTube errors. OAuth secret values are not printed.

The **Artifacts** section contains `output/short.mp4` and `output/metadata.json` even when the YouTube upload fails, as long as rendering succeeded. Artifacts are retained for two days to limit storage usage. Generated MP4 files are ignored by Git and are never committed.

## 9. Enable public uploads only after testing

The schedule always uses `private`. Manual runs also default to `private`, but offer `unlisted` and `public` choices. After repeated private tests, licensing review, and channel-policy review, you can intentionally select `public` for a manual run. If you later decide that scheduled uploads should be public, change the scheduled fallback in the workflow deliberately and review the diff before merging.

New or unaudited Google API projects can have API uploads restricted to private visibility regardless of the requested setting. Follow Google's audit and verification requirements rather than attempting to bypass the restriction.

## Schedule and quote rotation

GitHub runs the workflow every day at `19:07` using the IANA timezone `Asia/Yangon`. GitHub schedules can be delayed during high load, although choosing minute 7 avoids the busiest start-of-hour period.

`data/quotes.json` contains more than 100 original, unattributed quotes across discipline, success, healing, confidence, growth, and consistency. `data/state.json` prevents reuse until the catalog cycle is exhausted. A manual quote does not alter catalog rotation.

## GitHub Actions minutes

Standard GitHub-hosted runners are free for public repositories. Private repositories consume the Actions minutes and storage included with the repository owner's plan; usage beyond the allowance may be billed or blocked. Daily FFmpeg rendering can consume a meaningful portion of a private repository's allowance. Review **Settings > Billing and licensing** regularly. See [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

## Copyright and repetitive-content considerations

- Pexels access does not transfer rights to trademarks, recognizable people, music, or third-party material depicted in footage. Review the asset and Pexels license before publishing.
- Use only music you are authorized to synchronize with video and publish on YouTube.
- Do not copy famous quotations or imply endorsement by a real person.
- Automated template videos may be considered repetitive or low-value content. Meaningful human review, distinct creative editing, original commentary, and genuine variation are important, especially for monetization.
- Review every artifact and metadata file before making uploads public. Automation does not replace compliance with YouTube policies, copyright law, or license terms.

## Local checks

Run unit tests without uploading anything:

```bash
pytest -q tests/test_youtube_shorts.py
```

The test suite covers quote rotation, safe text wrapping, metadata/title generation, and state merging. The upload script is never invoked by these tests.

