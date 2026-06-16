"""Pure provenance-gated validator for Roblox method calls in transpiled Luau.

For every ``obj:Method(args)`` call site, classify the *receiver*'s provenance
(does it originate from a Roblox-typed source?), then check whether ``Method`` is
a real callable Roblox member (via :mod:`roblox_api_corpus`). A call whose method
is NOT a known Roblox member is emitted as an :class:`InvalidCall`, tagged
``proven`` or ``unproven`` so a downstream caller can decide severity.

This is a pure analysis function: the only external read is the in-memory corpus
(already loaded by the imported module). Inputs are never mutated.

Token-aware, NOT a multiline regex over ``:Method(``. A hand-rolled line/token
scanner resolves each receiver expression and tracks a shallow per-function
``var -> provenance`` map (alias propagation within one function scope only).
"""

from __future__ import annotations

import re
from typing import Literal, TypedDict

from .roblox_api_corpus import is_callable_member


class InvalidCall(TypedDict):
    method: str
    line: int  # 1-based
    receiver_provenance: Literal["proven", "unproven"]
    suggested_fix: str | None


# Auto-fix map: an invalid method name -> its correct Roblox replacement.
_AUTOFIX: dict[str, str] = {"FindFirstChildOfType": "FindFirstChildWhichIsA"}


# --- Field/method-name signals that confer "proven" provenance ----------------

# Field accesses whose RESULT is unconditionally Roblox-typed, regardless of the
# base expression's own provenance. The field name itself is the signal.
# (Load-bearing: ``plr.Character`` where ``plr`` is a host result.)
_PROVEN_FIELDS: frozenset[str] = frozenset({"Parent", "Character", "Instance"})

# Method calls whose RESULT is a Roblox instance (so a local bound to the call
# is proven). The method must be invoked with the ``:`` call syntax.
_PROVEN_RESULT_METHODS: frozenset[str] = frozenset(
    {
        "FindFirstChild",
        "FindFirstChildWhichIsA",
        "FindFirstChildOfClass",
        "FindFirstAncestorWhichIsA",
        "WaitForChild",
    }
)
# ``FindFirstChild*`` family — any method starting with this prefix counts.
_PROVEN_RESULT_PREFIX = "FindFirstChild"

# Methods that return a collection of Roblox instances; a loop variable bound
# from ``for _, x in <expr>:<method>()`` (or the GetPartBoundsInRadius form) is
# proven.
_PROVEN_ITER_METHODS: frozenset[str] = frozenset(
    {"GetChildren", "GetDescendants", "GetPartBoundsInRadius"}
)

# Host APIs that return a NON-Roblox component table (a peer MonoBehaviour
# instance), NOT a real Roblox Instance. A local bound from one of these is
# tracked as ``component`` provenance: its ``.Parent``/``.Character``/``.Instance``
# field access must NOT be promoted to proven (the base is not Roblox-typed).
# Confirmed against runtime/scene_runtime.luau:
#   findObjectOfType -> _byClass[name][1] (a component instance)
#   GetComponent     -> peer MonoBehaviour (or built-in fallback)
#   addComponent     -> _buildComponent(...) result (a component)
# Host APIs that DO return a Roblox Instance (playerFromTouch -> Player,
# findGameObject(sWithTag) -> GameObject Instance, instantiatePrefab -> Instance)
# are deliberately EXCLUDED so their ``.Parent``/``.Character`` stays proven.
_COMPONENT_HOST_APIS: frozenset[str] = frozenset(
    {"findObjectOfType", "getComponent", "GetComponent", "addComponent", "AddComponent"}
)


# --- Tokenizing -------------------------------------------------------------

# A receiver expression immediately precedes a ``:method(`` call. We capture the
# whole dotted/indexed/called chain that ends right before the ``:``.
# Examples of receivers we must classify:
#   workspace                game:GetService("X")     script.Parent
#   self.gameObject          plr.Character            char
#   result.Instance          self.host.foo            require(...)
#
# Token kinds we care about for receiver-chain walking: identifiers, dots,
# colons, and balanced (...) / [...] groups.

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"

# Match a ``:Name(`` method-call operator. We deliberately require the ``:``
# (method call), not ``.`` (field access). We then walk left to grab the
# receiver chain. Group 1 = method name.
_CALL_RE = re.compile(r":(" + _IDENT + r")\s*\(")


def _strip_strings_and_comments(line: str) -> str:
    """Blank out Luau string literals and ``--`` comments on a single line.

    Replaces string *contents* with spaces (preserving length and the quote
    chars) so column offsets stay aligned, and truncates at a ``--`` comment.
    Long-bracket strings ``[[...]]`` are not handled (rare in real output and
    they don't contain ``:method(`` receiver chains we care about).
    """
    out: list[str] = []
    i = 0
    n = len(line)
    quote: str | None = None
    while i < n:
        ch = line[i]
        if quote is not None:
            if ch == "\\" and i + 1 < n:
                out.append("  ")
                i += 2
                continue
            if ch == quote:
                out.append(ch)
                quote = None
            else:
                out.append(" ")
            i += 1
            continue
        # not in a string
        if ch == "-" and i + 1 < n and line[i + 1] == "-":
            break  # rest of line is a comment
        if ch == '"' or ch == "'":
            quote = ch
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _match_balanced_left(s: str, close_idx: int) -> int:
    """Given a closing bracket at ``close_idx``, return the index of its match.

    Returns the index of the matching opening bracket, or -1 if unbalanced.
    """
    close = s[close_idx]
    opener = {")": "(", "]": "["}[close]
    depth = 0
    i = close_idx
    while i >= 0:
        c = s[i]
        if c == close:
            depth += 1
        elif c == opener:
            depth -= 1
            if depth == 0:
                return i
        i -= 1
    return -1


def _receiver_chain(line: str, colon_idx: int) -> str:
    """Extract the receiver expression chain ending just before ``colon_idx``.

    Walks left from the ``:`` collecting a contiguous dotted/indexed/called
    chain (identifiers, ``.``, balanced ``(...)``/``[...]``, ``:method(...)``
    sub-calls). Returns the substring, stripped.
    """
    i = colon_idx - 1
    # skip whitespace
    while i >= 0 and line[i].isspace():
        i -= 1
    end = i + 1
    while i >= 0:
        c = line[i]
        if c == ")" or c == "]":
            open_i = _match_balanced_left(line, i)
            if open_i < 0:
                break
            i = open_i - 1
            continue
        if c.isalnum() or c == "_" or c == ".":
            i -= 1
            continue
        if c == ":":
            # part of an inner ``:method()`` call within the chain — keep it,
            # but only if what's to the left is part of the chain too.
            i -= 1
            continue
        break
    start = i + 1
    return line[start:end].strip()


# --- Provenance classification ---------------------------------------------

ProvLit = Literal["proven", "unproven"]
# Tracked provenance of a local in the per-scope map. ``component`` is an
# internal-only state (a non-Roblox host-component table); it never surfaces as
# an :class:`InvalidCall` provenance — a component receiver classifies as
# ``unproven`` at a call site, and it suppresses ``.Parent`` field-promotion.
TrackLit = Literal["proven", "unproven", "component"]


def _split_top_level(expr: str) -> list[str]:
    """Split a receiver chain on top-level ``.`` and ``:`` into segments.

    Bracketed groups are kept intact. Each segment is the text between
    separators; we also return which separator preceded it implicitly by order
    (caller re-reads the source for the field-name test, so we just need the
    segment list and the final separator/name).
    """
    segs: list[str] = []
    depth = 0
    cur: list[str] = []
    i = 0
    n = len(expr)
    while i < n:
        c = expr[i]
        if c in "([":
            depth += 1
            cur.append(c)
        elif c in ")]":
            depth -= 1
            cur.append(c)
        elif depth == 0 and (c == "." or c == ":"):
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    segs.append("".join(cur))
    return segs


def _classify_receiver(expr: str, prov: dict[str, TrackLit]) -> ProvLit | None:
    """Classify the provenance of a receiver chain.

    Returns ``"proven"``, ``"unproven"``, or ``None`` (meaning: SKIP this call
    entirely — receiver is self/self.host/require, never emit). A tracked
    ``component`` local never returns ``proven``: as a bare receiver it is
    ``unproven``, and it suppresses ``.Parent``/``.Character``/``.Instance``
    field-promotion (the base is a non-Roblox component table).
    """
    expr = expr.strip()
    if not expr:
        return "unproven"

    # --- SKIP receivers: self / self.host / self.host.*(...) / require(...) ---
    # self, self.foo, self.host, self.host.bar, self.host.bar(...) ...
    # But self.gameObject is a PROVEN origin (handled below before the skip).
    if expr == "self.gameObject":
        return "proven"
    if expr == "self" or expr.startswith("self.") or expr.startswith("self:"):
        return None
    if expr.startswith("require(") or expr.startswith("require ("):
        return None

    # Split into top-level segments to inspect the FINAL operation, which is the
    # strongest signal (field name / method result).
    segs = _split_top_level(expr)

    # Re-scan separators in order to know the LAST separator + final segment.
    # Find the position of the last top-level '.' or ':'.
    last_sep, last_name = _last_segment(expr)

    # --- proven-by-final-field: x.Parent / x.Character / x.Instance ---
    # Promote ONLY when the base is not a tracked component table. ``gm.Parent``
    # where ``gm = self.host.findObjectOfType(...)`` (a component) stays
    # unproven — the field name alone does not make a component-table base
    # Roblox-typed. An untracked base still promotes (no evidence it's a
    # component).
    if last_sep == "." and last_name in _PROVEN_FIELDS:
        head = _split_top_level(expr)[0].strip()
        if prov.get(head) == "component":
            return "unproven"
        return "proven"

    # --- Proven-by-final-method-result: x:FindFirstChild*(...) etc. ---
    if last_sep == ":" and _is_proven_result_method(last_name):
        return "proven"

    # --- Single bare identifier: look up tracked provenance ---
    if re.fullmatch(_IDENT, expr):
        tracked = prov.get(expr, "unproven")
        # A component table is never a proven Roblox receiver.
        return "unproven" if tracked == "component" else tracked

    # --- Roblox global origins ---
    head = segs[0].strip()
    if head == "workspace":
        return "proven"
    if head == "script":
        # script, script.Parent, script.Parent.Parent ... all proven
        return "proven"
    if head == "Instance" and expr.startswith("Instance.new("):
        return "proven"
    if expr.startswith("game:GetService(") or expr.startswith("game :GetService("):
        return "proven"

    # --- Base identifier with a non-proven trailer: inherit base provenance only
    # for a *bare* tracked var (handled above). A dotted access onto an untracked
    # base whose final field is not a proven field => unproven.
    return "unproven"


def _last_segment(expr: str) -> tuple[str | None, str]:
    """Return (last_top_level_separator, final_segment_name).

    Separator is '.' or ':' or None (no separator). The final segment name has
    any trailing ``(...)`` call args stripped to bare the method/field name.
    """
    depth = 0
    last_sep_idx = -1
    last_sep: str | None = None
    i = 0
    n = len(expr)
    while i < n:
        c = expr[i]
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif depth == 0 and (c == "." or c == ":"):
            last_sep_idx = i
            last_sep = c
        i += 1
    final = expr[last_sep_idx + 1 :]
    # strip trailing call args / index
    paren = final.find("(")
    if paren >= 0:
        final = final[:paren]
    bracket = final.find("[")
    if bracket >= 0:
        final = final[:bracket]
    return last_sep, final.strip()


def _is_proven_result_method(name: str) -> bool:
    if name in _PROVEN_RESULT_METHODS:
        return True
    if name.startswith(_PROVEN_RESULT_PREFIX):
        return True
    return False


# --- Assignment / alias tracking -------------------------------------------

_LOCAL_ASSIGN_RE = re.compile(
    r"^\s*local\s+(" + _IDENT + r")\s*=\s*(.+?)\s*$"
)
_PLAIN_ASSIGN_RE = re.compile(r"^\s*(" + _IDENT + r")\s*=\s*(.+?)\s*$")
_FOR_IN_RE = re.compile(
    r"^\s*for\s+(.+?)\s+in\s+(.+?)\s+do\b"
)
_FUNC_RE = re.compile(r"\bfunction\b")


def _rhs_provenance(rhs: str, prov: dict[str, TrackLit]) -> TrackLit:
    """Provenance of an assignment RHS (for binding a local).

    Distinct from ``_classify_receiver`` only in that a bare untracked identifier
    is ``unproven`` (not skipped) and self/require RHS yields ``unproven`` rather
    than None — assignments always bind *some* provenance. A call to a
    component-returning host API binds ``component``.
    """
    rhs = rhs.strip()
    # ``a and a:Method()`` / ``a and a.Field`` guard form: take the part after
    # the last ``and`` (the real value), common in transpiled output.
    guard = _strip_and_guard(rhs)
    if _is_component_host_call(guard):
        return "component"
    res = _classify_receiver(guard, prov)
    if res is None:
        return "unproven"
    return res


def _is_component_host_call(rhs: str) -> bool:
    """True if ``rhs`` is a call to a component-returning host API.

    Recognizes both call shapes the transpiler emits:
      - ``self.host.findObjectOfType("X")`` / ``self.host.getComponent("X")``
        (dotted access on ``self.host``)
      - ``self:GetComponent("X")`` (colon-method on ``self``)
    Token-aware: matches the API name as the final ``.``/``:`` segment of the
    callee, immediately followed by ``(``. Receiver-instance host APIs
    (playerFromTouch, findGameObject, instantiatePrefab) are excluded by the
    :data:`_COMPONENT_HOST_APIS` allowlist.
    """
    rhs = rhs.strip()
    sep, name = _last_segment(rhs)
    if name not in _COMPONENT_HOST_APIS:
        return False
    # Must be an actual call (``name(`` somewhere after the segment) and the
    # base must be a host surface (``self`` / ``self.host``), not an unrelated
    # local that happens to share the method name.
    if "(" not in rhs:
        return False
    head = _split_top_level(rhs)[0].strip()
    return head in ("self", "self.host") or head.startswith("self")


def _strip_and_guard(rhs: str) -> str:
    """For ``cond and value`` (top level), return ``value``; else ``rhs``.

    Only splits on a top-level ``and`` so ``char and char:FindFirstChild(...)``
    binds from the ``char:FindFirstChild(...)`` value.
    """
    depth = 0
    i = 0
    n = len(rhs)
    last = -1
    while i < n:
        c = rhs[i]
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif depth == 0 and rhs[i : i + 5] == " and " :
            last = i
        i += 1
    if last >= 0:
        return rhs[last + 5 :].strip()
    return rhs


def _loop_var_provenance(target: str, iterable: str) -> dict[str, TrackLit]:
    """Provenance for variables bound by ``for <target> in <iterable> do``.

    Returns a {var: provenance} map for the loop body. Only the value var of a
    ``GetChildren``/``GetDescendants``/``GetPartBoundsInRadius`` iteration is
    proven; the key var (``_``, index) is not.
    """
    _last_sep, last_name = _last_segment(iterable.strip())
    if last_name not in _PROVEN_ITER_METHODS:
        return {}
    # Targets: ``_, col`` (generic for) — the value is the SECOND name.
    names = [t.strip() for t in target.split(",")]
    out: dict[str, TrackLit] = {}
    if len(names) >= 2:
        val = names[1]
        if re.fullmatch(_IDENT, val):
            out[val] = "proven"
    elif len(names) == 1:
        # numeric/ipairs-less ``for x in expr:GetChildren()`` is uncommon, but
        # if present the single var is the value.
        if re.fullmatch(_IDENT, names[0]):
            out[names[0]] = "proven"
    return out


# --- Main scanner -----------------------------------------------------------

def find_invalid_roblox_calls(luau_source: str) -> list[InvalidCall]:
    """Find ``obj:Method(args)`` call sites whose method is not a Roblox member.

    Pure function. Returns one :class:`InvalidCall` per offending call site,
    excluding calls on ``self`` / ``self.host`` / ``self.host.*`` / ``require``
    receivers entirely. See module docstring for provenance rules.
    """
    results: list[InvalidCall] = []

    raw_lines = luau_source.split("\n")
    # Per-function-scope provenance map. We reset on each ``function`` keyword
    # (shallow scope tracking, as the design specifies — one function scope).
    prov: dict[str, TrackLit] = {}
    # Trailing receiver chain of the previous non-blank CODE line, used to
    # resolve a ``\n  :Method()`` continuation whose line-local receiver is
    # empty (FINDING 2 — a multiline chain must not downgrade proven->unproven).
    prev_trailing_chain = ""

    for idx, raw in enumerate(raw_lines):
        line_no = idx + 1
        code = _strip_strings_and_comments(raw)

        if _FUNC_RE.search(code):
            prov = {}

        # 1) Record bindings introduced on this line BEFORE scanning calls so a
        #    same-line use after the binding sees it (rare, but cheap/correct).
        _record_bindings(code, prov)

        # 2) Scan every ``:Method(`` call site on this line.
        for m in _CALL_RE.finditer(code):
            method = m.group(1)
            colon_idx = m.start()
            receiver = _receiver_chain(code, colon_idx)
            if not receiver:
                # Leading ``:`` continuation of a chain split across lines:
                # classify against the previous code line's trailing chain.
                receiver = prev_trailing_chain
            classification = _classify_receiver(receiver, prov)
            if classification is None:
                continue  # self/self.host/require — excluded entirely
            if is_callable_member(method):
                continue  # valid Roblox method
            results.append(
                InvalidCall(
                    method=method,
                    line=line_no,
                    receiver_provenance=classification,
                    suggested_fix=_AUTOFIX.get(method),
                )
            )

        # 3) Remember this line's trailing receiver chain for a possible
        #    continuation on the next line. Only a non-blank code line that does
        #    NOT itself end on a dangling separator updates it; a blank/comment
        #    line preserves the prior anchor so a two-line gap still resolves.
        if code.strip():
            prev_trailing_chain = _trailing_chain(code)

    return results


def _trailing_chain(code: str) -> str:
    """Return the receiver chain at the END of a code line (for continuations).

    Walks left from the last non-space char, collecting the contiguous
    dotted/indexed/called/``:method()`` chain — the expression a following
    ``  :Method()`` continuation line attaches to. ``""`` if the line does not
    end in such a chain.
    """
    end = len(code)
    while end > 0 and code[end - 1].isspace():
        end -= 1
    if end == 0:
        return ""
    return _receiver_chain(code, end)


def _record_bindings(code: str, prov: dict[str, TrackLit]) -> None:
    """Mutate ``prov`` with any local/loop bindings introduced on ``code``.

    Operates on a single (string/comment-stripped) line. Handles:
      - ``for <t> in <iter> do`` loop value vars
      - ``local <name> = <rhs>``
      - ``<name> = <rhs>`` (plain reassignment, only when name already tracked
        or RHS is provably proven — keeps it shallow)
    """
    fm = _FOR_IN_RE.search(code)
    if fm:
        prov.update(_loop_var_provenance(fm.group(1), fm.group(2)))
        return

    lm = _LOCAL_ASSIGN_RE.match(code)
    if lm:
        name = lm.group(1)
        rhs = lm.group(2)
        prov[name] = _rhs_provenance(rhs, prov)
        return

    pm = _PLAIN_ASSIGN_RE.match(code)
    if pm:
        name = pm.group(1)
        rhs = pm.group(2)
        # Only upgrade to proven; don't clobber a tracked proven with unproven
        # from an unrelated branch (shallow heuristic).
        new = _rhs_provenance(rhs, prov)
        if new == "proven":
            prov[name] = "proven"
        elif name not in prov:
            prov[name] = new
