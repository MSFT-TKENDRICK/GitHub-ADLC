#!/usr/bin/env bash
# ADLC side-load bootstrap.
#
# Designed for the dotfiles pattern in GitHub Codespaces: point your dotfiles
# repo (or any machine setup) at this script and it installs the ADLC CLI and
# wires the current repository, operating in the repo's own context.
#
#   curl -fsSL https://raw.githubusercontent.com/MSFT-TKENDRICK/GitHub-ADLC/v0/bootstrap.sh | bash
#
# It deliberately does two small things and nothing else:
#   1. install the `adlc` CLI
#   2. run `adlc init` against the target repository
#
# It never copies the framework into your repo and never touches existing CI.

set -euo pipefail

ADLC_REF="${ADLC_REF:-v0}"
ADLC_REPO="${ADLC_REPO:-MSFT-TKENDRICK/GitHub-ADLC}"
ADLC_TARGET="${ADLC_TARGET:-${GITHUB_WORKSPACE:-$PWD}}"
ADLC_PROFILE="${ADLC_PROFILE:-minimal}"

log()  { printf '\033[36m[adlc]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[adlc]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[adlc]\033[0m %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || die "git is required"

# --- locate a usable Python -------------------------------------------------
PYTHON=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
      PYTHON="$candidate"
      break
    fi
  fi
done
[ -n "$PYTHON" ] || die "Python 3.11+ is required but was not found"
log "using $($PYTHON --version)"

# --- install the CLI --------------------------------------------------------
if command -v adlc >/dev/null 2>&1 && [ "${ADLC_FORCE_INSTALL:-0}" != "1" ]; then
  log "adlc already installed: $(adlc --version)"
elif command -v uv >/dev/null 2>&1; then
  log "installing adlc with uv from ${ADLC_REPO}@${ADLC_REF}"
  uv tool install --force "git+https://github.com/${ADLC_REPO}@${ADLC_REF}"
else
  log "installing adlc with pip from ${ADLC_REPO}@${ADLC_REF}"
  "$PYTHON" -m pip install --user --upgrade \
    "git+https://github.com/${ADLC_REPO}@${ADLC_REF}" \
    || die "pip install failed; install uv or a newer pip and retry"
fi

# `pip install --user` may land outside PATH.
if ! command -v adlc >/dev/null 2>&1; then
  USER_BIN="$($PYTHON -c 'import site,os;print(os.path.join(site.USER_BASE,"bin"))' 2>/dev/null || true)"
  if [ -n "$USER_BIN" ] && [ -x "$USER_BIN/adlc" ]; then
    export PATH="$USER_BIN:$PATH"
    log "added $USER_BIN to PATH for this session"
    for profile in "$HOME/.bashrc" "$HOME/.zshrc"; do
      [ -f "$profile" ] || continue
      grep -q "$USER_BIN" "$profile" 2>/dev/null || \
        printf '\n# added by ADLC bootstrap\nexport PATH="%s:$PATH"\n' "$USER_BIN" >> "$profile"
    done
  fi
fi
command -v adlc >/dev/null 2>&1 || die "adlc is installed but not on PATH"

# --- wire the target repository --------------------------------------------
if [ ! -d "$ADLC_TARGET/.git" ]; then
  warn "$ADLC_TARGET is not a git repository; installed the CLI only"
  exit 0
fi

log "installing ADLC into $ADLC_TARGET (profile=$ADLC_PROFILE, pinned to $ADLC_REF)"
adlc init --target "$ADLC_TARGET" --profile "$ADLC_PROFILE" --ref "$ADLC_REF"

log "capability detection:"
( cd "$ADLC_TARGET" && adlc doctor ) || warn "adlc doctor reported problems"

cat <<'EOF'

ADLC is ready.

  adlc doctor                          # what is available here
  adlc run new --brief <file.md>       # start a run
  adlc report latest --open            # read the result

Set `commands.test` in .adlc/config.yaml — the `tests` gate is required and
reports not_run without it, which fails the build by design.
EOF
