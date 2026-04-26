"""shared_state_linter.py -- post-transpile cross-script attribute linter.

C# source often carries inter-class state via direct method calls. The AI
transpiler sometimes lowers a cross-script read to ``character:GetAttribute("X")``
even when the writing class actually exports a getter. Attributes only work
when something in the project also writes them, so an orphan ``GetAttribute``
silently returns ``nil``.

This linter rewrites those orphans to ``require(script.Parent.<Module>).<getter>()``
when the writer exposes a matching getter, and emits an UNCONVERTED entry
otherwise.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from converter.code_transpiler import TranspiledScript

log = logging.getLogger(__name__)

# Match :SetAttribute("name", ...) — captures the attribute name only.
_SET_ATTR_RE = re.compile(r":SetAttribute\(\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]")

# Match a `<target>:GetAttribute("name")` call. <target> may be a simple
# identifier or a chain like script.Parent — captured loosely so we can
# replace the whole call site. The trailing arg slot is empty (Roblox
# GetAttribute takes no extra args).
_GET_ATTR_RE = re.compile(
    r"(?P<target>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)"
    r":GetAttribute\(\s*['\"](?P<attr>[A-Za-z_][\w]*)['\"]\s*\)"
)

# Match exported ModuleScript methods/getters of the form
# ``function ModuleTable.method(`` or ``ModuleTable.method = function``.
_METHOD_FORM_RE = re.compile(r"function\s+(\w+)\.(\w+)\s*\(")
_FIELD_FORM_RE = re.compile(r"\b(\w+)\.(\w+)\s*=\s*function\b")

# Match the trailing ``return <Identifier>`` that closes a ModuleScript.
_MODULE_RETURN_RE = re.compile(r"^return\s+([A-Za-z_]\w*)\s*$", re.MULTILINE)


def _candidate_methods(attr: str) -> list[str]:
    """Return the method names that could expose ``attr`` as a getter."""
    cap = attr[:1].upper() + attr[1:]
    return [attr, f"get{cap}", f"Get{cap}", f"is{cap}", f"has{cap}"]


def _collect_writers(scripts: list[TranspiledScript]) -> set[str]:
    """Names of every attribute that something in the project writes."""
    writers: set[str] = set()
    for s in scripts:
        for m in _SET_ATTR_RE.finditer(s.luau_source):
            writers.add(m.group(1))
    return writers


def _collect_exports(scripts: list[TranspiledScript]) -> dict[str, tuple[str, str]]:
    """Map attribute name -> (module_name, method_name) for ModuleScript getters.

    Only ModuleScripts that end with ``return <Tbl>`` count; the ``<Tbl>`` name
    must match the table the method is attached to.
    """
    exports: dict[str, tuple[str, str]] = {}
    for s in scripts:
        if s.script_type != "ModuleScript":
            continue
        ret = _MODULE_RETURN_RE.search(s.luau_source)
        if not ret:
            continue
        table_name = ret.group(1)
        module_name = Path(s.output_filename).stem

        method_names: set[str] = set()
        for m in _METHOD_FORM_RE.finditer(s.luau_source):
            if m.group(1) == table_name:
                method_names.add(m.group(2))
        for m in _FIELD_FORM_RE.finditer(s.luau_source):
            if m.group(1) == table_name:
                method_names.add(m.group(2))

        # Index by every attribute that maps to one of these methods.
        for method in method_names:
            for attr in _attrs_for_method(method):
                # First-writer-wins: don't let a later module override
                # an earlier mapping for the same attribute.
                exports.setdefault(attr, (module_name, method))
    return exports


def _attrs_for_method(method: str) -> list[str]:
    """Inverse of _candidate_methods — what attribute names does a method serve?"""
    out = [method]
    for prefix in ("get", "Get", "is", "has"):
        if method.startswith(prefix) and len(method) > len(prefix):
            stripped = method[len(prefix):]
            out.append(stripped[:1].lower() + stripped[1:])
    return out


def lint_and_rewrite(
    scripts: list[TranspiledScript],
) -> list[dict[str, str]]:
    """Rewrite orphan GetAttribute calls in place; return UNCONVERTED entries.

    A GetAttribute call is "orphan" when no script in the project does a
    matching SetAttribute. If the writing class instead exports a getter,
    the call is rewritten to ``require(script.Parent.<Module>).<method>()``.
    Otherwise an entry is added to the returned list so it surfaces in
    UNCONVERTED.md.
    """
    if not scripts:
        return []

    writers = _collect_writers(scripts)
    exports = _collect_exports(scripts)

    seen_orphans: set[tuple[str, str]] = set()
    unconverted: list[dict[str, str]] = []
    rewrite_count = 0

    for script in scripts:
        if ":GetAttribute(" not in script.luau_source:
            continue

        consumer = Path(script.output_filename).stem
        rewrites_in_script = 0

        def _replace(match: re.Match[str]) -> str:
            nonlocal rewrites_in_script
            attr = match.group("attr")
            if attr in writers:
                return match.group(0)
            export = exports.get(attr)
            # A self-export can't be the fix -- rewriting to require(self)
            # would cycle. Treat as if no export exists so the orphan still
            # shows up in UNCONVERTED.md.
            if export is None or export[0] == consumer:
                key = (consumer, attr)
                if key not in seen_orphans:
                    seen_orphans.add(key)
                    unconverted.append({
                        "category": "shared_state",
                        "item": f"{consumer}:GetAttribute(\"{attr}\")",
                        "reason": (
                            "No script writes this attribute and no module "
                            "exports a matching getter. The read returns nil."
                        ),
                    })
                return match.group(0)
            module, method = export
            rewrites_in_script += 1
            return f"require(script.Parent:WaitForChild(\"{module}\")).{method}()"

        script.luau_source = _GET_ATTR_RE.sub(_replace, script.luau_source)
        rewrite_count += rewrites_in_script

    if rewrite_count or unconverted:
        log.info(
            "[shared_state_linter] %d GetAttribute call(s) rewritten, "
            "%d orphan(s) flagged",
            rewrite_count, len(unconverted),
        )
    return unconverted
