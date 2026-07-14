# Thai 3D GitHub Pages pipeline

This pipeline is independent from the Myanmar 2D calculation and publisher. It
uses the Thailand Government Lottery Office (GLO) first-prize result and defines
3D as the final three characters of that validated six-digit string.

## Official source and validation

- Public results page: <https://www.glo.or.th/mission/reward-payment/check-reward>
- Structured endpoint used by that page: `POST https://www.glo.or.th/api/lottery/getLatestLottery`
- Parsed schema: `response.date` and
  `response.data.first.number[0].value`

The HTTP client uses a 15-second timeout, bounded exponential retries, rejects
redirects, and validates the response before returning it. On HTTP/network
failure only, Playwright opens the official results page and captures the same
official JSON response. It does not use cookies, profiles, stealth plugins,
CAPTCHA solving, or unofficial result sites. Schema errors are not hidden by a
browser fallback.

The endpoint currently exposes a draw date but no consistently documented,
timezone-aware update timestamp, so `source_updated_at` is `null` when the
field is absent. `fetched_at` is always an aware UTC timestamp.

## Published files

- <https://young-standing.github.io/thai-2d-api/latest-3d.json>
- <https://young-standing.github.io/thai-2d-api/history-3d.json>
- <https://young-standing.github.io/thai-2d-api/history-3d-all.json>

`latest-3d.json` is one record. Recent history contains at most 50 records and
all-time history is never intentionally truncated. Records are unique by
`draw_date`. A same-date record is replaced only by a validated official record
with a newer `source_updated_at`, or `fetched_at` when the former is absent.

Example schema (illustrative values only, not a published result):

```json
{
  "draw_date": "YYYY-MM-DD",
  "first_prize": "six ASCII digits",
  "three_d": "three ASCII digits",
  "strategy": "first_prize_last_three_digits",
  "source_updated_at": null,
  "fetched_at": "timezone-aware UTC ISO timestamp",
  "source": "official HTTPS GLO URL",
  "source_client": "http",
  "publication_type": "scheduled_result",
  "stale": false
}
```

## Workflow and safety

`.github/workflows/publish-3d.yml` starts on the standard 1st and 16th schedule
at 10:00 UTC (17:00 Thailand). It polls every 60 seconds for at most 30 minutes,
but publishes only when the official `draw_date` equals the expected date. GLO
may move a draw; a manual production run can supply the officially confirmed
date through `expected_draw_date`. Freshness changes to the new standard draw
at the configured 17:00 Thailand publication cutoff, so the previous valid draw
is not marked stale earlier on draw day.

Manual dispatch defaults to `smoke`. Smoke mode fetches and prints a normalized
record without writing or deploying. `publish` is explicit. Before a 3D Pages
deployment, the workflow copies the complete `public` directory and hydrates
the currently published 2D JSON into its temporary artifact. All Pages writers
share the `github-pages-deployment` concurrency group.

The three 3D files are staged and validated before replacement. A mid-write
failure restores the previous files. The existing remote all-time history is
accepted only from the exact HTTPS GitHub Pages host/path, without redirects.

## Commands

```bash
python three_d_publisher.py --once
python three_d_publisher.py --publish --expected-draw-date 2026-07-16
python three_d_publisher.py --publish --poll --expected-draw-date 2026-07-16
```

Do not use the example date unless it is the officially expected draw date.
No local command should deploy Pages; deployment remains a GitHub Actions step.

## Flutter consumption

Use `latest-3d.json` for the current result and `history-3d.json` for recent
draws. Keep `first_prize` and `three_d` as strings. Add
`?t=<current Unix milliseconds>` to reads and retain cached validated data when
the network fails. Treat placeholder `{}` and `[]` as “no published result,” not
as an error or a zero result.

Known limitation: the 1st/16th schedule is a starting expectation, not proof
that a draw occurs on that date. Publication therefore depends on the official
response date and may require a manual run after an officially announced
schedule adjustment.
