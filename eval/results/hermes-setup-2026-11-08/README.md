# Native Hermes setup verification — 2026-11-08

The four-mode installer was exercised through its real `setup hermes --mode subscriptions --yes --home ...` command in a disposable profile. It selected Claude Haiku 5.5 for routine work and DeepSeek V4.1 Flash as the provisional specialist. Availability checks rejected two Muse models (HTTP 403) and an unconfirmed GPT-6 Luna probe (HTTP 400), then confirmed the selected pair. Published capabilities are not benchmark scores.

**The installed native two-model handoff passed.** Actual API observations identify Haiku as parent and DeepSeek as child. The specialist completed the arithmetic check, and the parent returned the same result, confirmed by its digest. `temporary-verification.json` and `temporary-events.json` are observed runtime data, not synthetic patches or model labels alone. Status was verified after the run. Parent usage and child API usage are separate in these artifacts; no cost/quota savings claim is made.

All project checks passed: 117 Python tests, 35 Node tests, the harness self-test and both CI schemas. A fresh critical review follows before installation in the regular profile. Final regular-profile evidence will be added after that installation.

No login export, API key, access/refresh token, original configuration, or private backup is included here. Account/workspace binding remains only in the local profile policy. Credentials are read in memory from the existing OpenCode Console login. Provider billing and unknown quotas remain explicit limits; this is not a global spending firewall or comparative evaluation.
