"""
test_code_transpiler.py -- Tests for C# -> Luau transpilation.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestRuleBasedTranspile:
    def test_debug_log(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'Debug.Log("Hello World");'
        luau, confidence, warnings = _rule_based_transpile(csharp)
        assert "print" in luau

    def test_variable_declaration(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "float speed = 5.0f;"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "local" in luau or "speed" in luau

    def test_operator_conversion(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "if (a != b && c || !d)"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "~=" in luau
        assert "and" in luau
        assert "or" in luau

    def test_lifecycle_mapping(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = """
        void Update() {
            transform.position += Vector3.forward * Time.deltaTime;
        }
        """
        luau, _, _ = _rule_based_transpile(csharp)
        assert "Heartbeat" in luau or "RunService" in luau

    def test_vector3_conversion(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "Vector3 pos = new Vector3(1, 2, 3);"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "Vector3.new" in luau

    def test_instantiate(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "var obj = Instantiate(prefab);"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "Clone" in luau

    def test_getcomponent(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'GetComponent<Rigidbody>()'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "FindFirstChildOfClass" in luau

    def test_confidence_score(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'Debug.Log("simple");'
        _, confidence, _ = _rule_based_transpile(csharp)
        assert 0.0 <= confidence <= 1.0


class TestScriptClassification:
    def test_classify_server_script(self):
        from converter.code_transpiler import _classify_script_type
        csharp = """
        public class GameManager : MonoBehaviour {
            void Start() { }
            void Update() { }
        }
        """
        assert _classify_script_type(csharp, None) == "Script"

    def test_classify_local_script(self):
        from converter.code_transpiler import _classify_script_type
        csharp = """
        public class PlayerInput : MonoBehaviour {
            void Update() {
                if (Input.GetKeyDown(KeyCode.Space)) { }
                Camera.main.transform.position = pos;
            }
        }
        """
        assert _classify_script_type(csharp, None) == "LocalScript"

    def test_classify_ui_script_as_local(self):
        from converter.code_transpiler import _classify_script_type
        csharp = """
        using UnityEngine.UI;
        public class HudControl : MonoBehaviour {
            private Text ammoText;
            void Start() { ammoText = GetComponent<Text>(); }
        }
        """
        assert _classify_script_type(csharp, None) == "LocalScript"

    def test_classify_module_script(self):
        from converter.code_transpiler import _classify_script_type
        csharp = """
        public static class Util {
            public static float Clamp(float val, float min, float max) {
                return Mathf.Clamp(val, min, max);
            }
        }
        """
        assert _classify_script_type(csharp, None) == "ModuleScript"


    def test_try_catch_to_pcall(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = """try {
    DoSomething();
} catch (Exception e) {
    Debug.Log(e.Message);
}"""
        luau, _, _ = _rule_based_transpile(csharp)
        assert "pcall" in luau

    def test_string_concat_with_plus(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'string msg = "Hello " + name;'
        luau, _, _ = _rule_based_transpile(csharp)
        assert ".." in luau

    def test_throw_to_error(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'throw new ArgumentException("bad value");'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "error" in luau

    def test_if_else_conversion(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = """if (health <= 0) {
    isDead = true;
} else if (health < 20) {
    isLow = true;
} else {
    isOk = true;
}"""
        luau, _, _ = _rule_based_transpile(csharp)
        assert "if health <= 0 then" in luau
        assert "elseif health < 20 then" in luau
        assert "else" in luau
        assert "end" in luau

    def test_while_loop(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "while (running) {"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "while running do" in luau

    def test_for_loop_inclusive(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "for (int i = 0; i <= count; i++)"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "for i = 0, count do" in luau

    def test_for_loop_decrement(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "for (int i = n; i >= 0; i--)"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "for i = n, 0, -1 do" in luau

    def test_foreach_generic_type(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "foreach (KeyValuePair<string, int> pair in dict)"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "for _, pair in dict do" in luau

    def test_auto_property(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "public bool isActive { get; set; }"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "local isActive = nil" in luau

    def test_enum_declaration(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "enum GameState { Menu, Playing, Paused, GameOver }"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "local GameState" in luau
        assert "Menu = 0" in luau
        assert "GameOver = 3" in luau

    def test_interface_declaration(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "public interface IMovable {"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "interface IMovable" in luau

    def test_struct_declaration(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "public struct DamageInfo {"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "local DamageInfo" in luau

    def test_override_keyword_stripped(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "public override void OnDeath()"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "override" not in luau
        assert "OnDeath" in luau

    def test_field_with_semicolon(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "private float currentHealth;"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "local currentHealth" in luau

    def test_readonly_keyword_stripped(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "public readonly int maxCount = 10;"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "readonly" not in luau

    def test_const_keyword_stripped(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "public const float GRAVITY = 9.81f;"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "const" not in luau
        assert "9.81" in luau

    def test_array_initialization(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "int[] arr = new int[] { 1, 2, 3 };"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "{ 1, 2, 3 }" in luau
        assert "new" not in luau

    def test_list_initialization(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = "var names = new List<string>();"
        luau, _, _ = _rule_based_transpile(csharp)
        assert "{}" in luau


class TestVisualOnlyDetection:
    """Tests for _is_visual_only_script classification."""

    def test_shader_script_is_visual(self):
        from converter.code_transpiler import _is_visual_only_script
        source = """
        using UnityEngine;
        public class CustomEffect : MonoBehaviour {
            void OnRenderImage(RenderTexture src, RenderTexture dest) {
                Graphics.Blit(src, dest, material);
            }
            Material material;
        }
        """
        assert _is_visual_only_script(Path("CustomEffect.cs"), source) is True

    def test_gameplay_script_not_visual(self):
        from converter.code_transpiler import _is_visual_only_script
        source = """
        using UnityEngine;
        public class Explosive : MonoBehaviour {
            private void OnCollisionEnter(Collision col) {
                Instantiate(explosionPrefab, transform.position, Quaternion.identity);
                Destroy(gameObject);
            }
        }
        """
        assert _is_visual_only_script(Path("Explosive.cs"), source) is False

    def test_standard_assets_gameplay_not_visual(self):
        """Standard Assets scripts with gameplay logic should NOT be stubbed."""
        from converter.code_transpiler import _is_visual_only_script
        source = """
        using UnityEngine;
        namespace UnityStandardAssets.Utility {
            public class ObjectResetter : MonoBehaviour {
                private void Start() {
                    originalPosition = transform.position;
                }
                public void DelayedReset(float delay) {
                    StartCoroutine(ResetCoroutine(delay));
                }
            }
        }
        """
        assert _is_visual_only_script(
            Path("Standard Assets/Utility/ObjectResetter.cs"), source
        ) is False

    def test_water_visual_script_is_visual(self):
        from converter.code_transpiler import _is_visual_only_script
        source = "using UnityEngine; public class WaterBase : MonoBehaviour { }"
        assert _is_visual_only_script(Path("WaterBase.cs"), source) is True


class TestLuauValidatorCSharpRemnants:
    """Test that luau_validator catches and fixes C# remnants."""

    def test_typeof_fix(self):
        from converter.luau_validator import validate_and_fix
        source = 'if typeof(MyClass) == "string" then\nend'
        fixed, fixes = validate_and_fix("test", source)
        assert "typeof" not in fixed or '"MyClass"' in fixed

    def test_typeof_comparison_fix(self):
        from converter.luau_validator import validate_and_fix
        source = 'local same = typeof(a) == typeof(b)'
        fixed, fixes = validate_and_fix("test", source)
        # Should keep Luau typeof() for runtime variables
        assert "typeof(a)" in fixed

    def test_length_fix(self):
        from converter.luau_validator import validate_and_fix
        source = 'local n = items.Length'
        fixed, fixes = validate_and_fix("test", source)
        assert "#items" in fixed
        assert ".Length" not in fixed

    def test_count_fix(self):
        from converter.luau_validator import validate_and_fix
        source = 'local n = myList.Count'
        fixed, fixes = validate_and_fix("test", source)
        assert "#myList" in fixed

    def test_contains_fix(self):
        from converter.luau_validator import validate_and_fix
        source = 'if items.Contains(target) then\nend'
        fixed, fixes = validate_and_fix("test", source)
        assert "table.find" in fixed

    def test_tostring_fix(self):
        from converter.luau_validator import validate_and_fix
        source = 'local s = value.ToString()'
        fixed, fixes = validate_and_fix("test", source)
        assert "tostring(value)" in fixed

    def test_null_to_nil(self):
        from converter.luau_validator import validate_and_fix
        source = 'if x == null then\nend'
        fixed, fixes = validate_and_fix("test", source)
        assert "nil" in fixed
        assert "null" not in fixed

    def test_compound_assignment_fix(self):
        from converter.luau_validator import validate_and_fix
        source = 'count += 1'
        fixed, fixes = validate_and_fix("test", source)
        assert "+=" not in fixed
        assert "count = count + 1" in fixed

    def test_semicolons_removed(self):
        from converter.luau_validator import validate_and_fix
        source = 'local x = 5;\nprint(x);'
        fixed, fixes = validate_and_fix("test", source)
        assert not fixed.rstrip().endswith(";")

    def test_contains_key_fix(self):
        from converter.luau_validator import validate_and_fix
        source = 'if dict.ContainsKey(key) then\nend'
        fixed, fixes = validate_and_fix("test", source)
        assert "dict[key] ~= nil" in fixed

    def test_try_get_value_fix(self):
        from converter.luau_validator import validate_and_fix
        source = 'dict.TryGetValue(key, out result)'
        fixed, fixes = validate_and_fix("test", source)
        assert "result = dict[key]" in fixed

    def test_event_invoke_fix(self):
        from converter.luau_validator import validate_and_fix
        source = 'OnDeath.Invoke(player)'
        fixed, fixes = validate_and_fix("test", source)
        assert ":Fire(" in fixed
