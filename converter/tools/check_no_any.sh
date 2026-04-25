#!/usr/bin/env bash
# check_no_any.sh — Block new `Any` annotations outside boundary files.
#
# PRINCIPLE: `Any` is allowed only where untyped external data crosses the
# system boundary (YAML parsers, untyped library seams). Inside the typed
# core, `Any` is a bug — either the type exists and was skipped, or the
# boundary was pushed too far inside.
#
# This gate runs against the PR diff and only fails on ADDED lines. Existing
# `Any` in non-boundary files is recognized debt, tracked as cleanup work.
# A refactor that moves a legacy `Any` line will trip the gate (boy-scout
# rule: clean it while you're there).
#
# Usage: bash check_no_any.sh [<base_ref>]
#   default base_ref = origin/main
#
# Allowlist lives at tools/no-any-allowlist.txt.

set -euo pipefail

BASE="${1:-origin/main}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ALLOWLIST_FILE="$SCRIPT_DIR/no-any-allowlist.txt"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [ ! -f "$ALLOWLIST_FILE" ]; then
  echo "ERROR: allowlist file not found: $ALLOWLIST_FILE" >&2
  exit 2
fi

# Verify $BASE is reachable. If checkout fetch-depth is too small or the base
# ref was never fetched, fail loudly here — silently passing the gate when
# the diff can't be computed is exactly the failure mode we don't want.
if ! (cd "$REPO_ROOT" && git rev-parse --verify --quiet "$BASE" >/dev/null); then
  echo "ERROR: base ref '$BASE' not reachable locally." >&2
  echo "Fetch it before running the gate, e.g.:" >&2
  echo "  git fetch origin '${BASE#origin/}' --depth=200" >&2
  exit 2
fi

# Build the allowed-files set (paths only, strip the | reason).
allowed_paths=$(grep -v '^\s*#' "$ALLOWLIST_FILE" | grep -v '^\s*$' | awk -F' \\| ' '{print $1}' | sed 's/[[:space:]]*$//')

# Get the diff. Only added lines (start with `+`, not `+++`).
# Restrict to .py files in core/, converter/, unity/, roblox/ under converter/.
# No `|| true` here — if git diff itself errors, we want the gate to fail.
diff_output=$(cd "$REPO_ROOT" && git diff "$BASE"...HEAD --unified=0 -- \
  ':(glob)converter/core/**/*.py' \
  ':(glob)converter/converter/**/*.py' \
  ':(glob)converter/unity/**/*.py' \
  ':(glob)converter/roblox/**/*.py')

if [ -z "$diff_output" ]; then
  echo "no-any-gate: no relevant Python changes in diff."
  exit 0
fi

# Walk the diff. Track current file (from `+++ b/<path>` headers).
# For each added line, check if it introduces an `Any` annotation.
# Annotation patterns we flag:
#   `: Any`            param/var/field annotation
#   `-> Any`           return annotation
#   `Any |`  / `| Any` union member
#   `[Any]` / `[Any,`  type argument (list[Any], dict[X, Any], etc.)
# We do NOT flag:
#   `from typing import Any`     (import line, no `:` before)
#   `# ... Any ...`              (comment-only)
#   string literals containing Any
#   the word in identifiers like `MyAny`, `Anything`, `anyone` (word-boundary regex below)

violations=$(echo "$diff_output" | awk '
  /^\+\+\+ b\// {
    sub(/^\+\+\+ b\//, "")
    file=$0
    next
  }
  /^\+/ && !/^\+\+\+/ {
    line=substr($0, 2)

    # KNOWN LIMITATION (documented, not enforced):
    # Under PEP 563 / `from __future__ import annotations`, real annotations
    # can be written as strings: `x: "Any"`, `x: 'typing.Any'`. A regex check
    # for these collides with ordinary list/dict literals like `["Any"]` or
    # `{"mode": "Any"}` — both have a delimiter+quoted-Any+quote pattern.
    # Distinguishing the two requires real AST analysis, out of scope for
    # this grep gate. Reviewers should catch stringized Any in code review.

    # Strip single-line string literals (both quote styles) so that string
    # content matching the annotation regex does not false-positive. Does
    # not handle triple-quoted multiline docstrings; a line inside one that
    # mentions an annotation-shaped fragment can still false-positive.
    # Treated as a known limitation; rephrase or split if hit.
    gsub(/"[^"]*"/, "", line)
    gsub(/\x27[^\x27]*\x27/, "", line)
    # Strip comments (everything from the first # to end of line).
    sub(/[ \t]*#.*$/, "", line)

    # Note: bare `from typing import Any` and `import typing` are NOT skipped
    # by an explicit clause. The annotation regex below requires `:`, `->`,
    # `[`, `,`, or `|` immediately before `Any`, none of which appear on a
    # plain import line. An older version of this script had explicit `next`
    # clauses for those imports; they enabled a `import typing; v: typing.Any
    # = ...` bypass via Python multi-statement lines. Removing them closes
    # that hole without false-positives on imports.

    # Block alias-bypass patterns: `Any as <NAME>` or `import typing as <NAME>`.
    # Renaming Any (or the typing module) is the obvious way to slip a typed
    # escape hatch past a literal-token grep. Documented limitation: type
    # alias via assignment (`Dyn = Any; def f(x: Dyn)`) would still slip
    # through; that requires real AST analysis and is left for follow-up.
    if (match(line, /(^|[^a-zA-Z0-9_])Any[ \t]+as[ \t]+[a-zA-Z_]/)) {
      print file ": " line "  [forbidden: aliasing Any]"
      next
    }
    # Note: we do NOT block `import typing as t` outright — typing exports
    # plenty of legitimate names (Self, TypeAlias, TypeVar, ...) and a
    # refactor that uses an alias for those should not fail the gate.
    # The annotation regex below catches `t.Any` (or `whatever.Any`) at the
    # actual use site via the optional module-prefix `[a-zA-Z_]\w*\.`, so
    # the bypass is closed without false-positives on the import line.

    # Match Any (or <module>.Any) with annotation-context delimiter on the
    # left side and a non-identifier char (or EOL) on the right side, so we
    # also catch `value: Any = ...`, `value: typing.Any = ...`, `int | Any =`,
    # `value: t.Any = None` (any single-identifier alias of typing), etc.
    if (match(line, /(:[ \t]*|->[ \t]*|\[[ \t]*|,[ \t]*|\|[ \t]*)([a-zA-Z_][a-zA-Z0-9_]*\.)?Any([^a-zA-Z0-9_]|$)/)) {
      print file ": " line
    }
  }
')

if [ -z "$violations" ]; then
  echo "no-any-gate: pass (no new Any annotations in diff)."
  exit 0
fi

# Filter out allowed files. Allowed entries match the START of the diff path.
# (i.e. allowlist `converter/unity/yaml_parser.py` matches that exact path.)
real_violations=""
while IFS= read -r line; do
  [ -z "$line" ] && continue
  # Extract just the file path (everything before ": ")
  vfile="${line%%: *}"
  is_allowed=0
  while IFS= read -r allowed; do
    [ -z "$allowed" ] && continue
    if [ "$vfile" = "$allowed" ]; then
      is_allowed=1
      break
    fi
  done <<< "$allowed_paths"
  if [ $is_allowed -eq 0 ]; then
    real_violations="${real_violations}${line}"$'\n'
  fi
done <<< "$violations"

if [ -z "${real_violations//[$'\n\t ']/}" ]; then
  echo "no-any-gate: pass (all Any additions were in allowlisted boundary files)."
  exit 0
fi

cat >&2 <<EOF

ERROR: new \`Any\` annotations introduced outside the boundary allowlist.

PRINCIPLE: \`Any\` is allowed only where untyped external data crosses the
system boundary. Inside the typed core, \`Any\` is a bug.

Offending additions:
$(echo "$real_violations" | sed 's/^/  /')

Fix options:
  1. Replace \`Any\` with the real type. The dest type system lives at
     converter/core/roblox_types.py and converter/core/unity_types.py.
  2. If this code is genuinely a typed/untyped boundary (YAML parser,
     untyped library seam), add the file to converter/tools/no-any-allowlist.txt
     with a one-line architectural justification.

EOF
exit 1
