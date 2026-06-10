"""test_dead_module_empty_subclass.py -- an empty subclass of a Roblox-dead
base inherits the base's dead verdict and is inert-stubbed, deterministically.

Root cause this guards (pre-existing, intermittent): a literal empty subclass
``class GerstnerDisplace : Displace {}`` (where ``Displace`` is a dead water-
shader) has no rendering API of its own, so ``is_input_side_dead`` misses it and
it falls through to AI transpile. The AI emits inheritance wiring -- a module-
scope ``require`` of the now-stubbed base + ``setmetatable`` -- that
intermittently trips the generic runtime contract (rules a/b) and fail-closes
the build ("violation(s) survived reprompt"). The fix inert-stubs the empty
subclass before transpile, deterministically, so the contract is satisfied.

The verdict is computed where project context exists (``code_transpiler`` has
``script_infos``) and STAMPED on ``TranspiledScript.intentional_inert_stub`` so
``contract_pipeline``'s stub_strategy exemption trusts the stamp instead of
recomputing ``_is_visual_only_script`` (which lacks the base-class context).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.code_transpiler import (  # noqa: E402
    TranspiledScript,
    transpile_scripts,
)
from converter.contract_pipeline import transpile_with_contract  # noqa: E402
from converter.roblox_dead_modules import (  # noqa: E402
    _empty_subclass_base,
    is_empty_subclass_of_dead_base,
    is_input_side_dead,
)
from converter.runtime_contract import verify_module  # noqa: E402

# A minimal hermetic Roblox-dead rendering helper: a render-image hook with no
# gameplay effect and dead-leaning mapping coverage.
_DEAD_BASE = (
    "using UnityEngine;\n"
    "public class WaterFx : MonoBehaviour {\n"
    "  void OnRenderImage(RenderTexture src, RenderTexture dst) {\n"
    "    Graphics.Blit(src, dst);\n"
    "  }\n"
    "}\n"
)
# A live gameplay base (real transform mutation -> NOT dead).
_LIVE_BASE = (
    "using UnityEngine;\n"
    "public class Mover : MonoBehaviour {\n"
    "  void Update() { transform.position = Vector3.zero; }\n"
    "}\n"
)
_EMPTY_SUBCLASS = (
    "using UnityEngine;\n"
    "namespace Game { public class GerstnerFx : WaterFx { } }\n"
)


class _ScriptInfo:
    """Minimal stand-in for ``unity.script_analyzer.ScriptInfo`` (only the
    fields the transpiler's class-name resolver reads)."""

    def __init__(self, path: Path, class_name: str,
                 suggested_type: str = "Script") -> None:
        self.path = path
        self.class_name = class_name
        self.referenced_types: list[str] = []
        self.suggested_type = suggested_type


# ---------------------------------------------------------------------------
# Pure-function verdict (codex-required cases)
# ---------------------------------------------------------------------------

class TestEmptySubclassVerdict:
    def test_dead_base_is_input_side_dead(self) -> None:
        # Anchor: the hermetic base really is input-side-dead, else the rest
        # of this suite would be vacuous.
        assert is_input_side_dead(_DEAD_BASE) is True

    def test_empty_subclass_of_dead_base_is_dead(self) -> None:
        assert is_empty_subclass_of_dead_base(
            _EMPTY_SUBCLASS, lambda n: _DEAD_BASE if n == "WaterFx" else None,
        ) is True

    def test_empty_subclass_of_live_base_is_not_dead(self) -> None:
        assert is_empty_subclass_of_dead_base(
            "public class Sub : Mover {}",
            lambda n: _LIVE_BASE if n == "Mover" else None,
        ) is False

    def test_subclass_with_a_member_is_not_dead(self) -> None:
        # Even one member -> not a literal empty subclass -> abstain.
        assert is_empty_subclass_of_dead_base(
            "public class Sub : WaterFx { int x; }",
            lambda n: _DEAD_BASE if n == "WaterFx" else None,
        ) is False

    def test_unknown_base_abstains(self) -> None:
        assert is_empty_subclass_of_dead_base(
            _EMPTY_SUBCLASS, lambda n: None,
        ) is False

    def test_ambiguous_base_abstains(self) -> None:
        # A resolver that returns None for a colliding name (the contract the
        # transpiler's resolver honours) -> abstain.
        assert is_empty_subclass_of_dead_base(
            "public class Sub : Dup {}", lambda n: None,
        ) is False

    def test_transitive_chain_to_dead_ancestor(self) -> None:
        # A : B, B : C, only C is the rendering helper.
        sources = {"B": "class B : C {}", "C": _DEAD_BASE}
        assert is_empty_subclass_of_dead_base(
            "class A : B {}", lambda n: sources.get(n),
        ) is True

    def test_cycle_abstains(self) -> None:
        sources = {"A": "class A : B {}", "B": "class B : A {}"}
        assert is_empty_subclass_of_dead_base(
            "class A : B {}", lambda n: sources.get(n),
        ) is False

    def test_generic_base_abstains(self) -> None:
        # ``class Foo : Base<T> {}`` does not parse as a plain base name.
        assert _empty_subclass_base("class Foo : Base<T> {}") is None

    def test_non_subclass_returns_none(self) -> None:
        assert _empty_subclass_base(_DEAD_BASE) is None

    def test_partial_class_abstains(self) -> None:
        # A partial class can carry behavior in a sibling file this single
        # source cannot see -> never inherit deadness.
        assert _empty_subclass_base("public partial class Sub : WaterFx {}") is None
        assert is_empty_subclass_of_dead_base(
            "public partial class Sub : WaterFx {}",
            lambda n: _DEAD_BASE if n == "WaterFx" else None,
        ) is False

    def test_dotted_base_abstains(self) -> None:
        # A dotted base could collapse to an unrelated same-named class -> the
        # bare-identifier requirement abstains.
        assert _empty_subclass_base("public class Sub : Vendor.WaterFx {}") is None

    def test_multi_base_list_abstains(self) -> None:
        # ``class Sub : WaterFx, IFoo {}`` -- the interface list breaks the
        # bare-base + empty-body match -> abstain (conservative).
        assert _empty_subclass_base("public class Sub : WaterFx, IFoo {}") is None

    def test_mixed_declaration_file_abstains(self) -> None:
        # A file that also declares a non-empty type is not a pure empty
        # subclass -> abstain.
        src = (
            "public class Sub : WaterFx {}\n"
            "public class Other { void Run() { } }\n"
        )
        assert _empty_subclass_base(src) is None

    def test_sibling_record_or_delegate_abstains(self) -> None:
        # ``record`` / ``delegate`` are type declarations too -- a file mixing
        # the empty subclass with either must abstain (else stubbing the file
        # would drop the sibling's behavior).
        assert _empty_subclass_base(
            "public class Sub : WaterFx {} public record Helper { void Run(){} }",
        ) is None
        assert _empty_subclass_base(
            "public class Sub : WaterFx {} public delegate void Tick();",
        ) is None


# ---------------------------------------------------------------------------
# Transpile: the empty subclass becomes a stamped inert ModuleScript stub
# ---------------------------------------------------------------------------

@pytest.fixture
def water_project(tmp_path: Path):
    proj = tmp_path / "project"
    (proj / "Assets" / "Scripts").mkdir(parents=True)
    base = proj / "Assets" / "Scripts" / "WaterFx.cs"
    base.write_text(_DEAD_BASE)
    sub = proj / "Assets" / "Scripts" / "GerstnerFx.cs"
    sub.write_text(_EMPTY_SUBCLASS)
    infos = [
        _ScriptInfo(base, "WaterFx", suggested_type="ModuleScript"),
        _ScriptInfo(sub, "GerstnerFx", suggested_type="ModuleScript"),
    ]
    return proj, base, sub, infos


class TestTranspileStubsEmptySubclass:
    def test_generic_empty_subclass_is_inert_stub(self, water_project) -> None:
        proj, base, sub, infos = water_project
        paths = frozenset({base, sub})
        result = transpile_scripts(
            proj, infos, use_ai=False, runtime_mode="generic",
            runtime_bearing_paths=paths, component_class_paths=paths,
        )
        by_name = {Path(s.source_path).name: s for s in result.scripts}
        gerstner = by_name["GerstnerFx.cs"]
        # The fix: deterministic inert stub, stamped intentional.
        assert gerstner.strategy == "stub"
        assert gerstner.intentional_inert_stub is True
        assert gerstner.script_type == "ModuleScript"
        # No module-scope require / setmetatable -> passes the runtime contract.
        assert "require(" not in gerstner.luau_source
        assert not verify_module(gerstner.luau_source).violations

    def test_legacy_mode_unchanged(self, water_project) -> None:
        # Legacy mode has no generic contract; the empty-subclass path must not
        # alter legacy output (the dead base still stubs via the visual-only
        # gate, but the empty subclass is NOT force-stubbed there).
        proj, base, sub, infos = water_project
        result = transpile_scripts(proj, infos, use_ai=False)
        by_name = {Path(s.source_path).name: s for s in result.scripts}
        gerstner = by_name["GerstnerFx.cs"]
        assert gerstner.intentional_inert_stub is False


# ---------------------------------------------------------------------------
# Contract pipeline: the stamped stub is exempt from the stub_strategy
# fail-close; an empty subclass of a LIVE base is NOT exempt (control).
# ---------------------------------------------------------------------------

class TestContractPipelineExemption:
    def _project(self, tmp_path: Path):
        proj = tmp_path / "project"
        (proj / "Assets" / "Scripts").mkdir(parents=True)
        files = {
            "WaterFx.cs": _DEAD_BASE,
            "GerstnerFx.cs": _EMPTY_SUBCLASS,
            "Mover.cs": _LIVE_BASE,
            "LiveSub.cs": (
                "using UnityEngine;\n"
                "public class LiveSub : Mover { }\n"
            ),
        }
        for name, src in files.items():
            (proj / "Assets" / "Scripts" / name).write_text(src)
        infos = [
            _ScriptInfo(proj / "Assets" / "Scripts" / "WaterFx.cs", "WaterFx"),
            _ScriptInfo(proj / "Assets" / "Scripts" / "GerstnerFx.cs", "GerstnerFx"),
            _ScriptInfo(proj / "Assets" / "Scripts" / "Mover.cs", "Mover"),
            _ScriptInfo(proj / "Assets" / "Scripts" / "LiveSub.cs", "LiveSub"),
        ]
        scene_runtime = {
            "modules": {
                "g-waterfx": {
                    "stem": "WaterFx", "class_name": "WaterFx",
                    "runtime_bearing": True, "is_component_class": True,
                    "character_attached": False, "is_loader": False,
                },
                "g-gerstnerfx": {
                    "stem": "GerstnerFx", "class_name": "GerstnerFx",
                    "runtime_bearing": True, "is_component_class": True,
                    "character_attached": False, "is_loader": False,
                },
                "g-mover": {
                    "stem": "Mover", "class_name": "Mover",
                    "runtime_bearing": True, "is_component_class": True,
                    "character_attached": False, "is_loader": False,
                },
                "g-livesub": {
                    "stem": "LiveSub", "class_name": "LiveSub",
                    "runtime_bearing": True, "is_component_class": True,
                    "character_attached": False, "is_loader": False,
                },
            },
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }
        return proj, infos, scene_runtime

    def test_empty_subclass_of_dead_base_not_fail_closed(
        self, tmp_path: Path,
    ) -> None:
        proj, infos, scene_runtime = self._project(tmp_path)
        result = transpile_with_contract(
            unity_project_path=proj, script_infos=infos,
            scene_runtime=scene_runtime, use_ai=False,
        )
        # The empty subclass of the dead base produces NEITHER a stub_strategy
        # nor a verifier fail-close row -- it is a clean inert stub.
        offending = [
            f for f in result.fail_closed
            if "GerstnerFx" in f.detail
        ]
        assert offending == [], (
            f"GerstnerFx (empty subclass of dead WaterFx) must not fail-close; "
            f"got {[(f.kind, f.detail) for f in offending]}"
        )

    def test_empty_subclass_of_live_base_still_fail_closed(
        self, tmp_path: Path,
    ) -> None:
        # Control: an empty subclass of a LIVE base is runtime-bearing and, with
        # AI off, falls through to a non-intentional stub -> MUST fail-close
        # (proves the exemption is specific to dead bases, not all empty
        # subclasses).
        proj, infos, scene_runtime = self._project(tmp_path)
        result = transpile_with_contract(
            unity_project_path=proj, script_infos=infos,
            scene_runtime=scene_runtime, use_ai=False,
        )
        livesub_rows = [
            f for f in result.fail_closed
            if "LiveSub" in f.detail and f.kind == "stub_strategy"
        ]
        assert livesub_rows, (
            "LiveSub (empty subclass of a LIVE base) must still fail-close as a "
            "stub_strategy fallthrough when AI is unavailable."
        )
