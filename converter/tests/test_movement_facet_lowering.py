"""Pipeline-level player-binding SIGNAL fail-closeds (paradigm C identity).

Paradigm A (``movement_facet_lowering.py`` + the player path of
``camera_facet_lowering.py``) was deleted in Phase 5; paradigm C (the
deterministic host authority in ``scene_runtime.luau``, keyed on the upstream
``has_character_controller`` signal) now owns the player camera + WASD move.

What survives here is the SIGNAL-based fail-closed contract that keys on C's
own identity (the upstream ``has_character_controller`` module flag), NOT on any
A-locator fingerprint of the transpiled output. ``transpile_with_contract`` must
still surface ``player_signal_absent`` / ``player_ambiguous`` /
``player_unresolved`` on the respective inputs. ``player_unresolved`` is now
sourced from ``_player_controller_paths`` INTERSECTED with the emitted
``transpilation.scripts`` paths (P1-c), so it fires both on a stem mismatch AND
on a transpile-dropped player script.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.code_transpiler import (  # noqa: E402
    TranspilationResult,
    TranspiledScript,
)


def _row(stem: str, has_cc: bool) -> dict:
    return {
        "stem": stem,
        "class_name": stem,
        "runtime_bearing": True,
        "is_component_class": True,
        "character_attached": False,
        "is_loader": False,
        "has_character_controller": has_cc,
    }


# A minimal player-controller body. Its exact shape is irrelevant to the
# signal-based fail-closeds (those key on the upstream module flag + the
# stem<->script join, never on the transpiled output); it just has to be a
# plausible script body.
def _player_src() -> str:
    return (
        "local Player = {}\nPlayer.__index = Player\n\n"
        "function Player:Move(dt)\n"
        '\tlocal UIS = game:GetService("UserInputService")\n'
        "\tif UIS:IsKeyDown(Enum.KeyCode.W) then end\n"
        "end\n\nreturn Player\n"
    )


# --- Pipeline-invocation integration ---------------------------------------


class _PInfo:
    """Minimal ``ScriptInfo`` stand-in for ``transpile_with_contract``."""

    def __init__(self, path: Path, class_name: str) -> None:
        self.path = path
        self.class_name = class_name
        self.referenced_types: list[str] = []


class TestPipelineInvocation:
    """Drives the REAL ``contract_pipeline.transpile_with_contract`` so a future
    edit breaking the upstream-signal player-identity fail-closeds FAILS here."""

    def test_pipeline_surfaces_player_signal_absent(self) -> None:
        """A scene_runtime artifact that predates the upstream signal (no
        has_character_controller key on any module) surfaces player_signal_absent
        rather than silently skipping player binding."""
        from converter import contract_pipeline

        player_path = Path("/proj/Assets/Player.cs")
        infos = [_PInfo(player_path, "Player")]
        # Module row WITHOUT has_character_controller AND without
        # is_component_class (an artifact old enough to predate both) -- only
        # runtime_bearing (present since PR1) is left to trip the guard.
        stale_row = {
            "stem": "Player", "class_name": "Player", "runtime_bearing": True,
            "character_attached": False, "is_loader": False,
        }
        scene_runtime = {
            "modules": {"guid-player": stale_row},
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }
        ps = TranspiledScript(
            source_path=str(player_path), output_filename="Player.luau",
            csharp_source="", luau_source=_player_src(), strategy="ai",
            confidence=1.0, script_type="ModuleScript",
        )
        stub = TranspilationResult()
        stub.total_transpiled = 1
        stub.scripts.append(ps)
        with patch(
            "converter.contract_pipeline.transpile_scripts", return_value=stub,
        ):
            result = contract_pipeline.transpile_with_contract(
                "/proj", infos, scene_runtime=scene_runtime, use_ai=False,
            )
        kinds = {fc.kind for fc in result.fail_closed}
        assert "player_signal_absent" in kinds

    def test_pipeline_surfaces_player_ambiguous(self) -> None:
        """>1 CharacterController-bearing script -> a player_ambiguous row, and
        nothing bound."""
        from converter import contract_pipeline

        infos = [
            _PInfo(Path("/proj/Assets/PlayerA.cs"), "PlayerA"),
            _PInfo(Path("/proj/Assets/PlayerB.cs"), "PlayerB"),
        ]
        scene_runtime = {
            "modules": {
                "guid-a": _row("PlayerA", has_cc=True),
                "guid-b": _row("PlayerB", has_cc=True),
            },
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }
        scripts = []
        for stem in ("PlayerA", "PlayerB"):
            scripts.append(TranspiledScript(
                source_path=f"/proj/Assets/{stem}.cs",
                output_filename=f"{stem}.luau",
                csharp_source="",
                luau_source=_player_src().replace("Player", stem),
                strategy="ai", confidence=1.0, script_type="ModuleScript",
            ))
        stub = TranspilationResult()
        stub.total_transpiled = 2
        stub.scripts.extend(scripts)
        with patch(
            "converter.contract_pipeline.transpile_scripts", return_value=stub,
        ):
            result = contract_pipeline.transpile_with_contract(
                "/proj", infos, scene_runtime=scene_runtime, use_ai=False,
            )
        kinds = {fc.kind for fc in result.fail_closed}
        assert "player_ambiguous" in kinds

    def test_pipeline_surfaces_player_unresolved(self) -> None:
        """cc_module_count == 1 but the CC-flagged module's stem matches NO
        transpiled script (stem mismatch) -> the pipeline surfaces
        player_unresolved. This is a SIGNAL-based fail-closed (keyed on C's
        identity contract, not an A-locator fingerprint). Sub-case (a) of the
        P1-c re-source: ``player_controller_paths == ∅`` pre-transpile, so the
        emitted-path intersection is empty."""
        from converter import contract_pipeline

        # The script's source-path stem (the identity key) is NOT "Player".
        other_path = Path("/proj/Assets/SomethingElse.cs")
        infos = [_PInfo(other_path, "SomethingElse")]
        scene_runtime = {
            "modules": {"guid-player": _row("Player", has_cc=True)},
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }
        ps = TranspiledScript(
            source_path=str(other_path), output_filename="SomethingElse.luau",
            csharp_source="", luau_source=_player_src(), strategy="ai",
            confidence=1.0, script_type="ModuleScript",
        )
        stub = TranspilationResult()
        stub.total_transpiled = 1
        stub.scripts.append(ps)
        with patch(
            "converter.contract_pipeline.transpile_scripts", return_value=stub,
        ):
            result = contract_pipeline.transpile_with_contract(
                "/proj", infos, scene_runtime=scene_runtime, use_ai=False,
            )
        kinds = {fc.kind for fc in result.fail_closed}
        assert "player_unresolved" in kinds

    def test_pipeline_surfaces_player_unresolved_when_player_script_dropped(
        self,
    ) -> None:
        """P1-c sub-case (b): the CC-flagged module's stem DID match a script
        pre-transpile (``player_controller_paths`` is non-empty), but that
        script failed to read / was dropped at transpile and is ABSENT from
        ``transpilation.scripts``. The POST-transpile intersection is therefore
        empty -> player_unresolved still fires. This guards the re-source: the
        bare ``not player_controller_paths`` would NOT fire here (the path is
        still in the PRE-transpile ``script_infos``), regressing the deleted
        ``find_player_controllers`` POST-transpile fail-close."""
        from converter import contract_pipeline

        # The CC module's stem ("Player") MATCHES this info's path stem, so
        # ``_player_controller_paths`` (keyed on PRE-transpile script_infos) is
        # NON-empty. But the transpiled scripts list is EMPTY (the player .cs
        # was dropped at transpile), so the emitted-path intersection is empty.
        player_path = Path("/proj/Assets/Player.cs")
        infos = [_PInfo(player_path, "Player")]
        scene_runtime = {
            "modules": {"guid-player": _row("Player", has_cc=True)},
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }
        stub = TranspilationResult()
        stub.total_transpiled = 0  # the player script was dropped at transpile
        with patch(
            "converter.contract_pipeline.transpile_scripts", return_value=stub,
        ):
            result = contract_pipeline.transpile_with_contract(
                "/proj", infos, scene_runtime=scene_runtime, use_ai=False,
            )
        kinds = {fc.kind for fc in result.fail_closed}
        assert "player_unresolved" in kinds
