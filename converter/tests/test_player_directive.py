"""Slice 4.1 — per-script player-controller prompt directive (paradigm B,
NON-load-bearing).

Drives the REAL plumbing:
- ``contract_pipeline._player_controller_paths`` (deterministic upstream
  ``has_character_controller`` targeting, §3) — AC1b, AC6.
- ``code_transpiler.transpile_scripts`` ``player_controller_paths`` kwarg +
  ``_transpile_one`` directive append — AC1, AC7.

AC7 also confirms the directive lands in ``project_context`` (a cache-key
field) and NOT the byte-frozen ``_AI_SYSTEM_PROMPT`` (the frozen-prompt
assertion test in test_scene_runtime_transpiler.py stays green — re-run
asserted by the broader suite).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from converter import code_transpiler
from converter.code_transpiler import (
    _AI_SYSTEM_PROMPT,
    _PLAYER_CONTROLLER_DIRECTIVE,
    transpile_scripts,
)
from converter.contract_pipeline import (
    _player_controller_paths,
    transpile_with_contract,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

class _ScriptInfoStub:
    """Minimal stand-in for ``unity.script_analyzer.ScriptInfo`` (mirrors
    test_scene_runtime_transpiler.py): the transpiler only reads ``path``,
    ``class_name``, ``referenced_types``, ``suggested_type``, ``base_class``."""

    def __init__(self, path: Path, class_name: str,
                 suggested_type: str = "Script",
                 base_class: str = "MonoBehaviour") -> None:
        self.path = path
        self.class_name = class_name
        self.referenced_types: list[str] = []
        self.suggested_type = suggested_type
        self.base_class = base_class


def _row(stem: str, has_cc: bool) -> dict[str, object]:
    """A ``scene_runtime.modules`` row (mirrors test_movement_facet_lowering)."""
    return {
        "stem": stem,
        "class_name": stem,
        "runtime_bearing": True,
        "is_component_class": True,
        "character_attached": False,
        "is_loader": False,
        "has_character_controller": has_cc,
    }


def _modules(cc: tuple[str, ...] = (),
             plain: tuple[str, ...] = ()) -> dict[str, dict[str, object]]:
    mods: dict[str, dict[str, object]] = {}
    for stem in cc:
        mods[f"guid-{stem}"] = _row(stem, has_cc=True)
    for stem in plain:
        mods[f"guid-{stem}"] = _row(stem, has_cc=False)
    return mods


@pytest.fixture
def fps_like_project(tmp_path: Path):
    """A 2-script synthetic project: ``Player`` (the CC-bearing controller)
    and ``HUD`` (a non-player component). No dep between them, so each gets
    its OWN backend call — directive isolation is observable per script."""
    proj = tmp_path / "project"
    (proj / "Assets" / "Scripts").mkdir(parents=True)
    player_path = proj / "Assets" / "Scripts" / "Player.cs"
    player_path.write_text(
        "using UnityEngine;\n"
        "public class Player : MonoBehaviour { void Update() {} }\n"
    )
    hud_path = proj / "Assets" / "Scripts" / "HUD.cs"
    hud_path.write_text(
        "using UnityEngine;\n"
        "public class HUD : MonoBehaviour { void Update() {} }\n"
    )
    return proj, [
        _ScriptInfoStub(player_path, "Player"),
        _ScriptInfoStub(hud_path, "HUD"),
    ]


def _capture_contexts(monkeypatch) -> dict[str, str]:
    """Force the anthropic_api backend and capture the ``project_context``
    passed per ``class_name``. Returns the live (mutated) capture dict."""
    monkeypatch.setattr(code_transpiler, "_find_transpiler",
                        lambda: "anthropic_api")
    captured: dict[str, str] = {}

    def fake_ai(csharp_source, api_key, model, class_name="",
                script_type="Script", project_context="",
                runtime_mode="legacy", is_player_controller=False):
        captured[class_name] = project_context
        return ("local M = {}\nreturn M\n", 0.9, [])

    monkeypatch.setattr(code_transpiler, "_ai_transpile", fake_ai)
    monkeypatch.setattr(code_transpiler, "_claude_cli_transpile",
                        lambda *a, **k: ("", 0.0, []))
    return captured


# ---------------------------------------------------------------------------
# AC1 — directive emitted for the identified player ONLY
# ---------------------------------------------------------------------------

class TestDirectiveEmission:

    def test_directive_in_player_context_only(self, fps_like_project, monkeypatch):
        proj, infos = fps_like_project
        player_path = infos[0].path
        captured = _capture_contexts(monkeypatch)
        transpile_scripts(
            unity_project_path=proj,
            script_infos=infos,
            use_ai=True,
            api_key="dummy",
            runtime_mode="generic",
            runtime_bearing_paths=frozenset({p.path for p in infos}),
            player_controller_paths=frozenset({player_path}),
        )
        assert _PLAYER_CONTROLLER_DIRECTIVE in captured["Player"], (
            "Player controller's prompt context did not carry the directive."
        )
        assert _PLAYER_CONTROLLER_DIRECTIVE not in captured["HUD"], (
            "Non-player script wrongly received the player directive."
        )

    def test_no_directive_when_paths_empty(self, fps_like_project, monkeypatch):
        """B abstained (empty player set) -> NO script gets the directive."""
        proj, infos = fps_like_project
        captured = _capture_contexts(monkeypatch)
        transpile_scripts(
            unity_project_path=proj,
            script_infos=infos,
            use_ai=True,
            api_key="dummy",
            runtime_mode="generic",
            runtime_bearing_paths=frozenset({p.path for p in infos}),
            player_controller_paths=frozenset(),
        )
        for ctx in captured.values():
            assert _PLAYER_CONTROLLER_DIRECTIVE not in ctx

    def test_directive_gated_on_generic_mode(self, fps_like_project, monkeypatch):
        """Under legacy mode the directive is never appended, even if a path
        is (spuriously) passed — the §1.4 gate requires generic."""
        proj, infos = fps_like_project
        captured = _capture_contexts(monkeypatch)
        transpile_scripts(
            unity_project_path=proj,
            script_infos=infos,
            use_ai=True,
            api_key="dummy",
            runtime_mode="legacy",
            player_controller_paths=frozenset({infos[0].path}),
        )
        for ctx in captured.values():
            assert _PLAYER_CONTROLLER_DIRECTIVE not in ctx

    def test_default_kwarg_no_directive(self, fps_like_project, monkeypatch):
        """Legacy callers / direct unit tests omit the kwarg entirely ->
        byte-identical no-directive context preserved."""
        proj, infos = fps_like_project
        captured = _capture_contexts(monkeypatch)
        transpile_scripts(
            unity_project_path=proj,
            script_infos=infos,
            use_ai=True,
            api_key="dummy",
            runtime_mode="generic",
            runtime_bearing_paths=frozenset({p.path for p in infos}),
        )
        for ctx in captured.values():
            assert _PLAYER_CONTROLLER_DIRECTIVE not in ctx


# ---------------------------------------------------------------------------
# AC1b — _player_controller_paths matches find_player_controllers abstention
# ---------------------------------------------------------------------------

class TestPlayerControllerPaths:
    """0 -> ∅; exactly 1 (unique join) -> {path}; >1 -> ∅; stem-collision -> ∅.
    Mirrors find_player_controllers' fail-closed contract."""

    def _infos(self, tmp_path: Path, stems: tuple[str, ...]) -> list[_ScriptInfoStub]:
        infos: list[_ScriptInfoStub] = []
        for stem in stems:
            p = tmp_path / f"{stem}.cs"
            p.write_text(f"public class {stem} {{}}\n")
            infos.append(_ScriptInfoStub(p, stem))
        return infos

    def test_zero_cc_abstains(self, tmp_path):
        infos = self._infos(tmp_path, ("Player",))
        assert _player_controller_paths(_modules(plain=("Player",)), infos) == frozenset()

    def test_exactly_one_returns_path(self, tmp_path):
        infos = self._infos(tmp_path, ("Player", "HUD"))
        result = _player_controller_paths(_modules(cc=("Player",), plain=("HUD",)), infos)
        assert result == frozenset({infos[0].path})

    def test_two_cc_abstains(self, tmp_path):
        infos = self._infos(tmp_path, ("PlayerA", "PlayerB"))
        assert _player_controller_paths(
            _modules(cc=("PlayerA", "PlayerB")), infos,
        ) == frozenset()

    def test_flagged_stem_no_matching_script_abstains(self, tmp_path):
        """CC-flagged module's stem matches no script -> join yields 0 paths
        -> abstain (len(paths) != 1)."""
        infos = self._infos(tmp_path, ("HUD",))
        assert _player_controller_paths(_modules(cc=("OtherCtrl",)), infos) == frozenset()

    def test_stem_collision_abstains(self, tmp_path):
        """Two .cs files share the CC-flagged stem -> _join_module_paths flags
        a collision -> abstain (matches find_player_controllers' parity)."""
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        p1 = d1 / "Player.cs"
        p2 = d2 / "Player.cs"
        p1.write_text("public class Player {}\n")
        p2.write_text("public class Player {}\n")
        infos = [_ScriptInfoStub(p1, "Player"), _ScriptInfoStub(p2, "Player")]
        assert _player_controller_paths(_modules(cc=("Player",)), infos) == frozenset()

    def test_no_modules_abstains(self, tmp_path):
        infos = self._infos(tmp_path, ("Player",))
        assert _player_controller_paths({}, infos) == frozenset()


# ---------------------------------------------------------------------------
# AC6 — §3: targeting keyed on the deterministic signal, never an AI fingerprint
# ---------------------------------------------------------------------------

class TestNoFingerprint:

    def test_keys_on_has_character_controller(self):
        """Static guard: the helper's source reads the planner signal
        ``has_character_controller`` and never a transpiled fingerprint."""
        import inspect

        src = inspect.getsource(_player_controller_paths)
        assert "has_character_controller" in src, (
            "_player_controller_paths must key on the deterministic upstream "
            "has_character_controller signal."
        )
        # It must NOT read transpiled output fields.
        for forbidden in ("luau_source", "TranspiledScript", "scripts"):
            assert forbidden not in src, (
                f"_player_controller_paths reads transpiled output ({forbidden!r}) "
                "— §3 forbids targeting on an AI-output fingerprint."
            )

    def test_directive_targeting_reads_signal_not_output(self):
        """The contract_pipeline call site computes player paths from
        ``modules`` (the planner signal) BEFORE transpilation, not from the
        ``TranspilationResult``."""
        import inspect

        from converter import contract_pipeline

        src = inspect.getsource(contract_pipeline.transpile_with_contract)
        # The player paths are computed and passed before/at the transpile call.
        assert "_player_controller_paths(modules, script_infos)" in src
        assert "player_controller_paths=player_controller_paths" in src


# ---------------------------------------------------------------------------
# AC7 — frozen-prompt cache key preserved; directive is player-scoped
# ---------------------------------------------------------------------------

class TestFrozenPromptPreserved:

    def test_directive_not_in_frozen_system_prompt(self):
        """The directive lives in ``project_context``, NEVER in the byte-frozen
        ``_AI_SYSTEM_PROMPT`` — the frozen-prompt cache-key assertion holds."""
        assert _PLAYER_CONTROLLER_DIRECTIVE not in _AI_SYSTEM_PROMPT

    def test_directive_absent_from_every_non_player_context(
        self, fps_like_project, monkeypatch,
    ):
        """AC7: the DIRECTIVE substring is absent from every non-player
        backend call's ``project_context``."""
        proj, infos = fps_like_project
        player_path = infos[0].path
        captured = _capture_contexts(monkeypatch)
        transpile_scripts(
            unity_project_path=proj,
            script_infos=infos,
            use_ai=True,
            api_key="dummy",
            runtime_mode="generic",
            runtime_bearing_paths=frozenset({p.path for p in infos}),
            player_controller_paths=frozenset({player_path}),
        )
        for class_name, ctx in captured.items():
            if class_name == "Player":
                continue
            assert _PLAYER_CONTROLLER_DIRECTIVE not in ctx, (
                f"directive leaked into non-player {class_name!r} context."
            )

    def test_player_context_key_changes_by_exactly_the_directive(
        self, fps_like_project, monkeypatch,
    ):
        """The player script's ``project_context`` differs from the no-directive
        run by EXACTLY the appended directive block (its cache key changes by
        the directive append, nothing else)."""
        proj, infos = fps_like_project
        player_path = infos[0].path

        without = _capture_contexts(monkeypatch)
        transpile_scripts(
            unity_project_path=proj, script_infos=infos, use_ai=True,
            api_key="dummy", runtime_mode="generic",
            runtime_bearing_paths=frozenset({p.path for p in infos}),
            player_controller_paths=frozenset(),
        )
        base_ctx = without["Player"]

        with_dir = _capture_contexts(monkeypatch)
        transpile_scripts(
            unity_project_path=proj, script_infos=infos, use_ai=True,
            api_key="dummy", runtime_mode="generic",
            runtime_bearing_paths=frozenset({p.path for p in infos}),
            player_controller_paths=frozenset({player_path}),
        )
        player_ctx = with_dir["Player"]

        assert player_ctx == f"{base_ctx}\n\n{_PLAYER_CONTROLLER_DIRECTIVE}", (
            "Player context changed by something other than the directive append."
        )


# ---------------------------------------------------------------------------
# AC1 / AC7 (DYNAMIC) — drive the REAL transpile_with_contract handoff
# (modules -> _player_controller_paths -> transpile_scripts -> _transpile_one)
# rather than source-inspecting it, AND assert the AC7 downstream-dependent
# cache-fanout MECHANISM: the player's transpiled Luau flows into a direct
# dependent's `scoped` context (the channel that re-keys the dependent), while
# the DIRECTIVE itself reaches only the player's context.
# ---------------------------------------------------------------------------

class TestTranspileWithContractDynamic:
    """Closes the slice-4.1 P2: the handoff was only ``inspect.getsource``-d.
    Here the directive targeting runs through the REAL pipeline seam with a
    mocked backend, and the per-class ``project_context`` is captured live."""

    _PLAYER_LUAU = "local Player = {}\nPlayer.MARK = 'PLAYER_OUTPUT_SENTINEL'\nreturn Player\n"

    def _make_project(self, tmp_path: Path):
        """``Player`` is the CC-bearing controller; ``HUD`` REFERENCES ``Player``
        (a direct dependency) so the player's transpiled Luau lands in HUD's
        scoped context — the AC7 fan-out channel."""
        proj = tmp_path / "unity"
        (proj / "Assets").mkdir(parents=True)
        player = proj / "Assets" / "Player.cs"
        player.write_text(
            "using UnityEngine;\n"
            "public class Player : MonoBehaviour { void Update() {} }\n"
        )
        hud = proj / "Assets" / "HUD.cs"
        hud.write_text(
            "using UnityEngine;\n"
            "public class HUD : MonoBehaviour { Player p; void Update() {} }\n"
        )
        infos = [
            _ScriptInfoStub(player, "Player"),
            _ScriptInfoStub(hud, "HUD"),
        ]
        scene_runtime = {
            "modules": {
                "guid-player": {
                    "stem": "Player", "class_name": "Player",
                    "runtime_bearing": True, "is_component_class": True,
                    "has_character_controller": True,
                },
                "guid-hud": {
                    "stem": "HUD", "class_name": "HUD",
                    "runtime_bearing": True, "is_component_class": True,
                    "has_character_controller": False,
                },
            },
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }
        return proj, infos, scene_runtime

    def _capture(self, monkeypatch, player_luau: str):
        monkeypatch.setattr(code_transpiler, "_find_transpiler",
                            lambda: "anthropic_api")
        captured: dict[str, str] = {}

        def fake_ai(csharp_source, api_key, model, class_name="",
                    script_type="Script", project_context="",
                    runtime_mode="legacy", is_player_controller=False):
            captured[class_name] = project_context
            luau = player_luau if class_name == "Player" else (
                f"local {class_name} = {{}}\nreturn {class_name}\n"
            )
            return (luau, 0.9, [])

        monkeypatch.setattr(code_transpiler, "_ai_transpile", fake_ai)
        monkeypatch.setattr(code_transpiler, "_claude_cli_transpile",
                            lambda *a, **k: ("", 0.0, []))
        return captured

    def test_directive_targets_player_through_real_pipeline(
        self, tmp_path, monkeypatch,
    ):
        """AC1 dynamic: the directive reaches ONLY the player's context when
        the targeting is computed by the REAL ``_player_controller_paths``
        inside ``transpile_with_contract`` (NOT a hand-passed kwarg)."""
        proj, infos, scene_runtime = self._make_project(tmp_path)
        captured = self._capture(monkeypatch, self._PLAYER_LUAU)

        transpile_with_contract(
            unity_project_path=proj,
            script_infos=infos,
            scene_runtime=scene_runtime,
            api_key="dummy",
            use_ai=True,
        )

        assert _PLAYER_CONTROLLER_DIRECTIVE in captured["Player"], (
            "directive did not reach the player via transpile_with_contract."
        )
        assert _PLAYER_CONTROLLER_DIRECTIVE not in captured["HUD"], (
            "directive leaked into the non-player dependent."
        )

    def test_abstains_when_two_cc_modules_through_real_pipeline(
        self, tmp_path, monkeypatch,
    ):
        """AC1b dynamic: with TWO ``has_character_controller`` modules the
        helper abstains, so NO script gets the directive even though both are
        flagged — proves the abstention rides the real handoff."""
        proj, infos, scene_runtime = self._make_project(tmp_path)
        scene_runtime["modules"]["guid-hud"]["has_character_controller"] = True
        captured = self._capture(monkeypatch, self._PLAYER_LUAU)

        transpile_with_contract(
            unity_project_path=proj,
            script_infos=infos,
            scene_runtime=scene_runtime,
            api_key="dummy",
            use_ai=True,
        )

        for ctx in captured.values():
            assert _PLAYER_CONTROLLER_DIRECTIVE not in ctx, (
                "ambiguous (>1 CC) must abstain — no directive anywhere."
            )

    def test_player_output_flows_into_dependent_scoped_context(
        self, tmp_path, monkeypatch,
    ):
        """AC7 fan-out MECHANISM: the player's transpiled Luau is inlined into
        the dependent HUD's ``scoped`` context. THIS is the channel that
        re-keys (and thus cold-re-transpiles) a downstream dependent when the
        directive shifts the player's output — distinct from the directive
        touching HUD's context (it does not; asserted above)."""
        proj, infos, scene_runtime = self._make_project(tmp_path)
        captured = self._capture(monkeypatch, self._PLAYER_LUAU)

        transpile_with_contract(
            unity_project_path=proj,
            script_infos=infos,
            scene_runtime=scene_runtime,
            api_key="dummy",
            use_ai=True,
        )

        # HUD depends on Player, so Player's already-transpiled Luau (sentinel)
        # is embedded in HUD's prompt context. If the player's OUTPUT changes
        # (e.g. because the directive re-transpiled it), HUD's context — hence
        # its cache key — changes too: the bounded level-by-level fan-out.
        assert "PLAYER_OUTPUT_SENTINEL" in captured["HUD"], (
            "the player's transpiled Luau must flow into the dependent's "
            "scoped context — the AC7 cache-fan-out channel."
        )
