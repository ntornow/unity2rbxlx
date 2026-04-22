#!/usr/bin/env bash
# One-shot setup for the nightly self-hosted GitHub Actions runner on this Mac.
# Installs + registers the runner, loads it as a LaunchAgent (GUI-capable),
# schedules a 02:55 wake so the 19:00 UTC cron finds the Mac awake, and
# pre-installs converter dev deps. Idempotent — re-run anytime.

set -euo pipefail

REPO="jiazou/unity2rbxlx"
RUNNER_LABEL="studio"
RUNNER_DIR="$HOME/actions-runner"
PLIST_LABEL="com.github.actions-runner"
PLIST="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
WAKE_TIME="02:55:00"
WAKE_DAYS="MTWRFSU"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONVERTER_DIR="$REPO_ROOT/converter"
# Shared venv under $HOME so it survives actions/checkout's git-clean between
# runs. The nightly workflow re-runs `pip install -e .` each time to rebind
# the editable install to the fresh workspace copy.
VENV_DIR="$HOME/.unity2rbxlx-venv"

log()  { printf "\033[1;34m[setup]\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m[ok]\033[0m    %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m  %s\n" "$*"; }
die()  { printf "\033[1;31m[err]\033[0m   %s\n" "$*" >&2; exit 1; }

# ---- 1. preflight --------------------------------------------------------
log "preflight checks"
[ "$(uname)" = "Darwin" ] || die "macOS only"
[ -d "/Applications/RobloxStudio.app" ] || die "Roblox Studio not installed in /Applications"
command -v python3 >/dev/null || die "python3 not in PATH"
command -v gh >/dev/null || die "gh CLI not in PATH — run: brew install gh"
gh auth status >/dev/null 2>&1 || die "gh not authenticated — run: gh auth login"
ok "prereqs look good"

# ---- 2. pre-install converter deps into a shared venv --------------------
# Homebrew python3 is PEP-668 externally-managed, so we must use a venv.
# $HOME/.unity2rbxlx-venv is reused by the nightly workflow too — see
# .github/workflows/test.yml `smoke-test` job.
if [ ! -x "$VENV_DIR/bin/python" ]; then
    log "creating venv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi
log "installing converter dev deps (can take a minute on first run)"
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
(cd "$CONVERTER_DIR" && "$VENV_DIR/bin/python" -m pip install --quiet -e ".[dev]")
ok "converter deps ready in $VENV_DIR"

# ---- 3. download + extract the actions-runner binary ---------------------
if [ ! -x "$RUNNER_DIR/config.sh" ]; then
    log "resolving latest actions-runner release"
    RUNNER_VERSION="$(gh api /repos/actions/runner/releases/latest -q .tag_name | sed 's/^v//')"
    [ -n "$RUNNER_VERSION" ] || die "could not resolve runner version from GitHub"
    ARCH="$(uname -m)"
    case "$ARCH" in
        arm64)  RUNNER_ARCH="osx-arm64" ;;
        x86_64) RUNNER_ARCH="osx-x64" ;;
        *) die "unsupported arch: $ARCH" ;;
    esac
    TARBALL="actions-runner-$RUNNER_ARCH-$RUNNER_VERSION.tar.gz"
    URL="https://github.com/actions/runner/releases/download/v$RUNNER_VERSION/$TARBALL"
    log "downloading $TARBALL"
    mkdir -p "$RUNNER_DIR"
    curl -fL -o "/tmp/$TARBALL" "$URL"
    tar -xzf "/tmp/$TARBALL" -C "$RUNNER_DIR"
    rm -f "/tmp/$TARBALL"
    ok "runner extracted to $RUNNER_DIR"
else
    ok "runner already downloaded"
fi

# ---- 4. register runner with GitHub (token auto-fetched via gh) ----------
if [ ! -f "$RUNNER_DIR/.runner" ]; then
    log "fetching one-time registration token via gh api"
    TOKEN="$(gh api -X POST "repos/$REPO/actions/runners/registration-token" -q .token 2>/dev/null || true)"
    [ -n "$TOKEN" ] || die "could not get registration token — gh user needs admin on $REPO"
    log "configuring runner"
    (cd "$RUNNER_DIR" && ./config.sh \
        --unattended \
        --url "https://github.com/$REPO" \
        --token "$TOKEN" \
        --labels "$RUNNER_LABEL" \
        --name "$(hostname -s)-studio" \
        --replace)
    ok "runner registered with label '$RUNNER_LABEL'"
else
    ok "runner already registered"
fi

# ---- 5. install as USER LaunchAgent (critical: NOT a LaunchDaemon) -------
# LaunchDaemons run as root with no GUI session — osascript/screencapture
# both fail. A user LaunchAgent runs inside the logged-in GUI session, which
# is exactly what smoke_test.py needs to drive Studio.
log "writing LaunchAgent plist → $PLIST"
mkdir -p "$HOME/Library/LaunchAgents"
cat >"$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>
    <key>WorkingDirectory</key>
    <string>$RUNNER_DIR</string>
    <key>ProgramArguments</key>
    <array>
        <string>$RUNNER_DIR/run.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$RUNNER_DIR/runner.out.log</string>
    <key>StandardErrorPath</key>
    <string>$RUNNER_DIR/runner.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
</dict>
</plist>
EOF
ok "plist written"

USER_UID="$(id -u)"
log "(re)loading LaunchAgent"
launchctl bootout "gui/$USER_UID/$PLIST_LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$USER_UID" "$PLIST"
launchctl enable "gui/$USER_UID/$PLIST_LABEL"
ok "runner is now running and will auto-start at every login"

# ---- 6. wake the Mac at 02:55 local daily --------------------------------
log "ensuring Mac wakes daily at $WAKE_TIME (needs sudo once)"
if pmset -g sched | grep -qE "wake.*$WAKE_TIME"; then
    ok "wake schedule already set"
else
    sudo pmset repeat wake "$WAKE_DAYS" "$WAKE_TIME"
    ok "wake scheduled — verify with: pmset -g sched"
fi

# ---- 7. trigger TCC prompts so the user grants perms NOW, not at 3am -----
log "probing Accessibility + Screen Recording permissions"
# These two calls trigger the prompts the first time they run. If already
# granted, they're harmless no-ops.
osascript -e 'tell application "System Events" to name of first process' >/dev/null 2>&1 || true
screencapture -x /tmp/.nightly-perm-test.png >/dev/null 2>&1 || true
rm -f /tmp/.nightly-perm-test.png

# Open the Settings pane directly so the user doesn't have to navigate.
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" >/dev/null 2>&1 || true

cat <<EOF

============================================================================
  SETUP DONE — one manual step remains (Apple forces a click for this):

  The System Settings → Privacy & Security pane just opened. Grant BOTH:

    • Accessibility        → enable for 'Terminal' (or 'sh'/'bash' if listed)
    • Screen Recording     → same

  Without these, the 3 am smoke test can't send F5 to Studio or screenshot.

  ── Verify the runner is online ─────────────────────────────────────────
    gh api repos/$REPO/actions/runners -q \\
       '.runners[] | {name, status, labels:[.labels[].name]}'

  ── Trigger a smoke run NOW (don't wait for 3 am) ───────────────────────
    gh workflow run tests --ref \$(git branch --show-current)

  ── Tail runner logs ─────────────────────────────────────────────────────
    tail -f $RUNNER_DIR/runner.out.log

  ── Stop the runner (if you ever need to) ───────────────────────────────
    launchctl bootout gui/$USER_UID/$PLIST_LABEL
============================================================================
EOF
