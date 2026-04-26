"""
credentials.py -- Resolve Roblox Open Cloud credentials from CLI/env/file.

Single source for the precedence rule used by both ``u2r.py`` and
``convert_interactive.py``:

1. CLI argument (literal value, or path to a file containing the value)
2. Environment variable
3. Auto-discover a file named ``filename`` under the project's parent
   directory, parent.parent, or the current working directory.
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_credential(
    cli_value: str | None,
    env_var: str,
    filename: str,
    project_path: Path,
) -> str | None:
    """Return a credential string, or None if no source has it.

    ``cli_value`` may be the literal credential or a path to a file whose
    contents are the credential. ``project_path`` anchors the auto-discovery
    search.
    """
    if cli_value:
        p = Path(cli_value)
        if p.is_file():
            return p.read_text().strip()
        return cli_value.strip()

    val = os.environ.get(env_var, "")
    if val:
        return val.strip()

    for search_dir in (project_path.parent, project_path.parent.parent, Path.cwd()):
        fp = search_dir / filename
        if fp.exists():
            return fp.read_text().strip()

    return None
