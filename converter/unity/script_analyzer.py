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

_RE_CLASS = re.compile(
    r"(?:public\s+)?class\s+(\w+)\s*:\s*(\w+)",
)
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

    # Extract class info
    m = _RE_CLASS.search(source)
    if m:
        info.class_name = m.group(1)
        info.base_class = m.group(2)

    # Extract lifecycle hooks
    info.lifecycle_hooks = list({m.group(1) for m in _RE_LIFECYCLE.finditer(source)})

    # Extract Unity API usage
    info.unity_apis_used = list({m.group(1) for m in _RE_UNITY_API.finditer(source)})

    # Extract serialized fields
    info.serialized_fields = [
        (m.group(1), m.group(2)) for m in _RE_SERIALIZED_FIELD.finditer(source)
    ]

    # Extract type references (field types, method parameter types, etc.)
    # Matches PascalCase identifiers used as types in declarations
    _type_refs: set[str] = set()
    # Field declarations: Type fieldName; or [Attr] Type fieldName;
    for m2 in re.finditer(r'(?:private|public|protected|internal)\s+(?:readonly\s+)?([A-Z]\w+)\s+\w+', source):
        _type_refs.add(m2.group(1))
    # Serialized fields: [SerializeField] Type name
    for ft, _ in info.serialized_fields:
        if ft and ft[0].isupper():
            _type_refs.add(ft)
    # Constructor calls: new TypeName(
    for m2 in re.finditer(r'\bnew\s+([A-Z]\w+)\s*\(', source):
        _type_refs.add(m2.group(1))
    # Generic type args: List<TypeName>, Dictionary<K, TypeName>
    for m2 in re.finditer(r'<\s*([A-Z]\w+)', source):
        _type_refs.add(m2.group(1))
    # Method parameters: (TypeName param, ...)
    for m2 in re.finditer(r'[,(]\s*([A-Z]\w+)\s+\w+', source):
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
