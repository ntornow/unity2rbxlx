"""Tests for the OnTriggerStay lowering pass (generic allowlist, slice 1.2).

The generic transpiler collapses Unity OnTriggerStay onto the same ``.Touched``
EDGE binding as OnTriggerEnter. ``lower_trigger_stay`` rewrites the SPECIFIC
``connectGameObjectSignal(go, "Touched", fn)`` binding whose immediately-
preceding origin comment is ``-- OnTriggerStay...`` to slice 1.1's host poll
primitive ``connectGameObjectSignalStay(go, fn)`` (dropping the ``"Touched"``
arg). It must leave OnTriggerEnter/Exit and the OnCollision* edge bindings
untouched, match the EXACT ``OnTriggerStay`` token (not ``OnCollisionStay``),
anchor on the comment immediately above the binding it rewrites, and be
idempotent.

This file covers the lowering entry over ``luau_source`` plus the generic-
pipeline wiring (``transpile_with_contract`` actually invokes it).
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
from converter.trigger_stay_lowering import (  # noqa: E402
    lower_trigger_stay,
    rewrite_trigger_stay_source,
)


class _S:
    """Minimal TranspiledScript stand-in (carries ``luau_source``)."""

    def __init__(self, src: str) -> None:
        self.luau_source = src


# A turret-shaped ``Awake`` with the OnTriggerStay->Touched edge binding
# (under its mandated origin comment).
_TURRET_STAY = textwrap.dedent("""\
    local Turret = {}
    Turret.__index = Turret

    function Turret:Awake()
        -- OnTriggerStay(other)
        self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
            local plr = self.host.playerFromTouch(other)
            if not plr then return end
            self:_engage(plr)
        end)
    end

    return Turret
""")


def test_turret_onstay_binding_is_lowered() -> None:
    """(a) The ``-- OnTriggerStay``-origin ``connectGameObjectSignal(go,
    "Touched", fn)`` binding is rewritten to ``connectGameObjectSignalStay(go,
    fn)`` -- the ``"Touched"`` arg dropped, the receiver + function body
    preserved."""
    s = _S(_TURRET_STAY)
    n = lower_trigger_stay([s])
    assert n == 1
    assert (
        'self.host:connectGameObjectSignal(self.gameObject, "Touched"'
        not in s.luau_source
    )
    assert (
        "self.host:connectGameObjectSignalStay(self.gameObject, function(other)"
        in s.luau_source
    )
    # The function body is preserved verbatim.
    assert "local plr = self.host.playerFromTouch(other)" in s.luau_source
    assert "self:_engage(plr)" in s.luau_source


# A multi-binding ``Awake``: OnTriggerStay->Touched AND OnTriggerExit->TouchEnded.
_MULTI = textwrap.dedent("""\
    function Turret:Awake()
        -- OnTriggerStay(other)
        self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
            self:_engage(other)
        end)
        -- OnTriggerExit(other)
        self.host:connectGameObjectSignal(self.gameObject, "TouchEnded", function(other)
            self:_search()
        end)
    end
""")


def test_multi_binding_rewrites_only_stay() -> None:
    """(b) In an ``Awake`` with BOTH OnTriggerStay->Touched and
    OnTriggerExit->TouchEnded, only the Stay binding is rewritten; the
    TouchEnded edge binding is left untouched."""
    s = _S(_MULTI)
    n = lower_trigger_stay([s])
    assert n == 1
    # The Stay binding became the poll primitive.
    assert (
        "self.host:connectGameObjectSignalStay(self.gameObject, function(other)"
        in s.luau_source
    )
    # The Exit binding is untouched -- still an edge with "TouchEnded".
    assert (
        'self.host:connectGameObjectSignal(self.gameObject, "TouchEnded", '
        "function(other)" in s.luau_source
    )
    # And no Stay-poll variant was emitted for the Exit binding.
    assert "TouchEnded" in s.luau_source
    assert s.luau_source.count("connectGameObjectSignalStay") == 1


def test_oncollisionstay_is_not_lowered() -> None:
    """(c) An ``-- OnCollisionStay``-commented ``.Touched`` binding (which also
    maps to ``.Touched``) is NOT rewritten -- only the exact ``OnTriggerStay``
    token gates the lowering."""
    src = textwrap.dedent("""\
        function Bomb:Awake()
            -- OnCollisionStay(collision)
            self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
                self:_tick(other)
            end)
        end
    """)
    s = _S(src)
    before = s.luau_source
    n = lower_trigger_stay([s])
    assert n == 0
    assert s.luau_source == before
    assert "connectGameObjectSignalStay" not in s.luau_source


def test_ontriggerenter_is_not_lowered() -> None:
    """(d) An ``-- OnTriggerEnter``-commented ``.Touched`` edge binding (Door /
    Machine / Plane) keeps its edge semantics -- NOT rewritten."""
    src = textwrap.dedent("""\
        function Door:Awake()
            -- OnTriggerEnter(other)
            self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
                self:_open(other)
            end)
        end
    """)
    s = _S(src)
    before = s.luau_source
    n = lower_trigger_stay([s])
    assert n == 0
    assert s.luau_source == before
    assert "connectGameObjectSignalStay" not in s.luau_source


def test_idempotent_twice_applied() -> None:
    """(e) Re-running the pass yields identical output: the rewritten call has
    no ``"Touched"`` literal so it no longer matches."""
    s = _S(_TURRET_STAY)
    n1 = lower_trigger_stay([s])
    after_first = s.luau_source
    n2 = lower_trigger_stay([s])
    assert n1 == 1
    assert n2 == 0
    assert s.luau_source == after_first


def test_dot_call_form_is_lowered() -> None:
    """Robustness: the ``self.host.connectGameObjectSignal`` (dot) call form is
    also lowered, mirroring the ``self.host:`` (method) form."""
    src = textwrap.dedent("""\
        function Turret:Awake()
            -- OnTriggerStay(other)
            self.host.connectGameObjectSignal(self.gameObject, "Touched", function(other)
                self:_engage(other)
            end)
        end
    """)
    s = _S(src)
    n = lower_trigger_stay([s])
    assert n == 1
    assert (
        "self.host.connectGameObjectSignalStay(self.gameObject, function(other)"
        in s.luau_source
    )


def test_comment_directly_above_is_lowered() -> None:
    """(FINDING 3a) The origin comment on the LITERAL immediately-preceding line
    (no blank, no statement between) authorizes the rewrite."""
    src = textwrap.dedent("""\
        function Turret:Awake()
            -- OnTriggerStay(other)
            self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
                self:_engage(other)
            end)
        end
    """)
    s = _S(src)
    n = lower_trigger_stay([s])
    assert n == 1
    assert (
        "self.host:connectGameObjectSignalStay(self.gameObject, function(other)"
        in s.luau_source
    )


def test_blank_line_between_comment_and_binding_is_not_lowered() -> None:
    """(FINDING 3b) The contract emits the comment DIRECTLY above the binding.
    A blank line between the origin comment and the binding means the comment is
    NOT the literal immediately-preceding line -> the binding is left an edge
    (strict immediately-preceding, no blank-line skip)."""
    src = textwrap.dedent("""\
        function Turret:Awake()
            -- OnTriggerStay(other)

            self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
                self:_engage(other)
            end)
        end
    """)
    s = _S(src)
    before = s.luau_source
    n = lower_trigger_stay([s])
    assert n == 0
    assert s.luau_source == before
    assert "connectGameObjectSignalStay" not in s.luau_source


def test_intervening_statement_is_not_lowered() -> None:
    """(FINDING 3c) A non-blank statement between the comment and the binding
    blocks the rewrite -- the immediately-preceding line is the statement."""
    src = textwrap.dedent("""\
        function Turret:Awake()
            -- OnTriggerStay(other)
            self.foo = 1
            self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
                self:_engage(other)
            end)
        end
    """)
    s = _S(src)
    before = s.luau_source
    n = lower_trigger_stay([s])
    assert n == 0
    assert s.luau_source == before


def test_complex_go_expressions_are_lowered() -> None:
    """(FINDING 2) A non-trivial first arg -- a local alias, an index, or a call
    (even with an internal comma) -- is captured whole and preserved verbatim,
    not silently skipped."""
    cases = [
        "trigger",
        "self.parts[1]",
        "self:getTriggerPart()",
        "self.host.findPart(a, b)",  # internal comma -> captured whole
    ]
    for go in cases:
        src = textwrap.dedent(f"""\
            function Turret:Awake()
                -- OnTriggerStay(other)
                self.host:connectGameObjectSignal({go}, "Touched", function(other)
                    self:_engage(other)
                end)
            end
        """)
        s = _S(src)
        n = lower_trigger_stay([s])
        assert n == 1, f"go={go!r} should lower"
        assert (
            f"self.host:connectGameObjectSignalStay({go}, function(other)"
            in s.luau_source
        ), f"go={go!r} not preserved verbatim"
        assert '"Touched"' not in s.luau_source


def test_go_with_internal_touched_abstains_no_corruption() -> None:
    """(FINDING MAJOR) A go expression that itself contains an internal
    ``, "Touched",`` (``self:pick("foo", "Touched", x)``) makes the non-greedy
    ``<go>`` capture anchor on the INTERNAL ``"Touched"`` and over-capture a
    short UNBALANCED fragment (``self:pick("foo"``). The balance guard ABSTAINS
    (count 0, source UNCHANGED) rather than corrupt the call. RED against the
    pre-guard code (which produced ``connectGameObjectSignalStay(self:pick("foo",
    x), "Touched", ...)``)."""
    src = textwrap.dedent("""\
        function Turret:Awake()
            -- OnTriggerStay(other)
            self.host:connectGameObjectSignal(self:pick("foo", "Touched", x), "Touched", function(other) end)
        end
    """)
    s = _S(src)
    before = s.luau_source
    n = lower_trigger_stay([s])
    assert n == 0
    assert s.luau_source == before
    assert "connectGameObjectSignalStay" not in s.luau_source


def test_balanced_go_expressions_still_lower() -> None:
    """(FINDING MAJOR, complement) The balanced first-arg cases -- including a
    call whose arg list legitimately contains a ``"Touched"`` string literal
    (``self:pick("Touched", x)``) -- remain balanced and STILL lower correctly
    (count 1). Only the pathological internal-anchor fragment abstains."""
    cases = [
        "self.gameObject",
        "self.parts[1]",
        "self:getTriggerPart()",
        'self:pick("Touched", x)',
    ]
    for go in cases:
        src = textwrap.dedent(f"""\
            function Turret:Awake()
                -- OnTriggerStay(other)
                self.host:connectGameObjectSignal({go}, "Touched", function(other)
                    self:_engage(other)
                end)
            end
        """)
        s = _S(src)
        n = lower_trigger_stay([s])
        assert n == 1, f"go={go!r} should lower"
        assert (
            f"self.host:connectGameObjectSignalStay({go}, function(other)"
            in s.luau_source
        ), f"go={go!r} not preserved verbatim"


def test_go_with_longstring_internal_touched_still_lowers() -> None:
    """(FINDING 1) A BALANCED go whose first arg is a Luau long-bracket string
    literal carrying an internal ``, "Touched",`` -- ``self:pick([[foo,
    "Touched", bar]], x)`` -- is balanced (the ``[[ ... ]]`` payload is a
    string, its contents skipped) and LOWERS (count 1). RED against the
    pre-fix helper, which treated ``[[``/``]]`` as structural brackets and
    judged it unbalanced -> false-abstain."""
    go = 'self:pick([[foo, "Touched", bar]], x)'
    src = textwrap.dedent(f"""\
        function Turret:Awake()
            -- OnTriggerStay(other)
            self.host:connectGameObjectSignal({go}, "Touched", function(other)
                self:_engage(other)
            end)
        end
    """)
    s = _S(src)
    n = lower_trigger_stay([s])
    assert n == 1
    assert (
        f"self.host:connectGameObjectSignalStay({go}, function(other)"
        in s.luau_source
    )


def test_go_with_mismatched_delimiters_abstains() -> None:
    """(FINDING 2) A go fragment whose brackets are present but of MISMATCHED
    type (``self.parts(1]`` -- a ``(`` closed by a ``]``) is NOT balanced under
    a type-matched stack, so the pass ABSTAINS (count 0, source unchanged).
    RED against the single-counter helper, which let ``(`` and ``]`` net to
    depth zero and wrongly accepted the fragment."""
    src = textwrap.dedent("""\
        function Turret:Awake()
            -- OnTriggerStay(other)
            self.host:connectGameObjectSignal(self.parts(1], "Touched", function(other) end)
        end
    """)
    s = _S(src)
    before = s.luau_source
    n = lower_trigger_stay([s])
    assert n == 0
    assert s.luau_source == before
    assert "connectGameObjectSignalStay" not in s.luau_source


def test_binding_inside_long_string_is_not_lowered() -> None:
    """(FINDING 1) A binding inside a MULTI-LINE ``[[ ... ]]`` long string
    (opened on an earlier line, with a ``-- OnTriggerStay`` line inside the
    payload) is NOT real code -> abstain, source unchanged."""
    src = textwrap.dedent("""\
        local doc = [[
        -- OnTriggerStay(other)
        self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other) return other end)
        ]]
        return doc
    """)
    s = _S(src)
    before = s.luau_source
    n = lower_trigger_stay([s])
    assert n == 0
    assert s.luau_source == before
    assert "connectGameObjectSignalStay" not in s.luau_source


def test_binding_inside_long_block_comment_is_not_lowered() -> None:
    """(FINDING 1) The same, inside a ``--[[ ... ]]`` long block comment."""
    src = textwrap.dedent("""\
        --[[
        -- OnTriggerStay(other)
        self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other) return other end)
        ]]
        local x = 1
    """)
    s = _S(src)
    before = s.luau_source
    n = lower_trigger_stay([s])
    assert n == 0
    assert s.luau_source == before


def test_binding_inside_leveled_long_string_is_not_lowered() -> None:
    """(FINDING 1) A leveled ``[=[ ... ]=]`` long string is also respected."""
    src = textwrap.dedent("""\
        local doc = [=[
        -- OnTriggerStay(other)
        self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other) return other end)
        ]=]
        return doc
    """)
    s = _S(src)
    before = s.luau_source
    n = lower_trigger_stay([s])
    assert n == 0
    assert s.luau_source == before


def test_real_code_after_closed_long_string_still_lowered() -> None:
    """Guard the abstain isn't over-broad: a closed ``[[ ... ]]`` above the
    binding must NOT suppress a legitimate later lowering."""
    src = textwrap.dedent("""\
        function Turret:Awake()
            local doc = [[ harmless ]]
            -- OnTriggerStay(other)
            self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
                self:_engage(other)
            end)
        end
    """)
    s = _S(src)
    n = lower_trigger_stay([s])
    assert n == 1
    assert (
        "self.host:connectGameObjectSignalStay(self.gameObject, function(other)"
        in s.luau_source
    )


def test_binding_inside_string_is_not_lowered() -> None:
    """A ``connectGameObjectSignal(..., "Touched", ...)`` occurrence inside a Lua
    string literal (not real code) is NOT rewritten."""
    src = textwrap.dedent('''\
        function M:doc()
            -- OnTriggerStay(other)
            local s = "self.host:connectGameObjectSignal(self.gameObject, \\"Touched\\", fn)"
            return s
        end
    ''')
    s = _S(src)
    before = s.luau_source
    n = lower_trigger_stay([s])
    assert n == 0
    assert s.luau_source == before


def test_rewrite_source_helper_returns_count() -> None:
    """The string-level helper returns ``(new_source, count)`` and leaves the
    source unchanged when count is 0."""
    unchanged, count = rewrite_trigger_stay_source("local x = 1\n")
    assert count == 0
    assert unchanged == "local x = 1\n"


# --- Generic-pipeline wiring -----------------------------------------------


class _PInfo:
    """Minimal ``ScriptInfo`` stand-in for ``transpile_with_contract``."""

    def __init__(self, path: Path, class_name: str) -> None:
        self.path = path
        self.class_name = class_name
        self.referenced_types: list[str] = []


_TURRET_PIPELINE_SRC = textwrap.dedent("""\
    local Turret = {}
    Turret.__index = Turret

    function Turret:Awake()
        -- OnTriggerStay(other)
        self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
            local plr = self.host.playerFromTouch(other)
            if plr then self:_engage(plr) end
        end)
    end

    return Turret
""")


class TestPipelineInvocation:
    """Drives the REAL ``contract_pipeline.transpile_with_contract`` (generic
    mode) so a future edit that deletes / mis-threads the ``lower_trigger_stay``
    wiring would FAIL this test. ``transpile_scripts`` is stubbed; everything
    downstream is the production path inside ``transpile_with_contract``."""

    def test_generic_pipeline_lowers_trigger_stay(self) -> None:
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

        assert mock_transpile.called

        lowered_src = result.transpilation.scripts[0].luau_source
        assert (
            'self.host:connectGameObjectSignal(self.gameObject, "Touched"'
            not in lowered_src
        )
        assert (
            "self.host:connectGameObjectSignalStay(self.gameObject, "
            "function(other)" in lowered_src
        )
