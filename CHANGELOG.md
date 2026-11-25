# Changelog

All notable changes to `awask` are recorded here.

## [0.2.4] - 2026-11-24

### Added

- **decisions**: an ACTION card re-probes on answer; the web OS shows the ask (38d580f5c6)
- **awask**: a permission card names the exact waiting call and offers Yes at 1 (#15517) (b029d6d0f0)
- **awask**: a session card mirrors the AskUserQuestion it is waiting on (#15500) (eee9e4f15d)

### Fixed

- **awask**: regenerate the decision plane after the attention layer (#18992) (#19005) (39a47a8c06)
- **decisions**: the console tier types only into an IDLE session (472af820c1)
- **decisions**: a bare via="test" stays a neutral label (dca60c3fb2)
- **decisions**: quality gate — one DecisionSource import in the self-test (F811) (93080233ba)
- **decisions**: quality gate — wrap reprobe lines, say why a write failed (e7343a5384)
- **decisions**: a TEST answer never reaches a live delivery tier (a2f1ff83e9)
- **decisions**: the store's read path closes a dead permission grant at once (#18265) (fc76fd8d35)
- **decisions**: bind a card to its requester's life and surface the newest ask (#18246) (17afc07e1d)
- **decisions**: free text never answers a card - a terminal reply must name an option (d-9vph) (348d7e594b)
- **decisions**: a card that answers itself ranks below the ones that need you (#15859) (ea1a92784a)
- **credentials**: the Bridge card verifies a password before it stores it (#15752) (c44d39cd0a)
- **awask**: card answers reach the session through a PreToolUse hold, not keystrokes (#15598) (b8108d64b2)
- **decisions**: a test run never opens a card window on the owner's screen (#15475) (6e6bbc4ac1)
- **decisions**: the desk fairy carries a card; the card window drags by its title (#15417) (7c24a42ce2)

## [0.2.1] - 2026-11-07

### Fixed

- **awask**: the credential-verify probe's vault URL comes from `AWASK_VAULT_URL` (or the host vault script's `VAULT_URL`), defaulting to `https://localhost:8111`; no deployment hostname ships in the package. Comments no longer cite internal checker ids or container names.
