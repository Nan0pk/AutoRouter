# Native Hermes setup verification — 2026-11-08

The four-mode installer was exercised through its real `setup hermes --mode subscriptions --yes --home ...` command in a disposable profile. It selected Claude Haiku 5.5 for routine work and DeepSeek V4.1 Flash as the provisional specialist. Availability checks rejected two Muse models (HTTP 403) and an unconfirmed GPT-6 Luna probe (HTTP 400), then confirmed the selected pair. Published capabilities are not benchmark scores.

**The installed native two-model handoff passed.** Actual API observations identify Haiku as parent and DeepSeek as child. The specialist completed the arithmetic check, and the parent returned the same result, confirmed by its digest. `temporary-verification.json` and `temporary-events.json` are observed runtime data, not synthetic patches or model labels alone. Status was verified after the run. Parent usage and child API usage are separate in these artifacts; no cost/quota savings claim is made.

Final required checks passed: 125 Python tests, 35 Node tests, the harness self-test and both CI schemas. The independent final review identified missing-price, malformed-quota, free-price-expiry, retry-forcing and stale-verification bugs; all were reproduced with failing regression tests and fixed. The empty-provider diagnostic was fixed too. A regular-profile symlink check was narrowed to owned files after its own regression reproduction.

No login export, API key, access/refresh token, original configuration, or private backup is included here. Account/workspace binding remains only in the local profile policy. Credentials are read in memory from the existing OpenCode Console login. Provider billing and unknown quotas remain explicit limits; this is not a global spending firewall or comparative evaluation.

## Regular profile result

The reviewed setup is installed in the normal Hermes profile, with an exact private configuration backup. A fresh ordinary Hermes CLI session through its normal background service passed Haiku → DeepSeek → Haiku handoff. `regular-profile-verification.json`, `regular-profile-events.json` and the matching installed-source hashes record the proof. `status hermes` reports `verified`. The pre-update running Hermes service had a code-version mismatch and was restarted before this successful check; disconnected earlier turns were not counted as success.

The code and natural-language setup instructions are now available through the local repository checkout. Start a new Hermes session (`/new`) to use the installed model/delegation settings. Additional provider-side spending guarantees and arbitrary third-party plugin behavior remain outside the supported boundary. No change has been published to GitHub.
