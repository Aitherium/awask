# Changelog

All notable changes to `awask` are recorded here.

## [0.2.1] - 2026-11-07

### Fixed

- **awask**: the credential-verify probe's vault URL comes from `AWASK_VAULT_URL` (or the host vault script's `VAULT_URL`), defaulting to `https://localhost:8111`; no deployment hostname ships in the package. Comments no longer cite internal checker ids or container names.
