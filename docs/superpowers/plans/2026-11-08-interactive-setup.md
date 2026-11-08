# Interactive AutoRouter Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Install usable AutoRouter routing into normal Hermes sessions from one setup command, with four model-source choices.

**Architecture:** A dependency-free Python setup package discovers and filters models, produces a credential-free policy and installs a native Hermes plugin. A helper runs in the already installed Hermes Python for its provider/YAML APIs; Hermes retains credential handling and native delegation.

**Tech Stack:** Python 3.11+, standard library; Hermes v0.23.0+ native plugin/provider/delegation APIs and its installed ruamel.yaml. No new service or model proxy.

**Spec:** `docs/superpowers/specs/2026-11-08-interactive-setup.md`

## Global Constraints
- Preserve existing logins, unrelated settings and comments; keep credentials out of artifacts.
- Included subscription allowance and zero-price models are different billing categories.
- Unknown price/quota/capability stays unknown; never score illustrative examples as verified evidence.
- No additional paid API spending; no fallback outside approved candidates.
- Only Hermes is a supported new setup target until other harnesses have actual adapters.
- A two-model live handoff cannot pass when the specialist was skipped.

## Review Focus
- A connected account with stale/absent catalog or hidden quota must not silently authorize spending.
- OAuth account/workspace changes must fail closed and never pair another account's token with old headers.
- Repeated setup/undo and failed installation must preserve later user edits and unrelated config.
- Native plugin failures, missing delegation tools and manual models must not silently claim automatic routing.
- A real installed two-model run, with child recursion disabled and actual routes recorded, is required.

## Task 1: Discovery and model policy
**Files:** Create `autorouter/__init__.py`, `autorouter/policy.py`, `autorouter/credentials.py`, `autorouter/hermes.py`; test `tests/test_setup_policy.py`.
**Interfaces:** `discover() -> dict` returns only sanitized providers/models/errors; model rows contain id, provider, runtime_provider, prices, billing, tools, reasoning, quota and source/fetched_at. `select_models(inventory, mode, providers=(), models=(), confirmed_allowances=()) -> dict` returns routine/specialist rows, allowed rows, billing policy and reasons. `opencode_credential(binary, expected=None) -> dict` is internal memory-only and exposes no secret in errors.
- [x] Write tests for all four modes, exact selection, no paid/unknown/free fallback, unknown quotas, false/negative prices, provider-scoped IDs, distinct tool-capable models, stale metadata and credential rejection.
- [x] Run `python3 -m unittest discover -s tests -p test_setup_policy.py`; confirm new behavior fails, then implement and rerun to PASS.
- [x] Native discovery uses installed registry/auth/catalog/account-usage APIs; read-only OpenCode metadata and model listing supplement the Go provider. Reuse existing runtime, never install dependencies during discovery.
- [x] Commit the tested discovery/policy unit with prerequisites from the earlier Hermes pilot fix.

## Task 2: Native Hermes integration
**Files:** Create `integrations/hermes/autorouter/plugin.yaml`, `integrations/hermes/autorouter/__init__.py`, `integrations/hermes/opencode-go/plugin.yaml`, `integrations/hermes/opencode-go/__init__.py`; test `tests/test_hermes_plugin.py`.
**Interfaces:** The general plugin reads the installed credential-free policy from `autorouter-policy.json`; `register(ctx)` installs a delegation provider, pre-turn hook and request middleware. The provider's `route_task(task, config)` returns a native route restricted to policy; main and child providers can differ. The model-provider bridge resolves existing OpenCode access in memory and pins account/workspace.
- [x] Write routing/forcing tests including complex/security prompts, simple prompts, explicit models, missing tool, child recursion, unauthorized candidate and account changes.
- [x] Run `python3 -m unittest discover -s tests -p test_hermes_plugin.py`; confirm failures, implement with supported APIs, rerun to PASS.
- [x] Test discovery of these plugins using the real installed Hermes runtime in a temporary profile; do not monkeypatch installed Hermes source.
- [x] Commit native integration and tests.

## Task 3: Setup command, transactional install and undo
**Files:** Create `scripts/setup_autorouter.py`, `autorouter/cli.py`, `autorouter/install.py`; tests `tests/test_setup_install.py`, `tests/test_setup_cli.py`.
**Interfaces:** Commands `setup hermes`, `inspect hermes`, `status hermes`, `verify hermes`, `undo hermes`. Setup flags include `--mode subscriptions|provider|models|free`, repeated `--provider`/`--model`, `--confirm-allowance`, `--dry-run`, `--yes`, `--home` and optional sanitized `--inventory` fixture for offline tests. Install returns backup/policy paths, SHA-256 install manifest and verification state.
- [x] Write subprocess tests for menu choices and matching flags, missing input without TTY, unsupported harness/no writes, dry-run/no inference, installation rollback, idempotence, config preservation, symlink refusal and undo after later edits.
- [x] Run focused test files, confirm failure, implement the menu plus staged install with exact-byte backups and conflict-aware undo; rerun to PASS.
- [x] Install a copy of the native plugin and small shared package; no dependency on this checkout's continued location. Save credential-free policy with local-only file permissions and configure main/delegation without extra fallback models.
- [x] Commit installer, CLI and tests.

## Task 4: Live verification and usable documentation
**Files:** Create `autorouter/verify.py`, `SETUP.md`, `AGENTS.md`, `tests/test_setup_verify.py`; update `README.md`, `docs/enforcement.md`, `scripts/check.sh` as needed; save report under `eval/results/`.
**Interfaces:** `verify_install(home, live=False) -> dict` distinguishes installed/configured/routing_verified/handoff_verified. Live verification exercises the installed native tool/backend, asserts two distinct observed provider/model routes and successful results, and redacts artifacts. Failure never reports setup complete; installation is rolled back or visibly marked unverified.
- [x] Write tests that reject no child, wrong model, missing usage, empty result and skipped handoff. Confirm failure, implement and rerun to PASS.
- [x] Document natural language entry, four modes, reasons, price/quota limits, normal Hermes usage, status, undo and unsupported targets. Document runtime guarantees and limits precisely.
- [x] Run `bash scripts/check.sh` and both CI schema validations using installed dependencies. Expected PASS without skipped sandbox tests.
- [x] Use saved OpenCode Go login for bounded live verification in a disposable profile, then install/verify the current Hermes profile with a backup. Save a report with real observed routes and no credentials.
- [x] Commit implementation/docs/evidence. Request one fresh whole-branch review, fix substantive findings with regression tests, and rerun affected/required checks. Leave the branch local for review.

## Critic review and amendments
Fresh builder-check review completed before implementation. Amendments:
1. Native Hermes hooks/middleware fail open on unexpected plugin errors. Restrict hard allowlisting claims to the delegation backend; root forcing is verified behavior, with explicit invalid-policy/tool blocks and status diagnostics, not a global billing firewall.
2. Normalize all owned auxiliary provider/model/fallback_chain keys, root fallback aliases and delegation fallbacks; disable title upgrades and smart routing. Explicit main provider suppresses ambient auxiliary discovery. Preserve exact original config privately for undo.
3. Record provider billing evidence/user allowance assertions with source/time. Reject known exhausted allowances; fresh zero prices require both input/output. Do not promise a provider-side hard billing cap. Keep the four requested modes; omit additional paid spending.
4. Bridge name is `autorouter-opencode-go`; inherit the native Go wire/reasoning profile. Auth errors raise sanitized exceptions and never return None into the generic credential ladder. Pin account/workspace and use only the official Go endpoint.
5. Bind a complex root's specialist requirement to session/turn so a simplified delegated goal cannot erase escalation. Exclude child turns; test concurrent sessions, multi-turn calls, retries and visible manual override behavior.
6. Installation uses an exclusive lock, staged generation, private rollback journal and config activation last; it is recoverable, not a claim of multi-file atomicity. Undo checks all owned hashes before any writes. Refuse fewer than two eligible models.
7. Live verification observes actual provider/model request events plus parent/child lineage in a fresh installed Hermes session, not backend labels. Tests reject skipped children. `--inventory` is a dry-run-only test input, never a path to installing unverified free pricing.

The user authorized amendment and continued execution; no new approval checkpoint is needed.

## Completion evidence
All four tasks completed. Final critic findings were reproduced and fixed; required checks passed 125 Python tests, 35 Node tests and both schemas. Normal-profile live evidence and source hashes are in `eval/results/hermes-setup-2026-11-08/`. The regular Hermes service was refreshed after its earlier source upgrade. A local fast-forward will make the setup entry point available in the original checkout, preserving the feature branch and all user eval reports.
