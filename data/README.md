# Data formats and status

Two input files feed `scripts/score_models.py`. Both are joined on **(provider, model)**.

## `benchmarks.example.csv` — status: illustrative, unverified

The example dataset is **illustrative**. The model names and figures are a worked example of the
format, not a reproducible measurement: **no row has a `source_url` or an `archive_url`, and
every row is marked `provenance_status: unverified`.** The scorer reports
`0/97 observations verified` for exactly this reason, and the README must not be read as claiming
otherwise.

That is deliberate. Metadata being present (source name, date, version, conditions) proves only
that metadata was supplied. It does not prove the number was read from that source, or that the
conditions match. Verification is a separate, explicit claim:

| `provenance_status` | Means | Enforced by the scorer |
|---|---|---|
| `verified` | someone opened the source **and** an archived snapshot and confirmed the figure and conditions | requires `http(s)` `source_url` **and** `archive_url` |
| `unverified` | metadata supplied, not traced to a page or snapshot | allowed, counted, and reported as unverified |
| `estimated` | derived by this repo rather than observed | requires `derivation` describing the calculation |

## Columns

| Column | Meaning |
|---|---|
| `provider`, `model` | join key; both required |
| `metric` | must match a weight key (`deepswe`, `tb4`, `swe_bench`, `lmarena_coding`, `aa_intelligence`) to affect capability |
| `value` | numeric reading |
| `source` | board, card, or derivation that published it |
| `source_kind` | `independent`, `aggregate`, or `lab_card` (conflict resolution prefers independent) |
| `benchmark_version` | benchmark/leaderboard revision, e.g. `v4.0`, `verified-2026-04` |
| `collected_on` | ISO date the figure was read (`YYYY-MM-DD`) |
| `conditions` | tool use, trials, temperature, subset — anything that changes the number |
| `provenance_status` | `verified` / `unverified` / `estimated` |
| `source_url` | the page the figure was read from (required for `verified`) |
| `archive_url` | an archived snapshot of that page (required for `verified`) |
| `derivation` | how an `estimated` value was produced (required for `estimated`) |
| `notes` | caveats: reward-hacking, source conflicts, preview status |

## `quota.example.csv` — cache prices are estimates

`cache_read_cost` and `cache_write_cost` in the example are **derived, not fetched**:
`0.10x` and `1.25x` the input price. Every row says so in `cache_price_source`, and the scorer
emits `cost_model.cache_prices_estimated: true`. If you have vendor cards, replace them and set
`cache_price_source` to the real reference; otherwise keep the estimate label.

Monthly allowances and request counts are likewise part of the example, not a live quota probe.

## Adding real, verified data

1. Read the figure from the publisher's page and from an archived snapshot (Wayback, archive.today,
   or your own capture).
2. Add one row per `(provider, model, metric)` with `provenance_status: verified`, both URLs, the
   collection date, and the conditions you actually observed.
3. Run `python3 scripts/score_models.py --benchmarks data/benchmarks.example.csv --quota data/quota.example.csv --require-verified`.
   Models without enough verified evidence get `tier: null` / `insufficient verified evidence`
   instead of a tier — that is the point.
4. If you cannot verify a figure, keep it `unverified` rather than deleting it; the report tells
   the reader how much of the ladder rests on unverified evidence.
