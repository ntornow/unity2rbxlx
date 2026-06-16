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

# GLOBAL scene-lookup generics whose type argument is NOT a dependency
# edge at all: ``FindObjectOfType<T>()`` locates an ALREADY-EXISTING
# instance of T somewhere in the scene. T's reachability/placement comes
# from its own scene authoring or instantiation — NOT from the finder — so
# the finder creates no structural relationship and no ``require()`` need.
# Counting the type arg poisons ``dependency_map`` (e.g. ``Plane`` doing
# ``FindObjectOfType<GameManager>()`` misroutes ``GameManager`` to
# ServerStorage). If T is genuinely required it appears via a
# field/``new``/param/base reference, captured by the other patterns.
#
# NOTE (Codex review, 2026-06-01): this set is deliberately LIMITED to
# GLOBAL lookups. ``GetComponent<T>`` / ``AddComponent<T>`` /
# ``TryGetComponent<T>`` / ``GetComponentInChildren<T>`` etc. are NOT here:
# those reference a PEER component (or, for ``AddComponent``, create it),
# which IS a real dependency edge that ``dependency_map`` feeds to
# ``resolve_caller_graph`` / ``derive_reachability_requirements`` /
# ``_compute_network_behaviour_reachable`` — none of which re-scan these
# APIs. Dropping a component-lookup edge would ORPHAN a component that's
# referenced only that way. (Whether such edges should also drive a
# ``require()`` is a separate, pre-existing concern at the injection site,
# out of scope here.)
_GLOBAL_LOOKUP_GENERIC_METHODS = frozenset({
    # Legacy global finders.
    "FindObjectOfType", "FindObjectsOfType",
    # ``Resources.FindObjectsOfTypeAll<T>()`` is PLURAL "Objects" (Codex
    # review 2026-06-01 — the singular form does not exist as an API).
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
    # (``FindObjectOfType<T>`` etc.): those locate an already-existing T,
    # creating no dependency edge and no ``require()`` need. Counting them
    # poisons ``dependency_map`` (which feeds the legacy require-injector
    # AND the topology caller_graph), e.g. ``Plane`` calling
    # ``FindObjectOfType<GameManager>()`` misroutes ``GameManager`` to
    # ServerStorage. Component-lookup generics (``GetComponent<T>`` /
    # ``AddComponent<T>`` / …) are NOT excluded — they are real peer edges
    # the reachability consumers need (see _GLOBAL_LOOKUP_GENERIC_METHODS).
    # If T is genuinely require-worthy it's also captured by the
    # new/field/param/base patterns. See TODO.md "Transpiler false-positive
    # require() injection".
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
