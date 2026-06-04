"""Tests for runtime/scene_camera_input.luau — the generic first-person
camera/input service (PR5).

Two layers:
  * Pure look-state logic (`_advance`) under the standalone luau interpreter —
    yaw is UNBOUNDED (the bug was yaw pinned to 0), pitch clamps, signs match
    Roblox's positive-down GetMouseDelta().Y. (`_composeLook` uses CFrame, a
    Roblox datatype absent from standalone luau, so it's verified in e2e.)
  * A scope-cap guard (pure Python) — the service's public API must NOT grow
    into locomotion/weapon territory ("owning locomotion = rebuilding Unity").
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

SERVICE_PATH = Path(__file__).parent.parent / "runtime" / "scene_camera_input.luau"


def _luau_available() -> bool:
    return shutil.which("luau") is not None


# ---------------------------------------------------------------------------
# Pure look-state logic under luau
# ---------------------------------------------------------------------------

def _load_preamble() -> str:
    """Mock just the load-time Roblox surface (the module only calls
    game:GetService at module scope) and expose the loaded service."""
    src = SERVICE_PATH.read_text(encoding="utf-8")
    delim = "==="
    while f"]{delim}]" in src or f"[{delim}[" in src:
        delim += "="
    embedded = f"[{delim}[\n{src}\n]{delim}]"
    return textwrap.dedent(f"""\
        local function noop() end
        local _svc = setmetatable({{}}, {{__index = function() return noop end}})
        game = {{ GetService = function(_, _n) return _svc end }}
        workspace = {{ CurrentCamera = nil,
            GetAttribute = function() return nil end, SetAttribute = noop }}
        local SRC = {embedded}
        local chunk, err = loadstring(SRC, "scene_camera_input")
        assert(chunk, "load failed: " .. tostring(err))
        local SceneCameraInput = chunk()
    """)


def _run(scenario: str) -> tuple[int, str, str]:
    script = _load_preamble() + "\n" + scenario + "\n"
    with tempfile.NamedTemporaryFile(suffix=".luau", mode="w", delete=False) as f:
        f.write(script)
        path = f.name
    try:
        r = subprocess.run(["luau", path], capture_output=True, text=True, timeout=15)
        return r.returncode, r.stdout, r.stderr
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.mark.skipif(
    not _luau_available() or not SERVICE_PATH.exists(),
    reason="needs standalone luau + service file",
)
class TestAdvanceLookState:
    def test_yaw_pitch_and_clamps(self) -> None:
        rc, out, err = _run(textwrap.dedent("""\
            local function approx(a, b) return math.abs(a - b) < 1e-6 end
            -- yaw subtracts dx*sensitivity; pitch unchanged when dy==0
            local y, p = SceneCameraInput._advance(0, 0, 100, 0, 0.01, -1, 1)
            assert(approx(y, -1), "yaw should be -1, got " .. tostring(y))
            assert(approx(p, 0), "pitch should be 0, got " .. tostring(p))
            -- THE FIX: yaw is UNBOUNDED — it accumulates past 0 (was pinned to 0)
            local y2 = SceneCameraInput._advance(-5, 0, 100, 0, 0.01, -10, 10)
            assert(approx(y2, -6), "yaw must accumulate, got " .. tostring(y2))
            -- pitch clamps to both bounds
            local _, pmin = SceneCameraInput._advance(0, 0, 0, 100000, 0.01, -1, 1)
            assert(approx(pmin, -1), "pitch clamps to min")
            local _, pmax = SceneCameraInput._advance(0, 0, 0, -100000, 0.01, -1, 1)
            assert(approx(pmax, 1), "pitch clamps to max")
            -- sign: positive dy (mouse down) lowers pitch (Roblox Y is +down)
            local _, pdown = SceneCameraInput._advance(0, 0, 0, 10, 0.01, -1, 1)
            assert(pdown < 0, "dy>0 must lower pitch")
            print("DONE")
        """))
        assert rc == 0, f"luau failed: {err or out}"
        assert "DONE" in out


# ---------------------------------------------------------------------------
# Scope-cap guard (pure Python — no luau needed)
# ---------------------------------------------------------------------------

class TestScopeCap:
    # The capped public surface (everything else must be _-prefixed private).
    _ALLOWED_PUBLIC = {
        "acquire", "configure", "step", "applyRecoil",
        "getYawBasis", "getLookCFrame", "onRespawn",
    }
    # Words that would signal the service is absorbing locomotion/weapons.
    _FORBIDDEN_SUBSTRINGS = (
        "move", "walk", "jump", "strafe", "shoot", "fire",
        "weapon", "ammo", "reload", "damage", "velocity",
    )

    def test_public_api_within_cap(self) -> None:
        src = SERVICE_PATH.read_text(encoding="utf-8")
        # Public methods: ``function SceneCameraInput:Name(`` / ``.Name(``
        # whose name does not start with an underscore.
        methods = set(re.findall(
            r"function\s+SceneCameraInput[.:]([A-Za-z]\w*)\s*\(", src,
        ))
        public = {m for m in methods if not m.startswith("_")}
        extra = public - self._ALLOWED_PUBLIC
        assert not extra, (
            f"scene_camera_input grew public API beyond the scope cap: {sorted(extra)}. "
            "If intentional, update the cap AND the design-doc scope list."
        )

    def test_no_locomotion_or_weapon_methods(self) -> None:
        src = SERVICE_PATH.read_text(encoding="utf-8")
        names = re.findall(r"function\s+SceneCameraInput[.:](\w+)\s*\(", src)
        for n in names:
            low = n.lower()
            for bad in self._FORBIDDEN_SUBSTRINGS:
                assert bad not in low, (
                    f"method {n!r} suggests the service is crossing the scope cap "
                    f"(matched {bad!r}) — locomotion/weapons stay in the controller."
                )
