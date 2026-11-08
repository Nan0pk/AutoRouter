# Set up AutoRouter in Hermes

Ask your coding assistant, from this repository:

> Set up AutoRouter in Hermes.

Or run:

```bash
python3 scripts/setup_autorouter.py setup hermes
```

Setup finds existing local logins, offers a short menu, installs the native
Hermes integration, and checks a real two-model job. After successful setup,
start Hermes normally. Start a new session (`/new`) to load the new delegation
settings. Everyday use does not need a separate test runner.

Hermes is the first supported target. Other harness names are rejected before
any changes. The older OpenCode examples remain separate.

## Four choices

1. **Existing subscription allowances** — use connected providers whose included
   allowance can be established. This is the default.
2. **One provider** — choose a connected provider and use eligible models from it.
3. **Individual models** — select exact `provider/model` IDs across providers.
4. **Free models only** — both published input/output prices must be zero, with
   catalog data at most 24 hours old. Missing prices are excluded.

All modes exclude additional paid/unknown inference routes by default. An
included subscription allowance is different from a free model. Free mode
concerns model inference, not unrelated paid tools such as image generation.
No provider-side spending cap is claimed, and there is no extra paid-spending
mode in this release.

Setup prints the routine model, specialist, reasons, and reported quota. Missing
quota remains **unknown**; reported exhausted allowances are excluded. Bounded
checks remove inaccessible shortlisted models before installation; discovery
does not call every model. The OpenCode zero-price chat models require OpenCode
and are excluded from the external Hermes route.

Without verified capability scores, choices are labeled **provisional** and use
published tool/reasoning support and known prices. Names, price and context
length are not benchmark scores. The illustrative CSVs are not used as rankings.

## Commands

```bash
python3 scripts/setup_autorouter.py inspect hermes
python3 scripts/setup_autorouter.py setup hermes --dry-run
python3 scripts/setup_autorouter.py setup hermes --mode subscriptions --yes
python3 scripts/setup_autorouter.py setup hermes --mode provider --provider opencode-go --yes
python3 scripts/setup_autorouter.py setup hermes --mode models \
  --model opencode-go/claude-haiku-5-5 \
  --model opencode-go/deepseek-v4.1-flash --yes
python3 scripts/setup_autorouter.py setup hermes --mode free --yes
python3 scripts/setup_autorouter.py status hermes
python3 scripts/setup_autorouter.py verify hermes
python3 scripts/setup_autorouter.py undo hermes
```

`--home /path/to/profile` selects a Hermes profile directory; otherwise setup
uses `HERMES_HOME` or `~/.hermes`. Discovery uses that profile's Hermes logins;
the existing external OpenCode login can also be reused without importing it.

`--confirm-allowance PROVIDER` records **your assertion** of included allowance
when the provider cannot report billing. It does not discover a plan or prevent
overages, and cannot override explicit metered billing. An assistant must not
invent that assertion. Without a terminal, setup requires `--mode` and `--yes`.

`--offline` installs/checks locally and clearly reports live routing as
unverified. `--inventory` is a dry-run-only test input, never permission to
install fabricated free pricing. `--dry-run` changes no configuration and makes
no inference calls, although it can fetch read-only provider metadata.

## Normal Hermes behavior

The routine model remains the main model. AutoRouter classifies each root
request using the existing local classifier. For complex/risky work, a native
plugin requires `delegate_task` on the first model request and routes that child
to the specialist. Hermes carries the task context and result. Children are
not forced to delegate again. Routine tasks stay on the main model, which may
delegate a blocker to the specialist.

The delegation backend accepts only the two configured role routes and supplies
no fallback providers. A complex root cannot be downgraded by rewriting the
delegated goal. A manual main-model selection disables automatic root routing
and preserves that model.

Hermes hooks/middleware can fail open on unexpected plugin errors; this is not
a global billing firewall. Setup normalizes configured main, auxiliary and
fallback routes into the selected set and disables title-model upgrades. Other
plugins, manual changes and provider billing remain outside that guarantee.
See [enforcement boundaries](docs/enforcement.md).

## Credentials, installation and undo

Native Hermes handles ordinary saved credentials. The OpenCode bridge reads
the selected Console login in memory, pins its account/workspace, and uses the
official Go endpoint. It creates no keys, copies no tokens into config, forwards
no refresh token and does not independently rotate OAuth credentials. If a
login expires/changes, refresh it in OpenCode and rerun setup.

Hermes v0.23.0 or later and its existing source-install Python are required. No
new Python dependencies are installed. Set `AUTOROUTER_HERMES_SOURCE` for a
custom source checkout; current managed and older `venv`/`.venv` layouts work.

Runtime files are copied into the profile's `plugins/`; moving this checkout
later does not break the installed plugin. State and exact backups live under
`<profile>/autorouter/`. Backups are private and may contain secrets already in
your original configuration: never upload that directory as a whole. Share
only the credential-free verification report.

Installation uses a lock, staging, recovery journal and config activation last.
Repeat installation avoids duplicate entries. Undo preflights all managed
hashes and refuses to overwrite later user edits. A pending transaction is
recovered by the next setup/undo unless it conflicts with later edits; invalid
or pending policy blocks new AutoRouter sessions.

## What verification proves

A fresh normal Hermes session must make actual API calls on two distinct
selected models. The check observes provider response models, usage, parent/
child identities and the returned specialist-result digest. A skipped child,
wrong model, missing usage or unsuccessful result cannot pass. Checks use only
a harmless arithmetic task and consume allowance on selected providers.

Passing proves that this installed integration worked for that job/account.
It does not measure general model quality or quota savings. Native discovery
uses version-bound Hermes interfaces; upgrades may require reverification or
an AutoRouter update.
