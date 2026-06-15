"""Phase 1 (relation #8): behavioral tests for the host ``applyImpulse`` primitive in
``converter/runtime/scene_runtime.luau``. Drives the production runtime through standalone
``luau`` with mock BaseParts and asserts the faithful launch velocity
``Δv = (force / _UnityMass) * STUDS_PER_METER``. Skips cleanly when ``luau`` is absent.

Reuses the shared harness from ``test_scene_runtime_host_behavior`` (loads the real
``scene_runtime.luau`` into a standalone luau chunk and exposes ``SceneRuntime``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_scene_runtime_host_behavior import _luau_available, _run_scenario  # noqa: E402

pytestmark = pytest.mark.skipif(
    not _luau_available(),
    reason="needs standalone luau interpreter + host runtime file",
)

# A scalar stands in for the impulse force vector — the host arithmetic
# (force / m_unity) * STUDS_PER_METER + velocity is identical for a number, and
# standalone luau has no Vector3. STUDS_PER_METER is 3.571.
_MOCK = """
local function mockPart(attrs, root, ancestorModel)
    local p = { AssemblyLinearVelocity = 0, _impulsed = nil, AssemblyRootPart = root }
    function p:IsA(c) return c == "BasePart" end
    function p:GetAttribute(k) return attrs[k] end
    function p:FindFirstAncestorWhichIsA(c) if c == "Model" then return ancestorModel end return nil end
    function p:ApplyImpulse(f) self._impulsed = f end
    return p
end
"""


class TestApplyImpulse:
    def test_faithful_launch_mass_one(self):
        # A2: m_unity=1, force=60 -> 60 * 3.571 = 214.26 studs/s
        rc, out, err = _run_scenario(_MOCK + """
        local p = mockPart({ _UnityMass = 1.0 })
        SceneRuntime:applyImpulse(p, 60)
        print(string.format("A2_v=%.2f", p.AssemblyLinearVelocity))
        """)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "A2_v=214.26" in out, out

    def test_mass_agnostic_formula_scales_by_unity_mass(self):
        # A2b: the velocity uses the STAMPED Unity mass, never the Roblox mass.
        # m_unity=2.5, force=60 -> 60/2.5 * 3.571 = 85.70
        rc, out, err = _run_scenario(_MOCK + """
        local p = mockPart({ _UnityMass = 2.5 })
        SceneRuntime:applyImpulse(p, 60)
        print(string.format("A2b_v=%.2f", p.AssemblyLinearVelocity))
        """)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "A2b_v=85.70" in out, out

    def test_unstamped_falls_back_to_raw_impulse(self):
        # A3: no _UnityMass -> raw ApplyImpulse, velocity untouched (never worse than today).
        rc, out, err = _run_scenario(_MOCK + """
        local p = mockPart({})
        SceneRuntime:applyImpulse(p, 60)
        print(string.format("A3_raw=%s v=%.2f", tostring(p._impulsed), p.AssemblyLinearVelocity))
        """)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "A3_raw=60 v=0.00" in out, out

    def test_wrapped_resolves_unity_mass_on_inner_body(self):
        # A8: GetComponent returned a part with no _UnityMass; the 3-tier resolution finds the
        # stamped inner BasePart via the owning Model and launches IT (not the queried part).
        rc, out, err = _run_scenario(_MOCK + """
        local inner = mockPart({ _UnityMass = 1.0 })
        local owner = {}
        function owner:GetDescendants() return { inner } end
        local queried = mockPart({}, nil, owner)
        SceneRuntime:applyImpulse(queried, 60)
        print(string.format("A8_inner=%.2f queried=%.2f", inner.AssemblyLinearVelocity, queried.AssemblyLinearVelocity))
        """)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "A8_inner=214.26 queried=0.00" in out, out

    def test_assembly_root_tier_resolution(self):
        # Tier 2: _UnityMass is not on the queried part but on its AssemblyRootPart.
        rc, out, err = _run_scenario(_MOCK + """
        local root = mockPart({ _UnityMass = 1.0 })
        local queried = mockPart({}, root)
        SceneRuntime:applyImpulse(queried, 60)
        print(string.format("ROOT_v=%.2f queried=%.2f", root.AssemblyLinearVelocity, queried.AssemblyLinearVelocity))
        """)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "ROOT_v=214.26 queried=0.00" in out, out

    def test_wrong_typed_arg_is_safe_noop(self):
        # MAJOR fix: a non-Instance arg (number / table without IsA) must soft-return, not hard-error.
        rc, out, err = _run_scenario(_MOCK + """
        SceneRuntime:applyImpulse(42, 60)
        SceneRuntime:applyImpulse({}, 60)
        SceneRuntime:applyImpulse(nil, 60)
        print("SAFE_OK")
        """)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "SAFE_OK" in out, out

    def test_host_wrapper_dotted_and_colon_dispatch(self):
        # The self.host.applyImpulse wrapper routes both dotted and colon call forms to the engine.
        rc, out, err = _run_scenario(_MOCK + """
        local rt = setmetatable({}, SceneRuntime)
        local host = rt:_makeHostSurface({})
        local pdot = mockPart({ _UnityMass = 1.0 })
        host.applyImpulse(pdot, 60)              -- dotted
        local pcol = mockPart({ _UnityMass = 1.0 })
        host:applyImpulse(pcol, 60)             -- colon (arg1 == host)
        print(string.format("DOT_v=%.2f COL_v=%.2f", pdot.AssemblyLinearVelocity, pcol.AssemblyLinearVelocity))
        """)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "DOT_v=214.26 COL_v=214.26" in out, out
