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


class TestNewTranspilerFeatures:
    """Tests for newly added transpiler features."""

    def test_nameof_expression(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'Debug.Log(nameof(health));'
        luau, _, _ = _rule_based_transpile(csharp)
        assert '"health"' in luau

    def test_nameof_dotted(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'string name = nameof(MyClass.Property);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert '"MyClass.Property"' in luau

    def test_null_coalescing(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'var result = value ?? fallback;'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "~= nil" in luau
        assert "value" in luau
        assert "fallback" in luau

    def test_dictionary_initializer(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'var dict = new Dictionary<string, int> { "a", 1 };'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "Dictionary" not in luau or "-- " in luau
        assert "{" in luau

    def test_list_initializer(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'var items = new List<int> { 1, 2, 3 };'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "List" not in luau or "-- " in luau
        assert "{ 1, 2, 3 }" in luau

    def test_hashset_initializer(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'var set = new HashSet<string> { "a", "b" };'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "HashSet" not in luau or "-- " in luau

    def test_queue_enqueue(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'queue.Enqueue(item);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "table.insert(" in luau

    def test_stack_push(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'stack.Push(item);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "table.insert(" in luau

    def test_mathf_repeat(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'float r = Mathf.Repeat(t, length);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "mathRepeat(" in luau

    def test_mathf_approximately(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'if (Mathf.Approximately(a, b))'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "mathApproximately(" in luau

    def test_mathf_utility_functions_injected(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'float r = Mathf.Repeat(t, length);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "local function mathRepeat(" in luau

    def test_mathf_delta_angle_deps(self):
        """mathDeltaAngle depends on mathRepeat — both should be injected."""
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'float d = Mathf.DeltaAngle(current, target);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "local function mathDeltaAngle(" in luau
        assert "local function mathRepeat(" in luau

    def test_dotween_domove(self):
        from converter.luau_validator import validate_and_fix
        source = 'part.DOMove(targetPos, 1.5)'
        fixed, fixes = validate_and_fix("test", source)
        assert "TweenService:Create(" in fixed
        assert "Position" in fixed

    def test_dotween_doscale(self):
        from converter.luau_validator import validate_and_fix
        source = 'obj.DOScale(newSize, 0.5)'
        fixed, fixes = validate_and_fix("test", source)
        assert "TweenService:Create(" in fixed
        assert "Size" in fixed

    def test_dotween_dofade(self):
        from converter.luau_validator import validate_and_fix
        source = 'part.DOFade(0, 2.0)'
        fixed, fixes = validate_and_fix("test", source)
        assert "TweenService:Create(" in fixed
        assert "Transparency" in fixed

    def test_queue_dequeue_fix(self):
        from converter.luau_validator import validate_and_fix
        source = 'local item = queue.table.remove(, 1)'
        fixed, fixes = validate_and_fix("test", source)
        assert "table.remove(queue, 1)" in fixed

    def test_stack_pop_fix(self):
        from converter.luau_validator import validate_and_fix
        source = 'local item = stack.table.remove(, #)'
        fixed, fixes = validate_and_fix("test", source)
        assert "table.remove(stack, #stack)" in fixed

    def test_event_connect_touched(self):
        from converter.luau_validator import validate_and_fix
        source = 'part.Touched += onTouch'
        fixed, fixes = validate_and_fix("test", source)
        assert ":Connect(" in fixed

    def test_mathf_inverselerp(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'float t = Mathf.InverseLerp(a, b, value);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "mathInverseLerp(" in luau
        assert "local function mathInverseLerp(" in luau

    def test_lambda_expression(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'var result = items.Where(x => x.Health > 0);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "function(x)" in luau
        assert "return" in luau

    def test_linq_where_utility_injected(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'var alive = enemies.Where(x => x.Health > 0);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "linqWhere(" in luau
        assert "local function linqWhere(" in luau

    def test_linq_select(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'var names = items.Select(x => x.Name);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "linqSelect(" in luau
        assert "local function linqSelect(" in luau

    def test_linq_any(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'bool hasEnemy = enemies.Any(x => x.IsAlive);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "linqAny(" in luau

    def test_linq_first_or_default(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'var item = items.FirstOrDefault(x => x.Active);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "linqFirstOrDefault(" in luau

    def test_linq_order_by(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'var sorted = items.OrderBy(x => x.Priority);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "linqOrderBy(" in luau

    def test_linq_sum(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'int total = items.Sum(x => x.Value);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "linqSum(" in luau

    def test_linq_tolist_removed(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'var list = items.ToList();'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "ToList" not in luau

    def test_linq_distinct(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'var unique = items.Distinct();'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "linqDistinct(" in luau

    def test_oncomplete_tween(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'tween.OnComplete(Finish);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "Completed:Connect(" in luau


class TestSwitchCase:
    def test_simple_switch(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = """
switch (type) {
    case 0:
        DoA();
        break;
    case 1:
        DoB();
        break;
    default:
        DoC();
        break;
}"""
        luau, _, _ = _rule_based_transpile(csharp)
        assert "if type == 0 then" in luau
        assert "elseif type == 1 then" in luau
        assert "else" in luau
        assert "end" in luau
        assert "break" not in luau or "-- " in luau

    def test_string_case(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = """
switch (name) {
    case "fire":
        PlayFire();
        break;
    case "water":
        PlayWater();
        break;
}"""
        luau, _, _ = _rule_based_transpile(csharp)
        assert 'if name == "fire" then' in luau
        assert 'elseif name == "water" then' in luau


class TestMultiLineEnum:
    def test_multiline_enum(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = """
public enum ModulationType
{
    Sine,
    Triangle,
    Perlin,
    Random
}"""
        luau, _, _ = _rule_based_transpile(csharp)
        assert "ModulationType" in luau
        assert "Sine" in luau
        assert "Random" in luau

    def test_enum_with_values(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = """
enum State
{
    Idle = 0,
    Running = 1,
    Dead = 2
}"""
        luau, _, _ = _rule_based_transpile(csharp)
        assert "Idle = 0" in luau
        assert "Running = 1" in luau
        assert "Dead = 2" in luau


class TestOutParameters:
    def test_out_var(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'dict.TryGetValue(key, out var value);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "out" not in luau or "-- " in luau

    def test_out_type(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'Physics.Raycast(origin, direction, out RaycastHit hit, distance);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "out" not in luau or "-- " in luau

    def test_ref_param(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'void DoSomething(ref int x) {'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "ref" not in luau or "-- " in luau


class TestYieldReturn:
    def test_wait_for_seconds(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'yield return new WaitForSeconds(2.5f);'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "task.wait(" in luau
        assert "yield" not in luau

    def test_yield_null(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'yield return null;'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "task.wait()" in luau

    def test_yield_break(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'yield break;'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "return" in luau
        assert "yield" not in luau


class TestNullConditional:
    def test_property_access(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'var name = obj?.Name;'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "?" not in luau or "-- " in luau
        assert "obj" in luau
        assert "nil" in luau

    def test_null_coalescing_assignment(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'cache ??= new List<int>();'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "if cache == nil" in luau or "cache" in luau


class TestIsTypeCheck:
    def test_is_null(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'if (obj is null) return;'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "== nil" in luau
        assert " is " not in luau or "-- " in luau

    def test_is_not_null(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'if (obj is not null) DoSomething();'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "~= nil" in luau

    def test_is_type(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'if (comp is BoxCollider) return;'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "BoxCollider" in luau
        assert "typeof" in luau or "IsA" in luau


class TestStringInterpolation:
    def test_format_specifier_f2(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'string s = $"Value: {health:F2}";'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "%.2f" in luau
        assert "health" in luau

    def test_format_specifier_n0(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'string s = $"Score: {score:N0}";'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "%d" in luau

    def test_format_specifier_d3(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'string s = $"ID: {id:D3}";'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "%03d" in luau

    def test_format_specifier_x(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'string s = $"Hex: {val:X}";'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "%x" in luau

    def test_mixed_format(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'string s = $"HP: {hp:F1}/{maxHp}";'
        luau, _, _ = _rule_based_transpile(csharp)
        assert "%.1f" in luau
        assert "hp" in luau
        assert "maxHp" in luau


class TestPropertyBodies:
    def test_simple_getter(self):
        from converter.code_transpiler import _preprocess_multiline_constructs
        csharp = """public float Speed
{
    get { return m_Speed; }
}"""
        result = _preprocess_multiline_constructs(csharp)
        assert "local Speed" in result or "get_Speed" in result
        assert "m_Speed" in result
