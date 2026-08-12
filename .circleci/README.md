# .circleci/

CI-internal tooling for this repository, deliberately kept separate from
`install/` (the self-hosting product surface documented in
`install/README.md`/`install/CLAUDE.md`). A self-hoster running VoIPBin
never needs anything under this directory.

- `scripts/open-versions-lock-pr.sh` — opens a PR against this repo bumping
  one image's pin in `install/versions.lock.dist`/`install/docker-compose.yml.dist`
  (the committed templates new installs copy from - see `install/CLAUDE.md`'s
  "versions.lock.dist vs versions.lock" section; merging this PR never touches
  a running server), called by the release jobs in `monorepo`/`monorepo-voip`/`monorepo-javascript`
  after they push a new image (each of those repos clones this one and runs
  `install/scripts/bump-image-digest.sh` immediately before this script — see
  that script's header for why a single-service CI job doesn't need
  `install/scripts/generate-versions-lock.sh`'s full history walk). The PR
  still requires human review and merge like any other change to `install/`
  - this script never self-merges.
- `tests/` — bats tests for the above, with their own minimal
  `test_helper.bash`, independent of `install/tests/test_helper.bash`.
  Run with `bats .circleci/tests/`.
