"""Phase 2 (Unit 4) — roster-consumer dead-module exemption (NEW-FINDING-B).

A re-lowered roster consumer carries a deterministic ``roster_binding`` carrier;
it is LIVE BY CONSTRUCTION (it reads Phase 1's by_label tagged surface) even
though its canonical body is output-inert. The dead-module analysis MUST exempt
a carrier-bearing module from ``state.dead_modules`` on BOTH paths:

  AC4  FRESH transpile — ``_subphase_analyze_dead_modules`` with a
       ``transpilation_result`` present.
  AC5  RESUME (no transpile) — the carrier round-trips through
       ``conversion_plan.json`` and the resume revalidation branch honors it.

Each AC pairs a GREEN (carrier set -> exempt) with a RED control (carrier
removed -> the SAME inert body IS flagged dead), proving the exemption — not an
incidental classifier veto — is the load-bearing lever (D-P2-3).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.code_transpiler import TranspilationResult, TranspiledScript  # noqa: E402
from converter.pipeline import Pipeline  # noqa: E402
from core.roblox_types import RbxPlace, RbxScript  # noqa: E402

_CONSUMER_FILENAME = "CharacterDatabase.luau"


def _make_pipeline(tmp_path: Path) -> Pipeline:
    unity_project = tmp_path / "unity"
    (unity_project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"
    output.mkdir()
    return Pipeline(str(unity_project), str(output))


# An INERT roster-consumer body (the real empty-roster CharacterDatabase shape):
# is_output_inert + a dead-leaning C# input prior -> classify_module_dead == dead.
# This is the RED vehicle: WITHOUT the carrier it lands in the dead set.
_INERT_BODY = (
    "local CharacterDatabase = {}\n"
    "local m_CharactersDict = nil\n"
    "local m_Loaded = false\n"
    "function CharacterDatabase.dictionary()\n"
    "\treturn m_CharactersDict\n"
    "end\n"
    "function CharacterDatabase.loaded()\n"
    "\treturn m_Loaded\n"
    "end\n"
    "function CharacterDatabase.GetCharacter(type)\n"
    "\tif m_CharactersDict == nil then return nil end\n"
    "\treturn m_CharactersDict[type]\n"
    "end\n"
    "function CharacterDatabase.LoadDatabase()\n"
    "\tif m_CharactersDict == nil then\n"
    "\t\tm_CharactersDict = {}\n"
    "\t\tlocal RS = game:GetService(\"ReplicatedStorage\")\n"
    "\t\tlocal container = RS:FindFirstChild(\"Characters\")\n"
    "\t\tif container then\n"
    "\t\t\tfor _, op in ipairs(container:GetChildren()) do\n"
    "\t\t\t\tm_CharactersDict[op.Name] = op\n"
    "\t\t\tend\n"
    "\t\tend\n"
    "\t\tm_Loaded = true\n"
    "\tend\n"
    "end\n"
    "return CharacterDatabase\n"
)

_DEAD_LEANING_CS = (
    "using UnityEngine; using UnityEngine.AddressableAssets;\n"
    "using System.Collections; using System.Collections.Generic;\n"
    "public class CharacterDatabase {\n"
    "  static Dictionary<string,Character> m_CharactersDict;\n"
    "  public static IEnumerator LoadDatabase() {\n"
    "    yield return Addressables.LoadAssetsAsync<GameObject>(\"characters\","
    " op => {});\n"
    "  }\n"
    "}\n"
)

_CARRIER = {"label": "characters", "receiver": "CharacterDatabase", "lowered": True}


def _module_script(roster_binding: object) -> RbxScript:
    return RbxScript(
        name="CharacterDatabase",
        source=_INERT_BODY,
        script_type="ModuleScript",
        roster_binding=roster_binding,
    )


def _transpilation() -> TranspilationResult:
    return TranspilationResult(
        scripts=[
            TranspiledScript(
                source_path="Assets/CharacterDatabase.cs",
                output_filename="CharacterDatabase.luau",
                csharp_source=_DEAD_LEANING_CS,
                luau_source=_INERT_BODY,
                strategy="ai",
                confidence=1.0,
                script_type="ModuleScript",
            ),
        ],
        total_transpiled=1,
        total_ai=1,
    )


class TestAC4FreshTranspile:
    def test_carrier_exempts_module_from_dead_set(self, tmp_path: Path) -> None:
        pipeline = _make_pipeline(tmp_path)
        pipeline.state.rbx_place = RbxPlace(scripts=[_module_script(_CARRIER)])
        pipeline.state.transpilation_result = _transpilation()
        pipeline._subphase_analyze_dead_modules()
        assert "CharacterDatabase" not in pipeline.state.dead_modules, (
            "a carrier-bearing re-lowered roster consumer must be exempt from "
            "the dead set on a fresh transpile (AC4)"
        )

    def test_RED_without_carrier_module_is_flagged_dead(self, tmp_path: Path) -> None:
        pipeline = _make_pipeline(tmp_path)
        pipeline.state.rbx_place = RbxPlace(scripts=[_module_script(None)])
        pipeline.state.transpilation_result = _transpilation()
        pipeline._subphase_analyze_dead_modules()
        assert "CharacterDatabase" in pipeline.state.dead_modules, (
            "control: WITHOUT the carrier the SAME inert body IS flagged dead — "
            "so the carrier (not an incidental veto) is the load-bearing lever"
        )


class TestAC5ResumeNoTranspile:
    def test_carrier_exempts_module_on_resume(self, tmp_path: Path) -> None:
        pipeline = _make_pipeline(tmp_path)
        pipeline.state.rbx_place = RbxPlace(scripts=[_module_script(_CARRIER)])
        # Resume path: no transpilation_result; the prior run persisted the
        # module as dead, and the carrier was rehydrated onto the script.
        pipeline.state.transpilation_result = None
        pipeline.ctx.dead_modules = ["CharacterDatabase"]
        pipeline._subphase_analyze_dead_modules()
        assert "CharacterDatabase" not in pipeline.state.dead_modules, (
            "a rehydrated carrier-bearing module must stay out of the dead set "
            "on a no-transpile resume (AC5)"
        )

    def test_RED_without_carrier_module_stays_dead_on_resume(
        self, tmp_path: Path,
    ) -> None:
        pipeline = _make_pipeline(tmp_path)
        pipeline.state.rbx_place = RbxPlace(scripts=[_module_script(None)])
        pipeline.state.transpilation_result = None
        pipeline.ctx.dead_modules = ["CharacterDatabase"]
        pipeline._subphase_analyze_dead_modules()
        assert "CharacterDatabase" in pipeline.state.dead_modules, (
            "control: WITHOUT the rehydrated carrier the inert body is "
            "re-validated dead on resume — the carrier round-trip is required"
        )


class TestCarrierRoundTrip:
    def test_persist_and_rehydrate_loader_round_trips_carrier(
        self, tmp_path: Path,
    ) -> None:
        # The rehydration loader recovers a well-formed carrier and drops a
        # malformed one (partial carrier must never exempt blind).
        import json
        pipeline = _make_pipeline(tmp_path)
        plan = {
            "roster_binding": {
                "CharacterDatabase": _CARRIER,
                "Malformed": {"label": "characters"},  # missing receiver/lowered
                "BadLowered": {
                    "label": "x", "receiver": "X", "lowered": "yes",
                },
            }
        }
        (pipeline.output_dir / "conversion_plan.json").write_text(
            json.dumps(plan), encoding="utf-8",
        )
        loaded = pipeline._load_roster_binding_for_rehydration()
        assert loaded == {"CharacterDatabase": _CARRIER}, (
            "only a fully well-formed carrier (str label/receiver, lowered is "
            "True) rehydrates; partial/malformed rows are dropped"
        )

    def test_loader_returns_empty_on_missing_plan(self, tmp_path: Path) -> None:
        pipeline = _make_pipeline(tmp_path)
        assert pipeline._load_roster_binding_for_rehydration() == {}


class TestCarrierConversionCopy:
    """The carrier must FLOW from ``TranspiledScript`` onto the produced
    ``RbxScript`` at the fresh-transpile conversion site
    (``_subphase_emit_scripts_to_disk``). The AC4/AC5 dead-set tests construct
    ``RbxScript`` WITH the carrier directly, so they bypass this copy — if the
    ``roster_binding=ts.roster_binding`` line were dropped, those tests still
    pass while the real pipeline silently re-stubs the re-lowered module. This
    test drives the real conversion and asserts the copy + that the dead-set
    exemption honors the COPIED carrier end-to-end (no hand-built RbxScript)."""

    def _transpilation_with_carrier(
        self, roster_binding: object,
    ) -> TranspilationResult:
        return TranspilationResult(
            scripts=[
                TranspiledScript(
                    source_path="Assets/CharacterDatabase.cs",
                    output_filename=_CONSUMER_FILENAME,
                    csharp_source=_DEAD_LEANING_CS,
                    luau_source=_INERT_BODY,
                    strategy="ai",
                    confidence=1.0,
                    script_type="ModuleScript",
                    roster_binding=roster_binding,  # type: ignore[arg-type]
                ),
            ],
            total_transpiled=1,
            total_ai=1,
        )

    def test_carrier_copied_onto_rbxscript_then_exempts(
        self, tmp_path: Path,
    ) -> None:
        pipeline = _make_pipeline(tmp_path)
        pipeline.state.rbx_place = RbxPlace()
        pipeline.state.transpilation_result = self._transpilation_with_carrier(
            _CARRIER,
        )
        pipeline._subphase_emit_scripts_to_disk()
        produced = next(
            s for s in pipeline.state.rbx_place.scripts
            if s.name == "CharacterDatabase"
        )
        assert produced.roster_binding == _CARRIER, (
            "the carrier must be COPIED from TranspiledScript onto the produced "
            "RbxScript at the conversion site (pipeline.py ~:3139)"
        )
        # End-to-end: the COPIED carrier (not a hand-built one) keeps the module
        # out of the dead set.
        pipeline._subphase_analyze_dead_modules()
        assert "CharacterDatabase" not in pipeline.state.dead_modules

    def test_RED_no_carrier_on_transpiled_script_lands_dead(
        self, tmp_path: Path,
    ) -> None:
        # Control: a TranspiledScript WITHOUT a carrier produces an RbxScript
        # without one, and the inert body lands in the dead set — proving the
        # copied carrier (not an incidental veto) is the load-bearing lever.
        pipeline = _make_pipeline(tmp_path)
        pipeline.state.rbx_place = RbxPlace()
        pipeline.state.transpilation_result = self._transpilation_with_carrier(None)
        pipeline._subphase_emit_scripts_to_disk()
        produced = next(
            s for s in pipeline.state.rbx_place.scripts
            if s.name == "CharacterDatabase"
        )
        assert produced.roster_binding is None
        pipeline._subphase_analyze_dead_modules()
        assert "CharacterDatabase" in pipeline.state.dead_modules
