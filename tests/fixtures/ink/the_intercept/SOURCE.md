# Vendored source — The Intercept (ink)

A real, hand-authored narrative game used as ground-truth for the ink importer's
per-construct losslessness ledger (`scripts/run_ink_losslessness.py`).

| field | value |
|---|---|
| repository | https://github.com/inkle/the-intercept |
| commit (pinned) | `2a816b56e61ce4bf02bec1c638074645bdd871e3` |
| commit date | 2018-10-18 |
| upstream path | `Assets/Ink/TheIntercept.ink` |
| retrieved | 2026-07-11 |
| license | MIT (from upstream README.md — upstream has no standalone LICENSE file) |
| sha256 (TheIntercept.ink) | `1d7b5653b689a9da6801bbfaaa3fbd20b0cc47113b28bb1b1774f6cd11e9eea4` |

The commit is pinned so the ledger's ground-truth construct counts stay stable. The MIT license
text and the `Copyright (c) 2016 inkle Ltd.` line are reproduced in `LICENSE`, copied verbatim from
the upstream README.md at the pinned commit.

Only `Assets/Ink/TheIntercept.ink` is vendored (the ink script). The compiled `TheIntercept.json`,
Unity project files and `.meta` files are not needed and are not copied.

The pinned commit SHA and this fixture path are also recorded as module constants in
`src/owcopilot/recognize/ink_losslessness.py` (`THE_INTERCEPT_SHA`, `THE_INTERCEPT_FIXTURE`), so the
report and tests cite one authoritative source.
