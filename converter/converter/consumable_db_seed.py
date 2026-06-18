"""Build-time resolver for consumable-prototype materialization (Phase 1 §1.A).

A Consumable-style ScriptableObject (`Consumables.asset`) carries a serialized
field that is an ARRAY OF OBJECT-REFS, each pointing at a MonoBehaviour component
*inside a prefab* (e.g. `CoinMagnet.prefab`'s `&11491712`). The SO converter
resolves each such ref to a prefab-id STRING, so the emitted database iterates an
array of strings and `c:GetConsumableType()` throws at boot.

This module resolves each element to the materialization facts the boot shim
needs — the subclass `.cs` stem, the prefab id, the resolved subclass module
path, and the component's serialized overrides SPLIT into structured tables
(`ctor_config` scalars passed to `<Subclass>.new(...)` and `post_fields`
asset-ref ids assigned post-construction) — gated on TWO hard preconditions:

  (4) COMMON-BASE: every resolved in-prefab component class shares ONE common
      project-local base that derives from MonoBehaviour (so the array IS a
      coherent component family, not e.g. a sprite array). Computed by walking
      EACH element class's full ancestor chain and intersecting.
  (5) CONSUMER-USAGE: the owning DB's C# DRAINS the field treating elements as
      component OBJECTS — it invokes a method/member on an element over the
      field's iteration, NOT passes elements to a prefab/`Instantiate` path.

Both gates are keyed on deterministic build facts (the `.asset`/`.prefab` YAML
shape + the GuidIndex + the C# class->base graph via `analyze_script`), NEVER a
per-game string and NEVER the AI-emitted Luau. On any per-element miss the
element is DROPPED (never stringified); if either gate fails the resolver
ABSTAINS (contributes no seed).

The resolver functions are PURE: they read the inputs handed to them and return
records; pipeline wiring (`pipeline._build_consumable_db_seeds`) gathers the
state and stamps the result onto `scene_runtime`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypedDict

from unity.prefab_ref import (
    GuidIndexLike,
    prefab_id_for_guid,
    prefab_id_for_ref,
)
from unity.script_analyzer import _strip_comments_and_strings, analyze_script
from unity.yaml_parser import doc_body, parse_documents
from converter.scriptable_object_converter import (
    _SKIP_FIELDS,
    _is_asset_reference,
)

logger = logging.getLogger(__name__)

# A serialized object-ref element: ``{fileID, guid, type}`` (the disjoint arm the
# SO converter's ``_value_to_lua`` resolves at ``scriptable_object_converter.py:137``).
_OBJECT_REF_KEYS = {"fileID", "guid", "type"}

# Unity MonoBehaviour class id (``--- !u!114 &<fileID>``). The in-prefab component
# docs are MonoBehaviours; a fileID anchor pointing at a non-114 doc is not a
# component and is dropped.
_MONOBEHAVIOUR_CLASS_ID = 114

# Unity component base classes (mirror ``scene_runtime_planner._COMPONENT_BASE_CLASSES``).
# A class extending any of these — directly or through a project-local chain — is
# a component.
_COMPONENT_BASE_CLASSES = frozenset({"MonoBehaviour", "NetworkBehaviour"})


# A serialized scalar field value, AFTER bool coercion (a ``bool``-declared 0/1
# becomes a Python ``bool``). The plan encoder (``_plan_to_luau``) renders each as
# a Luau literal: ``bool`` -> ``true``/``false``, ``int``/``float`` -> a numeric
# literal, ``str`` -> a quoted string. Asset-ref ids (in ``post_fields``) are
# always ``str``.
ScalarFieldValue = str | int | float | bool


class ConsumableSeedElement(TypedDict):
    """One materialized element of a consumable-style DB array.

    The serialized overrides are SPLIT (no pre-rendered Luau string): ``ctor_config``
    carries the SCALAR fields the typed ``<Subclass>.new(config)`` reads (bool
    coercion already applied); ``post_fields`` carries the ASSET-REF fields
    ``Consumable.new`` drops and the shim must assign POST-construction. The plan
    encoder renders both as nested Luau TABLES (no ``loadstring`` on the client).
    """
    class_stem: str                        # the in-prefab component's subclass .cs stem
    prefab_id: str                         # the canonical "<guid>:<path>" prefab id
    module_path: str                       # the resolved full module path of the subclass
    ctor_config: dict[str, ScalarFieldValue]  # scalars passed to <Subclass>.new(config)
    post_fields: dict[str, str]            # asset-ref ids assigned post-construction


class ConsumableSeed(TypedDict):
    """One per-database seed the boot shim replays to materialize its array."""
    db_module_path: str
    array_field: str
    elements: list[ConsumableSeedElement]


def read_prefab_component(
    prefab_path: Path, file_id: int | str,
) -> dict[str, object] | None:
    """Return the MonoBehaviour body dict at ``&<file_id>`` in ``prefab_path``.

    Reuses the canonical ``parse_documents`` (classID, fileID, body) parse so the
    fileID anchor survives YAML cleaning. Returns the unwrapped component body
    (``doc_body`` strips the ``MonoBehaviour:`` wrapper), or ``None`` when the
    file is unreadable, the anchor is absent, or the doc at that anchor is not a
    MonoBehaviour (classID 114).
    """
    try:
        raw = prefab_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    want = str(file_id)
    for class_id, fid, doc in parse_documents(raw):
        if fid == want and class_id == _MONOBEHAVIOUR_CLASS_ID:
            return doc_body(doc)
    return None


def _is_object_ref(value: object) -> bool:
    """True iff *value* is a serialized object-ref ``{fileID, guid, type}`` with a
    non-zero guid (the prefab-component-ref shape)."""
    if not isinstance(value, dict):
        return False
    if not set(value.keys()) <= _OBJECT_REF_KEYS:
        return False
    guid = value.get("guid")
    return isinstance(guid, str) and bool(guid) and guid != "0" * 32


def find_component_ref_arrays(
    asset_body: Mapping[str, object],
) -> dict[str, list[dict[str, object]]]:
    """Find every top-level field whose value is a LIST OF object-refs.

    The field NAME is never hardcoded — the array is located structurally by its
    element shape. ``m_*`` internal fields are skipped. Returns ``field_name ->
    [object-ref dict, ...]`` for each candidate (a list of dicts where EVERY
    element matches the object-ref shape; a mixed list is not a candidate)."""
    candidates: dict[str, list[dict[str, object]]] = {}
    for key, value in asset_body.items():
        if key in _SKIP_FIELDS or key == "m_Name":
            continue
        if not isinstance(value, list) or not value:
            continue
        if all(_is_object_ref(item) for item in value):
            # mypy: each item is a dict per _is_object_ref
            candidates[str(key)] = [item for item in value if isinstance(item, dict)]
    return candidates


def _full_ancestor_chain(
    class_name: str, base_by_class: dict[str, str],
) -> list[str]:
    """The class's ancestor chain (the class itself, then each base) up to the
    first unknown/external base or a cycle.

    Note: a directly-extended component base IS included — ``base_by_class`` may
    map a project class to the external ``MonoBehaviour``/``NetworkBehaviour``
    base, and that truthy value is appended as the chain's tail (the walk only
    stops at a falsy/missing base or a cycle). Callers that intersect chains and
    must reject a bare-component-base shared root do so at SELECTION, not here.

    Mirrors the cycle-safe walk in ``scene_runtime_planner._resolves_to_component``
    but RETURNS the visited chain so callers can intersect chains across elements.
    """
    chain: list[str] = []
    seen: set[str] = set()
    current = class_name
    while current and current not in seen:
        chain.append(current)
        seen.add(current)
        current = base_by_class.get(current, "")
    return chain


def _resolves_to_component(
    class_name: str, base_by_class: dict[str, str],
) -> bool:
    """True when ``class_name`` extends a Unity component base directly or through
    a project-local chain (mirror of the planner walker, keyed on a single
    class->base map)."""
    seen: set[str] = set()
    current = base_by_class.get(class_name, "")
    # Include the class itself in case it IS a component base (unlikely for a
    # subclass, but keeps the check total).
    if class_name in _COMPONENT_BASE_CLASSES:
        return True
    while current and current not in seen:
        if current in _COMPONENT_BASE_CLASSES:
            return True
        seen.add(current)
        current = base_by_class.get(current, "")
    return False


def common_monobehaviour_base(
    element_classes: list[str], base_by_class: dict[str, str],
) -> str | None:
    """The gate (4) check: the shared project-local base of ``element_classes``,
    or ``None`` (ABSTAIN) when they do not share ONE common MonoBehaviour-derived
    base.

    Every element class must resolve to a Unity component base. The shared base
    is computed by intersecting each element's FULL ancestor chain (NOT immediate-
    base equality, so a mix of direct subclasses and an intermediate
    ``RareConsumable : Consumable`` still resolves the common ``Consumable``) and
    choosing the NEAREST shared ancestor — the first class on the first element's
    chain that appears on every other element's chain. A bare ``MonoBehaviour``
    (or ``NetworkBehaviour``) shared root is REJECTED at selection: it proves
    "all components" but not "one coherent family", so we ABSTAIN rather than
    return a component base. The shared root must be a PROJECT-LOCAL class that
    itself derives from a component base.

    Note: the component bases DO appear in an element's chain when a class
    extends one directly (``_full_ancestor_chain`` appends each truthy base, and
    ``"X": "MonoBehaviour"`` puts ``MonoBehaviour`` at the chain tail). It is the
    SELECTION loop — not the chain construction — that rejects a bare-base shared
    root; the topmost shared ancestor of two unrelated MonoBehaviour components
    IS ``MonoBehaviour``, and accepting it would reopen the unrelated-component-
    array hole this gate exists to close. The bare-base rejection is load-bearing.

    KNOWN LIMITATION (finding #4 — deliberately conservative, project-local).
    Beyond the external component bases, ancestor chains contain only project-
    local classes: a non-component external/package base (anything without a
    project ``.cs``) is never in ``base_by_class`` and so never appears in a
    chain. Consequence: a family whose ONLY shared PROJECT-LOCAL ancestor would
    be such an external base (e.g. every element derives from a package
    ``PackageConsumable : MonoBehaviour`` that ships compiled, with no project
    ``.cs``) shares no project-local node → this returns ``None`` (ABSTAIN). We
    do NOT widen to accept a Unity-base shared root, for the reason above.
    Abstaining is SAFE: gate (5) plus the per-element DROP invariant mean an
    abstain seeds nothing and stringifies nothing — it merely leaves that exotic
    family's pre-existing behavior unchanged, never introducing a wrong rewrite.
    (Trash-Dash's ``Consumable`` is a project-local ``.cs``, so this is not a
    live blocker; it bounds generality to project-local families.)
    """
    if not element_classes:
        return None
    chains: list[list[str]] = []
    for cls in element_classes:
        if not _resolves_to_component(cls, base_by_class):
            return None  # an element is not a component -> not a coherent family
        chains.append(_full_ancestor_chain(cls, base_by_class))
    shared = set(chains[0])
    for chain in chains[1:]:
        shared &= set(chain)
    # The component bases (MonoBehaviour/NetworkBehaviour) DO appear in a chain
    # whenever a class extends one directly: ``_full_ancestor_chain`` appends each
    # truthy ``base_by_class`` value, and a ``"X": "MonoBehaviour"`` mapping makes
    # ``MonoBehaviour`` the chain's tail. Pick the NEAREST shared ancestor along
    # the first element's chain.
    for cls in chains[0]:
        if cls not in shared:
            continue
        # A bare component base as the nearest shared ancestor means "all are
        # components" but NOT "one coherent family" (the topmost shared ancestor
        # of any two unrelated MonoBehaviour components is MonoBehaviour itself).
        # ABSTAIN — this is the load-bearing rejection that closes the
        # unrelated-component-array hole.
        if cls in _COMPONENT_BASE_CLASSES:
            return None
        # Otherwise return the shared PROJECT-LOCAL class, but only when it
        # itself derives from a component base (rejects a shared NON-component
        # ancestor such as an interface-less POCO base).
        if _resolves_to_component(cls, base_by_class):
            return cls
    return None


def _element_exprs(src: str, array_field: str) -> list[str]:
    """Collect the source EXPRESSIONS that name an element of ``array_field``.

    An element is named directly (``field[i]``), via a foreach loop variable
    (``foreach (var c in field)`` → ``c``), or via a local alias
    (``var c = field[i]`` → ``c``). The returned list contains the literal
    expressions to look for object-usage / prefab-usage on (the indexing
    expression itself, plus every bound alias identifier).
    """
    import re

    fld = re.escape(array_field)
    exprs: list[str] = []

    # Direct indexed element: keep the bracket-indexed form as a usable expr.
    for m in re.finditer(rf"\b{fld}\s*\[[^\]]*\]", src):
        exprs.append(m.group(0))

    # foreach (<type> <ident> in <field>) -> the loop var binds an element.
    for m in re.finditer(
        rf"foreach\s*\(\s*[\w<>.\[\]?]+\s+(?P<var>[A-Za-z_]\w*)\s+in\s+{fld}\b",
        src,
    ):
        exprs.append(m.group("var"))

    # Local alias: ``<type> c = <field>[i];`` / ``var c = <field>[i];`` binds an
    # element to a local. (``var c = field;`` aliases the WHOLE array, not an
    # element, so require the index.)
    for m in re.finditer(
        rf"\b[\w<>.\[\]?]+\s+(?P<var>[A-Za-z_]\w*)\s*=\s*{fld}\s*\[",
        src,
    ):
        exprs.append(m.group("var"))

    return exprs


def db_drains_field_as_objects(db_cs_source: str, array_field: str) -> bool:
    """The gate (5) check: does the DB's C# DRAIN ``array_field`` treating its
    elements as component OBJECTS (a method CALLED on an element), as opposed to
    passing elements (or an element member) to a prefab/`Instantiate` path?

    Structural, keyed on the USAGE SHAPE — never a per-game method name:
      - POSITIVE evidence (required): a METHOD CALL on an element — ``elem.M(…)``
        where ``elem`` is an indexed element (``field[i].M()``), a foreach loop
        var (``foreach (var c in field) … c.M()``), or a local alias
        (``var c = field[i]; c.M()``). A bare member ACCESS that is not called is
        NOT object usage — a property/field read whose value is then handed to a
        prefab path is prefab usage.
      - NEGATIVE (abstain): an element — or an element MEMBER — is passed to
        ``Instantiate(…)`` (``Instantiate(field[i])`` / ``Instantiate(c)`` /
        ``Instantiate(c.gameObject)`` / ``Instantiate(field[i].prefab)``). The
        array then holds prefab references, not component prototypes.

    Returns ``True`` only on positive method-call evidence AND no Instantiate
    consumption of any element (or element member). Comments/strings are stripped
    first so prose mentioning the field name does not match.
    """
    from unity.script_analyzer import _strip_comments_and_strings

    src = _strip_comments_and_strings(db_cs_source)
    if array_field not in src:
        return False

    import re

    exprs = _element_exprs(src, array_field)
    if not exprs:
        return False

    # NEGATIVE first: any element (or an element MEMBER) passed to Instantiate(…)
    # is prefab usage -> abstain. Matches ``Instantiate(<elem>`` and
    # ``Instantiate(<elem>.<member>`` for every bound element expression.
    for expr in exprs:
        e = re.escape(expr)
        if re.search(rf"\bInstantiate\s*\(\s*{e}\s*(?:\.\s*\w+\s*)*[,)]", src):
            return False

    # POSITIVE: a METHOD CALL on an element — ``<elem>.<Method>(`` (a member
    # access immediately followed by a call). A bare ``<elem>.<member>`` that is
    # never called is NOT counted (it may be a prefab-feeding member read).
    for expr in exprs:
        e = re.escape(expr)
        if re.search(rf"{e}\s*\.\s*[A-Za-z_]\w*\s*\(", src):
            return True

    return False


def build_base_by_class(guid_index: GuidIndexLike) -> dict[str, str]:
    """Build the project ``class_name -> immediate base_class`` map from every
    project ``.cs`` (mirror of ``scene_runtime_planner.py:1164-1174``).

    The analyzer does not expose this map; construct it here so the common-base
    gate can walk inheritance chains. Reads ``guid_to_entry`` directly so a
    duck-typed index (test fixtures) works too.
    """
    base_by_class: dict[str, str] = {}
    guid_to_entry = getattr(guid_index, "guid_to_entry", {})
    for entry in guid_to_entry.values():
        path = getattr(entry, "asset_path", None)
        if path is None or path.suffix != ".cs":
            continue
        info = analyze_script(path)
        if info.class_name:
            base_by_class[info.class_name] = info.base_class
    return base_by_class


def _resolve_class_stem(guid: str, guid_index: GuidIndexLike) -> str | None:
    """Resolve an ``m_Script`` guid to a project ``.cs`` stem, or ``None``."""
    if not isinstance(guid, str) or not guid:
        return None
    guid_to_entry = getattr(guid_index, "guid_to_entry", {})
    entry = guid_to_entry.get(guid)
    path = getattr(entry, "asset_path", None) if entry is not None else None
    if path is None or path.suffix != ".cs":
        return None
    return path.stem


def resolve_db_seed(
    db_module_path: str,
    db_cs_source: str,
    asset_body: Mapping[str, object],
    guid_index: GuidIndexLike,
    base_by_class: dict[str, str],
    module_path_for_stem: Callable[[str], str | None],
) -> ConsumableSeed | None:
    """Resolve ONE database SO ``.asset`` into a ``ConsumableSeed``, or ``None``.

    Pure: reads the inputs, returns a record. Steps (Phase 1 §1.A):
      1. Find the component-ref array field structurally (the NAME is never
         hardcoded). The FIRST candidate field that passes both gates wins.
      2. For each element resolve prefab id + in-prefab component class +
         resolved subclass ``module_path`` + serialized fields SPLIT into
         ``ctor_config``/``post_fields``; DROP an unresolvable element (never
         stringify). ``module_path_for_stem`` resolves a subclass stem to its
         full module path with the planner's collision-exclusion contract (it
         returns ``None`` on a stem/class collision or an absent module → the
         element is DROPPED, matching the planner's fail-closed).
      3. Gate (4) common-base: all resolved classes share one MonoBehaviour base.
      4. Gate (5) consumer-usage: the DB C# drains the field as objects.
    Returns the seed (possibly with an EMPTY ``elements`` list when every element
    dropped but the gates passed); ``None`` when no field passes the gates.
    """
    candidates = find_component_ref_arrays(asset_body)
    if not candidates:
        return None

    passing: list[ConsumableSeed] = []
    for array_field, refs in candidates.items():
        # Gate (5): the DB must DRAIN this field as component objects. Cheap
        # structural check first (no prefab IO) — abstain if it's a prefab-id
        # consumer or never dereferenced.
        if not db_drains_field_as_objects(db_cs_source, array_field):
            continue

        # Resolve each element. Collect the resolved class per element for the
        # common-base gate; DROP an unresolvable element.
        resolved_elements: list[ConsumableSeedElement] = []
        resolved_classes: list[str] = []
        for ref in refs:
            guid = ref.get("guid")
            file_id = ref.get("fileID")
            if not isinstance(guid, str) or file_id is None:
                logger.warning(
                    "[consumable_seed] %s.%s: element missing guid/fileID; dropping",
                    db_module_path, array_field,
                )
                continue
            prefab_id = prefab_id_for_guid(guid, guid_index)
            if prefab_id is None:
                logger.warning(
                    "[consumable_seed] %s.%s: guid %s does not resolve to a "
                    ".prefab; dropping element", db_module_path, array_field, guid,
                )
                continue
            guid_to_entry = getattr(guid_index, "guid_to_entry", {})
            entry = guid_to_entry.get(guid)
            prefab_path = getattr(entry, "asset_path", None) if entry else None
            if prefab_path is None:
                logger.warning(
                    "[consumable_seed] %s.%s: no asset_path for prefab guid %s; "
                    "dropping element", db_module_path, array_field, guid,
                )
                continue
            component = read_prefab_component(prefab_path, file_id)
            if component is None:
                logger.warning(
                    "[consumable_seed] %s.%s: fileID %s not a MonoBehaviour in %s; "
                    "dropping element", db_module_path, array_field, file_id,
                    prefab_path.name,
                )
                continue
            m_script = component.get("m_Script")
            script_guid = m_script.get("guid") if isinstance(m_script, dict) else None
            class_stem = _resolve_class_stem(script_guid, guid_index) if isinstance(
                script_guid, str,
            ) else None
            if class_stem is None:
                logger.warning(
                    "[consumable_seed] %s.%s: in-prefab component m_Script does not "
                    "resolve to a project .cs; dropping element",
                    db_module_path, array_field,
                )
                continue
            # Resolve the subclass's full module path at BUILD time, with the
            # planner's collision-exclusion contract (a colliding stem/class ->
            # None). DROP the element on a miss — the shim no longer scans for it.
            module_path = module_path_for_stem(class_stem)
            if module_path is None:
                logger.warning(
                    "[consumable_seed] %s.%s: subclass %s has no collision-free "
                    "module path; dropping element",
                    db_module_path, array_field, class_stem,
                )
                continue
            field_types = field_types_for_class(
                class_stem, base_by_class, guid_index,
            )
            ctor_config, post_fields = split_component_fields(
                component, field_types, guid_index,
            )
            resolved_elements.append(ConsumableSeedElement(
                class_stem=class_stem,
                prefab_id=prefab_id,
                module_path=module_path,
                ctor_config=ctor_config,
                post_fields=post_fields,
            ))
            resolved_classes.append(class_stem)

        # Gate (4): the resolved classes must share one MonoBehaviour-derived
        # base. Checked over the classes that DID resolve (a dropped element
        # contributes nothing). With zero resolved classes the gate cannot be
        # evaluated -> ABSTAIN on this field (no positive family evidence).
        if not resolved_classes:
            continue
        base = common_monobehaviour_base(resolved_classes, base_by_class)
        if base is None:
            logger.warning(
                "[consumable_seed] %s.%s: resolved classes %s do not share one "
                "MonoBehaviour-derived base; abstaining (not a component family)",
                db_module_path, array_field, sorted(set(resolved_classes)),
            )
            continue

        passing.append(ConsumableSeed(
            db_module_path=db_module_path,
            array_field=array_field,
            elements=resolved_elements,
        ))

    if not passing:
        return None
    if len(passing) > 1:
        # >1 candidate array on ONE database passes both gates. We seed only the
        # first (the shim assigns one array_field per DB); WARN so a multi-array
        # DB is not silently half-seeded (finding #5).
        logger.warning(
            "[consumable_seed] %s: %d candidate arrays passed both gates %s; "
            "seeding only the first (%s)",
            db_module_path, len(passing),
            [s["array_field"] for s in passing], passing[0]["array_field"],
        )
    return passing[0]


# A C# instance field declaration: ``[modifiers] <Type> <name> [= ...];``. We
# capture the declared TYPE token (group "type") and the field NAME (group
# "name"). Matched over de-commented source so prose never matches. Generic over
# the type — the resolver keys on the DECLARED TYPE (``bool`` -> coerce 0/1),
# never a field-name literal. ``static``/``const`` fields are not Unity-serialized
# instance fields, so they are excluded.
_RE_CS_FIELD = re.compile(
    r"\b(?:public|private|protected|internal)\b"
    r"(?P<mods>(?:\s+(?:readonly|new|volatile))*)"
    r"\s+(?P<type>[\w.<>\[\]?]+)"
    r"\s+(?P<name>[A-Za-z_]\w*)"
    r"\s*(?:=|;)",
)
_RE_CS_STATIC_OR_CONST = re.compile(r"\b(?:static|const)\b")


def field_types_for_class(
    class_stem: str, base_by_class: dict[str, str], guid_index: GuidIndexLike,
) -> dict[str, str]:
    """Map ``field_name -> declared C# type`` for ``class_stem``, walking the
    class+base chain so a field declared on a BASE (``canBeSpawned`` on
    ``Consumable``, not the subclass) is included.

    Reads each class's ``.cs`` (located via the GuidIndex) and extracts plain
    instance field declarations (``public bool canBeSpawned;``). A subclass
    declaration SHADOWS a base of the same name (nearest wins) — the chain is
    walked subclass-first and a name already seen is not overwritten. Cycle-safe.
    Returns ``{}`` when no source is readable (the caller then leaves values
    verbatim — never guesses a type).
    """
    types: dict[str, str] = {}
    seen: set[str] = set()
    current = class_stem
    while current and current not in seen:
        seen.add(current)
        src = _cs_source_for_stem(current, guid_index)
        if src is not None:
            decommented = _strip_comments_and_strings(src)
            for m in _RE_CS_FIELD.finditer(decommented):
                # Skip static/const — not Unity-serialized instance fields. Check
                # the modifier span BEFORE the captured access modifier too.
                line_start = decommented.rfind("\n", 0, m.start()) + 1
                head = decommented[line_start:m.start()]
                if _RE_CS_STATIC_OR_CONST.search(head + m.group("mods")):
                    continue
                name = m.group("name")
                if name not in types:
                    types[name] = m.group("type")
        current = base_by_class.get(current, "")
    return types


def _cs_source_for_stem(
    class_stem: str, guid_index: GuidIndexLike,
) -> str | None:
    """Read the ``.cs`` source whose file stem == ``class_stem`` from the
    GuidIndex, or ``None``. (The project class-name == file stem for the
    consumable family; mirrors ``_resolve_class_stem``'s stem keying.)"""
    guid_to_entry = getattr(guid_index, "guid_to_entry", {})
    for entry in guid_to_entry.values():
        path = getattr(entry, "asset_path", None)
        if path is None or path.suffix != ".cs":
            continue
        if path.stem == class_stem:
            try:
                return path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
    return None


def _coerce_scalar(
    value: object, declared_type: str | None,
) -> ScalarFieldValue | None:
    """Coerce one serialized SCALAR per its declared C# type.

    Bool coercion is the load-bearing rule (finding P1-2): Unity serializes a
    ``bool`` field as ``0``/``1`` (an int), and Luau treats ``0`` as TRUTHY, so a
    ``canBeSpawned: 0`` passed verbatim wrongly reads spawnable. A ``bool``-typed
    0/1 (or ``true``/``false``) becomes a Python ``bool`` here, which the plan
    encoder renders as Luau ``false``/``true``. Other scalars pass through as
    their native ``str``/``int``/``float``. Returns ``None`` for a value that is
    not a renderable scalar (a dict/list — handled as an asset-ref or skipped by
    the caller), so it is NOT placed in ``ctor_config``.
    """
    if isinstance(value, bool):
        return value
    if declared_type == "bool":
        # Unity serializes bool as 0/1; coerce per the DECLARED type, never a
        # field-name literal. Strings "true"/"false" tolerated for robustness.
        if isinstance(value, int):
            return value != 0
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("1", "true"):
                return True
            if low in ("0", "false"):
                return False
        # An unexpected shape for a bool field: leave verbatim if scalar.
    if isinstance(value, (int, float, str)):
        return value
    return None


def split_component_fields(
    component: Mapping[str, object],
    field_types: dict[str, str],
    guid_index: GuidIndexLike,
) -> tuple[dict[str, ScalarFieldValue], dict[str, str]]:
    """Split a component's serialized USER fields into ``(ctor_config,
    post_fields)``.

    - ``post_fields``: ASSET-REF fields — a serialized value that is an object-ref
      (``{fileID,guid,type}``) or an AssetReference struct — RESOLVED to a prefab/
      asset id string. ``Consumable.new`` drops these, so the shim assigns them
      post-construction. An UNRESOLVABLE ref is dropped (never a nil/string slot).
    - ``ctor_config``: every other serialized SCALAR (number / bool / string),
      with bool coercion applied per the field's declared C# type (``field_types``).

    ``_SKIP_FIELDS``/``m_Name`` internals are dropped. Keys are processed sorted
    so the recompute is byte-identical run to run.
    """
    ctor_config: dict[str, ScalarFieldValue] = {}
    post_fields: dict[str, str] = {}
    for key, value in sorted(component.items(), key=lambda kv: str(kv[0])):
        if key in _SKIP_FIELDS or key == "m_Name":
            continue
        name = str(key)
        if isinstance(value, dict):
            # An asset-ref field (AssetReference struct or {fileID,guid,type}
            # object-ref). Resolve to an id; drop if unresolvable.
            resolved = _resolve_asset_ref(value, guid_index)
            if resolved is not None:
                post_fields[name] = resolved
            continue
        coerced = _coerce_scalar(value, field_types.get(name))
        if coerced is not None:
            ctor_config[name] = coerced
        # A non-scalar non-ref (e.g. a list) is omitted: the typed ``.new`` only
        # reads scalars, and a list field is not an asset-ref carrier here.
    return ctor_config, post_fields


def _resolve_asset_ref(
    value: Mapping[str, object], guid_index: GuidIndexLike,
) -> str | None:
    """Resolve an asset-ref dict to its id string, or ``None`` if it is not a ref
    / cannot be resolved. Mirrors ``_value_to_lua``'s ref arms (AssetReference
    first, then the bare ``{fileID,guid,type}`` object-ref)."""
    d = dict(value)
    if _is_asset_reference(d):
        guid = d.get("m_AssetGUID")
        if isinstance(guid, str):
            pid = prefab_id_for_guid(guid, guid_index)
            if pid is not None:
                return pid
        cached = d.get("m_CachedAsset")
        if isinstance(cached, dict):
            return prefab_id_for_ref(cached, guid_index)
        return None
    if set(d.keys()) <= {"fileID", "guid", "type"}:
        return prefab_id_for_ref(d, guid_index)
    return None
