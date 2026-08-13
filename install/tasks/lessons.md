# Lessons

Team-shared engineering lessons for the sandbox repository. Add an entry
after any user correction, postmortem, or discovered failure mode: the
failure mode, the detection signal, and a prevention rule.

## 2026-07-31 mkcert CAROOT under sudo installs the wrong CA (VOIP-1275)

- **Failure mode:** running `mkcert -install` under `sudo` resolves
  `CAROOT` to root's directory, so the system trusts a different CA than
  the one the unprivileged `init.sh` used to sign the certificates. Trust
  silently never attaches; browsers keep warning even though "the CA is
  installed".
- **Detection signal:** `check-install.sh`'s `cert-trust` check compares
  the issuer of `certs/api/cert.pem` against the CA at the invoking user's
  `mkcert -CAROOT`; a mismatch fails the check explicitly instead of being
  discovered in a browser.
- **Prevention rule:** any root-context mkcert trust installation must
  resolve the invoking user's CAROOT explicitly
  (`CAROOT="$(sudo -u "$SUDO_USER" -H mkcert -CAROOT)"`) and install in
  two passes: as root for the system store, and as the invoking user for
  the user's NSS store (Chrome/Firefox). When `SUDO_USER` is unset (direct
  root login), skip the user pass entirely; never execute `sudo -u ""`.
  This lives exclusively in `scripts/setup-host.sh`.

## 2026-07-31 Unconditional regeneration paths are clobber sites (VOIP-1275)

- **Failure mode:** four separate code paths unconditionally regenerated
  DNS/TLS state (`common.sh:update_env_ips` URL rewrites,
  `common.sh:regenerate_ssl_certs` mkcert regeneration,
  `setup-voip-network.sh`'s IP-change block, and `start.sh` Steps 7-8).
  Any one of them, reached through any entry point, would overwrite an
  external-mode install's operator-managed URLs, BYO certificates, or
  write `.test` zones onto a real-domain tree.
- **Detection signal:** bats mode-gate tests
  (`tests/mode-gates.bats`, `tests/common.bats`) assert each choke point
  skips in external mode / `TLS_MODE=byo`; `check-install.sh` catches a
  clobbered install after the fact (cert expiry/issuer, realm mismatch).
- **Prevention rule:** gate destructive regeneration at the shared choke
  point (the function in `common.sh`), not only at the callers, so an
  untouched entry point cannot bypass the gate. Gates at choke points
  return 0 on refuse, never non-zero: callers invoke them bare under
  `set -e`, and a non-zero "skip" would abort the caller mid-run. When
  adding any new code that rewrites `.env`, `certs/`, or
  `config/coredns/`, it must check `get_domain_mode` / `TLS_MODE` first
  and a bats test must cover the external-mode no-op.

## 2026-08-13 CMD-SHELL healthcheck against a scratch-based image never passes (VOIP-1336)

- **Failure mode:** `oliver006/redis_exporter:v1.66.0` (the bare, non-`-alpine`
  tag) is a scratch-based image with no `/bin/sh`. A `healthcheck.test:
  ["CMD-SHELL", "wget ..."]` entry against it can never execute
  (`CMD-SHELL` requires a shell inside the container), so the container
  reports permanently `unhealthy` even though the process itself is
  working fine and serving `/metrics` correctly.
- **Detection signal:** caught in code review, not by the (static, grep-based)
  bats suite — `tests/config.bats` only asserts a `healthcheck:` block
  exists, it never actually runs the image. Reproduced by `docker run
  --rm --entrypoint sh <image> -c "which wget"` failing with "exec: sh:
  executable file not found in $PATH". `scripts/check-install.sh` and
  `scripts/doctor.sh` both generically fail the whole install if any
  container is unhealthy, so this would have broken every fresh install
  the moment this service was added.
- **Prevention rule:** before pinning any new third-party image that will
  get a `CMD`/`CMD-SHELL` healthcheck, verify the tag actually ships a
  shell and the healthcheck's binary (`docker run --rm --entrypoint sh
  <image>@<digest> -c "which <binary>"`) — many exporter/utility images
  publish both a scratch/distroless tag (smallest, no shell) and an
  `-alpine`/`-debian` tag (has a shell) under the same version; prefer the
  latter whenever a shell-based healthcheck is used. When in doubt, do a
  throwaway `docker run` smoke test of the exact pinned digest before
  adding it to `docker-compose.yml.dist`, not just a `docker compose
  config` syntax check (which cannot catch this class of bug).
