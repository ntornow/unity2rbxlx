"""Fix #15 (re-transpile collateral) -- cheap unit coverage for the
component-class routing fix and the Root-A fail-closed gating.

These run fast (no AI, no Studio) and are the guard Codex asked for BEFORE
paying the ~20-30 min generic e2e re-transpile: they catch predicate /
routing bugs that would otherwise only surface after a long real-AI run.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.scene_runtime_planner import (  # noqa: E402
    _resolves_to_component,
    _build_modules_table,
)
from converter.contract_pipeline import (  # noqa: E402
    FailClosed,
    _component_class_paths,
    _runtime_bearing_paths,
)
from converter.pipeline import _contract_failure_errors  # noqa: E402
from converter.code_transpiler import _inert_component_stub  # noqa: E402
from converter.runtime_contract import verify_module  # noqa: E402
from core.unity_types import GuidEntry, GuidIndex  # noqa: E402


# ---------------------------------------------------------------------------
# Fix B: inheritance-aware component detection (Codex C2)
# ---------------------------------------------------------------------------

class TestResolvesToComponent:

    def test_direct_monobehaviour(self):
        assert _resolves_to_component("Player", "MonoBehaviour", {})

    def test_direct_networkbehaviour(self):
        # Mirror / legacy UNet networked components are still components.
        assert _resolves_to_component("Mover", "NetworkBehaviour", {})

    def test_transitive_through_project_base(self):
        # Turret : Weapon, Weapon : MonoBehaviour  =>  Turret is a component
        # even though its immediate base is a project-local class.
        base_by_class = {"Turret": "Weapon", "Weapon": "MonoBehaviour"}
        assert _resolves_to_component("Turret", "Weapon", base_by_class)

    def test_plain_class_is_not_component(self):
        assert not _resolves_to_component("DamageMath", "", {})

    def test_scriptableobject_is_not_component(self):
        assert not _resolves_to_component("WeaponConfig", "ScriptableObject", {})

    def test_unrelated_base_is_not_component(self):
        base_by_class = {"Foo": "Bar", "Bar": "Baz"}
        assert not _resolves_to_component("Foo", "Bar", base_by_class)

    def test_inheritance_cycle_terminates(self):
        # A pathological self/mutual cycle must not loop forever.
        base_by_class = {"A": "B", "B": "A"}
        assert not _resolves_to_component("A", "B", base_by_class)


# ---------------------------------------------------------------------------
# is_component_class predicate behavior (Codex P2 round 1 + round 2)
#
# The predicate at scene_runtime_planner._build_modules_table line ~1028
# routes classes to either the host-bound generic contract (component) or
# the legacy ``script.Parent`` path. Both directions of misclassification
# are user-visible: a plain helper forced through the generic contract may
# emit a ModuleScript that throws on require; a real external-base
# MonoBehaviour forced through legacy emits ``script.Parent`` that throws
# at runtime. Round-1 tightening fixed the first; round-2 widening
# (``base_class != ""``) fixed the regression on the second.
# ---------------------------------------------------------------------------

def _drop_cs(tmp_path: Path, stem: str, source: str) -> tuple[str, GuidEntry]:
    p = tmp_path / f"{stem}.cs"
    p.write_text(source, encoding="utf-8")
    guid = f"guid-{stem.lower()}"
    return guid, GuidEntry(
        guid=guid, asset_path=p, relative_path=Path(p.name), kind="script",
    )


def _index_for(entries: list[GuidEntry]) -> GuidIndex:
    return GuidIndex(
        project_root=Path("/proj"),
        guid_to_entry={e.guid: e for e in entries},
        path_to_guid={e.asset_path.resolve(): e.guid for e in entries},
    )


class TestComponentClassPredicate:

    def test_plain_helper_class_with_start_is_not_component(self, tmp_path):
        # Codex P2 round 1 catch: ``class Stopwatch { void Start() {} }`` --
        # a helper with no base whose method just happens to be named
        # ``Start`` -- must NOT be classified as a Unity component.
        guid, entry = _drop_cs(tmp_path, "Stopwatch", (
            "public class Stopwatch {\n"
            "    public void Start() {}\n"
            "}\n"
        ))
        modules = _build_modules_table(_index_for([entry]), set(), set())
        assert modules[guid]["is_component_class"] is False

    def test_helper_class_without_hooks_is_not_component(self, tmp_path):
        # Sanity: no hook, no base -> not a component.
        guid, entry = _drop_cs(tmp_path, "DamageMath", (
            "public class DamageMath {\n"
            "    public static int Compute(int a) { return a; }\n"
            "}\n"
        ))
        modules = _build_modules_table(_index_for([entry]), set(), set())
        assert modules[guid]["is_component_class"] is False

    def test_direct_monobehaviour_is_component(self, tmp_path):
        guid, entry = _drop_cs(tmp_path, "Player", (
            "public class Player : MonoBehaviour {\n"
            "    void Awake() {}\n"
            "}\n"
        ))
        modules = _build_modules_table(_index_for([entry]), set(), set())
        assert modules[guid]["is_component_class"] is True

    def test_external_base_with_lifecycle_hook_is_component(self, tmp_path):
        # Codex P2 round 2 catch: a class inheriting from an EXTERNAL
        # MonoBehaviour-derived base (e.g. Photon's
        # MonoBehaviourPunCallbacks, Mirror's NetworkBehaviour in an
        # unwalkable package) plus a lifecycle hook must still route
        # through the host-bound generic contract -- the round-1
        # narrowing (requiring has_unity_api) wrongly dropped these
        # back into the legacy ``script.Parent`` path.
        guid, entry = _drop_cs(tmp_path, "LobbyManager", (
            "public class LobbyManager : MonoBehaviourPunCallbacks {\n"
            "    void Start() {}\n"
            "}\n"
        ))
        modules = _build_modules_table(_index_for([entry]), set(), set())
        assert modules[guid]["is_component_class"] is True

    def test_external_base_without_hook_is_not_component(self, tmp_path):
        # An external base alone is not enough -- without a lifecycle
        # hook, treat as a plain class (no over-classification).
        guid, entry = _drop_cs(tmp_path, "Helper", (
            "public class Helper : SomeExternalClass {\n"
            "    public void DoStuff() {}\n"
            "}\n"
        ))
        modules = _build_modules_table(_index_for([entry]), set(), set())
        assert modules[guid]["is_component_class"] is False


# ---------------------------------------------------------------------------
# Fix B: component-class path selection vs placement (C1 superset invariant)
# ---------------------------------------------------------------------------

class _Info:
    """Minimal ScriptInfo stand-in: the path-join only reads ``.path``."""

    def __init__(self, path: Path) -> None:
        self.path = path


class TestComponentClassPathSelection:

    def _modules_and_infos(self):
        placed = Path("/proj/Assets/Player.cs")
        spawned = Path("/proj/Assets/Explosive.cs")
        plain = Path("/proj/Assets/DamageMath.cs")
        modules = {
            "guid-placed": {
                "stem": "Player", "runtime_bearing": True,
                "is_component_class": True,
            },
            "guid-spawned": {
                "stem": "Explosive", "runtime_bearing": False,
                "is_component_class": True,
            },
            "guid-plain": {
                "stem": "DamageMath", "runtime_bearing": False,
                "is_component_class": False,
            },
        }
        infos = [_Info(placed), _Info(spawned), _Info(plain)]
        return modules, infos, placed, spawned, plain

    def test_spawned_component_is_a_generic_path_but_not_runtime_bearing(self):
        modules, infos, placed, spawned, plain = self._modules_and_infos()
        comp_paths, _ = _component_class_paths(modules, infos)
        bearing_paths, _ = _runtime_bearing_paths(modules, infos)

        # The spawned-only component routes generic (gets the contract)...
        assert spawned in comp_paths
        # ...but is NOT runtime-bearing, so the host won't boot it at start.
        assert spawned not in bearing_paths
        # Placed component is in both.
        assert placed in comp_paths and placed in bearing_paths
        # Plain non-component is in neither.
        assert plain not in comp_paths and plain not in bearing_paths

    def test_runtime_bearing_implies_component_backcompat(self):
        # Old artifact: runtime_bearing set but is_component_class absent.
        # The OR invariant keeps placed MonoBehaviours routed generic.
        placed = Path("/proj/Assets/Player.cs")
        modules = {"g": {"stem": "Player", "runtime_bearing": True}}
        infos = [_Info(placed)]
        comp_paths, _ = _component_class_paths(modules, infos)
        assert placed in comp_paths


# ---------------------------------------------------------------------------
# Fix A: fail-closed reasons become conversion errors (pure render helper)
# ---------------------------------------------------------------------------

class TestContractFailureErrors:

    def test_renders_each_row(self):
        rows = [
            FailClosed(kind="verifier", detail="Player.luau: 1 violation(s) survived reprompt"),
            FailClosed(kind="stub_strategy", detail="Explosive.cs: fell through to stub"),
        ]
        msgs = _contract_failure_errors(rows)
        assert len(msgs) == 2
        assert all(m.startswith("scene-runtime contract failed closed") for m in msgs)
        assert "verifier" in msgs[0] and "Player.luau" in msgs[0]

    def test_empty_is_empty(self):
        assert _contract_failure_errors([]) == []

    def test_does_not_mutate_input(self):
        rows = [FailClosed(kind="verifier", detail="x")]
        _contract_failure_errors(rows)
        assert rows == [FailClosed(kind="verifier", detail="x")]


# ---------------------------------------------------------------------------
# Visual-only component stub: contract-valid inert ModuleScript (e2e finding)
# ---------------------------------------------------------------------------

class TestInertComponentStub:

    def test_inert_stub_passes_the_verifier(self):
        # A water-shader MonoBehaviour is routed generic; its stub must be a
        # requirable inert class table, not a nil-returning print(...) — the
        # latter both fails rule (d) and throws when the host requires it.
        src = _inert_component_stub("WaterBase", "no Roblox equivalent")
        result = verify_module(src)
        assert result.ok, [(v.rule, v.line) for v in result.violations]

    def test_inert_stub_returns_a_class_table(self):
        src = _inert_component_stub("WaterBase", "no Roblox equivalent")
        assert src.rstrip().endswith("return WaterBase")

    def test_inert_stub_sanitizes_non_identifier_stems(self):
        src = _inert_component_stub("Weird-Name 2", "x")
        assert "local Weird_Name_2 = {}" in src
        assert verify_module(src).ok

    def test_inert_stub_digit_leading_stem_gets_underscore_prefix(self):
        # Codex P2 round 1: Luau identifiers cannot start with a digit, so
        # ``2DWater.cs`` must be prefixed (``_2DWater``); the regex-only
        # verifier silently accepts the invalid identifier and the place
        # ships a ModuleScript that explodes on require().
        src = _inert_component_stub("2DWater", "x")
        assert "local _2DWater = {}" in src
        assert verify_module(src).ok
        # Sanity: digit-leading AND non-identifier chars compose correctly.
        src2 = _inert_component_stub("3D-Effect", "x")
        assert "local _3D_Effect = {}" in src2
        assert verify_module(src2).ok

    def test_inert_stub_luau_keyword_stem_gets_underscore_prefix(self):
        # Codex review round 2: file stems that happen to be Luau reserved
        # words (``end.cs``, ``do.cs``, ``return.cs``, ...) survive the
        # non-word sanitize unchanged and emit ``local end = {}`` -- a
        # syntax error the regex verifier cannot detect. Prefix with ``_``.
        for kw in ("end", "do", "local", "function", "return", "then",
                   "else", "if", "for", "while", "repeat", "until",
                   "break", "continue", "nil", "true", "false",
                   "and", "or", "not", "in"):
            src = _inert_component_stub(kw, "x")
            assert f"local _{kw} = {{}}" in src, kw
            assert verify_module(src).ok, kw
