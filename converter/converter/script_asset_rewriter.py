"""
script_asset_rewriter.py -- Post-transpilation pass to substitute uploaded asset IDs
into Luau script source code.

Scans transpiled Luau scripts for references to local asset paths and replaces them
with rbxassetid:// URLs from the uploaded_assets dict.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from core.roblox_types import RbxScript

log = logging.getLogger(__name__)

# Minimum key length to avoid false positives on short stems like "file".
_MIN_KEY_LEN = 8


def rewrite_asset_references(
    scripts: list[RbxScript],
    uploaded_assets: dict[str, str],
    guid_index: object | None = None,
) -> int:
    """Rewrite local asset paths in Luau scripts to rbxassetid:// URLs.

    Builds a lookup from uploaded_assets and scans each script's source for
    matching paths/filenames inside string literals. Only replaces strings that
    look like asset references (contain path separators or file extensions).

    Args:
        scripts: List of RbxScript objects to modify in-place.
        uploaded_assets: Dict mapping local asset paths to rbxassetid:// URLs.
        guid_index: Optional GUID resolver (for GUID-based references).

    Returns:
        Total number of replacements made across all scripts.
    """
    if not uploaded_assets:
        return 0

    # Build a lookup of candidate keys -> rbxassetid URLs.
    # Only include keys that are specific enough to avoid false positives.
    replacements: dict[str, str] = {}

    for local_path, asset_url in uploaded_assets.items():
        p = Path(local_path)

        # Full path (always safe to match)
        replacements[local_path] = asset_url
        replacements[local_path.replace("\\", "/")] = asset_url

        # Filename with extension (e.g., "Diamond.png") — specific enough
        if p.suffix:
            replacements[p.name] = asset_url

        # Relative "Assets/..." form
        parts = p.parts
        for i, part in enumerate(parts):
            if part == "Assets":
                rel = "/".join(parts[i:])
                replacements[rel] = asset_url
                # Also without extension (Unity Resources.Load style)
                if "." in rel:
                    rel_no_ext = rel.rsplit(".", 1)[0]
                    replacements[rel_no_ext] = asset_url
                break

    if not replacements:
        return 0

    # Filter out keys that are too short to be safe.
    safe_keys = sorted(
        [k for k in replacements if len(k) >= _MIN_KEY_LEN],
        key=len,
        reverse=True,  # Longest match first
    )

    if not safe_keys:
        return 0

    # Match quoted string literals in Luau source.
    string_pattern = re.compile(
        r'("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')',
        re.DOTALL,
    )

    total_rewrites = 0

    for script in scripts:
        original = script.source

        def replace_in_string(m: re.Match) -> str:
            s = m.group(0)
            quote = s[0]
            inner = s[1:-1]

            # Only consider strings that look like asset references:
            # - contain a path separator
            # - contain a file extension
            # - are an exact match for a known asset key
            looks_like_path = ("/" in inner or "\\" in inner or
                               re.search(r'\.\w{2,4}$', inner))

            if not looks_like_path:
                # Check if the entire string content exactly matches a key
                if inner not in replacements:
                    return s

            # Try to match against known asset keys.
            for key in safe_keys:
                if key in inner:
                    asset_url = replacements[key]
                    # If the string IS the path (or very close), replace entirely.
                    # Otherwise, do a targeted substitution within the string.
                    if inner == key or inner.endswith(key):
                        return f'{quote}{asset_url}{quote}'
                    else:
                        inner_new = inner.replace(key, asset_url)
                        return f'{quote}{inner_new}{quote}'
            return s

        new_source = string_pattern.sub(replace_in_string, original)
        if new_source != original:
            script.source = new_source
            total_rewrites += 1

    return total_rewrites
