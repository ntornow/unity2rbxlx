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
  2. reroute the consumer READS of ``self.<field>`` in YIELD-SAFE methods to
     ``self:_resolve<Child>()`` (abstain in the non-yielding lifecycle methods
     ``Awake``/``Start``). This READ reroute is the LOAD-BEARING, RHS-AGNOSTIC
     discharge (Path A re-anchor): it keys on the AI-STABLE member access
     ``self.<field>``, NOT on the AI-VOLATILE write/RHS shape, so it fires across
     all 5 real write shapes (``:GetChildren()[1]``, ``self.gameObject``,
     ``__unityChild(...)``, a multi-step local, a ``FindFirstChild`` fallback);
  3. Tier-2 BEST-EFFORT HYGIENE (SKIP-on-ambiguity, DECOUPLED from discharge):
     neutralize an UNAMBIGUOUS camera-child init-write ``self.<field> =
     <camera-ordinal>`` to ``self.<field> = nil`` when it is recognized; on any other
     shape SKIP (the leftover write is dead data — no raw read survives to read it).
     The neutralize NEVER gates ``present``.

DISCHARGE (``present=True``) := the resolver method present + >=1 call + NO raw
``self.<field>`` dot-form READ survives (outside ``Awake``/``Start``). The init-WRITE
neutralize is NOT a discharge condition (Path A re-anchor — supersedes the round-5
write-shape coupling; the ``cam_receiver`` camera-RHS shape-matching is now an
OPTIONAL Tier-2 refinement, no longer load-bearing).

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

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from converter.child_ref_resolver import ChildRefMap, RigRootedRetargetFact
from converter.trigger_stay_lowering import (
    _long_bracket_open_level,
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
# epilogue. The resolver method is spliced immediately BEFORE this. An OPTIONAL
# trailing line-comment is tolerated (``return Player -- module epilogue``) so a
# commented epilogue is still recognized as the splice point AND its tail scan
# stays code-position aware (round-4 MINOR).
_RETURN_IDENT_RE = re.compile(
    r"^[ \t]*return\s+([A-Za-z_]\w*)[ \t]*(?:--[^\n]*)?$", re.MULTILINE
)

# A valid Luau identifier (the shape a method-name suffix must satisfy before it
# can be spliced into ``function <Class>:_resolve<suffix>()`` / ``self:_resolve<suffix>()``).
_LUAU_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _method_suffix(child: str) -> str:
    """A deterministic VALID-LUAU-IDENTIFIER suffix for the resolver method name
    ``_resolve<suffix>``. A Roblox child name may contain spaces/special chars
    (e.g. ``"Weapon Slot"``) that are illegal in a Luau identifier; the rig LOOKUP
    still uses the REAL ``child`` string in ``FindFirstChild("<real name>", true)``,
    so ONLY the Luau method identifier needs sanitizing.

    If ``child`` is already a valid identifier, it is used verbatim (preserves the
    happy-path emit + cross-run idempotency). Otherwise each illegal char is mapped
    to ``_`` and a short hash of the REAL name is appended for collision resistance
    (two distinct child names that sanitize to the same prefix get distinct
    suffixes). The result always satisfies ``_LUAU_IDENT_RE``."""
    if _LUAU_IDENT_RE.match(child):
        return child
    sanitized = re.sub(r"[^A-Za-z0-9_]", "_", child)
    if not sanitized or not re.match(r"[A-Za-z_]", sanitized):
        sanitized = "_" + sanitized
    digest = hashlib.sha1(child.encode("utf-8")).hexdigest()[:8]
    return f"{sanitized}_{digest}"


def lower_rifle_rig_retarget(
    scripts: list[_HasLuauSourceAndPath],
    child_ref_map: ChildRefMap,
) -> int:
    """For each script carrying ``RigRootedRetargetFact``(s), inject a per-instance
    memoized resolver METHOD, REROUTE the consumer READS of ``self.<field>`` to call
    it (the LOAD-BEARING RHS-agnostic discharge, Path A), best-effort-neutralize the
    AI's camera-child Awake assignment as Tier-2 hygiene (SKIP-on-ambiguity, decoupled
    from discharge), and STAMP the ``rig_binding`` carrier (default ``present=False``;
    flip to ``True`` only on discharge re-derived from the FINAL source). Returns the
    number of scripts modified."""
    modified = 0
    for script in scripts:
        rig_facts = _rig_facts_for(script, child_ref_map)
        if not rig_facts:
            continue
        # >1 rig fact on ONE script: the single-dict carrier (§1.5) can represent
        # exactly one binding, and the design (§1.5, edge 9) frames multi-fact as a
        # two-SCRIPTS case (the corpus is one-fact-per-script). Rather than silently
        # keep only the LAST fact's carrier (dropping the earlier bindings'
        # discharge from the verifier), FAIL CLOSED: stamp a FULL 5-key carrier
        # (present=False, multi_fact=True) from the FIRST rig fact and ABSTAIN on all
        # edits, so the verifier fail-closes LOUD instead of shipping an unverifiable
        # binding. REDESIGN r3 (close the resume-path hole): the carrier MUST be the
        # full 5-key shape — a 2-key carrier would be DROPPED to None by the 5-key
        # rehydrate LOAD validator, so the verifier would ABSTAIN (not fire) on a
        # preserve/resume assemble. Full-keyed -> round-trips the validator -> fires
        # loud on the resume path exactly as on the fresh path.
        if len(rig_facts) > 1:
            first = rig_facts[0]
            script.rig_binding = {
                "field": first.field_name,
                "child": first.child_name,
                "present": False,
                "cam_receiver": first.cam_receiver,
                "cam_ordinal": first.ordinal,
                "multi_fact": True,
            }
            continue
        # One carrier per script. Stamp from the FACT SET (default present=False)
        # so a script whose lowering ABSTAINS still carries the binding -> the
        # verifier fail-closes loud rather than abstaining silently.
        changed = False
        for fact in rig_facts:
            field = fact.field_name
            child = fact.child_name
            # cam_symbols is consumed ONLY by the Tier-2 best-effort neutralize to
            # opportunistically recognize the init-write; it does NOT gate discharge.
            cam_symbols = _camera_symbol_forms(fact.cam_receiver)
            # The method-name suffix is a VALID Luau identifier derived from the
            # child name (which may carry spaces/special chars); the rig LOOKUP
            # still uses the real ``child`` string. The cache local ``_<field>Cache``
            # splices ``field`` — a C# field name, always a valid identifier — but
            # guard it too: if either can't yield a valid identifier, ABSTAIN (the
            # carrier still stamps present=False so the verifier fail-closes loud).
            suffix = _method_suffix(child)
            if not _LUAU_IDENT_RE.match(field):
                script.rig_binding = {
                    "field": field,
                    "child": child,
                    "present": False,
                    "cam_receiver": fact.cam_receiver,
                    "cam_ordinal": fact.ordinal,
                }
                continue
            method = f"_resolve{suffix}"
            class_name = _read_class_name(script.luau_source)
            original = script.luau_source
            if class_name is not None:
                new_src, injected = _inject_resolver_method(
                    script.luau_source, class_name, child, field, suffix
                )
                if injected:
                    new_src, _reads = _rewrite_field_reads(new_src, field, method)
                    # Tier-2 best-effort hygiene (decoupled from discharge): neutralize
                    # an unambiguous camera-child init-write; SKIP on any other shape.
                    new_src, _neutralized = _neutralize_assignment(
                        new_src, field, suffix, cam_symbols
                    )
                    # Re-check Luau syntax on the FINAL source AFTER ALL rewrites
                    # (inject + read-rewrite + neutralize) — a read-rewrite or a
                    # neutralize can corrupt the module (e.g. drop a closing
                    # ``end``) AFTER the post-inject check, and ``_binding_discharged``
                    # is shape-only, so it would stamp present=True on broken Luau.
                    # If the FINAL source fails to parse, abstain/revert.
                    if not _luau_syntax_ok(new_src):
                        script.luau_source = original  # never ship unloadable Luau
                    elif _binding_discharged(new_src, field, child, suffix):
                        script.luau_source = new_src
                        changed = True
                    else:
                        # Discharge could not be confirmed on the final source (Path A:
                        # the read reroute did not land — e.g. the field is factored out
                        # of every consumer read). Abstain: leave the script unedited so
                        # the verifier sees the un-discharged binding.
                        script.luau_source = original
            # Re-derive discharge from the FINAL committed source (independent of the
            # in-flight locals): True only when the resolver method + rerouted reads
            # actually landed (Path A: RHS-agnostic, decoupled from neutralize). This
            # re-stamps identically on an idempotent second call (the method is already
            # present) and never stamps True off a reverted edit.
            present = _binding_discharged(script.luau_source, field, child, suffix)
            # REDESIGN r3: cam_receiver + cam_ordinal are deterministic projections of
            # the resolver fact, stamped regardless of discharge outcome — they anchor
            # check D's dead-write exemption (slice 1.2), NOT discharge.
            script.rig_binding = {
                "field": field,
                "child": child,
                "present": present,
                "cam_receiver": fact.cam_receiver,
                "cam_ordinal": fact.ordinal,
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
    statement line AT MODULE SCOPE. None if absent.

    Module scope is the discriminator (codex harden BLOCKING): a method-local
    ``return <class_name>`` (e.g. ``function <Class>.new() return <Class> end``)
    matches ``_RETURN_IDENT_RE`` and can be the LAST occurrence when the module
    epilogue returns a different symbol — splicing the resolver before it would
    nest the method inside another function (still syntactically valid, so the
    Luau re-check does NOT catch it), and stamp ``present=True`` off a misplaced
    splice. Reject any return that lies inside a function body by checking it has
    no enclosing colon-method."""
    chosen: tuple[int, int] | None = None
    for m in _RETURN_IDENT_RE.finditer(source):
        if m.group(1) != class_name:
            continue
        if not _luau_pos_is_code(source, m.start()):
            continue
        if _luau_pos_in_long_bracket(source, m.start()):
            continue
        if _return_is_inside_function_body(source, m.start()):
            continue  # method-local return, NOT the module epilogue -> skip
        chosen = (m.start(), m.end())
    return chosen


def _return_is_inside_function_body(source: str, pos: int) -> bool:
    """True iff ``pos`` (a code-level ``return`` statement start) lies inside ANY
    open ``function``…``end`` body at module scope — i.e. it is method-local, not
    the module-level trailing return.

    Tracks Luau block depth from the file start to ``pos`` using ``_BLOCK_TOKEN_RE``
    (the same token grammar ``_self_is_shadowed_at`` walks): ``function``/``do``/
    ``then``/``repeat`` open, ``end``/``until`` close. Only ``function`` openings
    that are still unclosed at ``pos`` make the position function-local — but since
    a bare ``do``/``then`` block can only appear INSIDE a function in transpiled
    module bodies, any non-zero open depth at a module-epilogue return means the
    return is not at module scope. We track a dedicated function-nesting counter so
    a module-level ``do … end`` block (rare) does not misclassify.

    ``elseif`` is the load-bearing edge (round-1 harden regress): an
    ``if a then … elseif b then … end`` chain has MULTIPLE ``then`` openers but ONE
    ``end``. Each ``elseif``'s own upcoming ``then`` over-counts ``block_depth``, so
    ``elseif`` DECREMENTS to cancel it (net 0 for the whole chain) — the same grammar
    ``_FALLBACK_BLOCK_CLOSERS`` uses. Without this, any method containing an
    if/elseif chain leaves ``block_depth`` permanently open, so a genuine
    module-trailing ``return <Class>`` after it is misclassified as function-local
    and the resolver is never injected. ``elseif`` never closes a ``function`` body,
    so it does not touch ``fn_depth``."""
    fn_depth = 0  # count of open ``function`` bodies (the load-bearing nesting)
    block_depth = 0  # all block openers (function/do/then/repeat), to pair ``end``
    fn_open_at_block_depth: list[int] = []  # block_depth at each open ``function``
    i = 0
    while i < pos:
        if not _luau_pos_is_code(source, i) or _luau_pos_in_long_bracket(source, i):
            i += 1
            continue
        tok = _BLOCK_TOKEN_RE.match(source, i)
        if tok is None:
            i += 1
            continue
        word = tok.group(1)
        if word == "function":
            block_depth += 1
            fn_open_at_block_depth.append(block_depth)
            fn_depth += 1
            i = tok.end()
            continue
        if word in ("do", "then", "repeat"):
            block_depth += 1
            i = tok.end()
            continue
        if word == "elseif":
            # Cancels its own upcoming ``then`` increment (net 0 for the if/elseif
            # chain). NEVER closes a ``function`` body, so leave ``fn_depth`` alone.
            block_depth -= 1
            i = tok.end()
            continue
        if word in ("end", "until"):
            if fn_open_at_block_depth and fn_open_at_block_depth[-1] == block_depth:
                fn_open_at_block_depth.pop()
                fn_depth -= 1
            block_depth -= 1
            i = tok.end()
            continue
        i = tok.end()
    return fn_depth > 0


def _resolver_method_text(
    class_name: str, child: str, field: str, suffix: str
) -> str:
    """The per-instance memoized resolver METHOD — a REAL rig-child Instance (or
    nil), bounded 30x0.1s retry, memoized on ``self._<field>Cache``. NO proxy,
    NO module-level state. Ported from the legacy WeaponMount pack.

    ``suffix`` is the VALID-LUAU-IDENTIFIER method-name suffix (``_resolve<suffix>``);
    the rig LOOKUP uses the REAL ``child`` string in ``FindFirstChild``. The body
    carries the lowering's OWN-EMIT marker (``-- _RIG_RETARGET_<suffix>``) and the
    distinctive ``_MainCameraRig`` rig scan so discharge can bind to the lowering's
    own emit, not a preexisting foreign method of the same name."""
    cache = f"_{field}Cache"
    return (
        f"-- _RIG_RETARGET_{suffix} (auto-generated: Camera.main child-ref retargeted to the rig; per-instance, lazy-at-use)\n"
        f"function {class_name}:_resolve{suffix}()\n"
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
    source: str, class_name: str, child: str, field: str, suffix: str
) -> tuple[str, bool]:
    """Inject the resolver method BEFORE the trailing ``return <Class>`` (so the
    module stays loadable), then a Luau syntax re-check. Returns
    ``(new_source, injected)``. Idempotency: if the LOWERING'S OWN resolver method
    is already present at a code position, return ``(source, False)``. A preexisting
    FOREIGN method of the same name (NOT the lowering's own emit) does NOT count as
    already-injected — it is re-injected (the OWN-emit body is the authority, not
    bare name presence). On a syntax-check failure, abstain (return the pre-edit
    source, False)."""
    # Idempotency: guard on the lowering's OWN injected method's presence at a code
    # position (STRUCTURAL equality to the canonical emit), NOT a bare same-named
    # declaration NOR a marker substring (round-5 BLOCKING: a foreign
    # ``_resolve<Child>`` whose body merely uses ``m:GetAttribute("_MainCameraRig")``
    # must not be treated as already-injected).
    method_text = _resolver_method_text(class_name, child, field, suffix)
    if _has_own_resolver_method(source, suffix, method_text):
        return source, False  # the lowering's own method already present -> no re-inject

    span = _last_module_return_span(source, class_name)
    if span is None:
        return source, False  # no module epilogue to splice before -> abstain

    ins_at = span[0]
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
        # Shadowed-``self`` guard: abstain if ``self`` here is a local/param shadow
        # (a closure ``function(self)`` or a ``local self``), NOT the colon-receiver.
        if _self_is_shadowed_at(source, start):
            continue  # shadowed self -> wrong object -> abstain
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


# Block keywords that OPEN a lexical scope and are closed by ``end``. ``if``/
# ``elseif``/``else``/``while``/``for`` headers and ``do``/``function`` bodies all
# nest under one ``end``; ``repeat`` closes with ``until`` (handled separately).
_BLOCK_OPEN_RE = re.compile(
    r"\b(function|do|then|repeat)\b"
)
_BLOCK_TOKEN_RE = re.compile(
    r"\b(function|if|for|while|do|then|repeat|until|end|local|elseif)\b"
)
# A ``function`` whose parameter list contains a bare ``self`` parameter — a
# nested closure that SHADOWS the method receiver (``function(self)`` /
# ``function foo(self, x)``). The optional name between ``function`` and ``(`` is
# captured (group 1) so a ``local function self()`` (whose NAME is ``self``) is
# also recognized as a shadow of the enclosing scope.
_FUNCTION_SELF_PARAM_RE = re.compile(
    r"\bfunction\b\s*([A-Za-z_][\w.:]*)?\s*\(\s*([^)]*)\)"
)
# The loop-variable list of a ``for`` header — everything between ``for`` and the
# terminating ``in`` (generic ``for a, b in xs``) or ``=`` (numeric ``for i = ...``).
# Group 1 is the comma-separated variable list; a ``self`` among them shadows the
# receiver inside the loop BODY (which opens at the header's ``do``).
_FOR_LOOPVARS_RE = re.compile(
    r"\bfor\b\s*([A-Za-z_][\w,\s]*?)\s*(?:\bin\b|=)"
)


def _self_is_shadowed_at(source: str, pos: int) -> bool:
    """True if the ``self`` token at ``pos`` resolves to a SHADOWED binding that is
    NOT the enclosing ``function <Class>:<method>()`` colon-receiver — in scope at
    ``pos``. Covers ALL Luau binding forms that introduce a ``self`` (codex round-3
    BLOCKING): ``local self``, a function PARAMETER named ``self``
    (``function(self)`` / ``function foo(self, x)``), a function NAMED ``self``
    (``local function self()`` / ``function self()``), and a ``for``-loop VARIABLE
    named ``self`` (``for self in`` / ``for _, self in`` / ``for self = ...``).

    Walks the lexical block structure outward from the nearest enclosing
    colon-method declaration to ``pos`` (code-position-aware), tracking block
    depth. A shadowing ``self`` introduced at some depth shadows the read iff that
    depth is still OPEN at ``pos``. The colon-method's implicit ``self`` is the
    real receiver and never counts as a shadow."""
    # The enclosing colon-method's body start (its receiver is the real ``self``).
    method_body_start = 0
    for m in _FUNCTION_METHOD_RE.finditer(source):
        if m.start() >= pos:
            break
        if not _luau_pos_is_code(source, m.start()):
            continue
        if _luau_pos_in_long_bracket(source, m.start()):
            continue
        method_body_start = m.end()  # after the ``(`` of the colon-method header

    # Scan from the colon-method body to ``pos``, tracking the lexical block depth
    # and the depths at which a shadowing ``self`` was introduced. ``depth`` 0 is
    # the colon-method body itself (its ``self`` is the receiver).
    depth = 0
    shadow_depths: list[int] = []  # depths whose block introduced a ``self`` shadow
    # A ``for`` header binding ``self`` introduces the shadow in the loop BODY,
    # which opens at the header's ``do`` (depth+1). When such a header is seen,
    # arm this flag so the NEXT ``do`` registers the shadow at the new depth.
    for_self_pending = False
    i = method_body_start
    n = len(source)
    while i < pos:
        if not _luau_pos_is_code(source, i) or _luau_pos_in_long_bracket(source, i):
            i += 1
            continue
        tok = _BLOCK_TOKEN_RE.match(source, i)
        if tok is None:
            # A ``local self`` declaration shadows the receiver in the CURRENT block.
            decl = re.match(r"local\s+self\b", source[i:])
            if decl is not None and _luau_pos_is_code(source, i):
                shadow_depths.append(depth)
            i += 1
            continue
        word = tok.group(1)
        if word == "function":
            # A ``function`` introduces TWO possible shadows:
            #   - its NAME is ``self`` (``local function self()`` / ``function self()``)
            #     -> shadows the ENCLOSING scope (current ``depth``); and/or
            #   - it declares a ``self`` PARAMETER (``function(self)``) -> shadows
            #     its own BODY (depth+1).
            fm = _FUNCTION_SELF_PARAM_RE.match(source, i)
            fn_name = (fm.group(1) or "") if fm else ""
            params = (fm.group(2) or "") if fm else ""
            if fn_name == "self":
                shadow_depths.append(depth)  # the function NAME shadows here
            depth += 1
            has_self_param = any(p.strip() == "self" for p in params.split(","))
            if has_self_param:
                shadow_depths.append(depth)  # the parameter shadows the body
            i = (fm.end() if fm else tok.end())
            continue
        if word == "do":
            depth += 1
            if for_self_pending:
                shadow_depths.append(depth)  # the for-loop var shadows the body
                for_self_pending = False
            i = tok.end()
            continue
        if word in ("then", "repeat"):
            depth += 1
            i = tok.end()
            continue
        if word in ("end", "until"):
            shadow_depths[:] = [d for d in shadow_depths if d < depth]
            depth -= 1
            i = tok.end()
            continue
        if word == "local":
            decl = re.match(r"local\s+self\b", source[i:])
            if decl is not None:
                shadow_depths.append(depth)
            i = tok.end()
            continue
        if word == "for":
            # Does the loop header bind a variable named ``self``? If so, arm the
            # pending flag so the body (opened at the next ``do``) is a shadow.
            fm = _FOR_LOOPVARS_RE.match(source, i)
            if fm is not None and any(
                v.strip() == "self" for v in fm.group(1).split(",")
            ):
                for_self_pending = True
            i = tok.end()
            continue
        # ``if``/``while``/``elseif`` headers don't open the block until their
        # ``do``/``then``; skip the keyword and continue.
        i = tok.end()
    # ``self`` at ``pos`` is shadowed iff a shadow was introduced at a depth still
    # open here.
    return any(d <= depth for d in shadow_depths)


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


# A WHOLE-RHS camera-child value: optional ``<guard> and`` nil-guards (the corpus
# ``self.cam and self.cam:GetChildren()[1]``) then the ordinal child access
# ``<recv>:GetChildren()[n]`` / ``<recv>:GetChild(n)`` / ``<recv>.GetChild(n)``, to
# end of value. Group 1 captures the whole guard prefix, group 2 the RECEIVER chain.
# Anchored ``^...$`` against the STRIPPED RHS so the access must BE the value, not
# merely appear inside a mixed expression — a trailing ``or <other>`` / any extra
# operand fails to match (codex round-5 R2). The guard prefix is then required to be
# ONLY the SAME camera receiver (codex round-5 R3): a foreign guard
# ``self.defaultSlots and self.cam:GetChild(0)`` makes the value conditional on a
# NON-camera (``nil`` when ``defaultSlots`` is falsy), so it must NOT be neutralized.
_CAMERA_CHILD_VALUE_RE = re.compile(
    r"^((?:[A-Za-z_][\w.]*\s+and\s+)*)"
    r"([A-Za-z_][\w.]*)"
    r"(?::GetChildren\(\)\s*\[\s*\d+\s*\]|[:.]GetChild\(\s*\d+\s*\))$"
)

# The canonical Luau main-camera receiver literals a transpiled ``Camera.main`` /
# ``Camera.main.transform`` resolves to (``api_mappings``: ``Camera.main`` ->
# ``workspace.CurrentCamera``). Case-insensitive ``workspace``/``Workspace``.
_CANONICAL_CAMERA_RECEIVERS: frozenset[str] = frozenset({
    "workspace.CurrentCamera",
    "Workspace.CurrentCamera",
    "game.Workspace.CurrentCamera",
})


def _camera_symbol_forms(cam_receiver: str) -> frozenset[str]:
    """The Luau receiver forms a SEEDED C# camera symbol (``cam``) transpiles to —
    ``self.cam`` and ``cam``. Used ONLY by the Tier-2 best-effort neutralize to
    OPPORTUNISTICALLY recognize the init-write to clean; the absence of a match is a
    SKIP (Path A: neutralize is decoupled from discharge, so a mis-recognition never
    affects ``present``). ``cam_receiver`` empty or dotted (``Camera.main.transform``,
    the direct form) contributes no symbol forms — the canonical camera literals
    suffice."""
    recv = cam_receiver.strip()
    if recv and recv != "Camera.main.transform" and "." not in recv:
        return frozenset({f"self.{recv}", recv})
    return frozenset()


def _rhs_is_camera_child(
    source: str, rhs_abs_start: int, rhs_abs_end: int, cam_symbols: frozenset[str]
) -> bool:
    """True iff the WHOLE RHS value ``source[rhs_abs_start:rhs_abs_end]`` IS a
    camera-rooted ordinal child access — ``(<cam> and )* <camrecv>:GetChildren()[n]``
    (the corpus nil-guard shape), nothing else — whose ``<camrecv>`` is a canonical
    camera literal OR a recorded camera-symbol form (``self.cam``/``cam``).

    PATH A: this is the Tier-2 best-effort neutralize's recognizer ONLY — it is NOT
    a discharge gate. The whole-RHS / same-guard checks stay (they keep the neutralize
    from clobbering an unrelated mixed expression), but the round-5 symbol-binding
    DOMINANCE proof is dropped: a mis-neutralize no longer affects ``present`` (it is
    caught by the final syntax gate if it corrupts the module, and is otherwise dead
    data since no read survives), so the heavy proof is not load-bearing."""
    raw = source[rhs_abs_start:rhs_abs_end]
    m = _CAMERA_CHILD_VALUE_RE.match(raw.strip())
    if m is None:
        return False
    guards = [g.strip() for g in m.group(1).split(" and ") if g.strip()]
    recv = m.group(2)
    # Every ``and`` guard must be the SAME token as the receiver (the corpus nil-guard
    # ``self.cam and self.cam:...``); a foreign guard makes the value non-camera-
    # conditional -> SKIP.
    if any(g != recv for g in guards):
        return False
    return recv in _CANONICAL_CAMERA_RECEIVERS or recv in cam_symbols


def _neutralized_marker(suffix: str) -> str:
    """The lowering's OWN deterministic neutralize comment for ``_resolve<suffix>``,
    emitted on the best-effort-neutralized camera-child write
    (``self.<field> = nil <marker>``). Cosmetic/diagnostic only under Path A — the
    neutralize is Tier-2 hygiene and does NOT gate discharge."""
    return f" -- rig-retargeted: resolved lazily at use via _resolve{suffix}"


def _neutralize_assignment(
    source: str, field: str, suffix: str, cam_symbols: frozenset[str]
) -> tuple[str, bool]:
    """Tier-2 BEST-EFFORT HYGIENE (Path A, SKIP-on-ambiguity): replace the RHS of the
    UNIQUE dominating camera-child init-write ``self.<field> = <camera-ordinal>`` with
    ``nil``, recognized via ``_rhs_is_camera_child`` (whole-RHS camera-rooted ordinal
    access on a canonical camera literal or a recorded camera-symbol form).
    Multiline-aware.

    SKIP-ON-AMBIGUITY (D-P1-PATHA.tier2): the init-write is only safe to neutralize
    when it is UNIQUELY identifiable. If there is MORE THAN ONE code-position
    ``self.<field> = ...`` write anywhere in the module, the "init" write can no
    longer be uniquely distinguished from a later (possibly legitimate, non-init)
    write — so the lowering SKIPS, neutralizing NOTHING (a stray neutralize would
    clobber the later write). Only when there is EXACTLY ONE such write, and it is the
    unambiguous camera-child shape, is it neutralized.

    DECOUPLED from discharge (Path A): the read reroute already left no raw read, so
    a leftover/collapsed write (``self.<field> = self.gameObject`` / ``__unityChild(...)``)
    is dead data — on any shape this recognizer does not match (or on ambiguity), the
    lowering SKIPS (no-op, NOT a failure, does NOT affect ``present``). ``suffix`` is
    the VALID-LUAU-IDENTIFIER method-name suffix (only used in the rig-retarget
    comment). Returns ``(new_source, neutralized)``."""
    assign_re = re.compile(r"self\." + re.escape(field) + r"\s*=(?!=)")
    writes = [
        m
        for m in assign_re.finditer(source)
        if _luau_pos_is_code(source, m.start())
        and not _luau_pos_in_long_bracket(source, m.start())
    ]
    # SKIP-on-ambiguity: a unique dominating init-write must be provable. Multiple
    # same-field writes -> the init one can't be uniquely identified -> neutralize
    # nothing (never clobber a later legitimate write).
    if len(writes) != 1:
        return source, False
    m = writes[0]
    rhs_start = m.end()
    rhs_end = _statement_rhs_end(source, rhs_start)
    if not _rhs_is_camera_child(source, rhs_start, rhs_end, cam_symbols):
        return source, False  # not a clear camera-rooted child write -> SKIP
    new_source = (
        source[:rhs_start] + " nil" + _neutralized_marker(suffix) + source[rhs_end:]
    )
    return new_source, True


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


def _binding_discharged(
    source: str, field: str, child: str, suffix: str
) -> bool:
    """INDEPENDENT, code-position-aware derivation: is ``field``'s binding
    discharged via the rig retarget in THIS source? Mirrors the verifier's
    authority (S1b owns the verifier copy; this is the lowering's own re-derive so
    it never stamps ``present=True`` off a reverted edit).

    BEST-EFFORT HINT, NOT THE AUTHORITY (phase-integration FINDING 2): this
    predicate (and its ``_has_unrewritable_boundary_read`` helper) is a BEST-EFFORT
    discharge HINT. The slice-1.2 verifier's ``_rig_binding_discharged`` — run on the
    FINAL output — is the SOLE discharge authority (design §1.6 / FIX 1). The two are
    independent text-scanners and can DESYNC on a few boundary forms (``self[<int>]``,
    a shadowed-``self`` dot-read, a concatenated bracket key ``self["weaponSlot".."" ]``)
    where this predicate is LENIENT (returns discharged) but the verifier FIRES. That
    desync is FAIL-CLOSED-SAFE BY DESIGN: a lowering-lenient / verifier-strict
    disagreement means the verifier fires and the binding fails closed (never silent-
    wrong). The UNSAFE direction — both discharged while a real read survives
    (mutual-mask) — does NOT occur: see ``test_f2_no_mutual_mask_lowering_at_least_as_
    lenient`` for the no-mutual-mask safety property. The residual desync forms are all
    NON-REACHABLE from the deterministic dot-form transpiler, so per-form parity
    between the two scanners is INTENTIONALLY NOT pursued (it would be brittle two-
    scanner mirroring); ``_has_unrewritable_boundary_read`` stays as the best-effort
    hint that reduces cosmetic edited+loud cases on common forms.

    ``child`` is the REAL rig-child name (for reconstructing the canonical emit);
    ``suffix`` is the VALID-LUAU-IDENTIFIER method-name suffix (``_resolve<suffix>``).

    PATH A re-anchor — discharge keys on the consumer-READ reroute (the AI-STABLE
    member access), NOT on the AI-VOLATILE write/ordinal shape. True IFF:
      (1a) the LOWERING'S OWN resolver method ``function <Class>:_resolve<suffix>(``
           (STRUCTURALLY equal to the canonical emit, NOT a preexisting foreign
           same-named method) exists; AND
      (1b) >=1 ``self:_resolve<suffix>(`` call exists; AND
      (1c) NO bare ``self.<field>`` dot-form READ survives at a consumer (outside
           the non-yielding lifecycle methods Awake/Start).

    The init-WRITE neutralize is NO LONGER a discharge condition (Path A re-anchor,
    supersedes the round-5 coupling): it is Tier-2 best-effort hygiene
    (SKIP-on-ambiguity), harmless because (1c) leaves no raw read to read the stale
    write. So a script whose neutralize was SKIPPED (the AI collapsed the RHS to
    ``self.gameObject`` / ``__unityChild(...)`` etc.) still discharges True once the
    reads are rerouted — fixing the D-S2-REDESIGN abstain on the real 5 write shapes.

    Condition (1a) still requires STRUCTURAL equality to the lowering's OWN canonical
    emit, so a preexisting foreign ``_resolve<suffix>`` method that merely uses the
    ``_MainCameraRig`` marker cannot false-discharge on a source the lowering never
    wrote this run."""
    method_call = f"self:_resolve{suffix}("
    # (1a) the lowering's OWN resolver method is present at a code position
    # (structural equality to the canonical emit). The class name is read from the
    # final source so the expected emit is reconstructed exactly.
    class_name = _read_class_name(source)
    if class_name is None:
        return False
    expected = _resolver_method_text(class_name, child, field, suffix)
    if not _has_own_resolver_method(source, suffix, expected):
        return False
    # (1b) >=1 ``self:_resolve<suffix>(`` CALL (distinct from the declaration).
    if not _code_contains_token(source, method_call):
        return False
    # (1c) no surviving bare ``self.<field>`` READ (an assignment LHS is allowed —
    # a leftover/neutralized ``self.<field> = ...`` write keeps the field a member).
    if _has_surviving_field_read(source, field):
        return False
    # (1d) ABSTAIN on any BOUNDARY-FORM read the reroute cannot rewrite and the
    # slice-1.2 verifier fails closed on (bracket/dynamic ``self[expr]`` / non-``self``
    # receiver / shadowed-``self`` / non-yielding-lifecycle raw read). Mirroring the
    # verifier's reject set here keeps the lowering's discharge in lock-step with the
    # verifier (phase-integration MAJOR #2): a boundary-form script abstains False on
    # BOTH, instead of the lowering stamping present=True on a verifier-fire case.
    if _has_unrewritable_boundary_read(source, field):
        return False
    return True


def _code_contains_token(source: str, token: str) -> bool:
    idx = source.find(token)
    while idx != -1:
        if _luau_pos_is_code(source, idx) and not _luau_pos_in_long_bracket(source, idx):
            return True
        idx = source.find(token, idx + 1)
    return False


def _normalize_lines(text: str) -> list[str]:
    """The non-blank lines of ``text``, each stripped of surrounding whitespace.
    Used for STRUCTURAL equality that tolerates indentation / blank-line drift
    while still requiring the FULL canonical body, line for line."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def _has_own_resolver_method(
    source: str, suffix: str, expected_method_text: str
) -> bool:
    """True if the LOWERING'S OWN ``function <Class>:_resolve<suffix>(`` method is
    present at a code position — identified by STRUCTURAL EQUALITY of the emitted
    method (from its ``function`` declaration through its closing ``end``) to the
    EXACT canonical emit (``expected_method_text``), NOT a marker substring (round-5
    BLOCKING: a FOREIGN same-named method whose body merely CONTAINS
    ``m:GetAttribute("_MainCameraRig")`` as live code must NOT count as the
    lowering's own emit).

    Comparison is line-normalized (each non-blank line stripped) over the
    function-declaration-through-closing-``end`` portion of the canonical emit
    (its leading rig-retarget COMMENT is cosmetic and excluded). A preexisting
    foreign method with a different body returns False; the lowering's own
    byte-identical prior output (idempotent re-run) returns True because its
    normalized body equals the canonical emit line for line."""
    # The load-bearing portion of the canonical emit: from the ``function`` line
    # through its closing ``end`` (drop the leading cosmetic comment + trailing
    # blank lines). These are the lines that must match at a candidate site.
    expected_lines = _normalize_lines(expected_method_text)
    try:
        fn_idx = next(
            i for i, ln in enumerate(expected_lines) if ln.startswith("function ")
        )
    except StopIteration:
        return False  # malformed canonical emit -> never claim own-emit
    expected_body = expected_lines[fn_idx:]
    n_lines = len(expected_body)

    pat = re.compile(
        r"\bfunction\s+[A-Za-z_]\w*[:.]_resolve" + re.escape(suffix) + r"\s*\("
    )
    for m in pat.finditer(source):
        if not _luau_pos_is_code(source, m.start()):
            continue
        if _luau_pos_in_long_bracket(source, m.start()):
            continue
        # Take the same number of normalized non-blank lines from the declaration
        # onward and compare line for line to the canonical function body.
        tail = _normalize_lines(source[m.start():])
        if tail[:n_lines] == expected_body:
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
        if _self_is_shadowed_at(source, start):
            continue  # shadowed self -> foreign object, not this script's consumer
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


def _has_unrewritable_boundary_read(source: str, field: str) -> bool:
    """True iff a BOUNDARY-FORM read of ``field`` survives — a form the consumer-read
    reroute CANNOT safely rewrite and which the slice-1.2 verifier's
    ``_rig_binding_discharged`` FAILS CLOSED on. The lowering MUST mirror these so it
    ABSTAINS (``present=False``, source unedited for the reroute) on exactly the
    scripts the verifier will reject — never stamp ``present=True`` on a verifier-fire
    case (the desync this guards, phase-integration MAJOR #2). The forms (design §1.6,
    lines 403-412):
      (a) a bracket-index read ``self["<field>"]`` (the string-key member access);
      (b) a DYNAMIC bracket read ``self[<expr>]`` (incl. a parenthesized ``self[(...)]``)
          whose key is not a pure integer array index — the key cannot be statically
          tied to (or ruled out as) the field, so it is a boundary form;
      (c) a NON-``self`` receiver read of the field — module-table ``<Class>.<field>``,
          ``owner.<field>``, a receiver-alias ``local p = self; p.<field>`` — any
          ``<ident>.<field>`` whose receiver is not the bare ``self`` token;
      (d) a raw ``self.<field>`` dot-form read inside a NON-yielding lifecycle method
          (``Awake``/``Start``) — a stale-derived-state hazard the verifier flags
          (design §1.6, supersedes the old "Awake read sees a safe nil" premise).

    A SHADOWED-``self`` read (``function(self) ... self.<field>`` / ``local self = ...;
    self.<field>``) is NOT a boundary form: ``self`` there is a FOREIGN object, so the
    read is not a read of THIS script's field — the verifier mirrors the lowering's
    shadow guard and treats it as neutral (does not block discharge), matching
    ``_has_surviving_field_read``. The bare ``self.<field>`` dot-form read in a
    YIELD-SAFE method is likewise NOT a boundary form: the reroute rewrites it (and
    ``_has_surviving_field_read`` already counts a leftover one). This detector covers
    ONLY the forms the reroute leaves un-rewritten AND the verifier rejects, so the
    two AGREE on discharge: a clean dot-form script discharges True on both; a
    boundary-form script abstains False on both."""
    # (a)/(b) bracket-index reads ``self[ ... ]`` (string key or dynamic expr).
    for m in re.finditer(r"self\s*\[", source):
        start = m.start()
        if not _luau_pos_is_code(source, start):
            continue
        if _luau_pos_in_long_bracket(source, start):
            continue
        # ``self`` must be a bare token (not ``x.self[...]``).
        j = start - 1
        while j >= 0 and source[j] in " \t":
            j -= 1
        if j >= 0 and source[j] == ".":
            continue
        if _self_is_shadowed_at(source, start):
            continue  # a shadowed self[...] is a different object -> not this field
        key = _bracket_key_text(source, m.end() - 1)
        if key is None:
            continue  # unbalanced/uncertain -> leave to syntax/other gates
        stripped = key.strip()
        # A string-literal key: boundary iff it is THIS field (``self["weaponSlot"]``).
        sm = re.fullmatch(r"""(['"])(.*)\1""", stripped, re.DOTALL)
        if sm is not None:
            if sm.group(2) == field:
                return True  # (a) bracket-index read of the field
            continue  # a different string key -> unrelated member
        # A pure integer key (``self[1]``) is array indexing, not a field read.
        if re.fullmatch(r"\d+", stripped):
            continue
        # Anything else is a DYNAMIC key (a symbol / call / parenthesized expr) the
        # reroute cannot tie to or rule out as the field -> boundary form (b).
        return True
    # (c) a NON-``self`` receiver read ``<ident>.<field>`` (module-table / owner /
    # alias). Excludes ``self.<field>`` (handled by the dot-form reroute) and a
    # member-tail ``x.<recv>.<field>`` recv that is itself ``self`` (``x.self.field``
    # is already not a self read).
    for m in re.finditer(r"([A-Za-z_]\w*)\s*\.\s*" + re.escape(field) + r"\b", source):
        start = m.start()
        if not _luau_pos_is_code(source, start):
            continue
        if _luau_pos_in_long_bracket(source, start):
            continue
        recv = m.group(1)
        if recv == "self":
            continue  # bare self.<field> -> the reroute's domain, handled elsewhere
        # Not an assignment LHS (a write to a foreign field is not a READ we must
        # reroute; the verifier's discharge gate is about surviving READS).
        a = m.end()
        while a < len(source) and source[a] in " \t":
            a += 1
        if a < len(source) and source[a] == "=" and not (
            a + 1 < len(source) and source[a + 1] == "="
        ):
            continue
        return True  # (c) non-self receiver read of the field
    # (d) a raw ``self.<field>`` dot-form read inside a non-yielding lifecycle method
    # (``Awake``/``Start``). A SHADOWED-self read is skipped (foreign object, neutral).
    for m in re.finditer(r"self\." + re.escape(field) + r"\b", source):
        start = m.start()
        if not _luau_pos_is_code(source, start):
            continue
        if _luau_pos_in_long_bracket(source, start):
            continue
        j = start - 1
        while j >= 0 and source[j] in " \t":
            j -= 1
        if j >= 0 and source[j] == ".":
            continue  # x.self.<field> -> not a bare self read
        a = m.end()
        while a < len(source) and source[a] in " \t":
            a += 1
        if a < len(source) and source[a] == "=" and not (
            a + 1 < len(source) and source[a + 1] == "="
        ):
            continue  # assignment LHS -> not a read
        if _self_is_shadowed_at(source, start):
            continue  # shadowed self -> foreign object, neutral (mirror the guard)
        if _enclosing_method(source, start) in _NON_YIELDING_LIFECYCLE_METHODS:
            return True  # (d) raw read in a non-yielding lifecycle method
    return False


def _bracket_key_text(source: str, open_idx: int) -> str | None:
    """The text between a balanced ``[`` at ``open_idx`` and its matching ``]``,
    bracket/string-aware at code positions. None if unbalanced/uncertain."""
    depth = 0
    i = open_idx
    n = len(source)
    while i < n:
        ch = source[i]
        if ch in ("'", '"') and _luau_pos_is_code(source, i):
            quote = ch
            i += 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if _luau_pos_is_code(source, i):
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0 and ch == "]":
                    return source[open_idx + 1:i]
                if depth < 0:
                    return None
        i += 1
    return None


def _luau_syntax_ok(source: str) -> bool:
    """Luau loadability check on the lowered source. Uses ``luau-analyze`` (the
    project's authoritative checker, the same one the transpiler runs) when the
    binary is installed; otherwise a conservative structural check that validates
    bracket balance, block-keyword (``function``/``do``/``then``/``repeat`` vs
    ``end``/``until``) balance, AND the module-epilogue invariant — fail-closing on
    any construct it cannot confidently validate (codex round-3 BLOCKING: the
    analyzer-absent path must be at least as strict about fail-closing as the
    analyzer path, so a broken single-line-``if`` neutralize cannot stamp
    present=True)."""
    from utils.luau_analyze import luau_analyze_path, syntax_errors_for_source
    if luau_analyze_path():
        return not syntax_errors_for_source(source)
    return _structural_balance_ok(source)


# Block keywords for the analyzer-absent fallback. Each opener adds one scope
# closed by ``end`` (or ``until`` for ``repeat``). ``if``/``while``/``for`` headers
# do NOT open the block themselves — their ``then``/``do`` does — so they are NOT
# openers here (counting them too would double-count). A bare ``then``/``do``
# keyword is the opener we count.
#
# ``elseif`` is special: an ``if a then ... elseif b then ... end`` has TWO
# ``then`` openers but ONE ``end``. The ``elseif`` itself is a +0 continuation, but
# its own upcoming ``then`` would over-count, so ``elseif`` DECREMENTS to cancel
# that ``then``'s increment (net 0 for the whole chain). ``else`` is a pure +0
# continuation (no ``then`` follows it).
_FALLBACK_BLOCK_OPENERS: frozenset[str] = frozenset({"function", "do", "then", "repeat"})
_FALLBACK_BLOCK_CLOSERS: frozenset[str] = frozenset({"end", "until", "elseif"})
_FALLBACK_BLOCK_TOKEN_RE = re.compile(
    r"\b(function|do|then|repeat|end|until|elseif)\b"
)


def _structural_balance_ok(source: str) -> bool:
    """Conservative, analyzer-absent loadability proxy. Validates, at code
    positions only:
      (1) bracket balance — ``(`` ``[`` ``{`` vs their closers, never negative;
      (2) block-keyword balance — ``function``/``do``/``then``/``repeat`` openers
          vs ``end``/``until`` closers, never negative, net zero (catches a
          single-line-``if`` whose ``end`` was swallowed by a bad RHS span — the
          ``then`` opener is left unclosed -> positive net -> FAIL); and
      (3) the module epilogue ``return <Ident>`` is the LAST code statement (no
          code-level ``function``/``end`` follows it — the after-``return`` splice
          bug).
    Returns False (fail-closed) on any imbalance. A genuinely ambiguous construct
    surfaces as an imbalance here and fail-closes, never a silent pass."""
    depth = 0  # bracket nesting
    block = 0  # block-keyword nesting
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        # Skip Luau long-bracket COMMENTS and STRINGS wholesale (round-5 MAJOR):
        # the per-char ``_luau_pos_in_long_bracket`` guard reports False at the
        # SECOND ``]`` of a closing ``]=*]`` (it sits just past the close), so that
        # ``]`` leaked as a code-level bracket closer -> a ``--[[ ... ]]`` block
        # comment containing brackets false-rejected valid Luau. Advancing past the
        # ENTIRE span (opener through closer) makes neither delimiter char count.
        if ch == "-" and i + 1 < n and source[i + 1] == "-":
            j = i + 2
            level = _long_bracket_open_level(source, j)
            if level is not None:
                close = source.find("]" + "=" * level + "]", j)
                if close == -1:
                    return False  # UNTERMINATED block comment -> invalid Luau (codex)
                i = close + level + 2
                continue
            nl = source.find("\n", j)  # plain ``--`` line comment
            i = n if nl == -1 else nl + 1
            continue
        # A long-bracket STRING ``[=*[ ... ]=*]`` (not preceded by ``--``).
        if ch == "[":
            level = _long_bracket_open_level(source, i)
            if level is not None:
                close = source.find("]" + "=" * level + "]", i + level + 2)
                if close == -1:
                    return False  # UNTERMINATED long string -> invalid Luau (codex)
                i = close + level + 2
                continue
        # A short quoted string -- skip it so its bracket/keyword chars don't count.
        if ch in ("'", '"'):
            quote = ch
            i += 1
            closed = False
            while i < n:
                c = source[i]
                if c == "\\":
                    i += 2
                    continue
                if c == "\n":
                    return False  # short string spans a newline -> invalid Luau (codex)
                if c == quote:
                    closed = True
                    i += 1
                    break
                i += 1
            if not closed:
                return False  # UNTERMINATED short string -> invalid Luau (codex)
            continue
        if ch in "([{":
            depth += 1
            i += 1
            continue
        if ch in ")]}":
            depth -= 1
            if depth < 0:
                return False
            i += 1
            continue
        tok = _FALLBACK_BLOCK_TOKEN_RE.match(source, i)
        if tok is not None:
            word = tok.group(1)
            if word in _FALLBACK_BLOCK_OPENERS:
                block += 1
            elif word in _FALLBACK_BLOCK_CLOSERS:
                block -= 1
                if block < 0:
                    return False
            i = tok.end()
            continue
        i += 1
    if depth != 0 or block != 0:
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
        # CODE-POSITION-AWARE tail scan (round-4 MINOR): a trailing COMMENT after
        # the module return (``return Player -- ends the function``) contains the
        # words ``end``/``function`` as prose, NOT as code. A raw-text scan would
        # false-reject it. Only a code-level ``function``/``end`` keyword after the
        # return is the after-``return`` splice bug.
        tail_re = re.compile(r"\bfunction\b|\bend\b")
        for m in tail_re.finditer(source, last_return_end):
            if _luau_pos_is_code(source, m.start()) and not _luau_pos_in_long_bracket(source, m.start()):
                return False
    return True
