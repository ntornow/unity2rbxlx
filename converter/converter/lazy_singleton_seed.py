"""Build-time resolver for lazily-created singleton MonoBehaviours (Phase 2 §1).

A ``MonoBehaviour`` that Unity lazily self-instantiates through a static
``instance``/``Instance`` property getter (``new GameObject(...)`` +
``AddComponent<Self>()``) and caches in a static backing field NEVER awakes in
the generic host: it has no scene/prefab placement and the converter comments the
lazy ``new GameObject + AddComponent`` path out as host-owned. So the backing
field stays ``nil``, ``getInstance()`` returns ``nil``, and every static call
(``CoroutineHandler.StartStaticCoroutine``) is a no-op.

This module detects such classes STRUCTURALLY (the C# shape + the component class
graph — never the ``CoroutineHandler`` literal, never the AI-emitted Luau) and
emits a ``LazySingletonSeed`` per qualifying class, which the boot shim (slice
2.2) replays to construct + Awake exactly one instance before any consumer uses
it. Detection requires:

  1. COMPONENT-CLASS — the class resolves to a Unity component base
     (MonoBehaviour/NetworkBehaviour) directly or through a project-local chain
     (reuses ``consumable_db_seed._resolves_to_component`` + ``build_base_by_class``).
  2. LAZY-SINGLETON SHAPE — a static self-typed backing field + a static
     ``instance``/``Instance`` getter that self-instantiates and caches
     (``ScriptInfo.lazy_singleton_field`` carries the backing-field NAME).
  3. SIDE-EFFECT-FREE BOOT SURFACE (§1.1a, bias-to-abstain) — the whole eager-boot
     lifecycle surface the host shim would trigger (getter body, C# Awake,
     OnEnable, Start, instance field-initializers) is benign; ANY extra side
     effect → ABSTAIN. Eagerly boot-Awaking a side-effecting singleton would run
     those effects earlier / wrong-side vs Unity.

ABSTAINS (emits no seed) when any signal is absent, a side effect is present, or
the class is ``runtime_bearing`` (scene-placed → already Awoken by the scene
batch; don't double-construct). The resolver functions are PURE: they read the
inputs handed to them and return records; pipeline wiring
(``pipeline._build_lazy_singleton_seeds``) gathers state and stamps the result.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import TypedDict

from unity.script_analyzer import (
    _matching_brace_span,
    _strip_comments_and_strings,
    analyze_script,
)
from unity.prefab_ref import GuidIndexLike
from converter.consumable_db_seed import _resolves_to_component

logger = logging.getLogger(__name__)


class LazySingletonSeed(TypedDict):
    """One per-class seed the boot shim replays to construct + Awake exactly one
    instance of a lazily-created singleton MonoBehaviour before any consumer uses
    it (Phase 2 §1.2)."""
    module_path: str    # dotted DataModel path of the singleton ModuleScript
    class_stem: str     # the .cs/RbxScript stem — identity for the dead-module exemption
    domain: str         # "client" | "server" | "helper" — the module's final classified domain
    script_guid: str    # the GUID key into plan.modules — the shim hands this to engine:addComponent
    backing_field: str  # the captured static backing-field name (m_Instance / _instance / ...)


# A C# instance field declaration with a NONTRIVIAL initializer (a ``new X()``, a
# method call, or a ``GetComponent``/``FindObjectOfType`` finder), which runs at
# construction time and so would fire at boot. Matched over de-commented source.
# ``static``/``const`` fields are NOT instance fields (they don't run per-Awake),
# so they are excluded by the leading-modifier check in ``_has_nontrivial_field_init``.
_RE_FIELD_WITH_INIT = re.compile(
    r"(?P<head>(?:\[[^\]]*\]\s*)*"            # optional attributes
    r"(?:public|private|protected|internal)?"
    r"(?:\s+(?:static|const|readonly|new|volatile))*"
    r"\s*[A-Za-z_][\w.<>\[\]?,\s]*?)"          # declared type
    r"\s+(?P<name>[A-Za-z_]\w*)"               # field name
    r"\s*=\s*(?P<init>[^;]+);",                # initializer up to ``;``
)

# A method declaration head we want the body of (Awake / OnEnable / Start). C#
# Unity lifecycle methods are ``void <Name>()`` (no params for these three).
def _re_method_head(name: str) -> re.Pattern[str]:
    return re.compile(rf"\b(?:void|IEnumerator)\s+{re.escape(name)}\s*\(\s*\)")


def _method_body(decommented: str, name: str) -> str | None:
    """Return the brace-delimited BODY (including braces) of the first
    ``void/IEnumerator <name>()`` method, or ``None`` if absent. The source MUST
    be de-commented."""
    m = _re_method_head(name).search(decommented)
    if m is None:
        return None
    span = _matching_brace_span(decommented, m.end())
    if span is None:
        return None
    return decommented[span[0] : span[1] + 1]


def _body_is_trivial(body: str) -> bool:
    """True when a method body has NO statements (empty or whitespace-only between
    the braces). ``body`` includes the surrounding braces."""
    inner = body.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1]
    return inner.strip() == ""


def _awake_assigns_only_backing_field(body: str, backing_field: str) -> bool:
    """True when a C# ``Awake`` body assigns ONLY the backing field
    (``<field> = this;``) and contains no other statement (§1.1a). ``body``
    includes the surrounding braces."""
    inner = body.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1]
    inner = inner.strip()
    if inner == "":
        return False  # a present-but-empty Awake is handled by the caller's gate
    # Split on ``;`` into statements; tolerate a trailing empty fragment.
    statements = [s.strip() for s in inner.split(";") if s.strip()]
    if len(statements) != 1:
        return False
    stmt = statements[0]
    # The single statement must be ``<backing_field> = <rhs>`` (typically
    # ``= this``). Any other lvalue (or a method call) → not just a field cache.
    return bool(
        re.fullmatch(rf"{re.escape(backing_field)}\s*=\s*.+", stmt)
    )


def _blank_nested_block_bodies(class_body: str) -> str:
    """Blank out the BODY of every nested brace block inside a class body, so a
    scan for class-level field declarations cannot see locals declared inside
    method/property bodies. ``class_body`` is the source BETWEEN the class's
    braces (de-commented). Replaces each depth>=1 ``{...}`` span (the contents,
    not the braces) with spaces, preserving offsets and statement terminators
    outside the blocks.
    """
    out = list(class_body)
    depth = 0
    for i, ch in enumerate(class_body):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif depth >= 1:
            # Inside a method/property/initializer block — blank it (keep newlines
            # so line structure / the ``;`` boundaries outside are unaffected).
            if ch != "\n":
                out[i] = " "
    return "".join(out)


def _has_nontrivial_field_init(decommented: str) -> bool:
    """True when an INSTANCE field has a nontrivial initializer — a ``new X()``, a
    method call, or a ``GetComponent``/``FindObjectOfType`` finder — which runs at
    construction time and would fire at boot (§1.1a). A constant/literal
    initializer (``= 5`` / ``= "x"`` / ``= true`` / ``= MyEnum.A``) is BENIGN.
    Static/const fields are excluded (they are not per-instance construction work).

    Only CLASS-LEVEL declarations are inspected: nested method/property/getter
    block bodies are blanked first, so a local var (``GameObject o = new …``)
    inside the lazy getter is NOT mistaken for a field initializer.
    """
    # Restrict to the class body and blank nested block contents so only
    # class-level field declarations remain visible.
    m_cls = re.search(r"\bclass\s+\w+", decommented)
    scope = decommented
    if m_cls is not None:
        span = _matching_brace_span(decommented, m_cls.end())
        if span is not None:
            scope = _blank_nested_block_bodies(
                decommented[span[0] + 1 : span[1]]
            )
    for m in _RE_FIELD_WITH_INIT.finditer(scope):
        head = m.group("head")
        if re.search(r"\b(?:static|const)\b", head):
            continue  # not a per-instance field initializer
        if _init_is_nontrivial(m.group("init")):
            return True
    # Auto-property initializers (``int Score { get; set; } = Load();``) run at
    # construction like a field initializer — the accessor block sits between the
    # property name and ``=``, so ``_RE_FIELD_WITH_INIT`` (name\s*=) cannot see
    # them. Scan them explicitly. ``static`` auto-properties are excluded (they
    # are not per-instance construction work).
    for m in _RE_AUTO_PROP_WITH_INIT.finditer(scope):
        if re.search(r"\b(?:static|const)\b", m.group("head")):
            continue
        if _init_is_nontrivial(m.group("init")):
            return True
    return False


# An auto-property declaration with an initializer: ``[mods] <type> <name>
# { get; ... } = <init>;``. The accessor block is matched as a single
# ``{...}`` (auto-property accessors are bodyless), then the ``= <init>;``.
_RE_AUTO_PROP_WITH_INIT = re.compile(
    r"(?P<head>(?:\[[^\]]*\]\s*)*"
    r"(?:public|private|protected|internal)?"
    r"(?:\s+(?:static|const|readonly|new|virtual|override|abstract))*"
    r"\s*[A-Za-z_][\w.<>\[\]?,\s]*?)"          # declared type
    r"\s+(?P<name>[A-Za-z_]\w*)"               # property name
    r"\s*\{[^{}]*\}"                            # bodyless accessor block
    r"\s*=\s*(?P<init>[^;]+);",                 # initializer
)


def _init_is_nontrivial(raw_init: str) -> bool:
    """True when a field/auto-property initializer runs construction-time work — a
    ``new X()``, a finder, or ANY method/generic-factory call. A bare literal /
    member-access constant (``= 5`` / ``= MyEnum.A``) is BENIGN. A lambda
    (``() => …``) is a delegate alloc, not a construction-time side effect, so it
    is excluded.
    """
    init = raw_init.strip()
    if re.search(r"\bnew\b", init):
        return True
    if re.search(r"\bGetComponent\b|\bFindObjectOfType\b", init):
        return True
    # A call: a ``(`` preceded by an identifier char OR a ``>`` (a generic factory
    # call ``Factory.Build<Foo>()`` — the ``(`` follows ``>``). EXCLUDE the lambda
    # ``() =>`` shape (a ``(`` preceded by neither, i.e. a bare/empty param list
    # feeding ``=>``), which is a delegate allocation, not a call.
    for cm in re.finditer(r"[\w>]\s*\(", init):
        return True
    return False


# Statements the getter body is ALLOWED to contain beyond the lazy-create
# mechanics (§1.1a/§1.1b). ``DontDestroyOnLoad`` is BENIGN (the host shim parents
# the synthetic container under the side's persistent runtime container, the
# faithful analog of Unity's DontDestroyOnLoad). Any OTHER statement → ABSTAIN.
def _getter_body_is_side_effect_free(
    body: str, class_name: str, backing_field: str,
) -> bool:
    """True when the static-instance getter body contains ONLY the allowed
    lazy-create statements (§1.1a) AND carries the lazy null-guard a lazy singleton
    always has: the guard, a ``new GameObject(...)`` local/assign, an
    ``AddComponent<class>()`` cache assignment to the backing field (or to a local
    that was itself bound to that ``AddComponent`` call), a benign
    ``DontDestroyOnLoad(...)``, and ``return <field>``. ANY other statement —
    including a backing-field assignment whose RHS WRAPS the ``AddComponent`` in
    another call (``= Register(o.AddComponent<Foo>())``), or whose RHS is a bare
    identifier/property NOT bound to the tracked ``AddComponent`` local
    (``Foo tmp = Instance; <field> = tmp;`` — a property access with side
    effects) — → False (ABSTAIN). ``body`` includes the surrounding braces; it is
    de-commented. Bias to abstain.

    Null-guard shapes accepted: ``if (<field> == null)`` and the yoda
    ``if (null == <field>)``. Other lawful guards (``??=``, ``ReferenceEquals``,
    an early-return ``if (<field> != null) return <field>``) intentionally ABSTAIN
    — false-abstain is SAFE (the field stays nil, the pre-Phase-2 behaviour), and
    chasing them is unwarranted complexity. ``GameObject`` may be qualified
    (``UnityEngine.GameObject`` / a namespaced type), handled in ``_stmt_is_allowed``.
    """
    inner = body.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1]
    # Strip the getter wrapper if present: ``get { ... }``. The caller passes the
    # PROPERTY block; the getter accessor may be nested.
    get_m = re.search(r"\bget\b", inner)
    if get_m is not None:
        get_span = _matching_brace_span(inner, get_m.end())
        if get_span is not None:
            inner = inner[get_span[0] + 1 : get_span[1]]
    # A lazy singleton ALWAYS guards before creating; require the canonical
    # ``if (<field> == null)`` or its yoda ``if (null == <field>)`` form.
    fld = re.escape(backing_field)
    cls = re.escape(class_name)
    if not re.search(
        rf"\bif\s*\(\s*(?:{fld}\s*==\s*null|null\s*==\s*{fld})\s*\)", inner
    ):
        return False
    # Remove the ``if (...) { ... }`` guard braces but KEEP its body statements
    # (they are the real work we vet). We do this by stripping ``if`` and matching
    # parens/braces structurally, then flattening.
    flattened = _flatten_control_braces(inner)
    statements = [s.strip() for s in flattened.split(";") if s.strip()]
    # Track the locals bound to the ``AddComponent<Self>()`` result, so the
    # cache-through ``<field> = <local>`` is allowed ONLY for that exact local and
    # not for an arbitrary identifier (which could be a side-effecting property).
    addcomponent_locals: set[str] = set()
    for stmt in statements:
        if _stmt_is_allowed(stmt, fld, cls, addcomponent_locals):
            continue
        return False
    return True


def _flatten_control_braces(src: str) -> str:
    """Remove ``{``/``}`` and ``if (...)`` guard heads so the getter body reduces
    to a flat ``;``-separated statement list (the guards add no eager side effect
    of their own — the body statements they wrap are vetted individually)."""
    out = src
    # Drop ``if ( ... )`` heads (the comparison is not a side effect).
    out = re.sub(r"\bif\s*\([^)]*\)", "", out)
    out = re.sub(r"\belse\b", "", out)
    out = out.replace("{", "").replace("}", "")
    return out


# A bare C# identifier (a local var name, e.g. the ``o`` in ``GameObject o``).
_IDENT = r"[A-Za-z_]\w*"
# An optionally namespace-qualified type name (``GameObject`` /
# ``UnityEngine.GameObject``). The receiver in an ``AddComponent`` RHS reuses the
# bare ``_IDENT`` (a local var, not a dotted type).
_QUALIFIED = rf"(?:{_IDENT}\s*\.\s*)*{_IDENT}"
# An ``AddComponent<cls>(...)`` call, optionally with a single receiver prefix
# (``o.AddComponent<cls>()`` / ``AddComponent<cls>()``). The receiver is a bare
# identifier — NOT another call — so a WRAPPED form (``Register(o.AddComponent…)``)
# does not match this as the whole RHS (Register's ``(`` is the outermost call).
_RE_ADDCOMPONENT_RHS = (
    rf"(?:{_IDENT}\s*\.\s*)?AddComponent\s*<\s*{{cls}}\s*>\s*\(\s*\)"
)
# A ``new GameObject(...)`` constructor RHS (the lazy host object). ``GameObject``
# may be namespace-qualified (``new UnityEngine.GameObject(...)``).
_RE_NEW_GAMEOBJECT_RHS = rf"new\s+(?:{_IDENT}\s*\.\s*)*GameObject\s*\([^;]*\)"


def _stmt_is_allowed(
    stmt: str, fld: str, cls: str, addcomponent_locals: set[str],
) -> bool:
    """True when a single getter statement matches one of the allowed lazy-create
    forms EXACTLY (structural, not substring). ``fld``/``cls`` are
    ``re.escape``-d field/class names; ``addcomponent_locals`` is the (mutated in
    place) set of local names already bound to the ``AddComponent<cls>()`` result —
    a ``<field> = <local>`` cache-through is allowed ONLY for one of those locals.

    ABSTAINS (returns False) on anything else — notably a backing-field assignment
    whose RHS WRAPS the ``AddComponent`` in another call
    (``= Register(o.AddComponent<Foo>())``), and a cache whose RHS is a bare
    identifier/property NOT bound to the tracked ``AddComponent`` local
    (``Foo tmp = Instance; <field> = tmp;`` — a property access with side
    effects). A local declaration whose RHS is a bare identifier/property is itself
    not an allowed statement → ABSTAIN.
    """
    s = stmt.strip()
    if s == "":
        return True
    add_rhs = _RE_ADDCOMPONENT_RHS.format(cls=cls)
    # ``return <field>`` (with or without ``return`` keyword after flatten).
    if re.fullmatch(rf"(?:return\s+)?{fld}", s):
        return True
    # ``[<Type>] <local> = new GameObject(...)`` or a bare ``new GameObject(...)``.
    if re.fullmatch(
        rf"(?:{_QUALIFIED}\s+{_IDENT}\s*=\s*)?{_RE_NEW_GAMEOBJECT_RHS}", s
    ):
        return True
    # ``DontDestroyOnLoad(...)`` — BENIGN (§1.1b).
    if re.fullmatch(r"DontDestroyOnLoad\s*\([^;]*\)", s):
        return True
    # An assignment ``[<Type>] <lvalue> = <rhs>``. The lvalue is the backing field
    # or a local; the RHS must be one of the allowed construction expressions
    # EXACTLY — NOT a wrapped/extra call, NOT an arbitrary identifier/property. We
    # match the lvalue + RHS as a whole so a wrapped RHS
    # (``= Register(o.AddComponent<…>())``) fails (its outermost call differs).
    m = re.fullmatch(
        rf"(?:{_QUALIFIED}\s+)?({_IDENT})((?:\s*\.\s*{_IDENT})*)\s*=\s*(.+)", s
    )
    if m is not None:
        lvalue_head = m.group(1)
        rhs = m.group(3).strip()
        is_decl = re.match(rf"{_QUALIFIED}\s+{_IDENT}\s*=", s) is not None
        if re.fullmatch(add_rhs, rhs):
            # The result of ``AddComponent<cls>()`` — track the local it binds to
            # (a simple ``<local> = ...`` lvalue, no member chain) so a later
            # ``<field> = <local>`` cache-through is recognised as benign.
            if m.group(2) == "":
                addcomponent_locals.add(lvalue_head)
            return True
        if re.fullmatch(_RE_NEW_GAMEOBJECT_RHS, rhs):
            return True
        # ``<field> = <local>`` cache-through — allowed ONLY when <local> was bound
        # to the ``AddComponent`` result above. A bare identifier that is NOT a
        # tracked AddComponent-local could be a side-effecting property access
        # (``<field> = Instance;``) → ABSTAIN. A local DECLARATION whose RHS is a
        # bare identifier (``Foo tmp = Instance;``) is likewise not allowed.
        if (
            not is_decl
            and re.fullmatch(_IDENT, rhs)
            and rhs in addcomponent_locals
        ):
            return True
    # Anything else (a wrapped/extra-call assignment, an ``.Init()`` call, naming,
    # event subscription, parentage moves, a property-access local) → ABSTAIN.
    return False


def _static_instance_property_body(
    decommented: str, class_name: str,
) -> str | None:
    """Return the static ``instance``/``Instance`` property block body (incl.
    braces) for ``class_name``, or ``None``. De-commented source required."""
    from unity.script_analyzer import _RE_STATIC_INSTANCE_PROP
    for pm in _RE_STATIC_INSTANCE_PROP.finditer(decommented):
        if pm.group("type") != class_name:
            continue
        span = _matching_brace_span(decommented, pm.end())
        if span is None:
            continue
        return decommented[span[0] : span[1] + 1]
    return None


def passes_boot_safety_gate(
    cs_source: str, class_name: str, backing_field: str,
) -> bool:
    """The §1.1a boot-safety gate (PURE, bias-to-abstain). True ONLY when the
    whole eager-boot lifecycle surface the host shim would trigger is benign:

      - the static-instance getter body contains ONLY the allowed lazy-create
        statements (``new GameObject`` / benign ``DontDestroyOnLoad`` /
        ``AddComponent<cls>`` cache / the ``if (field==null)`` guard / ``return``);
      - NO C# ``Awake`` OR an ``Awake`` body that assigns ONLY the backing field;
      - NO C# ``OnEnable`` and NO C# ``Start`` OR each, if present, is trivial
        (empty body) — ``addComponent`` fires ``OnEnable`` and schedules ``Start``;
      - no nontrivial instance field-initializers.

    Returns False (ABSTAIN) the moment any check is in doubt.
    """
    decommented = _strip_comments_and_strings(cs_source)

    # Getter body must be side-effect-free.
    prop_body = _static_instance_property_body(decommented, class_name)
    if prop_body is None:
        return False
    if not _getter_body_is_side_effect_free(prop_body, class_name, backing_field):
        return False

    # C# Awake: absent, or assigns ONLY the backing field.
    awake = _method_body(decommented, "Awake")
    if awake is not None:
        if _body_is_trivial(awake):
            pass  # an empty Awake is fine
        elif not _awake_assigns_only_backing_field(awake, backing_field):
            return False

    # C# OnEnable / Start: absent, or trivial (empty body).
    for hook in ("OnEnable", "Start"):
        body = _method_body(decommented, hook)
        if body is not None and not _body_is_trivial(body):
            return False

    # No nontrivial instance field-initializers.
    if _has_nontrivial_field_init(decommented):
        return False

    return True


def resolve_lazy_singletons(
    modules: dict[str, object],
    guid_index: GuidIndexLike,
    base_by_class: dict[str, str],
    module_path_for_stem: Callable[[str], str | None],
) -> list[LazySingletonSeed]:
    """Resolve every lazy-singleton class in ``modules`` to a ``LazySingletonSeed``.

    Pure: reads the inputs, returns records sorted by ``class_stem`` (deterministic
    boot order). ``modules`` is ``scene_runtime["modules"]`` — KEYED BY
    ``script_guid``, each row carrying ``stem`` / ``class_name`` / ``module_path``
    / ``domain`` / ``runtime_bearing``. The dict KEY is the ``script_guid`` the
    shim hands ``engine:addComponent`` (``plan.modules`` is GUID-keyed).

    For each row, ABSTAINS (emits no seed) unless ALL hold:
      1. the row is a component class (``_resolves_to_component``);
      2. NOT ``runtime_bearing`` (a scene-placed instance is already Awoken by the
         scene batch — don't double-construct);
      3. the ``.cs`` (resolved via the GuidIndex by the script_guid key) is a lazy
         singleton (``ScriptInfo.lazy_singleton_field`` non-empty);
      4. the boot-safety gate passes (``passes_boot_safety_gate``);
      5. ``module_path`` resolves under the collision-exclusion contract;
      6. the row carries a ``"client"``/``"server"``/``"helper"`` ``domain``. A
         ``"helper"`` module loads in BOTH VMs, so its per-VM singleton is
         boot-seeded on whichever entrypoint uses it (the shim constructs a
         helper seed on both sides; the per-VM idempotency guard prevents a
         double-construct within a VM).
    """
    seeds: list[LazySingletonSeed] = []
    for script_guid, row in modules.items():
        if not isinstance(script_guid, str) or not script_guid:
            continue
        if not isinstance(row, dict):
            continue

        class_name = _str(row.get("class_name"))
        stem = _str(row.get("stem"))
        # The component-graph check keys on the class name (the planner's walk).
        if not _resolves_to_component(class_name or stem, base_by_class):
            continue

        # (2) A scene-placed instance is already Awoken by the scene batch.
        if bool(row.get("runtime_bearing")):
            continue

        # (3) Resolve the .cs by the GUID key and detect the lazy-singleton shape.
        cs_path = guid_index.resolve(script_guid)
        if cs_path is None or cs_path.suffix != ".cs":
            continue
        info = analyze_script(cs_path)
        backing_field = info.lazy_singleton_field
        if not backing_field:
            continue
        detect_class = info.class_name or class_name
        if not detect_class:
            continue

        # (4) Boot-safety gate against the REAL source (bias to abstain).
        try:
            cs_source = cs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not passes_boot_safety_gate(cs_source, detect_class, backing_field):
            logger.info(
                "[lazy_singleton] %s: lazy-singleton shape but failed the "
                "side-effect-free boot-safety gate; abstaining", detect_class,
            )
            continue

        # (5) Resolve the module_path under the collision-exclusion contract. The
        # class_stem identity for the dead-module exemption is the .cs stem.
        class_stem = cs_path.stem
        module_path = module_path_for_stem(class_stem)
        if module_path is None:
            logger.warning(
                "[lazy_singleton] %s: no collision-free module path; dropping",
                class_stem,
            )
            continue

        # (6) The classified domain (client/server/helper) of the singleton's
        # module. A "helper" module loads in both VMs, so its seed is replayed
        # on both entrypoints (the shim's per-VM idempotency guard prevents a
        # double-construct within a VM).
        domain = _str(row.get("domain"))
        if domain not in ("client", "server", "helper"):
            logger.info(
                "[lazy_singleton] %s: module has no client/server/helper domain "
                "(%r); abstaining", class_stem, domain,
            )
            continue

        seeds.append(LazySingletonSeed(
            module_path=module_path,
            class_stem=class_stem,
            domain=domain,
            script_guid=script_guid,
            backing_field=backing_field,
        ))

    # Deterministic boot order: sort by class_stem (mirror the consumable /
    # placement determinism). Dedup on script_guid (one seed per class).
    seeds.sort(key=lambda s: s["class_stem"])
    return seeds


def _str(value: object) -> str:
    """Coerce a row value to ``str`` (``""`` for None / non-str)."""
    return value if isinstance(value, str) else ""
