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

import hashlib
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
        # >1 rig fact on ONE script: the single-dict carrier (§1.5) can represent
        # exactly one binding, and the design (§1.5, edge 9) frames multi-fact as a
        # two-SCRIPTS case (the corpus is one-fact-per-script). Rather than silently
        # keep only the LAST fact's carrier (dropping the earlier bindings'
        # discharge from the verifier), FAIL CLOSED: stamp a single overflow
        # carrier (present=False, multi_fact=True) and ABSTAIN on all edits, so the
        # verifier fail-closes LOUD instead of shipping an unverifiable binding.
        if len(rig_facts) > 1:
            first = rig_facts[0]
            script.rig_binding = {
                "field": first.field_name,
                "child": first.child_name,
                "present": False,
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
                    new_src, _neutralized = _neutralize_assignment(
                        new_src, field, suffix
                    )
                    # Re-check Luau syntax on the FINAL source AFTER ALL rewrites
                    # (inject + read-rewrite + neutralize) — a read-rewrite or a
                    # neutralize can corrupt the module (e.g. drop a closing
                    # ``end``) AFTER the post-inject check, and ``_binding_discharged``
                    # is shape-only, so it would stamp present=True on broken Luau.
                    # If the FINAL source fails to parse, abstain/revert.
                    if not _luau_syntax_ok(new_src):
                        script.luau_source = original  # never ship unloadable Luau
                    elif _binding_discharged(new_src, field, suffix):
                        script.luau_source = new_src
                        changed = True
                    else:
                        # Discharge could not be confirmed on the final source
                        # (e.g. reads/write the lowering couldn't anchor). Abstain:
                        # leave the script unedited so the verifier sees the
                        # un-discharged binding.
                        script.luau_source = original
            # Re-derive discharge from the FINAL committed source (independent of
            # the in-flight locals): True only when the resolver method + rewritten
            # reads + neutralized write actually landed. This re-stamps identically
            # on an idempotent second call (the method is already present) and
            # never stamps True off a reverted edit.
            present = _binding_discharged(script.luau_source, field, suffix)
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
    # position (own-emit body match), NOT a bare same-named declaration (round-4
    # BLOCKING: a foreign ``_resolve<Child>`` body of ``return nil`` must not be
    # treated as already-injected).
    if _has_own_resolver_method(source, suffix):
        return source, False  # the lowering's own method already present -> no re-inject

    span = _last_module_return_span(source, class_name)
    if span is None:
        return source, False  # no module epilogue to splice before -> abstain

    ins_at = span[0]
    method_text = _resolver_method_text(class_name, child, field, suffix)
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


# A camera-child RHS shape: the AI's positional ORDINAL child access the resolver
# fact says this field was bound from (``self.cam:GetChildren()[n]`` /
# ``...:GetChild(n)``). The neutralizer anchors on a ``self.<field> =`` whose RHS
# textually carries ONE OF THESE ordinal accesses — NOT a bare ``self.cam`` mention
# (a boolean/config RHS like ``self.cam and self.defaultSlot`` merely NAMES the
# camera but performs no child lookup; neutralizing it to ``nil`` would be a
# false-green — codex round-3 MAJOR). When the RHS carries no ordinal child access
# the neutralizer ABSTAINS (leaves the write -> the verifier sees an
# un-discharged binding). The leading ``[:.]`` requires the ordinal access to be
# rooted on a RECEIVER expression (``cam:GetChild(n)`` / ``cam.GetChild(n)``), not
# a bare ``GetChild(`` token inside a string the code-position gate already skips.
_CAMERA_CHILD_RHS_RE = re.compile(
    r"[:.]GetChildren\(\)\s*\[\s*\d+\s*\]"
    r"|[:.]GetChild\(\s*\d+\s*\)"
)


def _neutralize_assignment(
    source: str, field: str, suffix: str
) -> tuple[str, bool]:
    """Replace the RHS of the camera-child Awake write ``self.<field> = <rhs>``
    with ``nil``, FACT-ANCHORED on the camera-child RHS shape (not the first
    ``self.<field> =`` anywhere), multiline-aware. Abstain-safe (no-op if no
    camera-child write exists). ``suffix`` is the VALID-LUAU-IDENTIFIER method-name
    suffix (only used in the rig-retarget comment). Returns
    ``(new_source, neutralized)``."""
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
        comment = f" -- rig-retargeted: resolved lazily at use via _resolve{suffix}"
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


def _binding_discharged(source: str, field: str, suffix: str) -> bool:
    """INDEPENDENT, code-position-aware derivation: is ``field``'s binding
    discharged via the rig retarget in THIS source? Mirrors the verifier's
    authority (S1b owns the verifier copy; this is the lowering's own re-derive so
    it never stamps ``present=True`` off a reverted edit).

    ``suffix`` is the VALID-LUAU-IDENTIFIER method-name suffix (``_resolve<suffix>``).

    True IFF:
      (1) the LOWERING'S OWN resolver method ``function <Class>:_resolve<suffix>(``
          (own-emit body, NOT a preexisting foreign same-named method) exists AND
          >=1 ``self:_resolve<suffix>(`` call exists AND NO bare ``self.<field>``
          READ survives at a consumer; AND
      (2) the camera-child WRITE is gone — no surviving
          ``self.<field> = <... :GetChildren()[n] | :GetChild(n) | self.cam ...>``.

    Round-4 BLOCKING: condition (1a) requires the lowering's OWN emit, so a
    preexisting foreign ``_resolve<suffix>`` method (e.g. body ``return nil``) that
    the lowering never wrote this run cannot false-discharge ``present=True`` on an
    unchanged source."""
    method_call = f"self:_resolve{suffix}("
    # (1a) the lowering's OWN resolver method is present at a code position.
    if not _has_own_resolver_method(source, suffix):
        return False
    # (1b) >=1 ``self:_resolve<suffix>(`` CALL (distinct from the declaration).
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


# The lowering's OWN-EMIT body signature — the distinctive rig-scan line every
# emitted resolver carries. A preexisting FOREIGN ``_resolve<suffix>`` method (e.g.
# ``return nil``) will NOT contain it, so discharge / idempotency can bind to the
# lowering's own emit rather than a bare same-named declaration (round-4 BLOCKING).
_OWN_EMIT_BODY_MARKER = 'm:GetAttribute("_MainCameraRig")'


def _has_own_resolver_method(source: str, suffix: str) -> bool:
    """True if the LOWERING'S OWN ``function <Class>:_resolve<suffix>(`` method is
    present at a code position — identified by the own-emit body signature
    (``_OWN_EMIT_BODY_MARKER``) appearing inside the method body, NOT by the bare
    declaration. A preexisting foreign same-named method (different body) returns
    False."""
    pat = re.compile(
        r"\bfunction\s+[A-Za-z_]\w*[:.]_resolve" + re.escape(suffix) + r"\s*\("
    )
    for m in pat.finditer(source):
        if not _luau_pos_is_code(source, m.start()):
            continue
        if _luau_pos_in_long_bracket(source, m.start()):
            continue
        # The body runs from the declaration to the NEXT code-level module ``function``
        # (or EOF). The own-emit body marker must appear within it at a code position.
        body_end = _next_module_function_start(source, m.end())
        body = source[m.start():body_end]
        marker_idx = body.find(_OWN_EMIT_BODY_MARKER)
        while marker_idx != -1:
            abs_idx = m.start() + marker_idx
            if _luau_pos_is_code(source, abs_idx) and not _luau_pos_in_long_bracket(source, abs_idx):
                return True
            marker_idx = body.find(_OWN_EMIT_BODY_MARKER, marker_idx + 1)
    return False


def _next_module_function_start(source: str, after: int) -> int:
    """The char index of the next code-level ``function <Class>:<m>(`` declaration
    at or after ``after``, or ``len(source)`` if none. Bounds a resolver method's
    body for the own-emit body scan."""
    for m in _FUNCTION_METHOD_RE.finditer(source, after):
        if not _luau_pos_is_code(source, m.start()):
            continue
        if _luau_pos_in_long_bracket(source, m.start()):
            continue
        return m.start()
    return len(source)


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
        if not _luau_pos_is_code(source, i) or _luau_pos_in_long_bracket(source, i):
            i += 1
            continue
        ch = source[i]
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
