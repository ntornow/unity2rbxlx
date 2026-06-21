#!/usr/bin/env bash
# Self-heal the self-hosted Actions runner when it WEDGES — i.e. the process is
# alive but its broker long-poll has died, so GitHub marks the runner `offline`
# while launchd still sees a live PID. launchd's KeepAlive only restarts the
# runner on process EXIT, never on a hung-but-alive listener — which is exactly
# the failure that silently took the nightly down from 2026-06-10 to 2026-06-21.
#
# Runs every 15 min from the com.github.actions-runner-watchdog LaunchAgent.
#
# Signal choice: GitHub's runner `status` is authoritative. Local diag-log
# staleness is NOT usable — a healthy *idle* runner can go 40+ min between
# diag writes, and a busy `cold-e2e` job runs ~40 min, so a staleness check
# would both false-positive on idle and (worse) kill live jobs. `status` is
# only ever `online`/`offline`; a runner mid-job is `online` + `busy:true`, so
# matching `offline` can never restart a runner that is actually running a job.
set -euo pipefail

REPO="jiazou/unity2rbxlx"
RUNNER_LABEL="studio"
PLIST_LABEL="com.github.actions-runner"
RUNNER_PLIST="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
UID_NUM="$(id -u)"
# Two-consecutive-offline guard: a runner shows `offline` for ~1-2 min during
# its own auto-update restart. Requiring two offline polls (~15 min apart)
# before acting skips that benign window so the watchdog never kickstarts a
# runner that is merely auto-updating.
STREAK_FILE="$HOME/actions-runner/.watchdog-offline-seen"

log() { printf '%s runner-watchdog: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"; }

# Authoritative wedge signal: the GitHub-side status of the runner carrying our
# label. index() over the label-name array tolerates the runner's extra
# auto-labels (self-hosted, macOS, ARM64). Collect ALL matches so a stray
# duplicate registration is detected rather than silently picked by head -1.
statuses="$(gh api "repos/$REPO/actions/runners" \
  --jq ".runners[] | select([.labels[].name] | index(\"$RUNNER_LABEL\")) | .status" \
  2>/dev/null || true)"
match_count="$(printf '%s' "$statuses" | grep -c . || true)"
status="$(printf '%s' "$statuses" | head -1)"

# WATCHDOG_FORCE_STATUS lets tests exercise the decision without a real runner
# in that state; WATCHDOG_DRYRUN prints the kickstart instead of issuing it.
status="${WATCHDOG_FORCE_STATUS:-$status}"
match_count="${WATCHDOG_FORCE_MATCH_COUNT:-$match_count}"

if [ -z "$status" ] || [ "$match_count" = 0 ]; then
  # gh down, network blip, auth gap, or runner not registered. Do NOT restart on
  # an unknown status — fail safe (leave a working runner alone), just record it.
  log "could not read runner status from GitHub (gh/network/auth?); no action"
  exit 0
fi

if [ "$match_count" -ne 1 ]; then
  # >1 runner carries our label: a stale offline duplicate would otherwise make
  # us kickstart the healthy one every cycle. Ambiguous → fail safe + flag it.
  log "expected exactly 1 runner labelled '$RUNNER_LABEL', found $match_count — no action (clean up stale registrations)"
  exit 0
fi

if [ "$status" != "offline" ]; then
  rm -f "$STREAK_FILE"
  log "runner status=$status — healthy, no action"
  exit 0
fi

# status == offline below.
if [ ! -f "$STREAK_FILE" ]; then
  # First offline sighting — could be a transient auto-update restart. Arm the
  # streak and wait one more poll before acting.
  : > "$STREAK_FILE"
  log "runner OFFLINE (1st sighting) — will heal if still offline next poll (~15 min)"
  exit 0
fi

log "runner OFFLINE on two consecutive polls — kickstart -k to clear a wedged listener"
rm -f "$STREAK_FILE"  # re-arm: don't re-kick every poll while a dead runner recovers
if [ -n "${WATCHDOG_DRYRUN:-}" ]; then
  log "DRYRUN: launchctl kickstart -k gui/$UID_NUM/$PLIST_LABEL"
  exit 0
fi
if launchctl kickstart -k "gui/$UID_NUM/$PLIST_LABEL"; then
  log "kickstart issued"
else
  rc=$?
  # kickstart fails when the runner agent isn't loaded at all (e.g. booted out
  # after a hard crash). Bootstrap it back so a fully-unloaded runner still heals.
  log "kickstart failed (launchctl exit $rc) — runner agent not loaded? bootstrapping"
  if [ -f "$RUNNER_PLIST" ] && launchctl bootstrap "gui/$UID_NUM" "$RUNNER_PLIST" 2>/dev/null; then
    log "bootstrapped runner agent from $RUNNER_PLIST"
  else
    log "bootstrap FAILED — manual intervention needed (plist: $RUNNER_PLIST)"
  fi
fi
