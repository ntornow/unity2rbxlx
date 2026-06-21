"""Tests for the GENERIC-mode slider-fill lowering pass.

Drives the REAL captured generic HUD shape (the inlined guessed-fill resolution
``healthFill = healthFrame:FindFirstChild("Fill")``) through ``lower_slider_fill``
and asserts:
  * the guessed-fill resolution becomes ``_resolveSliderFill(<frame>)``;
  * the ``_resolveSliderFill`` helper is injected exactly once;
  * a second pass is a byte-identical no-op (idempotent);
  * a non-slider script is untouched;
  * the REAL ``contract_pipeline.transpile_with_contract`` (generic path) applies
    the rewrite end-to-end.
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
from converter.slider_fill_lowering import lower_slider_fill  # noqa: E402


class _S:
    """Minimal TranspiledScript stand-in (carries ``luau_source`` +
    ``output_filename``)."""

    def __init__(self, src: str, name: str = "HudControl.luau") -> None:
        self.luau_source = src
        self.output_filename = name


# The REAL captured generic HUD shape (``generic-hud-shape.luau``): the faithful
# OOP HUD with NO ``setSliderValue`` -- the inlined guessed-fill resolution
# ``local healthFill = healthFrame and healthFrame:FindFirstChild("Fill")``.
_GENERIC_HUD = textwrap.dedent("""\
    local Player = require(game:GetService("ReplicatedStorage"):FindFirstChild("Player", true) or game:GetService("ServerStorage"):FindFirstChild("Player", true))

    local HudControl = {}
    HudControl.__index = HudControl

    function HudControl:Awake()
        local moduleFrame  = self.gameObject:FindFirstChild("Module")
        local healthFrame  = moduleFrame and moduleFrame:FindFirstChild("Health")
        local healthFill   = healthFrame and healthFrame:FindFirstChild("Fill")

        local healthEvt = ensureEvent("HealthUpdate")
        self.host:connect(healthEvt.Event, function(curHealth)
            if healthFill then
                local pct = curHealth / Player.maxHealth
                healthFill.Size = UDim2.new(pct, 0, 1, 0)
            end
        end)
    end

    return HudControl
""")


# A non-slider script: no guessed-fill resolution anywhere.
_NON_SLIDER = textwrap.dedent("""\
    local Foo = {}
    Foo.__index = Foo

    function Foo:Awake()
        local panel = self.gameObject:FindFirstChild("Panel")
        if panel then
            panel.Visible = true
        end
    end

    return Foo
""")


def test_generic_hud_fill_resolution_is_rewritten() -> None:
    s = _S(_GENERIC_HUD)
    changed = lower_slider_fill([s])

    assert changed == 1
    # The guessed literal resolution is gone; the helper call replaced it.
    assert 'FindFirstChild("Fill")' not in s.luau_source
    assert "local healthFill = _resolveSliderFill(healthFrame)" in s.luau_source


def test_helper_injected_exactly_once() -> None:
    s = _S(_GENERIC_HUD)
    lower_slider_fill([s])

    assert s.luau_source.count("local function _resolveSliderFill(frame)") == 1
    # The helper reads the deterministic upstream attribute, not the guess.
    assert 'frame:GetAttribute("SliderFillElement")' in s.luau_source


def test_twice_call_is_byte_identical_noop() -> None:
    s = _S(_GENERIC_HUD)
    lower_slider_fill([s])
    once = s.luau_source

    changed_again = lower_slider_fill([s])

    assert changed_again == 0
    assert s.luau_source == once  # byte-identical: idempotent


def test_non_slider_script_untouched() -> None:
    s = _S(_NON_SLIDER, name="Foo.luau")
    changed = lower_slider_fill([s])

    assert changed == 0
    assert s.luau_source == _NON_SLIDER
    assert "_resolveSliderFill" not in s.luau_source


def test_coverage_warning_on_undrewritten_guess(caplog) -> None:
    """A guessed-fill resolution the structural gate cannot rewrite (no simple
    receiver: a parenthesised frame expr) is left in place and warned about, not
    silently abstained."""
    src = textwrap.dedent("""\
        local Hud = {}
        function Hud:Awake()
            local fill = (self.frames[1]):FindFirstChild("Fill")
        end
        return Hud
    """)
    s = _S(src)
    import logging

    with caplog.at_level(logging.WARNING):
        changed = lower_slider_fill([s])

    assert changed == 0
    assert 'FindFirstChild("Fill")' in s.luau_source  # left untouched
    assert any("slider_fill" in r.message for r in caplog.records)


# A generic HUD that CALLS some ``obj:setSliderValue(v)`` (NOT a definition) AND
# has an inlined guessed-fill resolution. The setter CALL must NOT gate the inline
# rewrite off -- only a setter DEFINITION does that.
_CALL_PLUS_INLINE_HUD = textwrap.dedent("""\
    local HudControl = {}
    HudControl.__index = HudControl

    function HudControl:Awake()
        local healthFrame = self.gameObject:FindFirstChild("Health")
        local healthFill  = healthFrame:FindFirstChild("Fill")

        local otherBar = self.gameObject:FindFirstChild("OtherBar")
        otherBar:setSliderValue(0.5)
    end

    return HudControl
""")


def test_setter_call_does_not_disable_inline_rewrite() -> None:
    """P1 regression: ``has_slider_setter`` must detect a setter DEFINITION only.
    A bare CALL ``obj:setSliderValue(v)`` must NOT gate the inline rewrite off, so
    a script that merely CALLS a setter AND has an inline guessed-fill resolution
    STILL gets rewritten (not silently skipped -> frozen bar).

    Pre-fix (``has_slider_setter`` used the ``":setSliderValue"`` substring) this
    returned True for the call site -> the inline rewrite no-opped -> ``changed==0``
    and the guessed literal survived."""
    s = _S(_CALL_PLUS_INLINE_HUD)
    changed = lower_slider_fill([s])

    assert changed == 1
    # The inline guessed-fill resolution was rewritten despite the setter CALL.
    assert 'FindFirstChild("Fill")' not in s.luau_source
    assert "local healthFill = _resolveSliderFill(healthFrame)" in s.luau_source
    # The unrelated setter CALL is left verbatim.
    assert "otherBar:setSliderValue(0.5)" in s.luau_source


# A generic setter-form shape: a ``setSliderValue`` DEFINITION whose body guesses
# the fill name. The generic inline pass does NOT own setter bodies (those are the
# legacy span rewrite's), so it abstains -- and the coverage guard must warn LOUDLY
# rather than leave the frozen bar silent.
_SETTER_FORM_GUESS_HUD = textwrap.dedent("""\
    local HudControl = {}
    HudControl.__index = HudControl

    function setSliderValue(slider, value)
        local fill = slider:FindFirstChild("Fill")
        if fill then
            fill.Size = UDim2.new(value, 0, 1, 0)
        end
    end

    return HudControl
""")


def test_setter_form_guess_warns_in_generic_pass(caplog) -> None:
    """P2 regression: a setter-form ``setSliderValue`` with a guessed fill that the
    generic inline pass does not own must be flagged LOUDLY by the coverage guard,
    not silently frozen.

    Pre-fix the coverage guard used ``has_inline_guessed_fill`` (False when a
    setter definition is present) -> no warning. Post-fix it uses
    ``has_any_guessed_fill`` -> the surviving guessed literal is warned about."""
    s = _S(_SETTER_FORM_GUESS_HUD)
    import logging

    with caplog.at_level(logging.WARNING):
        changed = lower_slider_fill([s])

    # The inline pass abstains on the setter body (owned by the legacy rewrite).
    assert changed == 0
    assert 'FindFirstChild("Fill")' in s.luau_source  # un-rewritten
    # ... but the freeze is LOUD, not silent.
    assert any("slider_fill" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Pipeline wiring: the REAL generic transpile_with_contract invokes the pass.
# ---------------------------------------------------------------------------


class _PInfo:
    """Minimal ``ScriptInfo`` stand-in for ``transpile_with_contract``."""

    def __init__(self, path: Path, class_name: str) -> None:
        self.path = path
        self.class_name = class_name
        self.name = class_name


def test_generic_pipeline_lowers_slider_fill() -> None:
    """Drive the REAL ``contract_pipeline.transpile_with_contract`` (generic
    path) and confirm the HUD's inlined guessed-fill resolution is rewritten to
    ``_resolveSliderFill`` with the helper injected downstream."""
    from converter import contract_pipeline

    hud_path = Path("/proj/Assets/HudControl.cs")
    infos = [_PInfo(hud_path, "HudControl")]
    scene_runtime = {
        "modules": {
            "guid-hud": {
                "stem": "HudControl",
                "class_name": "HudControl",
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

    hud_script = TranspiledScript(
        source_path=str(hud_path),
        output_filename="HudControl.luau",
        csharp_source="",
        luau_source=_GENERIC_HUD,
        strategy="ai",
        confidence=1.0,
        script_type="ModuleScript",
    )
    stub_result = TranspilationResult()
    stub_result.total_transpiled = 1
    stub_result.scripts.append(hud_script)

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
    assert 'FindFirstChild("Fill")' not in lowered_src
    assert "local healthFill = _resolveSliderFill(healthFrame)" in lowered_src
    assert lowered_src.count("local function _resolveSliderFill(frame)") == 1


class TestSetterDefRegexEdgeCases:
    """has_slider_setter must detect a setSliderValue DEFINITION only, not a
    call, and not a longer name like resetSliderValue (round-2 codex P1)."""

    def test_setter_def_regex_edge_cases(self) -> None:
        from converter.slider_fill_common import has_slider_setter

        assert has_slider_setter("function setSliderValue(s, p)")
        assert has_slider_setter("local function setSliderValue(s, p)")
        assert has_slider_setter("function T:setSliderValue(p)")
        assert has_slider_setter("function T :setSliderValue(p)")   # spaced colon
        assert has_slider_setter("function T: setSliderValue(p)")   # spaced after colon
        assert has_slider_setter("function M.setSliderValue(p)")
        # NOT a definition of setSliderValue:
        assert not has_slider_setter("function resetSliderValue(p)")     # name ends with ...setSliderValue
        assert not has_slider_setter("function M.resetSliderValue(p)")
        assert not has_slider_setter("obj:setSliderValue(0.5)")          # bare call
        assert not has_slider_setter("self.host:setSliderValue(f, v)")   # bare call
