"""Post-transpile lowering: retarget a Camera.main child-ref to the runtime rig.

The pre-transpile ``child_ref_resolver`` records a ``RigRootedRetargetFact``
(``field_name``, ``child_name``) for each Camera.main-rooted
``<field> = cam.GetChild(n)`` write whose receiver roots at the unique
``MainCamera``-tagged node. This module CONSUMES that fact POST-AI, deterministically
editing the AI's emitted Luau (the AI is NOT trusted to preserve the binding):

  1. inject a per-instance memoized resolver METHOD
     ``function <Class>:_resolve<Child>()`` (rig lookup + bounded retry + a REAL
     Instance), spliced BEFORE the trailing ``return <Class>`` (so the module
     stays loadable), then a Luau syntax re-check (abstain on failure);
  2. rewrite the consumer READS of ``self.<field>`` in YIELD-SAFE methods to
     ``self:_resolve<Child>()`` (abstain in the non-yielding lifecycle methods
     ``Awake``/``Start``);
  3. neutralize the AI's camera-child Awake WRITE ``self.<field> = <camera-child>``
     to ``self.<field> = nil`` (fact-anchored on the camera-child RHS shape).

The lowering STAMPS the ``rig_binding`` carrier for EVERY script with >=1 rig fact
(default ``present=False``); it flips ``present=True`` only after RE-DERIVING
discharge from the FINAL edited source (the same independent scan the verifier
runs) — never off the in-flight locals alone, so the syntax-revert path can never
leave ``present=True`` on a reverted script.

GENERIC: the field name + child name come from the fact (projections of the
upstream C# field + parsed hierarchy), never from a hardcoded string or ``s.name``.
String-span / structural edits, code-position-guarded — reusing the
``trigger_stay_lowering`` Luau position guards (imported, not re-copied).

Pure (mutates only the script objects' ``luau_source`` / ``rig_binding`` it is
handed, the documented lowering side effect — like ``lower_trigger_stay``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from converter.child_ref_resolver import ChildRefMap, RigRootedRetargetFact
from converter.trigger_stay_lowering import (
    _luau_pos_in_long_bracket,
    _luau_pos_is_code,
)

class _HasLuauSourceAndPath(Protocol):
    luau_source: str
    source_path: str
    rig_binding: dict[str, object] | None


# The methods scene_runtime runs INLINE under the synchronous, non-yielding build
# loop (``start() never yields``, scene_runtime.luau:2066). A ``task.wait`` in a
# read rewritten inside one of these would be an illegal yield, so reads here
# ABSTAIN. Every OTHER method (coroutine/event/equip contexts like GetRifle) CAN
# yield, so the read is rewritten. This is a CLOSED list of known non-yielding
# lifecycle methods, NOT a positive yield-evidence scan.
_NON_YIELDING_LIFECYCLE_METHODS: frozenset[str] = frozenset({"Awake", "Start"})

# A code-level ``function <Class>:<method>(`` declaration. Used to (a) read the
# class name and (b) locate the nearest enclosing method for the yield guard.
_FUNCTION_METHOD_RE = re.compile(r"\bfunction\s+([A-Za-z_]\w*)[:.]([A-Za-z_]\w*)\s*\(")

# A code-level ``return <Ident>`` at module scope — the transpiler's module
# epilogue. The resolver method is spliced immediately BEFORE this.
_RETURN_IDENT_RE = re.compile(r"^[ \t]*return\s+([A-Za-z_]\w*)\s*$", re.MULTILINE)


def lower_rifle_rig_retarget(
    scripts: list[_HasLuauSourceAndPath],
    child_ref_map: ChildRefMap,
) -> int:
    """For each script carrying ``RigRootedRetargetFact``(s), inject a per-instance
    memoized resolver METHOD, rewrite the consumer READS of ``self.<field>`` to
    call it, neutralize the AI's camera-child Awake assignment, and STAMP the
    ``rig_binding`` carrier (default ``present=False``; flip to ``True`` only on
    discharge re-derived from the FINAL source). Returns the number of scripts
    modified."""
    modified = 0
    for script in scripts:
        rig_facts = _rig_facts_for(script, child_ref_map)
        if not rig_facts:
            continue
        # One carrier per script. Stamp from the FACT SET (default present=False)
        # so a script whose lowering ABSTAINS still carries the binding -> the
        # verifier fail-closes loud rather than abstaining silently.
        changed = False
        for fact in rig_facts:
            field = fact.field_name
            child = fact.child_name
            method = f"_resolve{child}"
            class_name = _read_class_name(script.luau_source)
            original = script.luau_source
            if class_name is not None:
                new_src, injected = _inject_resolver_method(
                    script.luau_source, class_name, child, field
                )
                if injected:
                    new_src, _reads = _rewrite_field_reads(new_src, field, method)
                    new_src, _neutralized = _neutralize_assignment(
                        new_src, field, child
                    )
                    if _binding_discharged(new_src, field, child):
                        script.luau_source = new_src
                        changed = True
                    else:
                        # Discharge could not be confirmed on the final source
                        # (e.g. a syntax-reverted inject, or reads/write the
                        # lowering couldn't anchor). Abstain: leave the script
                        # unedited so the verifier sees the un-discharged binding.
                        script.luau_source = original
            # Re-derive discharge from the FINAL committed source (independent of
            # the in-flight locals): True only when the resolver method + rewritten
            # reads + neutralized write actually landed. This re-stamps identically
            # on an idempotent second call (the method is already present) and
            # never stamps True off a reverted edit.
            present = _binding_discharged(script.luau_source, field, child)
            script.rig_binding = {
                "field": field,
                "child": child,
                "present": present,
            }
        if changed:
            modified += 1
    return modified


def _rig_facts_for(
    script: _HasLuauSourceAndPath, child_ref_map: ChildRefMap
) -> tuple[RigRootedRetargetFact, ...]:
    """Join the script to its rig facts by canonical path (resolved-first, raw
    fallback — mirrors ``code_transpiler``'s child_ref_map lookup)."""
    raw = script.source_path
    try:
        canon = str(Path(raw).resolve())
    except OSError:
        canon = raw
    entry = child_ref_map.get(canon) or child_ref_map.get(raw)
    if entry is None:
        return ()
    return entry.rig_facts


def _read_class_name(source: str) -> str | None:
    """The class name from the first code-level ``function <Class>:<m>(`` /
    ``function <Class>.<m>(`` declaration (deterministically emitted for every
    host script). None if none is found (abstain)."""
    for m in _FUNCTION_METHOD_RE.finditer(source):
        if not _luau_pos_is_code(source, m.start()):
            continue
        if _luau_pos_in_long_bracket(source, m.start()):
            continue
        return m.group(1)
    return None


def _last_module_return_span(source: str, class_name: str) -> tuple[int, int] | None:
    """The (start, end) char span of the LAST code-level ``return <class_name>``
    statement line at module scope. None if absent."""
    chosen: tuple[int, int] | None = None
    for m in _RETURN_IDENT_RE.finditer(source):
        if m.group(1) != class_name:
            continue
        if not _luau_pos_is_code(source, m.start()):
            continue
        if _luau_pos_in_long_bracket(source, m.start()):
            continue
        chosen = (m.start(), m.end())
    return chosen


def _resolver_method_text(class_name: str, child: str, field: str) -> str:
    """The per-instance memoized resolver METHOD — a REAL rig-child Instance (or
    nil), bounded 30x0.1s retry, memoized on ``self._<field>Cache``. NO proxy,
    NO module-level state. Ported from the legacy WeaponMount pack."""
    cache = f"_{field}Cache"
    return (
        f"-- _RIG_RETARGET_{child} (auto-generated: Camera.main child-ref retargeted to the rig; per-instance, lazy-at-use)\n"
        f"function {class_name}:_resolve{child}()\n"
        f"    -- memoized per-instance; re-resolves if the cached slot was destroyed\n"
        f"    if self.{cache} and self.{cache}.Parent then\n"
        f"        return self.{cache}\n"
        f"    end\n"
        f"    local function findSlot()\n"
        f"        local rig\n"
        f"        for _, m in workspace:GetDescendants() do\n"
        f'            if m:IsA("Model") and m:GetAttribute("_MainCameraRig") then rig = m break end\n'
        f"        end\n"
        f'        return rig and rig:FindFirstChild("{child}", true)\n'
        f"    end\n"
        f"    local slot = findSlot()\n"
        f"    if not slot then\n"
        f"        -- the rig Model may stream in after this script's Awake runs.\n"
        f"        for _ = 1, 30 do\n"
        f"            task.wait(0.1)\n"
        f"            slot = findSlot()\n"
        f"            if slot then break end\n"
        f"        end\n"
        f"    end\n"
        f"    self.{cache} = slot\n"
        f"    return slot   -- a REAL Instance (or nil), NOT a table\n"
        f"end\n\n"
    )


def _inject_resolver_method(
    source: str, class_name: str, child: str, field: str
) -> tuple[str, bool]:
    """Inject the resolver method BEFORE the trailing ``return <Class>`` (so the
    module stays loadable), then a Luau syntax re-check. Returns
    ``(new_source, injected)``. Idempotency: if the method is already present at a
    code position, return ``(source, False)``. On a syntax-check failure, abstain
    (return the pre-edit source, False)."""
    # Idempotency: guard on the ACTUAL injected method's presence at a code
    # position, NOT a comment-marker substring (round-1 MINOR #6).
    method_decl = f"function {class_name}:_resolve{child}("
    idx = source.find(method_decl)
    while idx != -1:
        if _luau_pos_is_code(source, idx) and not _luau_pos_in_long_bracket(source, idx):
            return source, False  # already injected -> no re-inject
        idx = source.find(method_decl, idx + 1)

    span = _last_module_return_span(source, class_name)
    if span is None:
        return source, False  # no module epilogue to splice before -> abstain

    ins_at = span[0]
    method_text = _resolver_method_text(class_name, child, field)
    new_source = source[:ins_at] + method_text + source[ins_at:]

    if not _luau_syntax_ok(new_source):
        return source, False  # never ship unloadable Luau -> abstain
    return new_source, True


def _rewrite_field_reads(
    source: str, field: str, method_name: str
) -> tuple[str, int]:
    """Rewrite every code-position READ of bare ``self.<field>`` in a YIELD-SAFE
    method to ``self:<method_name>()``. Abstains on:
      - a member-tail ``self`` (``x.self.<field>``);
      - a read inside a NON-yielding lifecycle method (``Awake``/``Start``);
      - the assignment LHS (``self.<field> =``, not ``==``).
    Returns ``(new_source, reads_rewritten)``."""
    pattern = re.compile(r"self\." + re.escape(field) + r"\b")
    out: list[str] = []
    pos = 0
    rewritten = 0
    for m in pattern.finditer(source):
        start = m.start()
        if not _luau_pos_is_code(source, start):
            continue
        if _luau_pos_in_long_bracket(source, start):
            continue
        # ``self`` must be a bare token, not a member tail (``x.self.<field>``).
        k = start
        j = k - 1
        while j >= 0 and source[j] in " \t":
            j -= 1
        if j >= 0 and source[j] == ".":
            continue  # x.self.<field> -> not a bare self read
        # NOT the assignment LHS: a single ``=`` (not ``==``) immediately after.
        after = m.end()
        a = after
        while a < len(source) and source[a] in " \t":
            a += 1
        if a < len(source) and source[a] == "=" and not (
            a + 1 < len(source) and source[a + 1] == "="
        ):
            continue  # assignment LHS -> not a read
        # Yield guard: abstain in the known non-yielding lifecycle methods.
        method = _enclosing_method(source, start)
        if method in _NON_YIELDING_LIFECYCLE_METHODS:
            continue
        out.append(source[pos:start])
        out.append(f"self:{method_name}()")
        pos = m.end()
        rewritten += 1
    out.append(source[pos:])
    return "".join(out), rewritten


def _enclosing_method(source: str, pos: int) -> str | None:
    """The method name of the nearest enclosing code-level
    ``function <Class>:<method>(`` declaration before ``pos``. None if ``pos`` is
    at module scope."""
    method: str | None = None
    for m in _FUNCTION_METHOD_RE.finditer(source):
        if m.start() >= pos:
            break
        if not _luau_pos_is_code(source, m.start()):
            continue
        if _luau_pos_in_long_bracket(source, m.start()):
            continue
        method = m.group(2)
    return method


# A camera-child RHS shape: the AI's positional ordinal / camera-rooted access the
# resolver fact says this field was bound from (``self.cam:GetChildren()[n]`` /
# ``...:GetChild(n)`` / a ``self.cam``-rooted member). The neutralizer anchors on
# a ``self.<field> =`` whose RHS textually carries one of these — NOT an unrelated
# ``self.<field> = <config>`` elsewhere (round-1 BLOCKING #3).
_CAMERA_CHILD_RHS_RE = re.compile(
    r":GetChildren\(\)\s*\[\s*\d+\s*\]"
    r"|:GetChild\(\s*\d+\s*\)"
    r"|\bself\.cam\b"
)


def _neutralize_assignment(
    source: str, field: str, child: str
) -> tuple[str, bool]:
    """Replace the RHS of the camera-child Awake write ``self.<field> = <rhs>``
    with ``nil``, FACT-ANCHORED on the camera-child RHS shape (not the first
    ``self.<field> =`` anywhere), multiline-aware. Abstain-safe (no-op if no
    camera-child write exists). Returns ``(new_source, neutralized)``."""
    assign_re = re.compile(r"self\." + re.escape(field) + r"\s*=(?!=)")
    for m in assign_re.finditer(source):
        if not _luau_pos_is_code(source, m.start()):
            continue
        if _luau_pos_in_long_bracket(source, m.start()):
            continue
        rhs_start = m.end()
        rhs_end = _statement_rhs_end(source, rhs_start)
        rhs = source[rhs_start:rhs_end]
        if _CAMERA_CHILD_RHS_RE.search(rhs) is None:
            continue  # not the camera-child write -> leave it (e.g. a config)
        comment = f" -- rig-retargeted: resolved lazily at use via _resolve{child}"
        new_source = (
            source[:rhs_start] + " nil" + comment + source[rhs_end:]
        )
        return new_source, True
    return source, False


def _statement_rhs_end(source: str, start: int) -> int:
    """The end char index of the RHS expression beginning at ``start``, balanced
    across (), [], {} and short strings, terminating at the end of the logical
    statement (a code-level newline at bracket depth 0, or EOF). Multiline-aware
    so a multi-line camera-child RHS is fully spanned."""
    i = start
    n = len(source)
    depth = 0
    while i < n:
        ch = source[i]
        if ch in "([{":
            depth += 1
            i += 1
            continue
        if ch in ")]}":
            if depth == 0:
                break  # a closing bracket we don't own -> stop
            depth -= 1
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n:
                c = source[i]
                if c == "\\":
                    i += 2
                    continue
                if c == quote or c == "\n":
                    break
                i += 1
            i += 1
            continue
        if ch == "\n" and depth == 0:
            # A bare newline at top level usually ends the statement — UNLESS the
            # expression continues: the text before the newline ended with a
            # binary/continuation operator, OR the next non-blank token begins
            # with one (``and``/``or``/``..``/``.``/``:``/arithmetic/comparison).
            if _line_continues(source, start, i):
                i += 1
                continue
            break  # end of the logical statement at top level
        if ch == "-" and i + 1 < n and source[i + 1] == "-" and depth == 0:
            break  # a trailing comment -> RHS ends before it
        i += 1
    # Trim trailing whitespace from the span.
    while i > start and source[i - 1] in " \t":
        i -= 1
    return i


_CONTINUATION_HEAD_RE = re.compile(
    r"^(and|or|not|\.\.|[.:+\-*/%<>=~^#]|\bthen\b)"
)
_CONTINUATION_TAIL_RE = re.compile(
    r"(\b(and|or|not)|\.\.|[.:+\-*/%<>=~^,({\[]|=)\s*$"
)


def _line_continues(source: str, start: int, nl_pos: int) -> bool:
    """True if the RHS logical expression continues past the newline at
    ``nl_pos`` (bracket depth 0): the text from ``start`` to ``nl_pos`` ends with
    a binary/continuation operator, OR the next non-blank line begins with one."""
    before = source[start:nl_pos]
    if _CONTINUATION_TAIL_RE.search(before):
        return True
    # Peek the next non-blank line's first token.
    j = nl_pos + 1
    n = len(source)
    while j < n and source[j] in " \t\r\n":
        j += 1
    if j >= n:
        return False
    return _CONTINUATION_HEAD_RE.match(source[j:j + 4]) is not None


def _binding_discharged(source: str, field: str, child: str) -> bool:
    """INDEPENDENT, code-position-aware derivation: is ``field``'s binding
    discharged via the rig retarget in THIS source? Mirrors the verifier's
    authority (S1b owns the verifier copy; this is the lowering's own re-derive so
    it never stamps ``present=True`` off a reverted edit).

    True IFF:
      (1) the resolver method ``function <Class>:_resolve<Child>(`` exists AND
          >=1 ``self:_resolve<Child>(`` call exists AND NO bare ``self.<field>``
          READ survives at a consumer; AND
      (2) the camera-child WRITE is gone — no surviving
          ``self.<field> = <... :GetChildren()[n] | :GetChild(n) | self.cam ...>``.
    """
    method_call = f"self:_resolve{child}("
    # (1a) resolver METHOD declaration present at a code position.
    if not _has_resolver_method(source, child):
        return False
    # (1b) >=1 ``self:_resolve<Child>(`` CALL (distinct from the declaration).
    if not _code_contains_token(source, method_call):
        return False
    # (1c) no surviving bare ``self.<field>`` READ (an assignment LHS is allowed —
    # the neutralized ``self.<field> = nil`` keeps the field a member).
    if _has_surviving_field_read(source, field):
        return False
    # (2) the camera-child write is gone.
    if _has_camera_child_write(source, field):
        return False
    return True


def _code_contains_token(source: str, token: str) -> bool:
    idx = source.find(token)
    while idx != -1:
        if _luau_pos_is_code(source, idx) and not _luau_pos_in_long_bracket(source, idx):
            return True
        idx = source.find(token, idx + 1)
    return False


def _has_resolver_method(source: str, child: str) -> bool:
    """True if a code-position ``function <Class>:_resolve<Child>(`` declaration
    exists."""
    pat = re.compile(
        r"\bfunction\s+[A-Za-z_]\w*[:.]_resolve" + re.escape(child) + r"\s*\("
    )
    for m in pat.finditer(source):
        if _luau_pos_is_code(source, m.start()) and not _luau_pos_in_long_bracket(source, m.start()):
            return True
    return False


def _has_surviving_field_read(source: str, field: str) -> bool:
    """True if a bare ``self.<field>`` READ survives at a CONSUMER (yield-safe)
    method position (NOT the assignment LHS, NOT a member tail). A read inside a
    non-yielding lifecycle method (``Awake``/``Start``) is NOT a consumer — the
    yield-guard intentionally leaves it (it reads the neutralized ``nil`` safely),
    so it does not count against discharge."""
    pattern = re.compile(r"self\." + re.escape(field) + r"\b")
    for m in pattern.finditer(source):
        start = m.start()
        if not _luau_pos_is_code(source, start):
            continue
        if _luau_pos_in_long_bracket(source, start):
            continue
        j = start - 1
        while j >= 0 and source[j] in " \t":
            j -= 1
        if j >= 0 and source[j] == ".":
            continue  # x.self.<field> -> not a bare read
        a = m.end()
        while a < len(source) and source[a] in " \t":
            a += 1
        if a < len(source) and source[a] == "=" and not (
            a + 1 < len(source) and source[a + 1] == "="
        ):
            continue  # assignment LHS -> not a read
        if _enclosing_method(source, start) in _NON_YIELDING_LIFECYCLE_METHODS:
            continue  # non-yielding lifecycle read -> abstained, not a consumer
        return True
    return False


def _has_camera_child_write(source: str, field: str) -> bool:
    """True if a code-position camera-child write ``self.<field> = <camera-child>``
    survives."""
    assign_re = re.compile(r"self\." + re.escape(field) + r"\s*=(?!=)")
    for m in assign_re.finditer(source):
        if not _luau_pos_is_code(source, m.start()):
            continue
        if _luau_pos_in_long_bracket(source, m.start()):
            continue
        rhs_end = _statement_rhs_end(source, m.end())
        rhs = source[m.end():rhs_end]
        if _CAMERA_CHILD_RHS_RE.search(rhs) is not None:
            return True
    return False


def _luau_syntax_ok(source: str) -> bool:
    """Luau loadability check on the lowered source. Uses ``luau-analyze`` (the
    project's authoritative checker, the same one the transpiler runs) when the
    binary is installed; otherwise a structural balance check — enough to catch
    the round-1 BLOCKING (a method spliced AFTER ``return <Class>`` leaves a
    trailing ``function`` -> ``Expected <eof>, got 'function'``)."""
    from utils.luau_analyze import luau_analyze_path, syntax_errors_for_source
    if luau_analyze_path():
        return not syntax_errors_for_source(source)
    return _structural_balance_ok(source)


def _structural_balance_ok(source: str) -> bool:
    """Code-position bracket balance + ``function``/``end`` and ``do``/``end``
    sanity. A conservative loadability proxy: returns False on the unbalanced
    shape an after-``return`` splice would create."""
    depth = 0
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if not _luau_pos_is_code(source, i):
            i += 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    if depth != 0:
        return False
    # The module epilogue ``return <Ident>`` must be the LAST code statement: no
    # code-level ``function``/``end`` may follow it (the after-``return`` splice
    # bug). Find the last code-level module return and assert nothing of substance
    # follows.
    last_return_end = -1
    for m in _RETURN_IDENT_RE.finditer(source):
        if _luau_pos_is_code(source, m.start()) and not _luau_pos_in_long_bracket(source, m.start()):
            last_return_end = m.end()
    if last_return_end != -1:
        tail = source[last_return_end:]
        if re.search(r"\bfunction\b|\bend\b", tail):
            return False
    return True
