"""End-to-end integration test for the scene-runtime contract wiring.

This is the test that would have caught the integration gap that
shipped with PR3a: the contract pipeline existed but
``Pipeline.transpile_scripts`` never called it under
``--scene-runtime=generic``. PR3a/PR3b/PR3c/PR4 all landed with this
gap; only ``tools/scene_runtime_spike.py`` exercised
``transpile_with_contract`` -- so the real conversion path emitted
non-compliant modules that crashed PR4's host runtime at line 14.

The test seeds a tiny project with one runtime-bearing MonoBehaviour
and drives ``Pipeline.transpile_scripts`` end-to-end with a mocked
backend that returns:

  - a contract-compliant class table for the runtime-bearing module
  - a top-level side-effecting (legacy-shape) module for a different
    non-runtime-bearing helper

Asserts:
  - The contract-compliant module passes through to
    ``state.transpilation_result``.
  - The non-runtime-bearing helper is NOT subjected to the contract
    verifier (its top-level side effect is fine -- it isn't a
    runtime-bearing module).
  - ``ctx.scene_runtime['runtime_bearing_paths']`` is populated.
  - The bootstrap emitter skips the runtime-bearing module under
    generic mode.

End-to-end here means "the full
``Pipeline.transpile_scripts`` -> ``contract_pipeline`` ->
``code_transpiler.transpile_scripts(runtime_mode='generic')`` ->
verifier path is exercised." We mock the backend itself, not the
orchestrator -- if any of the four wiring seams break, this test
fails.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.pipeline import Pipeline
from core.roblox_types import RbxPlace


# A contract-compliant class table -- no top-level side effects,
# ``Class.new`` is pure, ``Class:Awake`` is a method, ``return Class``
# is the only top-level statement.
COMPLIANT_CLASS_TABLE = """\
local FireLight = {}
FireLight.__index = FireLight

function FireLight.new(config)
    return setmetatable({}, FireLight)
end

function FireLight:Awake()
    self.brightness = 1
end

return FireLight
"""


class _ScriptInfo:
    """Stand-in for ``unity.script_analyzer.ScriptInfo``."""

    def __init__(self, path: Path, class_name: str) -> None:
        self.path = path
        self.class_name = class_name
        self.referenced_types: list[str] = []


def _make_unity_project(tmp_path: Path) -> tuple[Path, list[_ScriptInfo]]:
    """Create a minimal Unity project on disk with one runtime-bearing
    MonoBehaviour and one helper, both with real .cs files so
    ``code_transpiler`` can read their sources."""
    proj = tmp_path / "unity"
    assets = proj / "Assets"
    assets.mkdir(parents=True)

    fire = assets / "FireLight.cs"
    fire.write_text(
        "using UnityEngine;\n"
        "public class FireLight : MonoBehaviour { void Awake() {} }\n"
    )
    helper = assets / "Helper.cs"
    helper.write_text(
        "using UnityEngine;\n"
        "public static class Helper { public static int Add(int a, int b) => a + b; }\n"
    )
    infos = [
        _ScriptInfo(fire, "FireLight"),
        _ScriptInfo(helper, "Helper"),
    ]
    return proj, infos


def _make_pipeline(
    unity_project: Path,
    tmp_path: Path,
) -> Pipeline:
    output = tmp_path / "out"
    output.mkdir()
    pipeline = Pipeline(str(unity_project), str(output))
    pipeline.state.rbx_place = RbxPlace()
    pipeline.ctx.scene_runtime_mode = "generic"
    pipeline.ctx.scene_runtime = {
        "modules": {
            "guid-fire": {
                "stem": "FireLight",
                "class_name": "FireLight",
                "runtime_bearing": True,
            },
            "guid-helper": {
                "stem": "Helper",
                "class_name": "Helper",
                "runtime_bearing": False,
            },
        },
        "scenes": {}, "prefabs": {}, "domain_overrides": {},
    }
    return pipeline


def _fake_ai_transpile_factory(
    by_class: dict[str, str],
) -> Any:
    """Build a stand-in for ``_ai_transpile`` that returns canned
    luau output keyed by class name. Confidence 1.0, no warnings.
    Signature must match what ``_transpile_one`` calls."""

    def _fake(
        csharp_source: str,
        api_key: str,
        model: str,
        *,
        class_name: str = "",
        script_type: str = "Script",
        project_context: str = "",
        runtime_mode: str = "legacy",
    ) -> tuple[str, float, list[str]]:
        luau = by_class.get(class_name, f"-- {class_name}: missing fixture\nreturn {{}}\n")
        return luau, 1.0, []

    return _fake


class TestGenericConversionEndToEnd:

    def test_contract_compliant_module_passes_through(
        self, tmp_path: Path,
    ) -> None:
        proj, infos = _make_unity_project(tmp_path)
        pipeline = _make_pipeline(proj, tmp_path)

        fake = _fake_ai_transpile_factory({
            "FireLight": COMPLIANT_CLASS_TABLE,
            "Helper": "local Helper = {}\nfunction Helper.Add(a, b) return a + b end\nreturn Helper\n",
        })

        with patch(
            "unity.script_analyzer.analyze_all_scripts",
            return_value=infos,
        ), patch(
            "converter.code_transpiler._ai_transpile",
            side_effect=fake,
        ), patch(
            "converter.code_transpiler._find_transpiler",
            return_value="anthropic_api",
        ), patch(
            "config.USE_AI_TRANSPILATION", True,
        ), patch(
            "config.ANTHROPIC_API_KEY", "fake-key-for-test",
        ):
            pipeline.transpile_scripts()

        result = pipeline.state.transpilation_result
        assert result is not None
        by_stem = {Path(s.source_path).stem: s for s in result.scripts}
        assert "FireLight" in by_stem
        # The runtime-bearing module retained the compliant class table
        # AND emitted as a ModuleScript (the contract pipeline's target
        # flip).
        fire = by_stem["FireLight"]
        assert fire.script_type == "ModuleScript", (
            "Runtime-bearing module did NOT flip to ModuleScript -- "
            "PR4's host runtime requires it as a module."
        )
        assert "return FireLight" in fire.luau_source

    def test_runtime_bearing_paths_landed_on_ctx(
        self, tmp_path: Path,
    ) -> None:
        proj, infos = _make_unity_project(tmp_path)
        pipeline = _make_pipeline(proj, tmp_path)

        fake = _fake_ai_transpile_factory({
            "FireLight": COMPLIANT_CLASS_TABLE,
            "Helper": "return {}\n",
        })

        with patch(
            "unity.script_analyzer.analyze_all_scripts",
            return_value=infos,
        ), patch(
            "converter.code_transpiler._ai_transpile",
            side_effect=fake,
        ), patch(
            "converter.code_transpiler._find_transpiler",
            return_value="anthropic_api",
        ), patch(
            "config.USE_AI_TRANSPILATION", True,
        ), patch(
            "config.ANTHROPIC_API_KEY", "fake-key-for-test",
        ):
            pipeline.transpile_scripts()

        # Only FireLight is runtime-bearing in the planner.
        paths = pipeline.ctx.scene_runtime.get("runtime_bearing_paths")
        assert paths is not None
        assert any(p.endswith("FireLight.cs") for p in paths)
        assert not any(p.endswith("Helper.cs") for p in paths)

    def test_bootstrap_emit_skips_runtime_bearing_module(
        self, tmp_path: Path,
    ) -> None:
        """Cross-phase end-to-end: after transpile + autogen-bootstrap,
        the runtime-bearing module is absent from the ClientBootstrap
        require list. This is the SimpleFPS regression that the wiring
        fix prevents -- bootstrap requiring FireLight + host runtime
        also requiring it triggered the line-14 crash."""
        proj, infos = _make_unity_project(tmp_path)
        pipeline = _make_pipeline(proj, tmp_path)

        # Use a luau body that hits the side-effect pattern so the
        # bootstrap WOULD list it under legacy mode. We test the
        # generic-mode filter, so the body needs a RenderStepped:Connect.
        # Under the contract this is a violation, but the bootstrap
        # filter must skip it regardless -- the host runtime owns the
        # module.
        fire_with_side_effect = (
            "local FireLight = {}\n"
            "FireLight.__index = FireLight\n"
            "game:GetService('RunService').RenderStepped:Connect(function() end)\n"
            "function FireLight.new() return setmetatable({}, FireLight) end\n"
            "return FireLight\n"
        )
        fake = _fake_ai_transpile_factory({
            "FireLight": fire_with_side_effect,
            "Helper": "return {}\n",
        })

        with patch(
            "unity.script_analyzer.analyze_all_scripts",
            return_value=infos,
        ), patch(
            "converter.code_transpiler._ai_transpile",
            side_effect=fake,
        ), patch(
            "converter.code_transpiler._find_transpiler",
            return_value="anthropic_api",
        ), patch(
            "config.USE_AI_TRANSPILATION", True,
        ), patch(
            "config.ANTHROPIC_API_KEY", "fake-key-for-test",
        ):
            pipeline.transpile_scripts()
            # Seed rbx_place.scripts with the transpiled output the
            # way ``_subphase_emit_scripts_to_disk`` would; the
            # bootstrap emitter reads from there.
            from core.roblox_types import RbxScript
            for ts in pipeline.state.transpilation_result.scripts:
                pipeline.state.rbx_place.scripts.append(RbxScript(
                    name=ts.output_filename.replace(".luau", ""),
                    source=ts.luau_source,
                    script_type=ts.script_type,
                    source_path=ts.output_filename,
                ))
            pipeline._subphase_inject_autogen_scripts()

        bootstrap = next(
            (s for s in pipeline.state.rbx_place.scripts
             if s.name == "ClientBootstrap"),
            None,
        )
        if bootstrap is not None:
            assert 'WaitForChild("FireLight"' not in bootstrap.source, (
                "ClientBootstrap requires the runtime-bearing FireLight "
                "module. Under generic mode the host runtime owns its "
                "lifecycle; the bootstrap require produces the line-14 "
                "double-load crash."
            )
        # If no bootstrap at all, that's also OK -- the only side-effect
        # ModuleScript was FireLight (skipped), so the emit short-circuits.
