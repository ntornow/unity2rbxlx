"""Validate that api_mappings.py has no duplicate dict keys.

Python silently keeps the last value when a literal dict has duplicate keys,
so the only way to catch them is to parse the source. A regression here
silently changes which mapping the transpiler emits.
"""
import ast
from pathlib import Path

import pytest

API_MAPPINGS_PATH = Path(__file__).parent.parent / "converter" / "api_mappings.py"


def _module_dicts() -> dict[str, ast.Dict]:
    tree = ast.parse(API_MAPPINGS_PATH.read_text())
    out: dict[str, ast.Dict] = {}
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign):
            continue
        if not isinstance(node.target, ast.Name):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        out[node.target.id] = node.value
    return out


@pytest.mark.parametrize(
    "dict_name",
    ["API_CALL_MAP", "TYPE_MAP", "LIFECYCLE_MAP", "SERVICE_IMPORTS", "UTILITY_FUNCTIONS"],
)
def test_no_duplicate_keys(dict_name: str) -> None:
    dicts = _module_dicts()
    assert dict_name in dicts, f"{dict_name} not found in api_mappings.py"
    seen: dict[str, int] = {}
    duplicates: list[tuple[str, int, int]] = []
    for key_node in dicts[dict_name].keys:
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
            continue
        key = key_node.value
        if key in seen:
            duplicates.append((key, seen[key], key_node.lineno))
        else:
            seen[key] = key_node.lineno
    if duplicates:
        msg = "\n".join(
            f"  {k!r}: first at line {first}, duplicated at line {dup}"
            for k, first, dup in duplicates
        )
        pytest.fail(f"Duplicate keys in {dict_name}:\n{msg}")
