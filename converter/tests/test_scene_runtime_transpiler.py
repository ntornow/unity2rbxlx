"""test_scene_runtime_transpiler.py -- PR3a test matrix coverage for the
generic-runtime transpiler integration:

  - prompt isolation (sha256(_AI_SYSTEM_PROMPT) byte-unchanged from main)
  - cache-namespace disjointness (legacy and generic prompts hash differently)
  - pre-transpile target switch for runtime-bearing MonoBehaviours
    (including prefab-only)
  - public ``transpile_scripts`` signature accepts and routes the new
    ``runtime_mode`` / ``runtime_bearing_paths`` kwargs

The compliance-spike pass-rate test lives separately under
``test_scene_runtime_spike.py`` and is marked slow.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.code_transpiler import (  # noqa: E402
    _AI_SYSTEM_PROMPT,
    _GENERIC_RUNTIME_PROMPT,
    _ai_cache_key,
    _select_prompt,
    transpile_scripts,
)


# ---------------------------------------------------------------------------
# Prompt isolation -- the byte-frozen invariant from the PR3a row.
# ---------------------------------------------------------------------------

class TestPromptIsolation:
    """``_AI_SYSTEM_PROMPT`` is the legacy transpile cache key. Editing it
    silently re-transpiles every legacy project. PR3a's contract is:
    the legacy prompt is byte-identical to ``origin/main``; the generic
    prompt is a NEW, separate constant.
    """

    def _git_show_main(self) -> str:
        """Read ``_AI_SYSTEM_PROMPT`` from the merge base with origin/main."""
        repo_root = Path(__file__).parent.parent.parent
        result = subprocess.run(
            ["git", "show", "origin/main:converter/converter/code_transpiler.py"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def test_legacy_prompt_byte_unchanged_from_main(self):
        # Load origin/main's ``_AI_SYSTEM_PROMPT`` value by ``exec``-ing
        # the assignment block out of main's source. This bypasses any
        # whitespace/escape ambiguity from manual offset arithmetic --
        # if Python parses both files into the same string, they're
        # byte-identical at the language level.
        main_src = self._git_show_main()
        idx = main_src.find('_AI_SYSTEM_PROMPT = """')
        assert idx != -1, "could not locate _AI_SYSTEM_PROMPT in origin/main"
        # The closing ``"""`` of the same assignment is the FIRST one
        # after ``idx``. The prompt body has no triple-quote inside it
        # -- verified at write time (`grep -n '"""'` on the constant
        # body returns nothing other than the openers/closers).
        body_start = idx + len('_AI_SYSTEM_PROMPT = ')
        end = main_src.find('"""', body_start + 3)
        assert end != -1, "could not locate end of _AI_SYSTEM_PROMPT in origin/main"
        # ``exec`` the assignment in an isolated namespace. Python is
        # the source of truth for what the string evaluates to.
        ns: dict[str, str] = {}
        exec(main_src[idx:end + 3], ns)
        main_prompt = ns["_AI_SYSTEM_PROMPT"]
        assert main_prompt == _AI_SYSTEM_PROMPT, (
            "_AI_SYSTEM_PROMPT diverged from origin/main. PR3a's contract "
            "requires the legacy prompt be byte-frozen so legacy cache "
            "entries stay valid. If a legacy-prompt edit is intentional, "
            "split it from PR3a."
        )

    def test_generic_prompt_is_a_distinct_constant(self):
        # Different content means different cache key, which means disjoint
        # namespace. Hashing both is the simplest invariant.
        assert _AI_SYSTEM_PROMPT != _GENERIC_RUNTIME_PROMPT
        legacy_hash = hashlib.sha256(_AI_SYSTEM_PROMPT.encode()).hexdigest()
        generic_hash = hashlib.sha256(_GENERIC_RUNTIME_PROMPT.encode()).hexdigest()
        assert legacy_hash != generic_hash, (
            "Generic and legacy prompts produce the same sha256 -- means "
            "the cache namespace check is broken. Generic transpile cache "
            "would overwrite legacy entries."
        )


# ---------------------------------------------------------------------------
# Specific delayed-Destroy emission shape pinned in the generic prompt.
# ``host.invoke(self, function() ... end, delay)`` silently no-ops because
# the runtime uses ``method`` as a string key into the class table; the
# correct primitive is ``startCoroutine + task.wait + destroy``. See
# ``TestDelayedDestroyPattern`` in test_scene_runtime_host_behavior.py
# for the matching behavioral coverage.
# ---------------------------------------------------------------------------

class TestGenericPromptDelayedDestroyShape:

    def test_prompt_does_not_teach_invoke_with_function_method(self):
        # Catches regression to the broken shape. Substring is precise
        # enough that it would only match the bad teaching, not a
        # legitimate ``self.host.invoke(self, "MethodName", delay)`` use.
        bad = 'self.host.invoke(self, function()'
        assert bad not in _GENERIC_RUNTIME_PROMPT, (
            f"Generic prompt still teaches the broken {bad!r} shape -- "
            "``invoke`` dispatches by string method name; a function "
            "literal is silently dropped (no-op)."
        )

    def test_prompt_teaches_startCoroutine_for_delayed_destroy(self):
        # Pin the corrected teaching so future edits don't regress to
        # ``task.delay`` (uncancelled on teardown -> leaks past destroy)
        # or back to ``invoke``.
        good = 'self.host.startCoroutine(self, function() task.wait(delay); self.host.destroy(target) end)'
        assert good in _GENERIC_RUNTIME_PROMPT, (
            f"Generic prompt missing the canonical delayed-destroy "
            f"shape {good!r}. ``startCoroutine`` is host-tracked and "
            "cancels on OnDestroy; raw ``task.delay`` leaks past teardown."
        )


# ---------------------------------------------------------------------------
# Cache key namespace -- ``_ai_cache_key`` already takes ``prompt_hash``,
# so generic vs legacy outputs cannot share a cache file by construction.
# ---------------------------------------------------------------------------

class TestCacheNamespace:

    def test_select_prompt_legacy_returns_ai_system_prompt(self):
        prompt, prompt_hash = _select_prompt("legacy")
        assert prompt is _AI_SYSTEM_PROMPT
        assert prompt_hash == hashlib.sha256(
            _AI_SYSTEM_PROMPT.encode()
        ).hexdigest()[:16]

    def test_select_prompt_generic_returns_generic_runtime_prompt(self):
        prompt, prompt_hash = _select_prompt("generic")
        assert prompt is _GENERIC_RUNTIME_PROMPT
        assert prompt_hash == hashlib.sha256(
            _GENERIC_RUNTIME_PROMPT.encode()
        ).hexdigest()[:16]

    def test_cache_keys_differ_across_modes(self):
        # Same C# input, same class name + script type, same model --
        # ONLY the prompt_hash differs. The cache key MUST differ.
        _, legacy_h = _select_prompt("legacy")
        _, generic_h = _select_prompt("generic")
        common = {
            "csharp_source": "public class Foo : MonoBehaviour { void Awake() {} }",
            "class_name": "Foo",
            "script_type": "ModuleScript",
            "project_context": "",
            "model": "claude-opus-4-7",
        }
        legacy_key = _ai_cache_key(prompt_hash=legacy_h, **common)
        generic_key = _ai_cache_key(prompt_hash=generic_h, **common)
        assert legacy_key != generic_key, (
            "Cache namespaces collapse when prompt hashes differ. Generic "
            "and legacy must never share a cache file -- editing the "
            "generic prompt would invalidate legacy cache entries."
        )


# ---------------------------------------------------------------------------
# Pre-transpile target switch -- runtime-bearing MonoBehaviours under
# generic transpile as ModuleScript regardless of what
# ``_classify_script_type`` would have inferred.
#
# We test this by running ``transpile_scripts`` with ``use_ai=False`` so
# nothing actually calls the AI -- the script_type ends up baked into the
# stub generator's output by ``TranspiledScript.script_type``.
# ---------------------------------------------------------------------------

class _ScriptInfoStub:
    """Minimal stand-in for ``unity.script_analyzer.ScriptInfo`` -- the
    transpiler only reads ``path``, ``class_name``, ``referenced_types``,
    ``suggested_type``, and (optionally) ``is_visual_only`` off it."""

    def __init__(self, path: Path, class_name: str,
                 suggested_type: str = "Script") -> None:
        self.path = path
        self.class_name = class_name
        self.referenced_types: list[str] = []
        self.suggested_type = suggested_type


@pytest.fixture
def fps_like_project(tmp_path: Path):
    """A 2-script synthetic project: a server MonoBehaviour and a UI
    behaviour. The server one is the canonical 'runtime-bearing' shape;
    we use it to assert the target switch flips its emitted type from
    ``Script`` to ``ModuleScript`` under generic.
    """
    proj = tmp_path / "project"
    (proj / "Assets" / "Scripts").mkdir(parents=True)
    server_path = proj / "Assets" / "Scripts" / "Enemy.cs"
    server_path.write_text(
        "using UnityEngine;\npublic class Enemy : MonoBehaviour { void Awake() {} }\n"
    )
    ui_path = proj / "Assets" / "Scripts" / "HUD.cs"
    ui_path.write_text(
        "using UnityEngine;\nusing UnityEngine.UI;\n"
        "public class HUD : MonoBehaviour { void Update() { Text t; } }\n"
    )
    return proj, [
        _ScriptInfoStub(server_path, "Enemy", suggested_type="Script"),
        _ScriptInfoStub(ui_path, "HUD", suggested_type="LocalScript"),
    ]


class TestTargetSwitch:

    def test_legacy_mode_keeps_classifier_target(self, fps_like_project):
        proj, infos = fps_like_project
        result = transpile_scripts(
            unity_project_path=proj,
            script_infos=infos,
            use_ai=False,
            runtime_mode="legacy",
        )
        # ``Enemy`` is a MonoBehaviour with no client API -- classifier
        # returns ``Script``. ``HUD`` references UI -- classifier
        # returns ``LocalScript``. Both stand under legacy.
        by_name = {s.csharp_source.split("class ")[1].split(" ")[0]: s
                   for s in result.scripts}
        assert by_name["Enemy"].script_type == "Script"
        assert by_name["HUD"].script_type == "LocalScript"

    def test_generic_mode_promotes_runtime_bearing_to_modulescript(self, fps_like_project):
        proj, infos = fps_like_project
        # ``Enemy`` is in the runtime-bearing set; ``HUD`` is not.
        bearing = frozenset({infos[0].path})
        result = transpile_scripts(
            unity_project_path=proj,
            script_infos=infos,
            use_ai=False,
            runtime_mode="generic",
            runtime_bearing_paths=bearing,
        )
        by_name = {s.csharp_source.split("class ")[1].split(" ")[0]: s
                   for s in result.scripts}
        # The flip: ``Enemy`` is now ``ModuleScript`` (host requires + instantiates).
        assert by_name["Enemy"].script_type == "ModuleScript", (
            "Runtime-bearing MonoBehaviour did not flip to ModuleScript "
            "under generic. The host runtime won't be able to require() it."
        )
        # HUD is not runtime-bearing -- classifier output is preserved.
        assert by_name["HUD"].script_type == "LocalScript", (
            "Non-runtime-bearing script under generic should keep its "
            "classifier-assigned target (LocalScript)."
        )

    def test_prefab_only_runtime_bearing_caught(self, tmp_path):
        # The doc explicitly calls out: prefab-only MonoBehaviours
        # (never attached to a scene GameObject) must still be picked
        # up by the predicate. PR1's planner already includes them in
        # the ``runtime_bearing`` set; PR3a's transpiler just respects
        # that set without re-deriving the predicate.
        proj = tmp_path / "project"
        (proj / "Assets" / "Scripts").mkdir(parents=True)
        prefab_only_path = proj / "Assets" / "Scripts" / "Bullet.cs"
        prefab_only_path.write_text(
            "using UnityEngine;\n"
            "public class Bullet : MonoBehaviour { void Awake() {} }\n"
        )
        infos = [_ScriptInfoStub(prefab_only_path, "Bullet", suggested_type="Script")]
        bearing = frozenset({prefab_only_path})
        result = transpile_scripts(
            unity_project_path=proj,
            script_infos=infos,
            use_ai=False,
            runtime_mode="generic",
            runtime_bearing_paths=bearing,
        )
        assert result.scripts[0].script_type == "ModuleScript", (
            "Prefab-only runtime-bearing MonoBehaviour was not flipped to "
            "ModuleScript -- ``instantiatePrefab`` will fail to require() "
            "the class table at runtime."
        )


# ---------------------------------------------------------------------------
# Default-arg ergonomics -- the new kwargs are opt-in. Existing callers
# that pass no ``runtime_mode`` get legacy behaviour.
# ---------------------------------------------------------------------------

class TestPerScriptGating:
    """Codex P1#1 regression: under ``runtime_mode="generic"`` ONLY
    paths in ``runtime_bearing_paths`` should receive the generic prompt
    + contract verifier. Non-runtime-bearing scripts (LocalScripts,
    helpers) stay on the legacy prompt because the contract was never
    meant to apply to them. Test via the backend-stub hook below: we
    monkeypatch the per-backend transpile entry to capture which
    ``runtime_mode`` it was called with per stem.
    """

    def _capture_runtime_modes(self, monkeypatch, infos, bearing_paths):
        """Return ``{stem: runtime_mode}`` recorded as each script was
        transpiled. Uses anthropic_api branch to make the runtime-mode
        value reach the backend (skipping ``use_ai=False`` which doesn't
        invoke any backend)."""
        from converter import code_transpiler

        # Force the anthropic_api branch with a dummy key so each script
        # actually reaches a backend call.
        monkeypatch.setattr(code_transpiler, "_find_transpiler",
                            lambda: "anthropic_api")
        captured: dict[str, str] = {}

        def fake_ai(csharp_source, api_key, model, class_name="",
                    script_type="Script", project_context="",
                    runtime_mode="legacy"):
            # Key by class_name (stem-ish); enough for the 2-script
            # synthetic project.
            captured[class_name] = runtime_mode
            return ("local M = {}\nreturn M\n", 0.9, [])

        monkeypatch.setattr(code_transpiler, "_ai_transpile", fake_ai)
        # Make sure no real network call happens.
        monkeypatch.setattr(code_transpiler, "_claude_cli_transpile",
                            lambda *a, **k: ("", 0.0, []))
        return captured

    def test_non_runtime_bearing_script_stays_on_legacy_prompt(
        self, fps_like_project, monkeypatch,
    ):
        proj, infos = fps_like_project
        bearing = frozenset({infos[0].path})  # Enemy bearing, HUD not
        captured = self._capture_runtime_modes(monkeypatch, infos, bearing)
        transpile_scripts(
            unity_project_path=proj,
            script_infos=infos,
            use_ai=True,
            api_key="dummy",
            runtime_mode="generic",
            runtime_bearing_paths=bearing,
        )
        # The runtime-bearing MB sees generic; the LocalScript does not.
        assert captured.get("Enemy") == "generic", (
            f"runtime-bearing Enemy should have been called with "
            f"runtime_mode='generic'; got {captured!r}"
        )
        assert captured.get("HUD") == "legacy", (
            f"non-runtime-bearing HUD should have stayed on legacy "
            f"prompt under generic-mode run; got {captured!r}. The "
            f"contract verifier rejecting LocalScripts for not returning "
            f"a class table is the symptom."
        )

    def test_legacy_mode_keeps_everything_legacy(
        self, fps_like_project, monkeypatch,
    ):
        # Sanity: under legacy mode the gate is irrelevant -- every
        # script must see legacy regardless of runtime_bearing_paths.
        proj, infos = fps_like_project
        captured = self._capture_runtime_modes(
            monkeypatch, infos, frozenset({infos[0].path}),
        )
        transpile_scripts(
            unity_project_path=proj,
            script_infos=infos,
            use_ai=True,
            api_key="dummy",
            runtime_mode="legacy",
            runtime_bearing_paths=frozenset({infos[0].path}),
        )
        assert captured.get("Enemy") == "legacy"
        assert captured.get("HUD") == "legacy"


class TestDefaultArgs:

    def test_runtime_mode_defaults_to_legacy(self, fps_like_project):
        # Same call without ``runtime_mode`` -- must behave identically
        # to ``runtime_mode='legacy'``.
        proj, infos = fps_like_project
        baseline = transpile_scripts(
            unity_project_path=proj, script_infos=infos, use_ai=False,
            runtime_mode="legacy",
        )
        implicit = transpile_scripts(
            unity_project_path=proj, script_infos=infos, use_ai=False,
        )
        by_baseline = {s.source_path: s.script_type for s in baseline.scripts}
        by_implicit = {s.source_path: s.script_type for s in implicit.scripts}
        assert by_baseline == by_implicit, (
            "Implicit (default) ``runtime_mode`` produced different output "
            "from explicit ``runtime_mode='legacy'`` -- the default flipped."
        )

    def test_runtime_bearing_paths_optional_under_generic(self, fps_like_project):
        # Caller may omit ``runtime_bearing_paths`` -- empty set means
        # nothing is flipped to ModuleScript, falls back to classifier.
        proj, infos = fps_like_project
        result = transpile_scripts(
            unity_project_path=proj, script_infos=infos, use_ai=False,
            runtime_mode="generic",
            runtime_bearing_paths=None,
        )
        by_name = {s.csharp_source.split("class ")[1].split(" ")[0]: s
                   for s in result.scripts}
        # Classifier output preserved (nothing promoted).
        assert by_name["Enemy"].script_type == "Script"
        assert by_name["HUD"].script_type == "LocalScript"
