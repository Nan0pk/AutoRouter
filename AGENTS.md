# AutoRouter setup and repository work

When asked **“set up AutoRouter in <harness>”**, read [SETUP.md](SETUP.md) and run
`python3 scripts/setup_autorouter.py`. A setup request authorizes ordinary
reversible installation into the current local profile, its private backup,
and bounded availability/two-model checks on the selected included/free routes.
Honor narrower provider or budget instructions; avoid redundant confirmations.

- Hermes is supported. Reject other harness names clearly; an example config
  or eval runner is not an installed integration.
- Inspect connected sources without printing credential values. Prefer
  `all` (AutoRouter-All: included subscriptions plus verified free models);
  honor one-provider, exact-model or free-only requests.
  Ask only for a missing choice that changes the result.
- Unattended setup needs `--mode ... --yes`. Never use `--confirm-allowance`
  without the user's actual allowance assertion. Unknown billing is not free.
- `--dry-run` previews without inference or changes. `--inventory` is only for
  dry-runs; `--offline` explicitly leaves the installation unverified.
- Report completion only after status is `verified` and live evidence shows
  two distinct actual API models and a returned child result. A skipped
  specialist is not a handoff pass. Start a fresh Hermes session after setup.
- Keep keys, refresh tokens, raw auth exports, private config backups and user
  prompts out of shared evidence. Use existing machine-local logins.
- Preserve manually selected models; explain that a manual main model disables
  automatic root routing. Select the named AutoRouter model to resume routing.
  The default installed picker entry is AutoRouter → AutoRouter-All. Verify the
  actual shared picker inventory and model selection as well as the handoff.

Preserve user changes. Use focused checks, then `bash scripts/check.sh` and both
CI schema validations. Never treat illustrative benchmark data as verified
model rankings. The implementation plan and critic amendments are recorded in
`docs/superpowers/plans/2026-11-08-interactive-setup.md`.
