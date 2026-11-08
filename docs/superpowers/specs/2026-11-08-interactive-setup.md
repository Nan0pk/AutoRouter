# Interactive AutoRouter setup

## Outcome
A person can ask an assistant to set up AutoRouter in a named harness, or run
`python3 scripts/setup_autorouter.py setup hermes`, and get an installed, usable
router. Normal Hermes sessions use it; a separate eval script does not count.
The accepted user direction is to plan, critique, amend, and proceed without
another approval checkpoint. Implement inline in a separate worktree.

## User choices
Offer four modes, defaulting to subscriptions/included allowance:
1. All connected providers with an included allowance.
2. One selected connected provider.
3. Individually selected models across connected providers.
4. Verified zero-price models only; no paid/unknown fallback.
Provider/model selection and the billing filter are separate internally so
one-provider and custom selections cannot accidentally authorize spending.
Additional paid API spending is excluded from this first release; an actual
hard budget needs billing enforcement, not a number printed in a menu.

## Discovery and model selection
Reuse Hermes' configured credentials, provider registry, model catalog and
account-usage interfaces. Also recognize the saved OpenCode Console login;
use a native Hermes provider bridge to read it in memory, without copying
access or refresh tokens into config. Unknown/unsupported providers remain
visible with an explanation. Signing in alone does not prove model access.
Discover exact model IDs, published tool/reasoning capability, input/output
prices and quota/reset information where available. Unknown values stay
unknown. Free requires both prices exactly zero from a fresh catalog;
subscription inference is separate from zero-price API inference. Reject
metered/unknown candidates by default. Allow an explicit local allowance
confirmation for a provider whose billing data cannot be read.

Do not use the repository's illustrative benchmark CSVs as real rankings.
Use verified capability scores when supplied; otherwise show that choices
are provisional, based on declared tool/reasoning features and known cost.
Select a routine model and a distinct specialist, print the reasons and
sources, and validate selected models with a bounded live check. No secret
values appear in discovery JSON, previews, reports or errors.

## Hermes integration
Use supported Hermes v0.23 plugin APIs, not edits to the installed Hermes
source. Install an AutoRouter delegation backend that selects only approved
models, a prompt hook that supplies routing instructions, and request
middleware that requires the native delegation tool on the first request
of a complex root turn. Child turns are excluded from this forcing to
prevent recursion. Route routine work on the selected main model; use the
existing classifier for complex/risky work and select the specialist for
those delegated tasks. Native Hermes owns child context, tools, execution
and result return; configured fallback stays within the allowed set. Native hook/middleware failures are not a global spending firewall: strict allowlisting is enforced by the delegation backend, and normal routing is checked at startup/status/verification. Explicit manual
model choices must not silently become a different automatic main model.
Native provider resolution handles ordinary Hermes logins. The OpenCode
bridge pins the selected Console account/workspace, validates official
origin and expiry, and reads only the current access token. It must not
mint keys, forward refresh tokens, persist credentials, or refresh OAuth
independently of OpenCode.

## Install, status, undo and assistant entry point
Provide setup, inspect, status, verify and undo commands. Noninteractive
flags express the same choices as the menu; no TTY means no hidden default
approval or hanging prompt. Dry-run produces a reviewable plan without
changing configuration or consuming inference. Preserve unrelated YAML
settings/comments, back up exact previous bytes, stage plugin files and
configuration with a lock, staged generation, private recovery journal and config activation last, and restore on a failed install. Undo refuses
to overwrite later user edits. Policy and install metadata are credential
free, private local files. Unsupported harnesses fail before any writes.
Add SETUP.md and a repo AGENTS.md pointer so an assistant given the natural
language setup request knows the exact command and completion criteria.

## Proof of completion
Offline tests cover billing/selection boundaries, missing/expired auth,
provider/model mismatches, unavailable quotas, noninteractive behavior,
install failures, repeated install, user edits before undo, plugin routing
and child recursion. Integration tests load the real installed Hermes
plugin in a temporary profile. A bounded live check must actually invoke
both selected models through the installed delegation backend and read
back the routes and results, even when the main model could solve the job.
It must never report a skipped specialist as a handoff pass. Run the repo
checks and both CI schemas; save a credential-free live report. Then install
the verified integration into the current Hermes profile with its backup.

## Boundaries
Hermes is the first supported setup target. Existing OpenCode examples
remain available but do not become a promise that the new installer supports
OpenCode v2 until a matching implementation and tests exist. No GitHub push
or merge is required. Credentials stay on the machine.

Critic amendments are recorded in the plan and are binding. Existing auxiliary routes/fallback aliases must be normalized into the allowed set and backed up privately. No provider-side spending cap is claimed. Verification requires observed request routes and parent/child lineage.
