"""
script_analyzer.py -- Lightweight C# script analysis for classification.

Determines script type (MonoBehaviour, ScriptableObject, editor-only),
extracts class names, base classes, and referenced Unity APIs.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Base-less classes (helper / utility classes with no `: Base`) must match too
# so the generic require graph can resolve them by stem. The base group stays
# optional and `analyze_script` falls back to "" when the second capture is None
# — preserving the legacy `base_class == "MonoBehaviour"` checks downstream.
_RE_CLASS = re.compile(
    r"(?:public\s+)?class\s+(\w+)(?:\s*:\s*(\w+))?",
)

# Comments and string/char literals must be stripped BEFORE the structural
# regexes run: a doc-comment like ``/// This class allows us …`` or a string
# ``"class Foo"`` is prose, not a declaration. ``.search`` returns the FIRST
# ``class <word>`` it finds, so an un-stripped source makes the first comment
# mentioning "class" win and ``class_name`` becomes an English word ("allows",
# "for", "used"). Mirrors ``code_transpiler._strip_comments_and_strings``.
_RE_COMMENTS_AND_STRINGS = re.compile(
    r"//[^\n]*"            # line comment ( // and /// )
    r"|/\*.*?\*/"          # block comment
    r'|@"(?:""|[^"])*"'    # C# verbatim string
    r'|"(?:\\.|[^"\\])*"'  # regular string
    r"|'(?:\\.|[^'\\])*'",  # char literal
    re.DOTALL,
)


def _strip_comments_and_strings(source: str) -> str:
    """Blank out comments and literals (replace with a space to preserve token
    boundaries) so declaration regexes can't match inside prose."""
    return _RE_COMMENTS_AND_STRINGS.sub(" ", source)
_RE_LIFECYCLE = re.compile(
    r"(?:void|IEnumerator)\s+(Awake|Start|Update|FixedUpdate|LateUpdate|"
    r"OnEnable|OnDisable|OnDestroy|OnCollisionEnter|OnCollisionExit|"
    r"OnTriggerEnter|OnTriggerExit|OnMouseDown|OnGUI|"
    r"OnApplicationQuit)\s*\(",
)
_RE_UNITY_API = re.compile(
    r"(Input\.\w+|Camera\.main|Physics\.\w+|SceneManager\.\w+|"
    r"Instantiate|Destroy|GetComponent|transform\.\w+|"
    r"UserInputService|AudioSource|Animator|NavMeshAgent)",
)
_RE_SERIALIZED_FIELD = re.compile(
    r"\[SerializeField\]\s*(?:private\s+)?(\w+)\s+(\w+)",
)

# C# ``static event`` declarations. A static event is a TYPE-level entity the
# converter lowers to a module-table FIELD shared across every instance of the
# class (the same cached require table). Each member name is exactly the Luau
# field name the producer assigns and the consumer reads (``Player.AmmoUpdate``),
# so this is the DETERMINISTIC upstream signal the channel-identity fix anchors
# on. We match ``[modifiers] static event <HandlerType[generic/qualified]>
# <declarator-list>`` where ``static`` and ``event`` co-occur (either order is
# legal C#). A SINGLE declaration may bind MULTIPLE members:
# ``public static event H Foo, Bar;`` declares both ``Foo`` AND ``Bar`` of type
# ``H``. We therefore capture the handler type (group 1, the first type token —
# generic/qualified allowed) and the WHOLE comma-separated declarator span up to
# the terminator (group 2), and split the names in ``analyze_script``. NEVER parse
# the emitted Luau.
_RE_STATIC_EVENT = re.compile(
    r"\bstatic\b"                  # the static modifier (anywhere in the modifier list)
    r"(?:\s+(?:public|private|protected|internal|readonly|new|abstract))*"
    r"\s+event\s+"                 # the event keyword
    r"([\w<>\.\[\]]+(?:\s*<[^;{}]*>)?)"  # handler type (generic / qualified ok), group 1
    r"\s+([\w\s,]+?)\s*"          # declarator list (one or more comma-separated names), group 2
    r"(?:;|=|\{)",                 # declaration terminator
)
# Split a captured declarator span (``Foo, Bar`` / ``Foo``) into member names.
_RE_DECLARATOR = re.compile(r"\b\w+\b")

# Lazy-singleton structural signal (Phase 2 §1.1). A class is a *lazy singleton*
# when it carries (a) a STATIC SELF-TYPED backing field
# (``static protected CoroutineHandler m_Instance;``) AND (b) a static
# ``instance``/``Instance`` property getter whose body SELF-INSTANTIATES — a
# ``new GameObject(...)``, an ``AddComponent<Self>()`` (the type arg == the
# declaring class), and an assignment to the backing field. The captured value is
# the backing-field NAME (carried forward so the runtime guard reads
# ``Cls[backing_field]``, never a hardcoded ``m_Instance``). The NAME varies
# across projects (``m_Instance``/``_instance``/``s_Instance``) so it is captured,
# never assumed. We key on the SHAPE, never the ``CoroutineHandler`` literal.
#
# Matched over the ALREADY-DE-COMMENTED source (so CoroutineHandler.cs's
# doc-comment ``/// Create a GameObject…`` never matches). The getter body is
# matched as a CONJUNCTION of facts found ANYWHERE in the body (NOT a contiguous
# regex), so the ``DontDestroyOnLoad(o);`` between ``new GameObject`` and the
# ``AddComponent`` cache (CoroutineHandler.cs:17-19) does not defeat the match.
# This module extracts only the FIELD NAME; the side-effect-free boot-safety gate
# lives in ``converter.lazy_singleton_seed`` (it needs richer source inspection
# than ``ScriptInfo`` carries).

# A static self-typed backing field: ``static <access?> <Class> <field>;`` where
# the declared type token == the declaring class name. The modifier order is free
# in C# (``static protected`` / ``protected static``), so ``static`` is anchored
# anywhere in the modifier list and the type/name pair follows. Group ``type`` is
# the declared type, ``name`` the field name.
#
# The OPTIONAL initializer accepts ONLY a BENIGN value — ``= null`` /
# ``= default`` / ``= default(<Type>)`` — so the common lazy declaration
# ``private static Foo _instance = null;`` is still recognised as the backing
# field, but a field that EAGER-constructs itself (``= new Foo()`` /
# ``= Build<Foo>()``) does NOT match: that is not the lazy-getter pattern Phase 2
# targets, and boot-constructing it would double-run. A non-benign initializer
# makes ``\s*;`` unreachable (the ``=`` is not consumed) → no match → ABSTAIN.
_RE_STATIC_SELF_FIELD = re.compile(
    r"\bstatic\b"
    r"(?:\s+(?:public|private|protected|internal|readonly|new))*"
    r"\s+(?P<type>[A-Za-z_]\w*)"
    r"\s+(?P<name>[A-Za-z_]\w*)"
    r"\s*(?:=\s*(?:null|default(?:\s*\(\s*[\w.<>\[\]?,\s]+\s*\))?))?"
    r"\s*;",
)
# A static ``instance``/``Instance`` property getter declaration head. The getter
# BODY span is extracted by brace-matching from this point (see
# ``_find_lazy_singleton_field``).
_RE_STATIC_INSTANCE_PROP = re.compile(
    r"\bstatic\b"
    r"(?:\s+(?:public|private|protected|internal|new))*"
    r"\s+(?P<type>[A-Za-z_]\w*)"
    r"\s+(?P<prop>[Ii]nstance)\b",
)


def _matching_brace_span(src: str, open_idx: int) -> tuple[int, int] | None:
    """Return ``(open_idx, close_idx)`` for the brace block whose ``{`` is at or
    after ``open_idx``. ``None`` if no balanced block is found.

    ``open_idx`` should point at-or-before the block's opening ``{``; the first
    ``{`` at/after it starts the block. Strings/comments are assumed already
    stripped by the caller (``_strip_comments_and_strings``) so a ``{`` inside a
    literal cannot unbalance the count.
    """
    start = src.find("{", open_idx)
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return (start, i)
    return None


def _find_lazy_singleton_field(decommented: str, class_name: str) -> str:
    """Return the lazy-singleton backing-field NAME for ``class_name``, or ``""``.

    Pure, structural, keyed on the C# SHAPE (never the ``CoroutineHandler``
    literal): requires a STATIC SELF-TYPED backing field whose declared type ==
    ``class_name`` AND a static ``instance``/``Instance`` getter whose body
    SELF-INSTANTIATES — a ``new GameObject(...)``, an ``AddComponent<class_name>()``,
    and an assignment to THAT backing field — bound to the SAME field name. The
    three getter facts are a CONJUNCTION found anywhere in the getter body
    (tolerating intermediate statements like ``DontDestroyOnLoad``); not a
    contiguous regex.

    ``decommented`` MUST be comment/string-stripped already. Returns ``""`` when
    any signal is absent (the resolver treats ``""`` as "not a lazy singleton").
    """
    if not class_name:
        return ""

    # (a) Candidate static self-typed backing fields (declared type == the class).
    candidate_fields: list[str] = []
    for m in _RE_STATIC_SELF_FIELD.finditer(decommented):
        if m.group("type") == class_name:
            candidate_fields.append(m.group("name"))
    if not candidate_fields:
        return ""

    # (b) The static instance/Instance getter body. Extract the property block,
    # then check the CONJUNCTION of self-instantiation facts within it.
    for pm in _RE_STATIC_INSTANCE_PROP.finditer(decommented):
        if pm.group("type") != class_name:
            continue
        span = _matching_brace_span(decommented, pm.end())
        if span is None:
            continue
        body = decommented[span[0] : span[1] + 1]
        # Fact 1: a ``new GameObject(...)`` self-instantiation.
        if not re.search(r"\bnew\s+GameObject\s*\(", body):
            continue
        # Fact 2: an ``AddComponent<class_name>()`` whose type arg == the class.
        if not re.search(
            rf"\bAddComponent\s*<\s*{re.escape(class_name)}\s*>", body
        ):
            continue
        # Fact 3: an assignment to ONE of the candidate backing fields. Bind the
        # field name from the assignment so the getter + field agree.
        for field_name in candidate_fields:
            # ``=(?!=)`` is an lvalue ASSIGNMENT, not the ``<field> == null`` guard
            # comparison (precision; harmless today as the conjunction also requires
            # ``new GameObject`` + ``AddComponent``).
            if re.search(rf"\b{re.escape(field_name)}\s*=(?!=)", body):
                return field_name
    return ""

# GLOBAL scene-lookup generics whose type argument is NOT a dependency
# edge: ``FindObjectOfType<T>()`` locates an ALREADY-EXISTING instance of
# T, so the finder creates no structural relationship and no ``require()``
# need; counting the type arg poisons ``dependency_map``. The set is
# deliberately LIMITED to GLOBAL lookups — component lookups
# (``GetComponent<T>`` / ``AddComponent<T>`` / …) reference a real peer
# edge the reachability consumers need, so they are NOT excluded here.
_GLOBAL_LOOKUP_GENERIC_METHODS = frozenset({
    # Legacy global finders.
    "FindObjectOfType", "FindObjectsOfType",
    # ``Resources.FindObjectsOfTypeAll<T>()`` is PLURAL "Objects" (the
    # singular form does not exist as an API).
    "FindObjectsOfTypeAll",
    # Unity 2023+ replacements for the deprecated finders above.
    "FindFirstObjectByType", "FindAnyObjectByType", "FindObjectsByType",
})

CLIENT_APIS = frozenset({
    "Input.GetKey", "Input.GetKeyDown", "Input.GetKeyUp",
    "Input.GetMouseButton", "Input.GetMouseButtonDown",
    "Input.GetAxis", "Input.mousePosition",
    "Camera.main",
    "UnityEngine.UI",       # UI namespace (Text, Image, Button, Slider)
    "Cursor.lockState",     # Mouse cursor control
    "Cursor.visible",
    "Screen.width",         # Screen dimensions
    "Screen.height",
})


@dataclass
class ScriptInfo:
    """Extracted metadata from a C# script."""
    path: Path
    class_name: str = ""
    base_class: str = ""
    lifecycle_hooks: list[str] = field(default_factory=list)
    unity_apis_used: list[str] = field(default_factory=list)
    serialized_fields: list[tuple[str, str]] = field(default_factory=list)
    is_editor_script: bool = False
    is_test_script: bool = False
    suggested_type: str = "Script"  # Script, LocalScript, ModuleScript
    referenced_types: list[str] = field(default_factory=list)  # Project types used
    # ``public static event`` member names. Each is a TYPE-level event the
    # converter lowers to a shared module-table FIELD (``Player.AmmoUpdate``);
    # the member name IS the Luau field name. Deterministic upstream signal for
    # the static-event channel-identity fix — never derived from the AI output.
    static_events: list[str] = field(default_factory=list)
    # The lazy-singleton backing-field NAME (Phase 2 §1.1), or ``""`` when the
    # class is not a lazy singleton. Set when the class has a STATIC SELF-TYPED
    # backing field AND a static ``instance``/``Instance`` getter that
    # self-instantiates (``new GameObject`` + ``AddComponent<Self>`` + field
    # cache). Carried forward so the runtime guard reads ``Cls[backing_field]``,
    # never a hardcoded ``m_Instance`` — the field name varies across projects.
    # Keys on the SHAPE, never a class-name literal; the boot-safety gate
    # (§1.1a) lives in ``converter.lazy_singleton_seed``, not here.
    lazy_singleton_field: str = ""


def analyze_script(script_path: str | Path) -> ScriptInfo:
    """Analyze a C# script file to extract metadata."""
    path = Path(script_path)
    info = ScriptInfo(path=path)

    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return info

    # Check if editor-only
    rel = str(path).replace("\\", "/")
    if "/Editor/" in rel or "/Tests/" in rel or "/Test/" in rel:
        info.is_editor_script = "/Editor/" in rel
        info.is_test_script = "/Test" in rel

    # Strip comments/strings ONCE and drive EVERY structural regex off the
    # cleaned source. A doc-comment ("/// This class allows…") or a string
    # literal must never be mistaken for a declaration, a lifecycle hook, or a
    # type reference — ``referenced_types`` flows into ``pipeline.dependency_map``
    # (→ storage_classifier / module_domain / build_topology), so a comment-hit
    # type would mis-route a module. (Editor/test detection above keys on the
    # path, not the source, so it stays on the raw read.)
    decommented = _strip_comments_and_strings(source)

    # Extract class info. _RE_CLASS now matches base-less classes, so the
    # second capture group can be None — coerce to "" so existing equality
    # checks (`base_class == "MonoBehaviour"`, etc.) keep their semantics.
    m = _RE_CLASS.search(decommented)
    if m:
        info.class_name = m.group(1)
        info.base_class = m.group(2) or ""

    # Extract lifecycle hooks
    info.lifecycle_hooks = list({m.group(1) for m in _RE_LIFECYCLE.finditer(decommented)})

    # Extract Unity API usage
    info.unity_apis_used = list({m.group(1) for m in _RE_UNITY_API.finditer(decommented)})

    # Extract serialized fields
    info.serialized_fields = [
        (m.group(1), m.group(2)) for m in _RE_SERIALIZED_FIELD.finditer(decommented)
    ]

    # Extract ``public static event`` member names (declaration order, deduped).
    # These are the deterministic upstream signal for the static-event channel-
    # identity fix: the member name is the Luau module-table field the converter
    # lowers the event to.
    _seen_events: set[str] = set()
    for m in _RE_STATIC_EVENT.finditer(decommented):
        # group(2) is the declarator list — one member (``Foo``) or a comma list
        # (``Foo, Bar``). Split so EVERY member of a multi-declarator declaration
        # reaches ``static_events`` (a single ``public static event H Foo, Bar;``
        # declares both ``Foo`` and ``Bar``, not just the last).
        for name in _RE_DECLARATOR.findall(m.group(2)):
            if name and name not in _seen_events:
                _seen_events.add(name)
                info.static_events.append(name)

    # Extract the lazy-singleton backing-field name (Phase 2 §1.1). Empty unless
    # the class matches the static-self-typed-field + self-instantiating-getter
    # shape. Keys on the SHAPE off the de-commented source, never a literal.
    info.lazy_singleton_field = _find_lazy_singleton_field(
        decommented, info.class_name,
    )

    # Extract type references (field types, method parameter types, etc.)
    # Matches PascalCase identifiers used as types in declarations
    _type_refs: set[str] = set()
    # Field declarations: Type fieldName; or [Attr] Type fieldName;
    for m2 in re.finditer(r'(?:private|public|protected|internal)\s+(?:readonly\s+)?([A-Z]\w+)\s+\w+', decommented):
        _type_refs.add(m2.group(1))
    # Serialized fields: [SerializeField] Type name
    for ft, _ in info.serialized_fields:
        if ft and ft[0].isupper():
            _type_refs.add(ft)
    # Constructor calls: new TypeName(
    for m2 in re.finditer(r'\bnew\s+([A-Z]\w+)\s*\(', decommented):
        _type_refs.add(m2.group(1))
    # Generic type args: List<TypeName>, Dictionary<K, TypeName>.
    # EXCLUDE the type arg of GLOBAL scene-lookup generics
    # (``FindObjectOfType<T>`` etc.) — see _GLOBAL_LOOKUP_GENERIC_METHODS
    # for why; component-lookup generics are kept.
    # Capture the optional token immediately before ``<`` so we can tell a
    # global-lookup method (``FindObjectOfType<Foo>``) from a collection
    # type (``List<Foo>``) or a component lookup (``GetComponent<Foo>``).
    for m2 in re.finditer(r'(\w+)?\s*<\s*([A-Z]\w+)', decommented):
        preceding = m2.group(1) or ""
        if preceding in _GLOBAL_LOOKUP_GENERIC_METHODS:
            continue
        _type_refs.add(m2.group(2))
    # Method parameters: (TypeName param, ...)
    for m2 in re.finditer(r'[,(]\s*([A-Z]\w+)\s+\w+', decommented):
        _type_refs.add(m2.group(1))
    # Base class
    if info.base_class:
        _type_refs.add(info.base_class)
    # Remove common Unity/C# types that aren't project-local
    _BUILTIN_TYPES = {
        "MonoBehaviour", "ScriptableObject", "Component", "GameObject", "Transform",
        "Vector2", "Vector3", "Vector4", "Quaternion", "Color", "Color32", "Rect",
        "Mathf", "Math", "Debug", "String", "Int32", "Single", "Boolean", "Object",
        "List", "Dictionary", "HashSet", "Queue", "Stack", "Array", "IEnumerator",
        "Action", "Func", "Task", "IEnumerable", "IList", "IDictionary",
        "Coroutine", "WaitForSeconds", "YieldInstruction",
        "Rigidbody", "Rigidbody2D", "Collider", "Collider2D", "BoxCollider",
        "SphereCollider", "CapsuleCollider", "MeshCollider", "CharacterController",
        "Camera", "Light", "AudioSource", "AudioClip", "Animator", "Animation",
        "SpriteRenderer", "MeshRenderer", "MeshFilter", "Renderer", "Material",
        "Texture", "Texture2D", "Sprite", "RenderTexture",
        "ParticleSystem", "TrailRenderer", "LineRenderer",
        "Canvas", "Image", "Text", "Button", "Slider", "Toggle", "InputField",
        "RectTransform", "LayoutGroup", "VerticalLayoutGroup", "HorizontalLayoutGroup",
        "EventSystem", "PointerEventData", "BaseEventData",
        "NavMeshAgent", "NavMeshPath",
        "SceneManager", "Scene", "Application", "Resources", "PlayerPrefs",
        "Physics", "Physics2D", "RaycastHit", "RaycastHit2D", "LayerMask",
        "Input", "KeyCode", "Cursor", "Screen", "SystemInfo",
        "TextMeshPro", "TextMeshProUGUI", "TMP_Text",
        "NetworkBehaviour", "ClientRpc", "ServerRpc", "Command",
        "UnityEvent", "UnityAction",
        "Enum", "Attribute", "Exception", "EventArgs", "Type",
        "SerializeField", "Header", "Tooltip", "Range", "Space",
        "Editor", "EditorWindow", "PropertyDrawer", "CustomEditor",
        "Void", "Float", "Int", "Bool", "Byte",
    }
    info.referenced_types = sorted(_type_refs - _BUILTIN_TYPES - {info.class_name})

    # Classify script type
    if info.base_class in ("Editor", "EditorWindow", "PropertyDrawer"):
        info.suggested_type = "ModuleScript"
        info.is_editor_script = True
    elif any(api in source for api in CLIENT_APIS):
        info.suggested_type = "LocalScript"
    elif not info.lifecycle_hooks and info.base_class not in ("MonoBehaviour",):
        info.suggested_type = "ModuleScript"
    else:
        info.suggested_type = "Script"

    return info


def analyze_all_scripts(unity_project_path: str | Path) -> list[ScriptInfo]:
    """Analyze all C# scripts in a Unity project."""
    project = Path(unity_project_path)
    assets_dir = project / "Assets"
    if not assets_dir.exists():
        return []

    scripts = sorted(assets_dir.rglob("*.cs"))
    results = []
    for script in scripts:
        info = analyze_script(script)
        if not info.is_editor_script and not info.is_test_script:
            results.append(info)

    log.info("Analyzed %d scripts (%d runtime)", len(scripts), len(results))
    return results
