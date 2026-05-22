"""
stub_generator.py -- Generate valid Luau stub scripts from C# analysis.

When AI transpilation is unavailable and rule-based output has syntax errors,
this generates minimal but VALID Luau scripts that:
1. Always parse without errors
2. Document what the original Unity script did
3. Implement basic behavior where possible (Touched events, Heartbeat loops)
"""

from __future__ import annotations

import re
import logging
from typing import Any

log = logging.getLogger(__name__)


def generate_stub(csharp_source: str, script_info: Any) -> str:
    """Generate a valid Luau stub from C# source analysis.

    Always produces parseable Luau. Never leaves C# syntax.
    """
    class_name = ""
    base_class = ""
    lifecycle_hooks: list[str] = []
    fields: list[tuple[str, str, str]] = []  # (type, name, default)
    methods: list[tuple[str, str]] = []  # (name, params)
    uses_input = False
    uses_collision = False

    # Extract class info
    m = re.search(r"class\s+(\w+)\s*:\s*(\w+)", csharp_source)
    if m:
        class_name = m.group(1)
        base_class = m.group(2)

    # Extract lifecycle hooks
    for hook in ["Awake", "Start", "Update", "FixedUpdate", "LateUpdate",
                 "OnEnable", "OnDisable", "OnDestroy",
                 "OnCollisionEnter", "OnCollisionExit",
                 "OnTriggerEnter", "OnTriggerExit",
                 "OnMouseDown"]:
        if re.search(rf"\b{hook}\s*\(", csharp_source):
            lifecycle_hooks.append(hook)

    # Extract fields with defaults
    for m in re.finditer(
        r"(?:public|private|protected)\s+(?:static\s+)?(\w+)\s+(\w+)\s*=\s*([^;]+)",
        csharp_source,
    ):
        fields.append((m.group(1), m.group(2), m.group(3).strip()))

    # Extract methods
    for m in re.finditer(
        r"(?:public|private|protected)\s+(?:static\s+)?(?:void|bool|int|float|string|\w+)\s+(\w+)\s*\(([^)]*)\)",
        csharp_source,
    ):
        name = m.group(1)
        if name not in lifecycle_hooks and name not in ("Awake", "Start", "Update"):
            methods.append((name, m.group(2)))

    # Detect feature usage
    uses_input = bool(re.search(r"Input\.(GetKey|GetAxis|GetMouse|GetButton)", csharp_source))
    uses_collision = "OnCollisionEnter" in lifecycle_hooks or "OnTriggerEnter" in lifecycle_hooks

    # Build Luau
    lines: list[str] = []
    lines.append(f"-- Converted from Unity C#: {class_name or 'UnknownScript'}")
    lines.append(f"-- AI transpilation recommended for full conversion")
    lines.append(f"-- Original base class: {base_class or 'MonoBehaviour'}")
    lines.append("")

    # Service imports
    services: list[str] = []
    if "Update" in lifecycle_hooks or "FixedUpdate" in lifecycle_hooks:
        services.append('local RunService = game:GetService("RunService")')
    if uses_input:
        services.append('local UserInputService = game:GetService("UserInputService")')
    if uses_collision:
        services.append('local Players = game:GetService("Players")')
    if services:
        lines.extend(services)
        lines.append("")

    # Script reference
    lines.append("local script = script")
    lines.append("local part = script.Parent")
    lines.append("")

    # Fields as local variables
    for ftype, fname, fdefault in fields:
        luau_default = _convert_default(ftype, fdefault)
        lines.append(f"local {fname} = {luau_default}")
    if fields:
        lines.append("")

    # Methods as local functions
    for mname, mparams in methods:
        luau_params = _convert_params(mparams)
        lines.append(f"local function {mname}({luau_params})")
        lines.append(f"\t-- TODO: implement {mname}")
        lines.append("end")
        lines.append("")

    # Lifecycle hooks
    if "Update" in lifecycle_hooks:
        lines.append("RunService.Heartbeat:Connect(function(dt)")
        lines.append(f"\t-- TODO: implement Update logic for {class_name}")
        lines.append("end)")
        lines.append("")

    if "FixedUpdate" in lifecycle_hooks:
        lines.append("RunService.Heartbeat:Connect(function(dt)")
        lines.append(f"\t-- TODO: implement FixedUpdate logic for {class_name}")
        lines.append("end)")
        lines.append("")

    if uses_collision:
        lines.append("if part and part:IsA('BasePart') then")
        lines.append("\tpart.Touched:Connect(function(otherPart)")
        lines.append(f"\t\t-- TODO: implement collision logic for {class_name}")
        lines.append("\tend)")
        lines.append("end")
        lines.append("")

    lines.append(f'print("{class_name or script_info.path.stem} loaded")')

    return "\n".join(lines)


def _convert_default(ctype: str, default: str) -> str:
    """Convert a C# default value to Luau."""
    default = default.strip().rstrip("f").rstrip("F")
    default = re.sub(r"\bnull\b", "nil", default)
    default = re.sub(r"\bnew\s+Vector3\(", "Vector3.new(", default)
    default = re.sub(r"\bnew\s+Vector2\(", "Vector2.new(", default)
    default = re.sub(r"\bnew\s+Color\(", "Color3.new(", default)
    default = re.sub(r"\bnew\s+\w+\(\)", "{}", default)

    # Simple type defaults
    if ctype in ("bool",):
        return default if default in ("true", "false") else "false"
    if ctype in ("int", "float", "double"):
        try:
            float(default)
            return default
        except ValueError:
            return "0"
    if ctype == "string":
        if default.startswith('"'):
            return default
        return '""'

    # Complex types
    if default in ("true", "false", "nil"):
        return default
    try:
        float(default)
        return default
    except ValueError:
        pass

    return "nil"


_LUAU_KEYWORDS = frozenset({
    "and", "break", "do", "else", "elseif", "end", "false", "for",
    "function", "if", "in", "local", "nil", "not", "or", "repeat",
    "return", "then", "true", "until", "while",
    # Luau adds these reserved contextually but rejecting them here
    # is safer than risking parser ambiguity in generated stubs.
    "continue", "type", "export",
})


def _is_luau_identifier(s: str) -> bool:
    if not s or not (s[0].isalpha() or s[0] == "_"):
        return False
    if not all(c.isalnum() or c == "_" for c in s):
        return False
    return s not in _LUAU_KEYWORDS


def _convert_params(params: str) -> str:
    """Convert a C# parameter list to a Luau parameter list.

    Naive token-tail extraction broke on default-value params like
    ``bool stopMoving = true`` -- ``parts[-1]`` would yield the literal
    ``true``, producing ``local function Foo(true)`` which fails to
    parse. Strip the ``= <default>`` tail first, then pick the
    rightmost token on the LHS as the name, and finally substitute
    ``_argN`` for anything that isn't a legal Luau identifier (out/ref
    qualifiers, generic angle-brackets, etc.) so the stub remains
    syntactically valid.
    """
    if not params.strip():
        return ""
    result = []
    for i, p in enumerate(params.split(",")):
        p = p.strip()
        # Drop default value — everything after the first ``=``.
        eq = p.find("=")
        if eq >= 0:
            p = p[:eq].strip()
        # Drop angle-bracket generics: ``List<int> items`` → ``List items``
        # but more importantly stops `<int>` tokens from polluting splits.
        while "<" in p and ">" in p:
            lt = p.find("<")
            gt = p.find(">", lt)
            if gt < 0:
                break
            p = (p[:lt] + p[gt + 1:]).strip()
        parts = p.split()
        # Strip C# parameter qualifiers (``ref``, ``out``, ``in``, ``params``)
        # so the name we pick is the actual variable name, not a keyword.
        while parts and parts[0] in ("ref", "out", "in", "params", "this"):
            parts = parts[1:]
        name = parts[-1] if parts else ""
        if not _is_luau_identifier(name):
            name = f"_arg{i + 1}"
        result.append(name)
    return ", ".join(result)
