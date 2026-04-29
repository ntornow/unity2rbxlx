"""Tests for Phase 5.11 (_analyze_csharp_patterns) and Phase 5.12
(_classify_script_type harmonization)."""

from __future__ import annotations

from types import SimpleNamespace

from converter.code_transpiler import (
    _analyze_csharp_patterns,
    _classify_script_type,
)


class TestAnalyzeCSharpPatterns:
    """Phase 5.11: pre-AI pattern warnings for six high-impact categories."""

    def test_empty_source_returns_empty(self):
        assert _analyze_csharp_patterns("") == []

    def test_linq_using_directive_warns(self):
        warnings = _analyze_csharp_patterns("using System.Linq;\nclass A {}")
        assert any("LINQ" in w for w in warnings)

    def test_linq_select_call_warns(self):
        warnings = _analyze_csharp_patterns(
            "var x = list.Select(i => i * 2).ToList();"
        )
        assert any("LINQ" in w for w in warnings)

    def test_linq_orderby_warns(self):
        warnings = _analyze_csharp_patterns(
            "var sorted = items.OrderBy(i => i.Name).ToArray();"
        )
        assert any("LINQ" in w for w in warnings)

    def test_async_method_signature_warns(self):
        warnings = _analyze_csharp_patterns(
            "public async Task<int> Run() { return 1; }"
        )
        assert any("async" in w.lower() for w in warnings)

    def test_await_expression_warns(self):
        warnings = _analyze_csharp_patterns("var r = await TaskOp();")
        assert any("async" in w.lower() for w in warnings)

    def test_unitask_warns(self):
        warnings = _analyze_csharp_patterns(
            "async UniTask LoadAssetAsync() { /* ... */ }"
        )
        assert any("async" in w.lower() for w in warnings)

    def test_unity_networking_warns(self):
        warnings = _analyze_csharp_patterns(
            "using UnityEngine.Networking;\nclass S : NetworkBehaviour {}"
        )
        assert any("Networking" in w or "Mirror" in w for w in warnings)

    def test_command_attribute_warns(self):
        warnings = _analyze_csharp_patterns(
            "[Command]\nvoid CmdShoot() {}"
        )
        assert any("Networking" in w or "Mirror" in w for w in warnings)

    def test_unity_web_request_warns(self):
        warnings = _analyze_csharp_patterns(
            "var w = UnityWebRequest.Get(url);"
        )
        assert any("Networking" in w or "Mirror" in w for w in warnings)

    def test_typeof_reflection_warns(self):
        warnings = _analyze_csharp_patterns("var t = typeof(MyClass);")
        assert any("Reflection" in w for w in warnings)

    def test_get_type_reflection_warns(self):
        warnings = _analyze_csharp_patterns("var t = obj.GetType();")
        assert any("Reflection" in w for w in warnings)

    def test_threading_thread_warns(self):
        warnings = _analyze_csharp_patterns(
            "var t = new Thread(Worker); t.Start();"
        )
        assert any("Threading" in w for w in warnings)

    def test_threading_lock_warns(self):
        warnings = _analyze_csharp_patterns("lock (myLock) { /* ... */ }")
        assert any("Threading" in w for w in warnings)

    def test_unsafe_block_warns(self):
        warnings = _analyze_csharp_patterns(
            "unsafe { fixed (int* p = &arr[0]) { *p = 1; } }"
        )
        assert any("Unsafe" in w for w in warnings)

    def test_intptr_warns(self):
        warnings = _analyze_csharp_patterns(
            "IntPtr handle = Marshal.AllocHGlobal(64);"
        )
        assert any("Unsafe" in w for w in warnings)

    def test_six_category_fixture(self):
        """Acceptance: a fixture script using each of the six pattern
        categories produces a corresponding warning. One warning per
        category — duplicate hits within a category collapse to one entry.
        """
        source = """
        using System;
        using System.Linq;
        using System.Reflection;
        using System.Threading;
        using UnityEngine.Networking;

        class Mega {
            public async Task<int> RunAsync() {
                lock (this) {
                    var t = typeof(Mega);
                    var w = UnityWebRequest.Get("x");
                    return await Task.Run(() => list.Select(x => x).Count());
                }
            }
            unsafe { var p = (int*)IntPtr.Zero; }
        }
        """
        warnings = _analyze_csharp_patterns(source)
        # Six categories — six warnings, one per category.
        joined = " | ".join(warnings)
        assert "LINQ" in joined
        assert "async" in joined.lower()
        assert "Networking" in joined or "Mirror" in joined
        assert "Reflection" in joined
        assert "Threading" in joined
        assert "Unsafe" in joined
        assert len(warnings) == 6

    def test_commented_out_code_does_not_warn(self):
        """// and /* */ comments are stripped before matching."""
        source = """
        // var x = list.Select(i => i);
        /* lock (this) { Thread.Sleep(1); } */
        class Plain {}
        """
        assert _analyze_csharp_patterns(source) == []

    def test_one_warning_per_category(self):
        """Multiple LINQ hits in one source produce ONE LINQ warning, not many."""
        source = """
        var a = list.Select(x => x).ToList();
        var b = items.Where(x => x).OrderBy(x => x).ToArray();
        """
        warnings = _analyze_csharp_patterns(source)
        linq_warnings = [w for w in warnings if "LINQ" in w]
        assert len(linq_warnings) == 1


class TestClassifyScriptTypeHarmonization:
    """Phase 5.12: default to ModuleScript when the source isn't clearly
    a MonoBehaviour or a client-side script.
    """

    def _info(self, suggested: str | None = None):
        ns = SimpleNamespace()
        if suggested is not None:
            ns.suggested_type = suggested
        return ns

    def test_analyzer_suggestion_wins(self):
        """info.suggested_type takes precedence over source heuristics."""
        info = self._info(suggested="LocalScript")
        assert _classify_script_type(
            "class Foo : MonoBehaviour {}", info,
        ) == "LocalScript"

    def test_monobehaviour_defaults_to_script(self):
        """A MonoBehaviour script with no client APIs goes to Script (server)."""
        source = "class Player : MonoBehaviour { void Update() {} }"
        assert _classify_script_type(source, self._info()) == "Script"

    def test_networkbehaviour_defaults_to_script(self):
        source = "class Net : NetworkBehaviour { void Update() {} }"
        assert _classify_script_type(source, self._info()) == "Script"

    def test_client_indicator_overrides_monobehaviour(self):
        """Client-side APIs override MonoBehaviour to LocalScript."""
        source = (
            "class Hud : MonoBehaviour { void Update() { "
            "var p = Input.GetKeyDown(KeyCode.Space); } }"
        )
        assert _classify_script_type(source, self._info()) == "LocalScript"

    def test_utility_class_defaults_to_modulescript(self):
        """A non-MonoBehaviour class is a ModuleScript by default."""
        source = (
            "public static class MathHelpers { "
            "public static int Square(int x) { return x * x; } }"
        )
        assert _classify_script_type(source, self._info()) == "ModuleScript"

    def test_no_class_no_monobehaviour_defaults_to_modulescript(self):
        """Phase 5.12 harmonization: source-repo behavior. A C# file with
        no class and no MonoBehaviour now defaults to ModuleScript instead
        of the historical dest-repo Script default.
        """
        # An exotic top-level-statements file (C# 9+) or a source file
        # that's only types/enums.
        source = "public enum Direction { North, South, East, West }"
        assert _classify_script_type(source, self._info()) == "ModuleScript"

    def test_modulescript_default_avoids_downstream_reclassification(self):
        """Pre-harmonization the same input would default to Script, then
        the script-coherence pass would reclassify it to ModuleScript on
        the second pass. With harmonization the initial classification is
        already correct.
        """
        source = "public interface IShootable { void Hit(); }"
        assert _classify_script_type(source, self._info()) == "ModuleScript"
