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
