"""Tests for the OnTriggerEnter/Exit lowering pass (generic allowlist).

The generic transpiler collapses Unity OnTriggerEnter/Exit AND OnCollisionEnter/
Exit onto the same ``.Touched``/``.TouchEnded`` EDGE binding resolved via
``getTouchPart(go)`` (the body). ``lower_trigger_enter`` rewrites the SPECIFIC
``connectGameObjectSignal(...)`` binding whose immediately-preceding origin
comment is ``-- OnTriggerEnter...`` / ``-- OnTriggerExit...`` to the trigger-
preferring host method ``connectGameObjectTriggerSignal(...)`` (a pure method-
head rename; all args preserved). It must:
  * leave OnCollisionEnter/Exit/Stay edge bindings untouched (body-bound);
  * leave OnTriggerStay untouched (lowered to the Stay poll by an earlier pass);
  * anchor on the comment immediately above the binding it rewrites;
  * be idempotent and abstain inside strings/comments;
  * be wired into the real generic pipeline (``transpile_with_contract``).
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.code_transpiler import (  # noqa: E402
    TranspilationResult,
    TranspiledScript,
)
from converter.trigger_enter_lowering import (  # noqa: E402
    lower_trigger_enter,
    rewrite_trigger_enter_source,
)


class _S:
    """Minimal TranspiledScript stand-in (carries ``luau_source``)."""

    def __init__(self, src: str) -> None:
        self.luau_source = src


# A mine-shaped ``Awake`` with the OnTriggerEnter->Touched edge binding under
# its mandated origin comment.
_MINE_ENTER = textwrap.dedent("""\
    local Mine = {}
    Mine.__index = Mine

    function Mine:Awake()
        -- OnTriggerEnter(other)
        self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
            local plr = self.host.playerFromTouch(other)
            if not plr then return end
            self.host.invoke(self, "Explode", self.explodeTime)
        end)
    end

    return Mine
""")


def test_mine_ontriggerenter_binding_is_lowered() -> None:
    """The ``-- OnTriggerEnter``-origin ``connectGameObjectSignal(go, "Touched",
    fn)`` binding is renamed to ``connectGameObjectTriggerSignal(...)`` -- the
    ``"Touched"`` signal name AND the receiver/function body preserved verbatim."""
    s = _S(_MINE_ENTER)
    n = lower_trigger_enter([s])
    assert n == 1
    assert (
        'self.host:connectGameObjectTriggerSignal(self.gameObject, "Touched", '
        "function(other)" in s.luau_source
    )
    # The bare connectGameObjectSignal head no longer present for this binding.
    assert (
        'self.host:connectGameObjectSignal(self.gameObject, "Touched"'
        not in s.luau_source
    )
    # Args untouched -> body intact.
    assert 'self.host.invoke(self, "Explode", self.explodeTime)' in s.luau_source


def test_ontriggerexit_touchended_is_lowered() -> None:
    """OnTriggerExit (->"TouchEnded") is also rewritten; signal name preserved."""
    src = textwrap.dedent("""\
        function Zone:Awake()
            -- OnTriggerExit(other)
            self.host:connectGameObjectSignal(self.gameObject, "TouchEnded", function(other)
                self:_left(other)
            end)
        end
    """)
    s = _S(src)
    assert lower_trigger_enter([s]) == 1
    assert (
        'self.host:connectGameObjectTriggerSignal(self.gameObject, "TouchEnded", '
        "function(other)" in s.luau_source
    )


def test_oncollisionenter_is_NOT_lowered() -> None:
    """OnCollisionEnter keeps its body-bound ``.Touched`` edge (PR #198 fix):
    its origin comment does not match, so the binding is untouched."""
    src = textwrap.dedent("""\
        function Bullet:Awake()
            -- OnCollisionEnter(other)
            self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
                self:_hit(other)
            end)
        end
    """)
    s = _S(src)
    assert lower_trigger_enter([s]) == 0
    assert "connectGameObjectTriggerSignal" not in s.luau_source
    assert 'connectGameObjectSignal(self.gameObject, "Touched"' in s.luau_source


def test_oncollisionexit_is_NOT_lowered() -> None:
    src = textwrap.dedent("""\
        function X:Awake()
            -- OnCollisionExit(other)
            self.host:connectGameObjectSignal(self.gameObject, "TouchEnded", function(other) end)
        end
    """)
    s = _S(src)
    assert lower_trigger_enter([s]) == 0
    assert "connectGameObjectTriggerSignal" not in s.luau_source


def test_ontriggerstay_is_NOT_lowered_by_enter_pass() -> None:
    """OnTriggerStay is the Stay pass's job (and runs first in the pipeline).
    The Enter pass must not touch an ``-- OnTriggerStay``-origin binding."""
    src = textwrap.dedent("""\
        function Turret:Awake()
            -- OnTriggerStay(other)
            self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other) end)
        end
    """)
    s = _S(src)
    assert lower_trigger_enter([s]) == 0
    assert "connectGameObjectTriggerSignal" not in s.luau_source


def test_dotted_call_form_is_lowered() -> None:
    """``self.host.connectGameObjectSignal(...)`` (dotted) is rewritten too."""
    src = textwrap.dedent("""\
        function Mine:Awake()
            -- OnTriggerEnter(other)
            self.host.connectGameObjectSignal(self.gameObject, "Touched", function(other) end)
        end
    """)
    s = _S(src)
    assert lower_trigger_enter([s]) == 1
    assert "self.host.connectGameObjectTriggerSignal(self.gameObject, " in s.luau_source


def test_idempotent_second_pass_is_noop() -> None:
    """A second pass over already-lowered source rewrites nothing -- the renamed
    head ``connectGameObjectTriggerSignal(`` does not match the head regex."""
    s = _S(_MINE_ENTER)
    assert lower_trigger_enter([s]) == 1
    once = s.luau_source
    second_src, n2 = rewrite_trigger_enter_source(once)
    assert n2 == 0
    assert second_src == once
    # No double-rename to ...TriggerTriggerSignal.
    assert "connectGameObjectTriggerTriggerSignal" not in once


def test_mixed_enter_and_collision_in_one_awake() -> None:
    """A body with BOTH an OnTriggerEnter and an OnCollisionEnter binding: only
    the Enter binding is rewritten; the Collision binding stays body-bound."""
    src = textwrap.dedent("""\
        function Hybrid:Awake()
            -- OnTriggerEnter(other)
            self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
                self:_proximity(other)
            end)
            -- OnCollisionEnter(other)
            self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
                self:_impact(other)
            end)
        end
    """)
    s = _S(src)
    assert lower_trigger_enter([s]) == 1
    assert s.luau_source.count("connectGameObjectTriggerSignal") == 1
    assert s.luau_source.count(
        'connectGameObjectSignal(self.gameObject, "Touched"'
    ) == 1  # the OnCollision binding remains an edge


def test_abstain_when_head_inside_string_literal() -> None:
    """A connectGameObjectSignal head inside a string is not rewritten."""
    src = textwrap.dedent("""\
        function X:Awake()
            -- OnTriggerEnter(other)
            local doc = "self.host:connectGameObjectSignal(self.gameObject, \\"Touched\\", fn)"
        end
    """)
    s = _S(src)
    assert lower_trigger_enter([s]) == 0
    assert "connectGameObjectTriggerSignal" not in s.luau_source


def test_comment_not_immediately_preceding_does_not_match() -> None:
    """An OnTriggerEnter comment separated from the binding by another statement
    (not the immediately-preceding line) does NOT trigger the rewrite."""
    src = textwrap.dedent("""\
        function X:Awake()
            -- OnTriggerEnter(other)
            local x = 1
            self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other) end)
        end
    """)
    s = _S(src)
    assert lower_trigger_enter([s]) == 0


# ---------------------------------------------------------------------------
# Pipeline wiring: the REAL generic transpile_with_contract invokes the pass.
# ---------------------------------------------------------------------------

_MINE_PIPELINE_SRC = textwrap.dedent("""\
    local Mine = {}
    Mine.__index = Mine

    function Mine:Awake()
        -- OnTriggerEnter(other)
        self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
            local plr = self.host.playerFromTouch(other)
            if not plr then return end
            self.host.invoke(self, "Explode", self.explodeTime)
        end)
    end

    return Mine
""")


class _PInfo:
    """Minimal ``ScriptInfo`` stand-in for ``transpile_with_contract``."""

    def __init__(self, path: Path, class_name: str) -> None:
        self.path = path
        self.class_name = class_name
        self.name = class_name


def test_generic_pipeline_lowers_trigger_enter() -> None:
    """Drive the REAL ``contract_pipeline.transpile_with_contract`` (generic
    path) and confirm the mine's OnTriggerEnter binding is rewritten to
    ``connectGameObjectTriggerSignal`` downstream."""
    from converter import contract_pipeline

    mine_path = Path("/proj/Assets/Mine.cs")
    infos = [_PInfo(mine_path, "Mine")]
    scene_runtime = {
        "modules": {
            "guid-mine": {
                "stem": "Mine",
                "class_name": "Mine",
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

    mine_script = TranspiledScript(
        source_path=str(mine_path),
        output_filename="Mine.luau",
        csharp_source="",
        luau_source=_MINE_PIPELINE_SRC,
        strategy="ai",
        confidence=1.0,
        script_type="ModuleScript",
    )
    stub_result = TranspilationResult()
    stub_result.total_transpiled = 1
    stub_result.scripts.append(mine_script)

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

    assert mock_transpile.called
    lowered_src = result.transpilation.scripts[0].luau_source
    assert (
        'self.host:connectGameObjectSignal(self.gameObject, "Touched"'
        not in lowered_src
    )
    assert (
        "self.host:connectGameObjectTriggerSignal(self.gameObject, "
        '"Touched", function(other)' in lowered_src
    )
