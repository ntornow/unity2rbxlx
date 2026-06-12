"""Tests for the child-index lowering pass (generic allowlist).

The transpiler flattens Unity ``transform.GetChild(n)`` to
``<recv>:GetChildren()[n+1]``. The converter injects an AudioSource->Sound at
child index 0 of Turret-like Parts, so the naive index returns the Sound and a
following ``:GetPivot()`` crashes. ``lower_child_index`` rewrites each such
site to ``__unityChild(recv, N)`` -- the SAME shared helper the legacy
coherence pack (``_fix_unity_transform_child_index``) uses, which resolves the
N-th authored child (prefer the N-th ``_SceneRuntimeId``-stamped child, else
the N-th ``Model``/``BasePart``, else ``nil``).

This file covers the GENERIC-mode delta only: the ``lower_child_index`` entry
point over ``luau_source`` (the legacy pack's behavior over ``RbxScript.source``
is covered by ``test_unity_transform_child_index.py``); the code-aware
helper-injection guard; nested-chain non-corruption; idempotency; and the
generic-pipeline wiring (``transpile_with_contract`` actually invokes it).
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.child_index_lowering import (  # noqa: E402
    lower_child_index,
)
from converter.code_transpiler import (  # noqa: E402
    TranspilationResult,
    TranspiledScript,
)


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
    """Acceptance 1 (generic entry): the flattened GetChild(0) no longer
    returns ``GetChildren()[1]`` (the injected Sound); it resolves via
    ``__unityChild`` over ``luau_source``, the helper is injected once, and the
    receiver + index are preserved."""
    s = _S(_TURRET)
    n = lower_child_index([s])
    assert n == 1
    assert "self.gameObject:GetChildren()[1]" not in s.luau_source
    assert "__unityChild(self.gameObject, 1)" in s.luau_source
    assert s.luau_source.count("local function __unityChild(") == 1
    assert "_SceneRuntimeId" in s.luau_source
    assert 'IsA("BasePart")' in s.luau_source
    assert 'IsA("Model")' in s.luau_source


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
    assert "self.gameObject:GetChildren()[2]" not in s.luau_source
    assert "__unityChild(self.gameObject, 2)" in s.luau_source


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


def test_helper_in_comment_only_still_injects_real_helper() -> None:
    """P1 #1 (code-aware guard): a source whose ONLY
    ``local function __unityChild(`` occurrence is inside a comment -- AND which
    has a real ``recv:GetChildren()[1]`` site -- must STILL get the real helper
    injected (else the rewritten call resolves to an undefined helper at
    runtime). The pre-fix raw substring guard suppressed injection here.

    Asserts exactly ONE code-position helper definition exists after the pass."""
    src = textwrap.dedent("""\
        -- historical note: local function __unityChild(p, i) used to live here
        function M:base()
            return self.gameObject:GetChildren()[1]
        end
    """)
    s = _S(src)
    n = lower_child_index([s])
    assert n == 1
    # The call site was rewritten...
    assert "__unityChild(self.gameObject, 1)" in s.luau_source
    # ...and a REAL helper definition is present (the comment occurrence does
    # not count). Exactly one code-position definition resolves the call.
    from converter.child_index_lowering import _luau_pos_is_code

    code_defs = sum(
        1
        for i in range(len(s.luau_source))
        if s.luau_source.startswith("local function __unityChild(", i)
        and _luau_pos_is_code(s.luau_source, i)
    )
    assert code_defs == 1
    # The comment occurrence is still present (untouched), so a raw substring
    # count would be >= 2 -- proving the guard is now code-aware.
    assert s.luau_source.count("local function __unityChild(") == 2


def test_nested_chain_no_corruption_inner_rewritten() -> None:
    """A flattened nested ``transform.GetChild(0).GetChild(0)`` ->
    ``a:GetChildren()[1]:GetChildren()[1]``. The simple-receiver regex cannot
    match a receiver containing ``()``/``[]`` and ``re.sub`` is non-overlapping,
    so ONLY the inner site rewrites; the source is not corrupted."""
    src = "local x = a:GetChildren()[1]:GetChildren()[1]\n"
    s = _S(src)
    n = lower_child_index([s])
    assert n == 1
    assert "__unityChild(a, 1):GetChildren()[1]" in s.luau_source
    assert s.luau_source.count("__unityChild(a, 1)") == 1
    assert s.luau_source.count(":GetChildren()[1]") == 1


def test_idempotent_twice_applied() -> None:
    """Edge case 5 / acceptance: re-running the pass yields identical output
    (the GetChildren()[literal] fingerprint is gone after the first pass; the
    rewritten ``__unityChild(...)`` receivers contain ()/[] so re-running the
    simple-receiver regex finds nothing) and the helper is not re-injected."""
    s = _S(_TURRET)
    n1 = lower_child_index([s])
    after_first = s.luau_source
    n2 = lower_child_index([s])
    assert n1 == 1
    assert n2 == 0
    assert s.luau_source == after_first
    assert s.luau_source.count("local function __unityChild(") == 1


# --- Generic-pipeline wiring (P1 #2) ---------------------------------------


class _PInfo:
    """Minimal ``ScriptInfo`` stand-in for ``transpile_with_contract`` --
    it reads only ``path``, ``class_name`` (via the planner join) and
    ``referenced_types`` (unused here)."""

    def __init__(self, path: Path, class_name: str) -> None:
        self.path = path
        self.class_name = class_name
        self.referenced_types: list[str] = []


# A turret/cam-shaped script carrying a flattened GetChild emission
# (``self.cam:GetChildren()[1]``), but NOT a player controller (no WASD
# method + no CharacterController) so the movement/camera passes leave it and
# the only lowering that should fire is child-index.
_TURRET_PIPELINE_SRC = textwrap.dedent("""\
    local Turret = {}
    Turret.__index = Turret

    function Turret:Awake()
        self.cam = workspace.CurrentCamera
    end

    function Turret:_tBase()
        -- transform.GetChild(0) flattened
        return self.cam:GetChildren()[1]
    end

    function Turret:_fire()
        local base = self:_tBase()
        if base then
            return base:GetPivot().Position
        end
        return nil
    end

    return Turret
""")


class TestPipelineInvocation:
    """Drives the REAL ``contract_pipeline.transpile_with_contract`` (generic
    mode). The post-transpile ``lower_child_index`` pass is RETIRED — child-ref
    handling moved to the pre-transpile ``child_ref_resolver``, so the generic
    path no longer lowers an emitted ``GetChildren()[n]`` ordinal. This test
    pins that retirement: a surviving ``self.cam:GetChildren()[1]`` passes through
    ``transpile_with_contract`` VERBATIM, with no ``__unityChild`` injection.
    ``transpile_scripts`` is stubbed so the test never hits the API."""

    def test_generic_pipeline_no_longer_lowers_child_index(self) -> None:
        from converter import contract_pipeline

        turret_path = Path("/proj/Assets/Turret.cs")
        infos = [_PInfo(turret_path, "Turret")]
        scene_runtime = {
            "modules": {
                "guid-turret": {
                    "stem": "Turret",
                    "class_name": "Turret",
                    "runtime_bearing": True,
                    "is_component_class": True,
                    "character_attached": False,
                    "is_loader": False,
                },
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
        }

        turret_script = TranspiledScript(
            source_path=str(turret_path),
            output_filename="Turret.luau",
            csharp_source="",
            luau_source=_TURRET_PIPELINE_SRC,
            strategy="ai",
            confidence=1.0,
            script_type="ModuleScript",
        )
        stub_result = TranspilationResult()
        stub_result.total_transpiled = 1
        stub_result.scripts.append(turret_script)

        with patch(
            "converter.contract_pipeline.transpile_scripts",
            return_value=stub_result,
        ) as mock_transpile:
            result = contract_pipeline.transpile_with_contract(
                "/proj",
                infos,
                scene_runtime=scene_runtime,
                use_ai=False,
            )

        assert mock_transpile.called, (
            "transpile_with_contract must call transpile_scripts (stubbed)."
        )

        out_src = result.transpilation.scripts[0].luau_source
        # The flattened GetChild site survives VERBATIM — the post-transpile
        # lowering is retired; the pre-transpile resolver owns child refs now.
        assert "self.cam:GetChildren()[1]" in out_src
        # No __unityChild injection from the (removed) lowering pass.
        assert "__unityChild(self.cam, 1)" not in out_src
        assert "local function __unityChild(" not in out_src
