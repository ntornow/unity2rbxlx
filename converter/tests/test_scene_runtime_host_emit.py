"""PR4: scene-runtime host emit + plan encoder + cross-domain report.

Python-side coverage for PR4's conversion-time integration:

  * ``generate_scene_runtime_plan_module`` encodes the planner artifact
    as a Luau-literal return that parses.
  * ``generate_scene_runtime_client_entrypoint`` /
    ``generate_scene_runtime_server_entrypoint`` produce shippable
    LocalScript / Script with the right parent paths.
  * ``compute_cross_domain_edges`` enumerates client<->server peer-
    component references; intra-domain refs do NOT appear.
  * ``render_cross_domain_report`` produces deterministic markdown.

Luau-harness behavioral coverage for the host runtime lives in
``test_scene_runtime_host_behavior.py`` (driven via the standalone
``luau`` interpreter when present).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.autogen import (  # noqa: E402
    _plan_to_luau,
    generate_scene_runtime_client_entrypoint,
    generate_scene_runtime_plan_module,
    generate_scene_runtime_server_entrypoint,
    render_cross_domain_report,
)
from converter.scene_runtime_domain import (  # noqa: E402
    compute_cross_domain_edges,
)


def _luau_available() -> bool:
    return shutil.which("luau") is not None


# ---------------------------------------------------------------------------
# _plan_to_luau encoder
# ---------------------------------------------------------------------------

class TestPlanToLuauEncoder:

    def test_primitives(self):
        assert _plan_to_luau(True) == "true"
        assert _plan_to_luau(False) == "false"
        assert _plan_to_luau(None) == "nil"
        assert _plan_to_luau(42) == "42"
        assert _plan_to_luau("hi") == "\"hi\""

    def test_string_escapes_double_quotes_and_newlines(self):
        out = _plan_to_luau("a\"b\nc")
        assert out == "\"a\\\"b\\nc\""

    def test_empty_collections(self):
        assert _plan_to_luau({}) == "{}"
        assert _plan_to_luau([]) == "{}"

    def test_dict_with_identifier_keys_unquoted(self):
        out = _plan_to_luau({"stem": "Foo", "domain": "client"})
        assert "stem = \"Foo\"" in out
        assert "domain = \"client\"" in out

    def test_dict_with_non_identifier_key_bracket_syntax(self):
        # Keys that contain ``:`` (e.g. scene_runtime instance ids
        # like ``"Scenes/A.unity:42"``) need bracket syntax.
        out = _plan_to_luau({"Scenes/A.unity:42": "x"})
        assert "[\"Scenes/A.unity:42\"] = \"x\"" in out

    def test_reserved_keyword_keys_bracket_syntax(self):
        # ``end`` is a Luau keyword; using it as a bare key would
        # parse as the statement terminator.
        out = _plan_to_luau({"end": 1, "function": 2})
        assert "[\"end\"]" in out
        assert "[\"function\"]" in out

    def test_unicode_identifier_keys_use_bracket_syntax(self):
        """R4-P2.2: Python's ``str.isidentifier()`` returns True for
        PEP 3131 Unicode identifiers (``"变量".isidentifier() == True``)
        but Luau's lexer rejects non-ASCII bare identifiers. The
        encoder must bracket-quote such keys so the embedded
        ``SceneRuntimePlan`` ModuleScript parses on the live host.
        """
        out = _plan_to_luau({"变量": 1, "naïve": 2, "ascii_ok": 3})
        # Non-ASCII keys must be bracket-quoted (string-keyed entry).
        assert "[\"变量\"]" in out, out
        assert "[\"naïve\"]" in out, out
        # An ASCII identifier still uses bare-key form.
        assert "ascii_ok = 3" in out, out
        # The bare-key form for the Unicode keys must NOT appear.
        assert "变量 = " not in out, out
        assert "naïve = " not in out, out

    @pytest.mark.skipif(not _luau_available(),
                        reason="luau interpreter not installed")
    def test_unicode_key_emit_parses_with_luau(self):
        """R4-P2.2: the encoded plan with Unicode keys must round-trip
        through standalone luau (the bracket-quoted form is required for
        the lexer to accept it)."""
        encoded = "return " + _plan_to_luau({"变量": "v", "naïve": 7})
        wrapper = (f"local p = (function() {encoded} end)(); "
                   "assert(type(p) == 'table'); "
                   "assert(p[\"变量\"] == 'v'); "
                   "assert(p[\"naïve\"] == 7); "
                   "print('ok')")
        with tempfile.NamedTemporaryFile(
            suffix=".luau", mode="w", delete=False,
        ) as fw:
            fw.write(wrapper)
            wpath = fw.name
        try:
            result = subprocess.run(
                ["luau", wpath], capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, (
                f"luau parse failed: stderr={result.stderr!r}, "
                f"stdout={result.stdout!r}"
            )
            assert "ok" in result.stdout
        finally:
            Path(wpath).unlink(missing_ok=True)

    def test_nested_dict_and_list(self):
        plan = {
            "modules": {
                "guid-1": {
                    "stem": "Foo",
                    "runtime_bearing": True,
                },
            },
            "scenes": {
                "Scenes/A.unity": {
                    "instances": [
                        {"instance_id": "Scenes/A.unity:1", "active": True},
                    ],
                    "lifecycle_order": ["Scenes/A.unity:1"],
                },
            },
        }
        out = _plan_to_luau(plan)
        # The presence of all the leaves is the test; parse correctness
        # is asserted below in TestPlanLuauParseable.
        for needle in ("modules", "Foo", "runtime_bearing = true",
                       "Scenes/A.unity:1", "lifecycle_order"):
            assert needle in out

    @pytest.mark.skipif(not _luau_available(),
                        reason="luau interpreter not installed")
    def test_encoded_plan_parses_with_luau(self):
        plan = {
            "modules": {
                "g1": {"stem": "Foo", "runtime_bearing": True,
                       "domain": "client", "module_path": "ReplicatedStorage.Foo"},
                "g2": {"stem": "Bar", "runtime_bearing": False},
            },
            "scenes": {
                "A.unity": {
                    "instances": [{
                        "instance_id": "A.unity:1",
                        "script_id": "g1",
                        "game_object_id": "A.unity:99",
                        "active": True, "enabled": True,
                        "config": {"speed": 5.0},
                    }],
                    "references": [{
                        "from": "A.unity:1",
                        "field": "target",
                        "index": None,
                        "target_kind": "asset",
                        "target_ref": "rbxassetid://123",
                        "target_is_ui": False,
                    }],
                    "lifecycle_order": ["A.unity:1"],
                },
            },
            "prefabs": {},
            "domain_overrides": {},
        }
        encoded = "return " + _plan_to_luau(plan)
        with tempfile.NamedTemporaryFile(
            suffix=".luau", mode="w", delete=False,
        ) as f:
            f.write(encoded)
            path = f.name
        try:
            # Wrap with a print so luau treats the file as runnable.
            wrapper = (f"local p = (function() {encoded} end)(); "
                       "assert(type(p) == 'table'); "
                       "assert(p.modules.g1.stem == 'Foo'); "
                       "assert(p.scenes['A.unity'].lifecycle_order[1] == 'A.unity:1'); "
                       "print('ok')")
            with tempfile.NamedTemporaryFile(
                suffix=".luau", mode="w", delete=False,
            ) as fw:
                fw.write(wrapper)
                wpath = fw.name
            result = subprocess.run(
                ["luau", wpath], capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, (
                f"luau exec failed: stderr={result.stderr!r}, "
                f"stdout={result.stdout!r}"
            )
            assert "ok" in result.stdout
        finally:
            Path(path).unlink(missing_ok=True)
            try:
                Path(wpath).unlink(missing_ok=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entrypoint generators
# ---------------------------------------------------------------------------

class TestEntrypointGenerators:

    def test_client_entrypoint_parent_path_is_starter_player_scripts(self):
        script = generate_scene_runtime_client_entrypoint()
        assert script.name == "SceneRuntimeClient"
        assert script.script_type == "LocalScript"
        assert script.parent_path == "StarterPlayer.StarterPlayerScripts"
        # Sanity-check the source mentions the engine + plan.
        assert "SceneRuntime" in script.source
        assert "SceneRuntimePlan" in script.source
        assert "start(\"client\")" in script.source

    def test_server_entrypoint_parent_path_is_server_script_service(self):
        script = generate_scene_runtime_server_entrypoint()
        assert script.name == "SceneRuntimeServer"
        assert script.script_type == "Script"
        assert script.parent_path == "ServerScriptService"
        assert "start(\"server\")" in script.source

    def test_plan_module_parent_path_replicated_storage(self):
        script = generate_scene_runtime_plan_module({
            "modules": {}, "scenes": {}, "prefabs": {},
        })
        assert script.name == "SceneRuntimePlan"
        assert script.script_type == "ModuleScript"
        assert script.parent_path == "ReplicatedStorage"
        # Even an empty plan emits ``return {...}`` so the
        # ``require`` doesn't fail with a non-table return value.
        assert script.source.strip().startswith("--")
        assert "return" in script.source

    def test_plan_module_embeds_scene_prefab_placements(self):
        """Bug B tier-1 regression: ``scene_prefab_placements`` must
        survive the ``_PLAN_KEYS_FOR_HOST`` allowlist filter and land in
        the embedded plan ModuleScript.

        The on-disk ``conversion_plan.json`` had 252 placements but the
        allowlist dropped the key, so the embedded plan carried zero rows
        and the runtime boot loop iterated nothing. This asserts the
        placements (id + prefab_id) reach the emitted Luau source.
        """
        placements = [
            {
                "placement_id": "main:1001",
                "prefab_id": "Crate__abc123",
                "active": True,
                "enabled": True,
            },
            {
                "placement_id": "main:1002",
                "prefab_id": "Barrel__def456",
                "active": True,
                "enabled": False,
            },
        ]
        script = generate_scene_runtime_plan_module({
            "modules": {}, "scenes": {}, "prefabs": {},
            "scene_prefab_placements": placements,
        })
        src = script.source
        assert "scene_prefab_placements" in src, (
            "embedded plan must carry the scene_prefab_placements key; "
            "if missing, the _PLAN_KEYS_FOR_HOST allowlist dropped it"
        )
        # Each placement's identifying fields must appear in the emitted
        # Luau literal so the host boot loop has rows to iterate.
        for row in placements:
            assert row["placement_id"] in src, (
                f"placement_id {row['placement_id']!r} missing from "
                f"embedded plan source"
            )
            assert row["prefab_id"] in src, (
                f"prefab_id {row['prefab_id']!r} missing from embedded "
                f"plan source"
            )

    def test_entrypoints_split_module_path_on_dot_not_slash(self):
        """R2-P1.1: ``module_path`` is the dotted DataModel path. Both
        client and server entrypoints must split on ``"."`` so they can
        walk ``game:FindFirstChild(...)`` down the chain. Pre-fix the
        entrypoints used ``string.split(modulePath, "/")`` which silently
        accepted the planner's old ``"scripts/Foo.luau"`` shape but
        couldn't actually resolve a ModuleScript in production.
        """
        for gen in (
            generate_scene_runtime_client_entrypoint,
            generate_scene_runtime_server_entrypoint,
        ):
            src = gen().source
            assert 'string.split(modulePath, ".")' in src, (
                f"{gen.__name__} must split modulePath on '.'; source: {src}"
            )
            assert 'string.split(modulePath, "/")' not in src, (
                f"{gen.__name__} must NOT split modulePath on '/'; that "
                f"shape was the pre-R2 on-disk path the host couldn't "
                f"resolve in production"
            )

    def test_entrypoints_resolve_prefab_via_template_name_map(self):
        """R2-P1.2: prefab templates live under
        ``ReplicatedStorage.Templates`` keyed by bare prefab name.
        Entrypoints must look up the bare name via
        ``plan.prefabs[prefab_id].template_name`` rather than feeding
        the stable ``prefab_id`` (which includes a GUID + path) directly
        to ``Templates:FindFirstChild(...)``.
        """
        for gen in (
            generate_scene_runtime_client_entrypoint,
            generate_scene_runtime_server_entrypoint,
        ):
            src = gen().source
            assert "Templates" in src, (
                f"{gen.__name__} must read from ReplicatedStorage.Templates"
            )
            assert "ScenePrefabs" not in src, (
                f"{gen.__name__} must NOT reference the legacy "
                f"ScenePrefabs folder (typo from PR4 round 1)"
            )
            assert "template_name" in src, (
                f"{gen.__name__} must resolve via the plan's "
                f"template_name map (R2-P1.2)"
            )

    @pytest.mark.skipif(not _luau_available(),
                        reason="luau interpreter not installed")
    def test_resolve_template_discriminates_colliding_character_prefabs(self):
        """AC4 (the spike's central proof). Place BOTH ``character__473ffa``
        (Cat) and ``character__2ae64d`` (Raccoon) as children under a
        ``Templates`` folder, drive the REAL ``_resolveTemplate`` literal that
        ``autogen`` emits into the entrypoints, and prove it resolves the Cat
        prefab_id → the Cat child and the Raccoon prefab_id → the Raccoon child.
        Unique resolved keys BEAT the bare-name collision (NOT first-wins).

        Runs the actual emitted Luau (extracted from the generated client
        entrypoint), not a reimplementation, so a regression in the real
        ``Plan.prefabs[id].template_name → Templates:FindFirstChild`` bridge is
        caught. MUST FAIL if both children shared the bare colliding name
        ``character`` (proving this is a real guard, not a tautology)."""
        src = generate_scene_runtime_client_entrypoint().source
        body = _extract_lua_function(src, "_resolveTemplate")

        cat_id = "473ffa01abcd:Assets/Bundles/Characters/Cat/character.prefab"
        rac_id = "2ae64d0eefab:Assets/Bundles/Characters/Raccoon/character.prefab"

        # A mock Roblox surface: ReplicatedStorage.Templates holds two children
        # named by the RESOLVED (suffixed) names; Plan maps each prefab_id to
        # its resolved template_name (single source of truth, what the planner
        # stores). Each Templates child carries a marker so we can assert which
        # one came back.
        harness = textwrap.dedent(f"""\
            local function makeChild(marker)
                local c = {{ _marker = marker }}
                return c
            end
            local templatesChildren = {{
                ["character__473ffa"] = makeChild("CAT"),
                ["character__2ae64d"] = makeChild("RACCOON"),
            }}
            local Templates = {{}}
            function Templates:FindFirstChild(name)
                return templatesChildren[name]
            end
            local RS = {{}}
            function RS:FindFirstChild(name)
                if name == "Templates" then return Templates end
                return nil
            end
            local Plan = {{
                prefabs = {{
                    ["{cat_id}"] = {{ template_name = "character__473ffa" }},
                    ["{rac_id}"] = {{ template_name = "character__2ae64d" }},
                }},
            }}

            {body}

            local cat = _resolveTemplate("{cat_id}")
            local rac = _resolveTemplate("{rac_id}")
            assert(cat ~= nil, "cat template resolved to nil")
            assert(rac ~= nil, "raccoon template resolved to nil")
            assert(cat._marker == "CAT",
                "catId resolved to " .. tostring(cat._marker) .. " not CAT")
            assert(rac._marker == "RACCOON",
                "raccoonId resolved to " .. tostring(rac._marker) .. " not RACCOON")
            assert(cat ~= rac, "colliding ids resolved to the SAME child")
            print("ok")
        """)
        with tempfile.NamedTemporaryFile(
            suffix=".luau", mode="w", delete=False,
        ) as f:
            f.write(harness)
            wpath = f.name
        try:
            result = subprocess.run(
                ["luau", wpath], capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, (
                f"luau exec failed: stderr={result.stderr!r}, "
                f"stdout={result.stdout!r}"
            )
            assert "ok" in result.stdout
        finally:
            Path(wpath).unlink(missing_ok=True)


def _extract_lua_function(source: str, name: str) -> str:
    """Slice the real ``local function <name>(...) ... end`` block out of an
    emitted Luau source string by balanced block nesting, so the test drives
    the ACTUAL emitted literal rather than a reimplementation. Lua blocks open
    on ``function``/``do``/``then`` (the closing keyword of an ``if``) and close
    on ``end``; ``elseif``/``else`` neither open nor close."""
    import re as _re
    marker = f"local function {name}("
    start = source.index(marker)
    depth = 0
    for m in _re.finditer(r"\b(function|do|then|end)\b", source[start:]):
        if m.group(1) in ("function", "do", "then"):
            depth += 1
        else:  # end
            depth -= 1
            if depth == 0:
                return source[start:start + m.end()]
    raise AssertionError(f"could not extract function {name!r}")


# ---------------------------------------------------------------------------
# compute_cross_domain_edges
# ---------------------------------------------------------------------------

def _make_artifact_with_edge(
    src_domain: str, tgt_domain: str,
) -> dict:
    return {
        "modules": {
            "src": {"stem": "Src", "runtime_bearing": True,
                    "domain": src_domain, "module_path": "ReplicatedStorage.Src"},
            "tgt": {"stem": "Tgt", "runtime_bearing": True,
                    "domain": tgt_domain, "module_path": "ReplicatedStorage.Tgt"},
        },
        "scenes": {
            "A.unity": {
                "instances": [
                    {"instance_id": "A.unity:1", "script_id": "src",
                     "game_object_id": "A.unity:1", "active": True,
                     "enabled": True, "config": {}},
                    {"instance_id": "A.unity:2", "script_id": "tgt",
                     "game_object_id": "A.unity:2", "active": True,
                     "enabled": True, "config": {}},
                ],
                "references": [{
                    "from": "A.unity:1",
                    "field": "peer",
                    "index": None,
                    "target_kind": "component",
                    "target_ref": "A.unity:2",
                    "target_is_ui": False,
                }],
                "lifecycle_order": ["A.unity:1", "A.unity:2"],
            },
        },
        "prefabs": {},
        "domain_overrides": {},
    }


class TestComputeCrossDomainEdges:

    def test_cross_domain_edge_listed(self):
        plan = _make_artifact_with_edge("client", "server")
        edges = compute_cross_domain_edges(plan)  # type: ignore[arg-type]
        assert len(edges) == 1
        edge = edges[0]
        assert edge["from_script"] == "src"
        assert edge["to_script"] == "tgt"
        assert edge["from_domain"] == "client"
        assert edge["to_domain"] == "server"
        assert edge["field"] == "peer"
        assert edge["owner_kind"] == "scene"
        assert edge["owner_ref"] == "A.unity"

    def test_same_domain_no_edge(self):
        plan = _make_artifact_with_edge("client", "client")
        edges = compute_cross_domain_edges(plan)  # type: ignore[arg-type]
        assert edges == []

    def test_legacy_domain_skipped(self):
        # ``legacy`` is the fail-closed verdict from PR3b; the host
        # never instantiates these, so a cross-edge to one is not
        # actionable -- omit it from the report.
        plan = _make_artifact_with_edge("client", "legacy")
        edges = compute_cross_domain_edges(plan)  # type: ignore[arg-type]
        assert edges == []

    def test_non_component_target_kind_skipped(self):
        plan = _make_artifact_with_edge("client", "server")
        plan["scenes"]["A.unity"]["references"][0]["target_kind"] = "asset"
        plan["scenes"]["A.unity"]["references"][0]["target_ref"] = (
            "rbxassetid://1"
        )
        edges = compute_cross_domain_edges(plan)  # type: ignore[arg-type]
        assert edges == []

    def test_prefab_owner_kind(self):
        plan = {
            "modules": {
                "src": {"stem": "Src", "runtime_bearing": True,
                        "domain": "client", "module_path": "ReplicatedStorage.Src"},
                "tgt": {"stem": "Tgt", "runtime_bearing": True,
                        "domain": "server", "module_path": "ReplicatedStorage.Tgt"},
            },
            "scenes": {},
            "prefabs": {
                "guid:Prefabs/Foo.prefab": {
                    "name": "Foo",
                    "instances": [
                        {"instance_id": "p:1", "script_id": "src",
                         "game_object_id": "p:1", "active": True,
                         "enabled": True, "config": {}},
                        {"instance_id": "p:2", "script_id": "tgt",
                         "game_object_id": "p:2", "active": True,
                         "enabled": True, "config": {}},
                    ],
                    "references": [{
                        "from": "p:1", "field": "peer", "index": None,
                        "target_kind": "component",
                        "target_ref": "p:2",
                        "target_is_ui": False,
                    }],
                    "lifecycle_order": ["p:1", "p:2"],
                },
            },
            "domain_overrides": {},
        }
        edges = compute_cross_domain_edges(plan)  # type: ignore[arg-type]
        assert len(edges) == 1
        assert edges[0]["owner_kind"] == "prefab"
        assert edges[0]["owner_ref"] == "guid:Prefabs/Foo.prefab"


# ---------------------------------------------------------------------------
# render_cross_domain_report
# ---------------------------------------------------------------------------

class TestRenderCrossDomainReport:

    def test_empty_returns_empty_string(self):
        assert render_cross_domain_report([]) == ""

    def test_single_edge_renders_table_row(self):
        out = render_cross_domain_report([{
            "from_script": "Foo",
            "to_script": "Bar",
            "field": "target",
            "from_domain": "client",
            "to_domain": "server",
        }])
        assert "## Cross-domain references" in out
        assert "| Foo | client | target | -> | Bar | server |" in out
        assert "Total edges: 1" in out

    def test_multiple_edges_listed_in_order(self):
        edges = [
            {"from_script": "A", "to_script": "B", "field": "x",
             "from_domain": "client", "to_domain": "server"},
            {"from_script": "C", "to_script": "D", "field": "y",
             "from_domain": "server", "to_domain": "client"},
        ]
        out = render_cross_domain_report(edges)
        # Row order matches input order.
        a_pos = out.index("| A |")
        c_pos = out.index("| C |")
        assert a_pos < c_pos
