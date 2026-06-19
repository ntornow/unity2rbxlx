"""Tests for the gap #5 L1 spawn call-site lowering.

Drives the REAL transpiled TrackManager.luau shapes (segment / obstacle /
premium / cloud rewrites + the deferred consumable), idempotence, fail-closed
abstention, and the in-place script orchestration helper.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from converter.spawn_call_site_lowering import (
    SpawnRewriteResult,
    lower_spawn_call_sites,
    lower_spawn_call_sites_in_scripts,
)


# --- Real-shape fixtures (verbatim spans from the #210 diag TrackManager.luau) ---

SEGMENT_SRC = """\
function TrackManager:SpawnNewSegment()
    local zone = self.currentTheme.zones[self.currentZone + 1]
    local prefabCount = #zone.prefabList
    local segmentUse = math.random(0, prefabCount - 1)

    -- AssetReference.InstantiateAsync(offscreen pos): no Roblox addressable equivalent.
    -- UNCONVERTED: instantiate the segment prefab from its zone asset reference.
    local newSegment = nil
    if newSegment == nil then
        warn(string.format("Unable to load segment %d.", segmentUse))
        return
    end

    newSegment.manager = self
end
"""

OBSTACLE_SRC = """\
function TrackManager:SpawnFromAssetReference(reference, segment, posIndex)
    local _ = reference
    -- Addressables.LoadAssetAsync<GameObject>(reference): no Roblox equivalent.
    -- UNCONVERTED: load the obstacle prefab from its asset reference.
    local obj = nil
    if obj ~= nil then
        local obstacle = obj:GetComponent("Obstacle")
        if obstacle ~= nil then
            obstacle:Spawn(segment, segment.obstaclePositions[posIndex + 1])
        end
    end
end
"""

PREMIUM_SRC = """\
function TrackManager:SpawnCoinAndPowerup(segment)
    local toUse = nil
    if true then
        -- Addressables.InstantiateAsync(premiumCollectible name): UNCONVERTED.
        toUse = nil
        if toUse == nil then
            warn(string.format("Unable to load collectable %s.",
                tostring(self.currentTheme.premiumCollectible.name)))
            return
        end
        toUse.Parent = segment.gameObject
    end
end
"""

CONSUMABLE_SRC = """\
function TrackManager:SpawnCoinAndPowerup(segment)
    local toUse = nil
    if true then
        -- Addressables.InstantiateAsync(consumable name): UNCONVERTED (no addressable-by-name).
        toUse = nil
        if toUse == nil then
            warn(string.format("Unable to load consumable %s.",
                tostring(self.consumableDatabase.consumbales[picked].gameObject.Name)))
            return
        end
        toUse.Parent = segment.gameObject
    end
end
"""

CLOUD_SRC = """\
function TrackManager:Update(dt)
    if cloud ~= nil then
        -- Instantiate(cloud) under parallaxRoot. Theme cloud assets carry no prefab_id
        -- (asset-reference); degrade to a Clone of the source instance.
        local obj = cloud:Clone()
        obj.Parent = self.parallaxRoot

        local cmd = self.currentTheme.cloudMinimumDistance
    end
end
"""

# The character site — already lowered to a scene-find, NO sentinel. Must abstain.
CHARACTER_SRC = """\
function TrackManager:Begin()
    -- Addressables.InstantiateAsync(PlayerData character name) loads a prefab by string
    -- key; Roblox has no addressable-by-name equivalent. Degrade to finding the existing
    -- Character component in the scene.
    local player = self.host.findObjectOfType("Character")
    if player == nil then
        warn(string.format("Unable to load character %s.", "x"))
        return
    end
end
"""


def test_segment_rewrite() -> None:
    new, res = lower_spawn_call_sites(SEGMENT_SRC)
    assert res.rewritten == 1
    assert res.deferred == 0
    assert (
        "local newSegment = self.host.instantiatePrefab("
        "zone.prefabList[segmentUse + 1], self.gameObject, nil)" in new
    )
    assert "local newSegment = nil" not in new  # sentinel removed
    # The fail-soft guard is preserved (host.instantiatePrefab may return nil).
    assert "Unable to load segment" in new


def test_obstacle_inverted_rewrite_keeps_body() -> None:
    new, res = lower_spawn_call_sites(OBSTACLE_SRC)
    assert res.rewritten == 1
    assert (
        "local obj = self.host.instantiatePrefab(reference, segment.gameObject, nil)"
        in new
    )
    assert "local obj = nil" not in new
    # The inverted guard + body are preserved (they consume obj).
    assert "if obj ~= nil then" in new
    assert 'obj:GetComponent("Obstacle")' in new


def test_premium_rewrite_recovers_prefab_id_expr() -> None:
    new, res = lower_spawn_call_sites(PREMIUM_SRC)
    assert res.rewritten == 1
    assert (
        "toUse = self.host.instantiatePrefab("
        "self.currentTheme.premiumCollectible, segment.gameObject, nil)" in new
    )
    # bare ``toUse = nil`` sentinel removed (the leading ``local toUse = nil`` decl
    # at the method top is NOT the sentinel and is left intact).
    assert "toUse = nil\n        if toUse == nil then" not in new
    assert "Unable to load collectable" in new


def test_premium_rewrite_warn_parens_balanced() -> None:
    # Bug-guard: the InstantiateAsync rewrite re-emits the ORIGINAL warn(...) call by
    # splicing the captured ``warn`` group (which already closes tostring+format+warn).
    # An over-eager trailing paren in the replacement over-closes the call and produces
    # a Luau syntax error that fails the whole module to compile. Assert the warn line
    # the rewrite leaves behind is paren-balanced byte-for-byte vs the original.
    orig_warn_line = next(
        ln for ln in PREMIUM_SRC.splitlines() if "premiumCollectible.name" in ln
    )
    new, _ = lower_spawn_call_sites(PREMIUM_SRC)
    new_warn_line = next(
        ln for ln in new.splitlines() if "premiumCollectible.name" in ln
    )
    # The rewrite must not touch the warn-args line — it is preserved verbatim.
    assert new_warn_line == orig_warn_line
    assert new_warn_line.count("(") == orig_warn_line.count("(")
    assert new_warn_line.count(")") == orig_warn_line.count(")")
    # Whole-module paren balance is preserved (no stray opener/closer introduced).
    assert new.count("(") - new.count(")") == PREMIUM_SRC.count("(") - PREMIUM_SRC.count(")")


def test_cloud_clone_on_string_rewritten() -> None:
    new, res = lower_spawn_call_sites(CLOUD_SRC)
    assert res.rewritten == 1
    assert (
        "local obj = self.host.instantiatePrefab(cloud, self.parallaxRoot, nil)"
        in new
    )
    assert ":Clone()" not in new  # the broken clone-on-a-string is gone
    assert "obj.Parent = self.parallaxRoot" in new


def test_consumable_is_deferred_not_rewritten() -> None:
    new, res = lower_spawn_call_sites(CONSUMABLE_SRC)
    assert res.rewritten == 0
    assert res.deferred == 1
    # Fail-closed: the site is UNCHANGED (its loud warn-abort path is preserved).
    assert new == CONSUMABLE_SRC
    assert "instantiatePrefab" not in new


def test_character_site_excluded_no_sentinel() -> None:
    # Origin comment present but already lowered to findObjectOfType (no sentinel).
    new, res = lower_spawn_call_sites(CHARACTER_SRC)
    assert res.rewritten == 0
    assert res.deferred == 0
    assert new == CHARACTER_SRC


def test_idempotent_twice_call() -> None:
    for src in (SEGMENT_SRC, OBSTACLE_SRC, PREMIUM_SRC, CLOUD_SRC):
        once, _ = lower_spawn_call_sites(src)
        twice, res2 = lower_spawn_call_sites(once)
        assert once == twice, "second pass must be byte-stable"
        assert res2.rewritten == 0, "second pass finds no sentinel"


def test_segment_fail_closed_without_zone_local() -> None:
    # Origin comment + sentinel present, but the zone/index locals are absent —
    # fail-closed (no rewrite), never a guessed expr.
    broken = """\
function TrackManager:SpawnNewSegment()
    -- AssetReference.InstantiateAsync(offscreen pos): no Roblox addressable equivalent.
    local newSegment = nil
    if newSegment == nil then
        warn(string.format("Unable to load segment %d.", segmentUse))
        return
    end
end
"""
    new, res = lower_spawn_call_sites(broken)
    assert res.rewritten == 0
    assert new == broken  # untouched


def test_abstain_when_no_origin_comment() -> None:
    # A segment-shaped sentinel WITHOUT the deterministic origin comment must NOT
    # be rewritten (the comment is the load-bearing identity gate).
    no_comment = """\
function TrackManager:SpawnNewSegment()
    local zone = self.currentTheme.zones[self.currentZone + 1]
    local segmentUse = math.random(0, prefabCount - 1)
    local newSegment = nil
    if newSegment == nil then
        warn(string.format("Unable to load segment %d.", segmentUse))
        return
    end
end
"""
    new, res = lower_spawn_call_sites(no_comment)
    assert res.rewritten == 0
    assert new == no_comment


def test_all_five_shapes_in_one_module() -> None:
    # The real TrackManager.luau carries all 5 sites; combine them and assert the
    # 4 active rewrites + 1 deferral fire together with no cross-shape interference.
    combined = SEGMENT_SRC + OBSTACLE_SRC + PREMIUM_SRC + CONSUMABLE_SRC + CLOUD_SRC
    new, res = lower_spawn_call_sites(combined)
    assert res.rewritten == 4
    assert res.deferred == 1
    assert new.count("instantiatePrefab") == 4
    # Idempotent on the combined module too.
    again, res2 = lower_spawn_call_sites(new)
    assert again == new
    assert res2.rewritten == 0


# --- Real-output fixtures, vendored so the suite is hermetic (no env-dependent
# skip). Captured verbatim from the #210 diag conversion; the live-path behaviour is
# re-confirmed against the actual files by ``test_real_diag_outputs_when_present``. ---

# Pooler.luau (real diag output): a GENERIC object pooler. ``local obj =
# original:Clone(); obj.Parent = Workspace`` with NO ``Instantiate(`` origin comment.
# Must ABSTAIN — rewriting it to instantiatePrefab(original, …) corrupts it (P1-1).
REAL_POOLER_SRC = """\
local function instantiate(original)
\tlocal obj = original:Clone()
\tobj.Parent = Workspace
\treturn obj
end
"""

# A generic clone with a NEARBY comment that mentions Instantiate( but is NOT the
# attached origin block (a code line intervenes) — must still abstain (codex tweak:
# only a comment block ATTACHED to the clone gates the rewrite).
DETACHED_COMMENT_CLONE_SRC = """\
function Foo:bar()
    -- Instantiate(thing) somewhere else entirely.
    local x = computeSomething()
    local obj = template:Clone()
    obj.Parent = Workspace
end
"""

CLOUD_NO_COMMENT_SRC = """\
function TrackManager:Update(dt)
    if cloud ~= nil then
        local obj = cloud:Clone()
        obj.Parent = self.parallaxRoot
    end
end
"""


def test_cloud_abstains_without_instantiate_origin_comment() -> None:
    # P1-1 regression: a cloud-shaped clone WITHOUT an attached ``Instantiate(``
    # origin comment must NOT be rewritten (the comment is the identity gate).
    new, res = lower_spawn_call_sites(CLOUD_NO_COMMENT_SRC)
    assert res.rewritten == 0
    assert res.deferred == 0
    assert new == CLOUD_NO_COMMENT_SRC
    assert "instantiatePrefab" not in new


def test_real_pooler_shape_is_not_rewritten() -> None:
    # P1-1 regression on the REAL Pooler.luau shape: a generic pooler clone with no
    # origin comment must be left byte-identical (no wrong-region rewrite).
    new, res = lower_spawn_call_sites(REAL_POOLER_SRC)
    assert res.rewritten == 0
    assert res.deferred == 0
    assert new == REAL_POOLER_SRC
    assert "instantiatePrefab" not in new


def test_clone_with_detached_instantiate_comment_abstains() -> None:
    # The ``Instantiate(`` mention is in a comment separated from the clone by a code
    # line — the attached-block rule rejects it (no leak-down false positive).
    new, res = lower_spawn_call_sites(DETACHED_COMMENT_CLONE_SRC)
    assert res.rewritten == 0
    assert new == DETACHED_COMMENT_CLONE_SRC


def test_obstacle_recovers_param_from_func_signature() -> None:
    # P1-2 generality: the obstacle prefab-id is recovered structurally even when the
    # ``local _ = <param>`` discard is absent — fall back to the function's first
    # parameter, NOT a game-specific method name.
    src = """\
function SomeManager:loadThing(myRef, segment, posIndex)
    -- Addressables.LoadAssetAsync<GameObject>(reference): no Roblox equivalent.
    local obj = nil
    if obj ~= nil then
        obj:doStuff()
    end
end
"""
    new, res = lower_spawn_call_sites(src)
    assert res.rewritten == 1
    assert "local obj = self.host.instantiatePrefab(myRef, segment.gameObject, nil)" in new


def test_obstacle_origin_without_recoverable_param_logs_abstain(caplog) -> None:
    # AC6 uniform fail-soft: obstacle origin present but neither a discard line nor a
    # function signature is in scope to recover the prefab-id → LOG + abstain.
    src = """\
-- Addressables.LoadAssetAsync<GameObject>(reference): no Roblox equivalent.
local obj = nil
if obj ~= nil then
    obj:doStuff()
end
"""
    with caplog.at_level("WARNING"):
        new, res = lower_spawn_call_sites(src)
    assert res.rewritten == 0
    assert new == src
    assert any("obstacle origin present" in r.message for r in caplog.records)


def test_instantiate_async_origin_without_shape_logs_abstain(caplog) -> None:
    # AC6 uniform fail-soft: InstantiateAsync origin present but no <v>=nil+warn-abort
    # shape located (transpiler drift) → LOG + abstain.
    src = """\
function M:f()
    -- Addressables.InstantiateAsync(thing name): UNCONVERTED.
    doSomethingElse()
end
"""
    with caplog.at_level("WARNING"):
        new, res = lower_spawn_call_sites(src)
    assert res.rewritten == 0
    assert res.deferred == 0
    assert new == src
    assert any(
        "Addressables.InstantiateAsync origin present" in r.message
        for r in caplog.records
    )


def test_pooler_clone_does_not_log_spurious_cloud_abstain(caplog) -> None:
    # The generic Pooler clone has NO ``Instantiate(`` origin comment, so the cloud
    # fail-soft must stay SILENT (no spurious drift warning).
    with caplog.at_level("WARNING"):
        lower_spawn_call_sites(REAL_POOLER_SRC)
    assert not any("Instantiate(" in r.message for r in caplog.records)


def test_obstacle_dot_form_self_param_does_not_misbind() -> None:
    # Hardening: a dot-form method listing an explicit ``self`` first param (and NO
    # ``local _ =`` discard) must NOT bind ``self`` as the prefab-id — fail-closed.
    src = """\
function TrackManager.spawn(self, reference)
    -- Addressables.LoadAssetAsync<GameObject>(reference): no Roblox equivalent.
    local obj = nil
    if obj ~= nil then
        obj:doStuff()
    end
end
"""
    new, res = lower_spawn_call_sites(src)
    assert res.rewritten == 0  # self is not a prefab-id; abstain
    assert "instantiatePrefab(self," not in new
    assert new == src


def test_real_diag_outputs_when_present() -> None:
    """Re-confirm the vendored fixtures against the REAL diag files when present.

    NOT a silent skip-if-absent: when the diag tree exists we assert the live
    behaviour (4 rewrites + 1 deferral on TrackManager; Pooler untouched). When it is
    absent (CI / a fresh clone) the hermetic vendored fixtures above carry the
    coverage, so this is a best-effort cross-check, explicitly marked.
    """
    import os

    base = (
        "/Users/jiazou/.claude/harness-runs/trash-dash-phase2-20260618T102928/"
        "wt/diag/converter/output/trash-dash-phase2-diag/scripts/"
    )
    tm_path = base + "TrackManager.luau"
    pooler_path = base + "Pooler.luau"
    if not (os.path.exists(tm_path) and os.path.exists(pooler_path)):
        pytest.skip(
            "diag tree absent in this environment — vendored real-shape fixtures "
            "(REAL_POOLER_SRC, the combined-module test) carry the coverage."
        )
    tm = open(tm_path, encoding="utf-8").read()
    new, res = lower_spawn_call_sites(tm)
    assert res.rewritten == 4, "segment/obstacle/premium/cloud must all rewrite"
    assert res.deferred == 1, "consumable must defer"
    assert ":Clone()" not in new  # cloud clone-on-string fixed
    assert "local newSegment = nil" not in new
    assert new.count("instantiatePrefab") == 4
    # The premium expr is recovered structurally (not by the C# arg name).
    assert "instantiatePrefab(self.currentTheme.premiumCollectible," in new
    # The consumable site is deferred (its loud warn-abort path is preserved).
    assert "Unable to load consumable" in new
    assert "instantiatePrefab(self.consumableDatabase" not in new
    again, res2 = lower_spawn_call_sites(new)
    assert again == new
    assert res2.rewritten == 0
    # Pooler: a generic clone with no origin comment is left untouched.
    pooler = open(pooler_path, encoding="utf-8").read()
    pnew, pres = lower_spawn_call_sites(pooler)
    assert pres.rewritten == 0
    assert pnew == pooler


@dataclass
class _FakeScript:
    source: str


def test_in_place_orchestration_helper() -> None:
    s1 = _FakeScript(source=SEGMENT_SRC)
    s2 = _FakeScript(source=OBSTACLE_SRC)
    s3 = _FakeScript(source="-- a module with no spawn sites\nreturn {}\n")
    result = lower_spawn_call_sites_in_scripts([s1, s2, s3])
    assert isinstance(result, SpawnRewriteResult)
    assert result.rewritten == 2
    assert "instantiatePrefab" in s1.source
    assert "instantiatePrefab" in s2.source
    assert s3.source == "-- a module with no spawn sites\nreturn {}\n"  # untouched


def test_in_place_helper_skips_non_string_source() -> None:
    @dataclass
    class _Bad:
        source: object

    bad = _Bad(source=None)
    result = lower_spawn_call_sites_in_scripts([bad])  # type: ignore[list-item]
    assert result.rewritten == 0
    assert result.deferred == 0
