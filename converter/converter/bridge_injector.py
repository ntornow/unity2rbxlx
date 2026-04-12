"""
bridge_injector.py — Detect and inject Unity bridge modules needed by transpiled Luau.

Scans for require() calls and API usage patterns to determine dependencies.
No other pipeline module is imported here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_BRIDGE_DIR = Path(__file__).resolve().parent.parent / "runtime"


@dataclass
class _BridgeSpec:
    filename: str
    module_name: str
    patterns: list[re.Pattern[str]]


def _p(*args: str) -> list[re.Pattern[str]]:
    return [re.compile(p) for p in args]


# AnimatorBridge and TransformAnimator are handled by animation_converter.py.
BRIDGE_SPECS: list[_BridgeSpec] = [
    _BridgeSpec(
        filename="Input.luau",
        module_name="Input",
        patterns=_p(
            r"""require\s*\(.*["']Input["']""",
            r"""\bInput\.GetKey(?:Down|Up)?\s*\(""",
            r"""\bInput\.GetAxis\s*\(""",
            r"""\bInput\.GetSwipe\s*\(""",
        ),
    ),
    _BridgeSpec(
        filename="Time.luau",
        module_name="Time",
        patterns=_p(
            r"""require\s*\(.*["']Time["']""",
            r"""\bTime\.deltaTime\b""",
            r"""\bTime\.time\b""",
            r"""\bTime\.timeScale\b""",
            r"""\bTime\.fixedDeltaTime\b""",
        ),
    ),
    _BridgeSpec(
        filename="Coroutine.luau",
        module_name="Coroutine",
        patterns=_p(
            r"""require\s*\(.*["']Coroutine["']""",
            r"""\bCoroutine\.Start\s*\(""",
            r"""\bCoroutine\.WaitForSeconds\s*\(""",
            r"""\bCoroutine\.WaitForEndOfFrame\s*\(""",
            r"""\bCoroutine\.Yield\s*\(""",
        ),
    ),
    _BridgeSpec(
        filename="physics_queries.luau",
        module_name="Physics",
        patterns=_p(
            r"""require\s*\(.*["']Physics["']""",
            r"""\bPhysics\.Raycast\s*\(""",
            r"""\bPhysics\.CheckSphere\s*\(""",
            r"""\bPhysics\.OverlapSphere\s*\(""",
        ),
    ),
    _BridgeSpec(
        filename="MonoBehaviour.luau",
        module_name="MonoBehaviour",
        patterns=_p(
            r"""require\s*\(.*["']MonoBehaviour["']""",
            r"""\bMonoBehaviour\.new\s*\(""",
        ),
    ),
    _BridgeSpec(
        filename="GameObjectUtil.luau",
        module_name="GameObjectUtil",
        patterns=_p(
            r"""require\s*\(.*["']GameObjectUtil["']""",
            r"""\bGameObjectUtil\.Instantiate(?:FromAsset)?\s*\(""",
            r"""\bGameObjectUtil\.Destroy\s*\(""",
            r"""\bGameObjectUtil\.Find(?:WithTag)?\s*\(""",
            r"""\bGameObjectUtil\.SetActive\s*\(""",
        ),
    ),
    _BridgeSpec(
        filename="StateMachine.luau",
        module_name="StateMachine",
        patterns=_p(
            r"""require\s*\(.*["']StateMachine["']""",
            r"""\bStateMachine\.new\s*\(""",
        ),
    ),
]


@dataclass
class BridgeInjectionResult:
    needed: list[str] = field(default_factory=list)
    already_present: list[str] = field(default_factory=list)


def detect_needed_bridges(
    luau_sources: list[str],
    existing_script_names: set[str] | None = None,
) -> BridgeInjectionResult:
    """Scan Luau sources for bridge module dependencies."""
    existing = existing_script_names or set()
    result = BridgeInjectionResult()

    for spec in BRIDGE_SPECS:
        if spec.filename in existing:
            result.already_present.append(spec.filename)
            continue

        needed = False
        for source in luau_sources:
            for pattern in spec.patterns:
                if pattern.search(source):
                    needed = True
                    break
            if needed:
                break

        if needed:
            result.needed.append(spec.filename)

    return result


def inject_bridges(
    needed_filenames: list[str],
    bridge_dir: Path | None = None,
) -> list[tuple[str, str]]:
    """Read bridge module files from disk and return (filename, source) pairs."""
    bdir = bridge_dir or _BRIDGE_DIR
    result: list[tuple[str, str]] = []

    for filename in needed_filenames:
        path = bdir / filename
        if path.exists():
            result.append((filename, path.read_text(encoding="utf-8")))

    return result
