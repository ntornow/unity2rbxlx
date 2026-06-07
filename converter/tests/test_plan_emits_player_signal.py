"""AC11 — the upstream player signal survives plan emission (regression guard).

Phase 2's player-embodiment authority keys EXCLUSIVELY on the deterministic
upstream module signal ``has_character_controller`` (a script co-located with a
Unity ``CharacterController`` on a placed GameObject). The runtime reads it off
``self._plan.modules[id].has_character_controller`` after the plan is embedded
into the place as the ``SceneRuntimePlan`` ModuleScript.

That embedding goes through ``generate_scene_runtime_plan_module``
(``autogen.py``), which selects ``_PLAN_KEYS_FOR_HOST`` (``"modules"`` among
them) and renders the WHOLE module dict via the recursive ``_plan_to_luau``
encoder. Divergence #1 of the Phase 2 design: the signal already reaches the
runtime — no NEW plan-emission code is required. This module is the
REGRESSION GUARD that a future encoder narrowing (e.g. a per-module field
whitelist mirroring ``_PLAN_KEYS_FOR_HOST``) cannot silently drop the one
field the whole authority binds on.

It drives the REAL planner (``plan_scene_runtime``) to stamp the signal — NOT a
hand-built module dict — then the REAL emitter, and asserts the emitted Luau
carries ``has_character_controller`` end-to-end. Pure-Python; runs without a
luau interpreter.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.unity_types import (
    ComponentData,
    GuidEntry,
    GuidIndex,
    ParsedScene,
    SceneNode,
)
from converter.scene_runtime_planner import plan_scene_runtime
from converter.autogen import generate_scene_runtime_plan_module


# ---------------------------------------------------------------------------
# Fixture builders (mirror test_scene_runtime_planner.py's idiom so the planner
# stamps the signal exactly as production does).
# ---------------------------------------------------------------------------

def _make_guid_index(
    project_root: Path, entries: dict[str, tuple[Path, str]],
) -> GuidIndex:
    """``entries: {guid: (absolute_asset_path, kind)}`` -> in-memory GuidIndex
    of the shape ``build_guid_index`` produces."""
    idx = GuidIndex(project_root=project_root)
    for guid, (asset_path, kind) in entries.items():
        try:
            relative_path = asset_path.relative_to(project_root)
        except ValueError:
            relative_path = asset_path
        idx.guid_to_entry[guid] = GuidEntry(
            guid=guid,
            asset_path=asset_path,
            relative_path=relative_path,
            kind=kind,  # type: ignore[arg-type]
        )
        idx.path_to_guid[asset_path.resolve()] = guid
    return idx


def _mb_props(script_guid: str, *, go_fid: str) -> dict[str, object]:
    """A MonoBehaviour ``properties`` dict — the shape scene_parser drops into
    ``ComponentData.properties``."""
    return {
        "m_Script": {"fileID": "11500000", "guid": script_guid, "type": 3},
        "m_GameObject": {"fileID": go_fid},
        "m_Enabled": 1,
    }


def _node(
    file_id: str, name: str, components: list[ComponentData],
) -> SceneNode:
    n = SceneNode(name=name, file_id=file_id, active=True, layer=0, tag="")
    n.components = components
    n.children = []
    return n


def _cc() -> ComponentData:
    """A Unity CharacterController component (the engine-level avatar signal)."""
    return ComponentData(
        component_type="CharacterController", file_id="900", properties={},
    )


def _planned_artifact(tmp_path: Path) -> tuple[dict, str, str]:
    """Drive the REAL planner over a scene with ONE CC-bearing player script and
    ONE plain enemy script. Returns ``(artifact, player_guid, enemy_guid)``.

    The signal is stamped by the production planner — never hand-seeded — so the
    emitter test exercises the same module-dict shape a real conversion produces.
    """
    sdir = tmp_path / "Assets" / "Scripts"
    sdir.mkdir(parents=True)
    player_cs = sdir / "Player.cs"
    player_cs.write_text("public class Player : MonoBehaviour { }")
    enemy_cs = sdir / "Enemy.cs"
    enemy_cs.write_text("public class Enemy : MonoBehaviour { }")
    player_guid, enemy_guid = "a" * 32, "b" * 32
    idx = _make_guid_index(tmp_path, {
        player_guid: (player_cs, "script"),
        enemy_guid: (enemy_cs, "script"),
    })

    player_node = _node("100", "Player", [
        _cc(),
        ComponentData(
            component_type="MonoBehaviour", file_id="200",
            properties=_mb_props(player_guid, go_fid="100"),
        ),
    ])
    enemy_node = _node("300", "Enemy", [
        ComponentData(
            component_type="MonoBehaviour", file_id="400",
            properties=_mb_props(enemy_guid, go_fid="300"),
        ),
    ])
    scene = ParsedScene(scene_path=tmp_path / "Assets" / "Scenes" / "Main.unity")
    scene.roots = [player_node, enemy_node]
    scene.all_nodes = {"100": player_node, "300": enemy_node}

    artifact = plan_scene_runtime(
        parsed_scenes=[scene], prefab_library=None,
        guid_index=idx, unity_project_root=tmp_path,
    )
    return artifact, player_guid, enemy_guid


def _module_block(source: str, script_guid: str) -> str:
    """Slice out the emitted Luau block for one module row, keyed by its guid.

    The encoder renders a module key either BARE (``<guid> = {`` — a guid that
    is a valid Luau identifier) or BRACKET-QUOTED (``["<guid>"] = {`` — e.g. a
    guid starting with a digit). Try both, then return the body between that
    key's opening ``{`` and its matching ``}`` so a field assertion is scoped to
    THAT module — not anywhere in the source.
    """
    for key in (f"{script_guid} = {{", f'["{script_guid}"] = {{'):
        pos = source.find(key)
        if pos != -1:
            break
    else:
        raise AssertionError(f"module key for {script_guid!r} not found in emitted plan")
    start = pos + len(key)
    depth = 1
    i = start
    while depth > 0:
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return source[start:i - 1]


class TestPlanEmissionPreservesPlayerSignal:
    """AC11: ``has_character_controller`` survives ``plan_scene_runtime`` ->
    ``generate_scene_runtime_plan_module`` end-to-end."""

    def test_cc_bearing_module_emits_signal_true(self, tmp_path: Path) -> None:
        artifact, player_guid, _ = _planned_artifact(tmp_path)
        # Precondition: the REAL planner stamped the signal (not the emitter).
        # If this regressed, the guard below would pass vacuously, so pin it.
        assert artifact["modules"][player_guid]["has_character_controller"] is True

        script = generate_scene_runtime_plan_module(artifact)
        player_block = _module_block(script.source, player_guid)
        # The field survives encoding, scoped to the player's own module row.
        assert "has_character_controller = true" in player_block

    def test_non_player_module_emits_signal_false(self, tmp_path: Path) -> None:
        artifact, _, enemy_guid = _planned_artifact(tmp_path)
        assert artifact["modules"][enemy_guid]["has_character_controller"] is False

        script = generate_scene_runtime_plan_module(artifact)
        enemy_block = _module_block(script.source, enemy_guid)
        assert "has_character_controller = false" in enemy_block

    def test_signal_is_present_for_every_module_row(self, tmp_path: Path) -> None:
        """Stronger guard: EVERY emitted module row carries the key. A whitelist
        that dropped it would emit module rows WITHOUT the field — caught here
        even if some row's value happened to match a stray match elsewhere."""
        artifact, _, _ = _planned_artifact(tmp_path)
        script = generate_scene_runtime_plan_module(artifact)

        module_count = len(artifact["modules"])
        assert module_count >= 2  # player + enemy
        emitted_signal_rows = len(
            re.findall(r"has_character_controller = (?:true|false)", script.source)
        )
        # One emitted ``has_character_controller`` per module row — the field is
        # not narrowed out of the encoded plan.
        assert emitted_signal_rows == module_count
