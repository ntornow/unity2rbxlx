"""Tests for the child-index lowering pass (generic allowlist).

The transpiler flattens Unity ``transform.GetChild(n)`` to
``<recv>:GetChildren()[n+1]``. The converter injects an AudioSource->Sound at
child index 0 of Turret-like Parts, so the naive index returns the Sound and a
following ``:GetPivot()`` crashes. ``lower_child_index`` rewrites each such
site to a structure-gated N-th-SPATIAL-child resolver (prefer the N-th
``_SceneRuntimeId``-stamped child, else the N-th ``BasePart``/``Model``).

The rule is GENERAL (keyed on the ``:GetChildren()[<literal>]`` emission shape,
never ``s.name``): it applies to any GetChild site, never just the turret.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.child_index_lowering import lower_child_index  # noqa: E402


class _S:
    """Minimal TranspiledScript stand-in (carries ``luau_source``)."""

    def __init__(self, src: str) -> None:
        self.luau_source = src


# The real Turret.luau emission shape (child[0]=injected Sound, child[1]=Base).
_TURRET = textwrap.dedent("""\
    local Turret = {}
    Turret.__index = Turret

    -- transform.GetChild(0)
    function Turret:_tBase()
        return self.gameObject:GetChildren()[1]
    end

    function Turret:_fire()
        local base = self:_tBase()
        if base then
            return base:GetPivot().Position
        end
        return nil
    end
""")


def test_turret_getchild_resolves_to_spatial_child_not_sound() -> None:
    """Acceptance 1: the flattened GetChild(0) no longer returns
    ``GetChildren()[1]`` (the injected Sound); it resolves the N-th spatial
    child, so a following :GetPivot() targets a Part, not the Sound."""
    s = _S(_TURRET)
    n = lower_child_index([s])
    assert n == 1
    # The naive index that hit the Sound is gone...
    assert ":GetChildren()[1]" not in s.luau_source
    # ...replaced by a structure-gated resolver that skips non-spatial children.
    assert "_SceneRuntimeId" in s.luau_source
    assert 'IsA("BasePart")' in s.luau_source
    assert 'IsA("Model")' in s.luau_source
    # The receiver is preserved verbatim inside the resolver.
    assert "(self.gameObject)" in s.luau_source


def test_resolver_skips_sound_at_index_0_picks_base() -> None:
    """Acceptance 1/2: prove the emitted resolver's iteration would skip a
    non-spatial child. We re-derive the intended semantics: count only spatial
    children, so child[0]=Sound is skipped and the 1st spatial child wins."""
    s = _S(_TURRET)
    lower_child_index([s])
    # The resolver counts spatial matches with ``__n``; index literal preserved.
    assert "__n == 1" in s.luau_source
    # It returns nil when no N-th spatial child exists (abstain, no crash).
    assert "return nil end)" in s.luau_source


def test_general_non_turret_getchild_site_is_lowered() -> None:
    """Acceptance 2: the rule is structure-gated, not turret-name-gated. A
    script with no turret identity but a GetChild emission is still lowered."""
    src = textwrap.dedent("""\
        local Elevator = {}
        function Elevator:platform()
            return self.gameObject:GetChildren()[2]
        end
    """)
    s = _S(src)
    n = lower_child_index([s])
    assert n == 1
    assert ":GetChildren()[2]" not in s.luau_source
    # N is preserved as the spatial-child ordinal.
    assert "__n == 2" in s.luau_source
    assert "(self.gameObject)" in s.luau_source


def test_variable_index_is_not_lowered() -> None:
    """A genuine dynamic lookup ``GetChildren()[i]`` (not a flattened constant
    GetChild) must NOT be rewritten -- only integer-literal indices."""
    src = textwrap.dedent("""\
        function M:pick(i)
            return self.gameObject:GetChildren()[i]
        end
    """)
    s = _S(src)
    before = s.luau_source
    n = lower_child_index([s])
    assert n == 0
    assert s.luau_source == before


def test_index_inside_string_or_comment_is_not_lowered() -> None:
    """Acceptance: structure-gate on CODE only -- a GetChildren()[1] inside a
    string literal or comment is never a signal."""
    src = textwrap.dedent("""\
        -- self.gameObject:GetChildren()[1] is the historical shape
        local doc = "call :GetChildren()[1] to fetch the base"
        function M:noop()
            return nil
        end
    """)
    s = _S(src)
    before = s.luau_source
    n = lower_child_index([s])
    assert n == 0
    assert s.luau_source == before


def test_abstain_returns_nil_not_crash() -> None:
    """Edge case 1: fewer real spatial children than the index -> the resolver
    returns nil (the existing ``if base then`` guards handle it), it does not
    crash. We assert the emitted expression has a terminal ``return nil``."""
    s = _S(_TURRET)
    lower_child_index([s])
    # Two tiers each fall through to the shared terminal ``return nil``.
    assert s.luau_source.count("return nil end)") == 1


def test_idempotent_twice_applied() -> None:
    """Edge case 5 / acceptance: re-running the pass yields identical output
    (the GetChildren()[literal] fingerprint is gone after the first pass)."""
    s = _S(_TURRET)
    n1 = lower_child_index([s])
    after_first = s.luau_source
    n2 = lower_child_index([s])
    assert n1 == 1
    assert n2 == 0
    assert s.luau_source == after_first


def test_multiple_getchild_sites_in_one_script() -> None:
    """All GetChild emissions in a script are lowered, with their distinct
    receivers and indices preserved (right-to-left splice keeps offsets sane)."""
    src = textwrap.dedent("""\
        function Turret:_tBase()
            return self.gameObject:GetChildren()[1]
        end
        function Turret:_tWeapon()
            local base = self:_tBase()
            return base:GetChildren()[1]
        end
        function Turret:_tThird()
            return self.gameObject:GetChildren()[3]
        end
    """)
    s = _S(src)
    n = lower_child_index([s])
    assert n == 1
    assert ":GetChildren()[1]" not in s.luau_source
    assert ":GetChildren()[3]" not in s.luau_source
    # Distinct receivers preserved.
    assert "(self.gameObject)" in s.luau_source
    assert "(base)" in s.luau_source
    # Third index preserved as ordinal 3.
    assert "__n == 3" in s.luau_source


def test_empty_and_no_match_scripts() -> None:
    """No GetChild sites -> no change, count 0; empty source is safe."""
    a = _S("")
    b = _S("function M:f() return self.x end")
    n = lower_child_index([a, b])
    assert n == 0
    assert a.luau_source == ""
    assert b.luau_source == "function M:f() return self.x end"
