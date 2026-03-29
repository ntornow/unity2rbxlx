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


class TestValidatorStructuralFixes:
    """Test structural syntax fixes: else if, ++/--, type decls, etc."""

    def test_else_if_to_elseif(self):
        from converter.luau_validator import validate_and_fix
        source = 'if x then\n    print(1)\nelse if y then\n    print(2)\nend'
        fixed, _ = validate_and_fix("test", source)
        assert 'elseif y then' in fixed
        assert 'else if' not in fixed

    def test_postfix_increment(self):
        from converter.luau_validator import validate_and_fix
        source = '    count++\n'
        fixed, _ = validate_and_fix("test", source)
        assert 'count = count + 1' in fixed
        assert '++' not in fixed

    def test_postfix_decrement(self):
        from converter.luau_validator import validate_and_fix
        source = '    health--\n'
        fixed, _ = validate_and_fix("test", source)
        assert 'health = health - 1' in fixed

    def test_csharp_type_declaration_with_init(self):
        from converter.luau_validator import validate_and_fix
        source = '    Vector3 dir = target - origin'
        fixed, _ = validate_and_fix("test", source)
        assert 'local dir = target - origin' in fixed
        assert 'Vector3' not in fixed

    def test_csharp_type_declaration_bare(self):
        from converter.luau_validator import validate_and_fix
        source = '    RaycastHit hit'
        fixed, _ = validate_and_fix("test", source)
        assert 'local hit = nil' in fixed

    def test_gameobject_to_script_parent(self):
        from converter.luau_validator import validate_and_fix
        source = 'gameObject:Destroy()'
        fixed, _ = validate_and_fix("test", source)
        assert 'script.Parent' in fixed

    def test_this_to_script_parent(self):
        from converter.luau_validator import validate_and_fix
        source = 'print(this)'
        fixed, _ = validate_and_fix("test", source)
        assert 'script.Parent' in fixed

    def test_iskeydowndown_fix(self):
        from converter.luau_validator import validate_and_fix
        source = 'UserInputService:IsKeyDownDown(Enum.KeyCode.W)'
        fixed, _ = validate_and_fix("test", source)
        assert 'IsKeyDown' in fixed
        assert 'IsKeyDownDown' not in fixed

    def test_timescale_fix(self):
        from converter.luau_validator import validate_and_fix
        source = 'workspace:GetServerTimeNow()Scale = 0'
        fixed, _ = validate_and_fix("test", source)
        assert 'GetServerTimeNow()Scale' not in fixed
        assert '_timeScale' in fixed

    def test_transform_removal(self):
        from converter.luau_validator import validate_and_fix
        source = 'local pos = obj.transform.position\nlocal fwd = obj.transform.forward'
        fixed, _ = validate_and_fix("test", source)
        assert '.transform' not in fixed
        assert '.Position' in fixed

    def test_attribute_stripping(self):
        from converter.luau_validator import validate_and_fix
        source = '    [Range(-45, -15)]\n    local angle = 0'
        fixed, _ = validate_and_fix("test", source)
        assert '[Range' not in fixed
        assert 'local angle = 0' in fixed

    def test_orphaned_destroy_simple(self):
        from converter.luau_validator import validate_and_fix
        source = '    .Destroy(obj)\n'
        fixed, _ = validate_and_fix("test", source)
        assert 'obj:Destroy()' in fixed

    def test_orphaned_destroy_with_delay(self):
        from converter.luau_validator import validate_and_fix
        source = '    .Destroy(obj, 0.5)\n'
        fixed, _ = validate_and_fix("test", source)
        assert 'Debris' in fixed
        assert 'AddItem' in fixed

    def test_orphaned_clone_with_position(self):
        from converter.luau_validator import validate_and_fix
        source = '    .Clone(prefab, pos, CFrame.new())\n'
        fixed, _ = validate_and_fix("test", source)
        assert 'prefab:Clone()' in fixed
        assert '.Parent = workspace' in fixed
        assert 'CFrame.new(pos)' in fixed

    def test_orphaned_clone_multiline(self):
        from converter.luau_validator import validate_and_fix
        source = '    .Clone(explosionPrefab, col.contacts[0].point,\n                CFrame.lookAt(col.contacts[0].normal))\n'
        fixed, _ = validate_and_fix("test", source)
        assert 'explosionPrefab:Clone()' in fixed

    def test_pitch_to_playbackspeed(self):
        from converter.luau_validator import validate_and_fix
        source = 'source.pitch = math.random(0.8, 1.2)'
        fixed, _ = validate_and_fix("test", source)
        assert 'PlaybackSpeed' in fixed
        assert '.pitch' not in fixed

    def test_addforce_to_applyimpulse(self):
        from converter.luau_validator import validate_and_fix
        source = 'rb:AddRelativeForce(Vector3.zAxis * force, ForceMode.Impulse)'
        fixed, _ = validate_and_fix("test", source)
        assert ':ApplyImpulse(' in fixed
        assert 'ForceMode' not in fixed

    def test_random_rotation(self):
        from converter.luau_validator import validate_and_fix
        source = 'local rot = Random.rotation'
        fixed, _ = validate_and_fix("test", source)
        assert 'CFrame.Angles' in fixed

    def test_stray_brace_to_end(self):
        from converter.luau_validator import validate_and_fix
        source = 'if x then\n    print(1)\n}'
        fixed, _ = validate_and_fix("test", source)
        assert 'end' in fixed
        assert '}' not in fixed

    def test_end_else_merge(self):
        from converter.luau_validator import validate_and_fix
        source = 'if x then\n    print(1)\nend\nelse\n    print(2)\nend'
        fixed, _ = validate_and_fix("test", source)
        assert 'end\nelse' not in fixed
        assert 'else\n' in fixed

    def test_other_to_otherpart(self):
        from converter.luau_validator import validate_and_fix
        source = 'function(otherPart)\n    if other.tag == "Player" then\n    end\nend'
        fixed, _ = validate_and_fix("test", source)
        # 'other' should be replaced with 'otherPart', and .tag → CollectionService:HasTag
        assert 'otherPart' in fixed
        assert 'HasTag' in fixed

    def test_dot_to_colon_playoneshot(self):
        from converter.luau_validator import validate_and_fix
        source = 'source.PlayOneShot(clip)'
        fixed, _ = validate_and_fix("test", source)
        assert 'source:Play()' in fixed  # PlayOneShot converted to :Play()

    def test_lowercase_parent_fix(self):
        from converter.luau_validator import validate_and_fix
        source = 'fire.parent = workspace'
        fixed, _ = validate_and_fix("test", source)
        assert '.Parent' in fixed

    def test_task_spawn_no_call(self):
        from converter.luau_validator import validate_and_fix
        source = 'task.spawn(DefaultUpdate())'
        fixed, _ = validate_and_fix("test", source)
        assert 'task.spawn(DefaultUpdate)' in fixed

    def test_task_spawn_with_args_wraps_closure(self):
        from converter.luau_validator import validate_and_fix
        source = 'task.spawn(EngagedUpdate(hit))'
        fixed, _ = validate_and_fix("test", source)
        assert 'function()' in fixed
        assert 'EngagedUpdate(hit)' in fixed

    def test_task_delay_arg_order(self):
        from converter.luau_validator import validate_and_fix
        source = 'task.delay(Explode", explodeTime)'
        fixed, _ = validate_and_fix("test", source)
        assert 'task.delay(explodeTime, Explode)' in fixed

    def test_hash_operator_fix(self):
        from converter.luau_validator import validate_and_fix
        source = 'if col.#contacts > 0 then\nend'
        fixed, _ = validate_and_fix("test", source)
        assert '#col.contacts' in fixed


    def test_double_dot_property_access_fix(self):
        """Double-dot from .gameObject/.transform removal becomes single dot."""
        from converter.luau_validator import validate_and_fix
        source = 'local dir = other..Position - tBase.Position'
        fixed, _ = validate_and_fix("test", source)
        assert 'other.Position' in fixed
        assert '..' not in fixed or '"..' in fixed  # no double-dot in property access

    def test_double_dot_preserves_string_concat(self):
        """String concat .. must NOT be changed to single dot."""
        from converter.luau_validator import validate_and_fix
        source = 'local msg = "hello" .. " " .. name'
        fixed, _ = validate_and_fix("test", source)
        assert '" .. "' in fixed  # concat preserved

    def test_undefined_part_receiver_touched(self):
        """Undefined 'part' in Touched handler → script.Parent."""
        from converter.luau_validator import validate_and_fix
        source = 'part.Touched:Connect(function(otherPart)\n    print(otherPart)\nend)'
        fixed, _ = validate_and_fix("test", source)
        assert 'script.Parent.Touched' in fixed

    def test_defined_part_receiver_not_changed(self):
        """If 'part' is locally defined, don't replace it."""
        from converter.luau_validator import validate_and_fix
        source = 'local part = workspace.Door\npart.Touched:Connect(function(otherPart)\n    print(otherPart)\nend)'
        fixed, _ = validate_and_fix("test", source)
        assert 'part.Touched' in fixed  # should NOT replace

    def test_operator_paren_fix(self):
        """==("value") → == "value"."""
        from converter.luau_validator import validate_and_fix
        source = 'if other:GetAttribute(\'Tag\') ==("Player") then\nend'
        fixed, _ = validate_and_fix("test", source)
        assert '== "Player"' in fixed
        assert '==("Player")' not in fixed

    def test_exists_to_table_find(self):
        """.Exists(pred) → table.find pattern."""
        from converter.luau_validator import validate_and_fix
        source = 'if items.Exists(x) then\nend'
        fixed, _ = validate_and_fix("test", source)
        assert 'table.find(items, x)' in fixed

    def test_local_inside_table_literal(self):
        """C# enum + class fields mixed in table → close table before locals."""
        from converter.luau_validator import validate_and_fix
        source = 'local State = {\n    Default = 0,\n    Engaged = 1,\n\n    local rotate = true\n    local speed = 125\n}'
        fixed, _ = validate_and_fix("test", source)
        # Table should close before local declarations (with -- enum marker)
        assert '} -- enum' in fixed, f"Table should have '}} -- enum' closing, got:\n{fixed}"
        # local declarations should come after the table close
        assert 'local rotate = true' in fixed
        # The stale } at the end should be removed (only the -- enum one remains)
        lines = fixed.split('\n')
        brace_lines = [l for l in lines if '}' in l]
        assert len(brace_lines) == 1, f"Should have exactly one line with }}, got {brace_lines}"

    def test_bare_method_in_for_in(self):
        """for _, t in :GetDescendants do → script.Parent:GetDescendants()."""
        from converter.luau_validator import validate_and_fix
        source = 'for _, t in :GetDescendants do\n    print(t)\nend'
        fixed, _ = validate_and_fix("test", source)
        assert 'script.Parent:GetDescendants()' in fixed

    def test_method_without_parens_in_for_in(self):
        """for _, t in obj:GetChildren do → obj:GetChildren()."""
        from converter.luau_validator import validate_and_fix
        source = 'for _, t in obj:GetChildren do\n    print(t)\nend'
        fixed, _ = validate_and_fix("test", source)
        assert 'obj:GetChildren()' in fixed

    def test_extra_paren_after_table(self):
        """originalStructure = {}) → originalStructure = {}."""
        from converter.luau_validator import validate_and_fix
        source = 'originalStructure = {})'
        fixed, _ = validate_and_fix("test", source)
        assert fixed.strip() == 'originalStructure = {}'

    def test_forward_reference(self):
        """local hasKey = gotKey (where gotKey defined later) → nil."""
        from converter.luau_validator import validate_and_fix
        source = 'local hasKey = gotKey\nlocal gotKey = nil'
        fixed, _ = validate_and_fix("test", source)
        assert 'hasKey = nil' in fixed
        assert 'forward ref' in fixed

    def test_bare_method_in_for_in_with_malformed_parens(self):
        """for _, t in :Method( do) → script.Parent:Method() do."""
        from converter.luau_validator import validate_and_fix
        source = 'for _, t in :GetDescendants( do)\n    print(t)\nend'
        fixed, _ = validate_and_fix("test", source)
        assert 'script.Parent:GetDescendants() do' in fixed

    def test_list_init_nested_parens(self):
        """--[[ new List ]] (obj:GetDescendants()) → {} (not {})."""
        from converter.luau_validator import validate_and_fix
        source = 'x = --[[ new List ]] (script.Parent:GetDescendants())'
        fixed, _ = validate_and_fix("test", source)
        assert '= {}' in fixed
        assert '})' not in fixed

    def test_table_close_preserved(self):
        """Table-closing } should not be converted to end."""
        from converter.luau_validator import validate_and_fix
        source = 'local State = {\n    A = 0,\n    B = 1,\n}\nlocal x = 1'
        fixed, _ = validate_and_fix("test", source)
        assert '}' in fixed, "Table closing } should be preserved"
        assert 'A = 0' in fixed

    def test_csharp_for_loop_custom_step(self):
        """for (local i = 0; i < N; i = i + step) → for i = 0, N - 1, step do."""
        from converter.luau_validator import validate_and_fix
        source = '    for (local i = 0; i < #data; i = i + channels)'
        fixed, _ = validate_and_fix("test", source)
        assert 'for i = 0, #data - 1, channels do' in fixed

    def test_csharp_for_loop_decrement_no_local(self):
        """for (i = N; i > 0; i--) → for i = N, 1, -1 do."""
        from converter.luau_validator import validate_and_fix
        source = '    for (i = #samples - 1; i > 0; i--)'
        fixed, _ = validate_and_fix("test", source)
        assert 'for i = #samples - 1, 0 + 1, -1 do' in fixed

    def test_csharp_for_fallback_to_while(self):
        """Complex for-loop → while loop fallback."""
        from converter.luau_validator import validate_and_fix
        source = '    for (local j = entryOffsets[i]; ; j++)'
        fixed, _ = validate_and_fix("test", source)
        # Should have been converted to while loop or similar
        assert 'for (' not in fixed

    def test_embedded_comment_in_variable_name(self):
        """local m_Ignore-- comment: text = true → local m_Ignore = true."""
        from converter.luau_validator import validate_and_fix
        source = '    local m_IgnoreAgent-- NavMeshAgent: use PathfindingService = true'
        fixed, _ = validate_and_fix("test", source)
        assert 'local m_IgnoreAgent = true' in fixed

    def test_incomplete_constructor_to_table(self):
        """nil -- (constructor removed) + table entries → { entries }."""
        from converter.luau_validator import validate_and_fix
        source = '    local msg = nil -- (constructor removed)\n        key1 = val1,\n        key2 = val2\n    }'
        fixed, _ = validate_and_fix("test", source)
        assert '= {' in fixed
        assert 'key1 = val1' in fixed

    def test_unity_input_jump_to_space(self):
        """IsKeyDown("Jump") → IsKeyDown(Enum.KeyCode.Space)."""
        from converter.luau_validator import validate_and_fix
        source = 'if UserInputService:IsKeyDown("Jump") then'
        fixed, _ = validate_and_fix("test", source)
        assert 'Enum.KeyCode.Space' in fixed
        assert '"Jump"' not in fixed

    def test_unity_input_fire_to_mouse(self):
        """IsKeyDown("Fire") → IsMouseButtonPressed(Enum.UserInputType.MouseButton1)."""
        from converter.luau_validator import validate_and_fix
        source = 'if UserInputService:IsKeyDown("Fire") then'
        fixed, _ = validate_and_fix("test", source)
        assert 'IsMouseButtonPressed' in fixed
        assert '"Fire"' not in fixed


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


class TestAsyncAwait:
    """Tests for async/await conversion."""

    def test_async_task_stripped(self):
        from converter.code_transpiler import _preprocess_async_await
        csharp = "async Task DoSomething() {"
        result = _preprocess_async_await(csharp)
        assert "async" not in result
        assert "Task" not in result
        assert "DoSomething" in result

    def test_await_task_delay(self):
        from converter.code_transpiler import _preprocess_async_await
        csharp = "await Task.Delay(1000);"
        result = _preprocess_async_await(csharp)
        assert "task.wait(1.0)" in result or "task.wait(1000 / 1000)" in result

    def test_await_task_yield(self):
        from converter.code_transpiler import _preprocess_async_await
        csharp = "await Task.Yield();"
        result = _preprocess_async_await(csharp)
        assert "task.wait()" in result

    def test_await_generic_stripped(self):
        from converter.code_transpiler import _preprocess_async_await
        csharp = "var result = await SomeAsyncMethod();"
        result = _preprocess_async_await(csharp)
        assert "await" not in result
        assert "SomeAsyncMethod()" in result

    def test_async_void_stripped(self):
        from converter.code_transpiler import _preprocess_async_await
        csharp = "async void OnButtonClick() {"
        result = _preprocess_async_await(csharp)
        assert "async" not in result
        assert "void" not in result
        assert "OnButtonClick" in result


class TestConditionalCompilation:
    """Tests for #if UNITY_EDITOR stripping."""

    def test_strip_unity_editor_block(self):
        from converter.code_transpiler import _preprocess_conditional_compilation
        csharp = """int x = 1;
#if UNITY_EDITOR
int editorOnly = 2;
Debug.Log("editor");
#endif
int y = 3;"""
        result = _preprocess_conditional_compilation(csharp)
        assert "editorOnly" not in result
        assert "x = 1" in result
        assert "y = 3" in result

    def test_keep_else_block(self):
        from converter.code_transpiler import _preprocess_conditional_compilation
        csharp = """#if UNITY_EDITOR
EditorDoSomething();
#else
RuntimeDoSomething();
#endif"""
        result = _preprocess_conditional_compilation(csharp)
        assert "EditorDoSomething" not in result
        assert "RuntimeDoSomething" in result

    def test_nested_ifdefs(self):
        from converter.code_transpiler import _preprocess_conditional_compilation
        csharp = """#if UNITY_EDITOR
#if UNITY_EDITOR_WIN
WinOnly();
#endif
EditorCode();
#endif
RuntimeCode();"""
        result = _preprocess_conditional_compilation(csharp)
        assert "WinOnly" not in result
        assert "EditorCode" not in result
        assert "RuntimeCode" in result

    def test_negated_ifdef_keeps_block(self):
        from converter.code_transpiler import _preprocess_conditional_compilation
        csharp = """Begin();
#if !UNITY_EDITOR
RuntimeCode();
#else
EditorCode();
#endif
End();"""
        result = _preprocess_conditional_compilation(csharp)
        assert "RuntimeCode" in result
        assert "EditorCode" not in result
        assert "Begin" in result
        assert "End" in result

    def test_keep_unknown_symbols(self):
        from converter.code_transpiler import _preprocess_conditional_compilation
        csharp = """#if MY_CUSTOM_FLAG
CustomCode();
#endif"""
        result = _preprocess_conditional_compilation(csharp)
        assert "CustomCode" in result


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


class TestGenericTypeDeclarations:
    """Test that generic/custom type variable declarations get 'local' prefix."""

    def test_generic_list_type(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile("List<int> numbers = {1, 2, 3};")
        assert "local numbers" in luau

    def test_generic_dictionary_type(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile("Dictionary<string, int> map = {};")
        assert "local map" in luau

    def test_custom_class_type(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile("PlayerController controller = GetComponent();")
        assert "local controller" in luau

    def test_does_not_match_keywords(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile("return value")
        assert "local value" not in luau


class TestComplexForLoops:
    """Test enhanced for-loop pattern matching."""

    def test_expression_init_for(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile("for (int i = arr.Length - 1; i < count; i++)")
        assert "for i = arr.Length - 1" in luau or "for i =" in luau

    def test_step_for(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile("for (int i = 0; i < 10; i += 2)")
        assert "2 do" in luau

    def test_negative_step_for(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile("for (int i = 10; i > 0; i -= 2)")
        assert "-2 do" in luau

    def test_expression_decrement_for(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile("for (int i = items.Count - 1; i >= 0; i--)")
        assert "for i = items.Count - 1" in luau or "for i =" in luau
        assert "-1 do" in luau


class TestTernaryOperator:
    """Test ternary operator conversion."""

    def test_simple_ternary(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile("result = isReady ? value1 : value2;")
        assert "if" in luau and "then" in luau and "else" in luau

    def test_parenthesized_condition_ternary(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile("result = (a > b) ? x : y;")
        assert "if" in luau and "then" in luau and "else" in luau


class TestMultiLineLambda:
    """Test multi-line lambda expression conversion."""

    def test_block_lambda_multi_param(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile("items.ForEach((x, y) => {")
        assert "function(x, y)" in luau
        assert "=>" not in luau

    def test_block_lambda_single_param(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile("items.ForEach(item => {")
        assert "function(item)" in luau
        assert "=>" not in luau

    def test_multi_param_expression_lambda(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile("items.Sort((a, b) => a.Name)")
        assert "function(a, b)" in luau


class TestStringGsubEscaping:
    """Test string.gsub pattern escaping in validator."""

    def test_dot_escaping(self):
        from converter.luau_validator import _fix_csharp_remnants
        source = 'local result = string.gsub(str, ".", "_")'
        fixes = []
        result = _fix_csharp_remnants("test", source, fixes)
        assert '"%."' in result

    def test_no_escaping_for_plain(self):
        from converter.luau_validator import _fix_csharp_remnants
        source = 'local result = string.gsub(str, "hello", "world")'
        fixes = []
        result = _fix_csharp_remnants("test", source, fixes)
        assert '"hello"' in result

    def test_no_double_escaping_lua_patterns(self):
        """Lua pattern classes like %s, %d should NOT be escaped."""
        from converter.luau_validator import _fix_csharp_remnants
        # Leading whitespace pattern
        source = 'local result = string.gsub(name, "^%s+", "")'
        fixes = []
        result = _fix_csharp_remnants("test", source, fixes)
        assert '"^%s+"' in result  # Should be preserved, not double-escaped

    def test_no_double_escaping_trailing_whitespace(self):
        """Trailing whitespace pattern %s+$ should be preserved."""
        from converter.luau_validator import _fix_csharp_remnants
        source = 'local result = string.gsub(name, "%s+$", "")'
        fixes = []
        result = _fix_csharp_remnants("test", source, fixes)
        assert '"%s+$"' in result  # Should be preserved

    def test_no_double_escaping_digit_pattern(self):
        """Digit pattern %d+ should be preserved."""
        from converter.luau_validator import _fix_csharp_remnants
        source = 'local result = string.gsub(str, "%d+", "NUM")'
        fixes = []
        result = _fix_csharp_remnants("test", source, fixes)
        assert '"%d+"' in result


class TestGenericTypeStripping:
    """Test C# generic type parameter stripping in validator."""

    def test_getcomponent_generic(self):
        from converter.luau_validator import _fix_csharp_remnants
        source = 'local rb = obj:GetComponent<Rigidbody>()'
        fixes = []
        result = _fix_csharp_remnants("test", source, fixes)
        assert ':FindFirstChildWhichIsA("Rigidbody")' in result

    def test_find_first_child_of_class_generic(self):
        from converter.luau_validator import _fix_csharp_remnants
        source = 'local player = other:FindFirstChildOfClass<Player>()'
        fixes = []
        result = _fix_csharp_remnants("test", source, fixes)
        assert ':FindFirstChildWhichIsA("Player")' in result

    def test_generic_method_call(self):
        from converter.luau_validator import _fix_csharp_remnants
        source = 'local items = GetAll<ItemData>()'
        fixes = []
        result = _fix_csharp_remnants("test", source, fixes)
        assert 'GetAll(' in result
        assert '<ItemData>' not in result


class TestBareReceiverFix:
    """Test bare :Method() and .Property without receiver."""

    def test_bare_method_call(self):
        from converter.luau_validator import _fix_csharp_remnants
        source = 'local snd = :FindFirstChildWhichIsA("Sound")'
        fixes = []
        result = _fix_csharp_remnants("test", source, fixes)
        assert 'script.Parent:FindFirstChildWhichIsA("Sound")' in result

    def test_bare_method_in_return(self):
        from converter.luau_validator import _fix_csharp_remnants
        source = 'return :FindFirstChild("Weapon")'
        fixes = []
        result = _fix_csharp_remnants("test", source, fixes)
        assert 'script.Parent:FindFirstChild("Weapon")' in result

    def test_bare_method_indented(self):
        from converter.luau_validator import _fix_csharp_remnants
        source = '        :FindFirstChildWhichIsA("Sound"):Play()'
        fixes = []
        result = _fix_csharp_remnants("test", source, fixes)
        assert 'script.Parent:FindFirstChildWhichIsA("Sound")' in result


class TestSetActiveConversion:
    """Test SetActive → recursive setActive utility function."""

    def test_setactive_in_validator(self):
        from converter.luau_validator import _fix_csharp_remnants
        source = 'obj.SetActive(false)'
        fixes = []
        result = _fix_csharp_remnants("test", source, fixes)
        assert 'setActive(obj, false)' in result

    def test_setactive_chained(self):
        from converter.luau_validator import _fix_csharp_remnants
        source = 'part.Parent.SetActive(true)'
        fixes = []
        result = _fix_csharp_remnants("test", source, fixes)
        assert 'setActive(part.Parent, true)' in result

    def test_utility_injection(self):
        from converter.luau_validator import _inject_utility_functions
        source = 'local x = 1\nsetActive(obj, false)\n'
        fixes = []
        result = _inject_utility_functions("test", source, fixes)
        assert 'local function setActive(' in result
        assert 'setActive(obj, false)' in result

    def test_no_injection_when_defined(self):
        from converter.luau_validator import _inject_utility_functions
        source = 'local function setActive(instance, active)\nend\nsetActive(obj, false)\n'
        fixes = []
        result = _inject_utility_functions("test", source, fixes)
        # Should not inject a second copy
        assert result.count('local function setActive(') == 1


class TestToStringWithFormat:
    """Test ToString with format specifier conversion."""

    def test_tostring_format_f2(self):
        from converter.luau_validator import _fix_csharp_remnants
        source = 'local s = value.ToString("F2")'
        fixes = []
        result = _fix_csharp_remnants("test", source, fixes)
        assert 'string.format("%.2f", value)' in result

    def test_tostring_plain(self):
        from converter.luau_validator import _fix_csharp_remnants
        source = 'local s = value.ToString()'
        fixes = []
        result = _fix_csharp_remnants("test", source, fixes)
        assert 'tostring(value)' in result


class TestStringFormatPlaceholders:
    """Test C# string.Format positional placeholder conversion."""

    def test_positional_placeholders(self):
        from converter.luau_validator import _fix_csharp_remnants
        source = 'local s = string.format("{0} has {1} items", name, count)'
        fixes = []
        result = _fix_csharp_remnants("test", source, fixes)
        assert '"%s has %s items"' in result

    def test_no_false_positive(self):
        from converter.luau_validator import _fix_csharp_remnants
        source = 'local s = string.format("%s has %d items", name, count)'
        fixes = []
        result = _fix_csharp_remnants("test", source, fixes)
        assert '"%s has %d items"' in result


class TestCollectionMethodFixes:
    """Test that C# collection methods are properly converted to Luau table operations."""

    def test_list_add(self):
        from converter.luau_validator import validate_and_fix
        source = 'inventory.Add(item)'
        fixed, fixes = validate_and_fix("test", source)
        assert "table.insert(inventory, item)" in fixed

    def test_list_remove(self):
        from converter.luau_validator import validate_and_fix
        source = 'items.Remove(target)'
        fixed, fixes = validate_and_fix("test", source)
        assert "table.remove(items, table.find(items, target))" in fixed

    def test_list_remove_at(self):
        from converter.luau_validator import validate_and_fix
        source = 'items.RemoveAt(idx)'
        fixed, fixes = validate_and_fix("test", source)
        assert "table.remove(items, idx + 1)" in fixed

    def test_list_insert(self):
        from converter.luau_validator import validate_and_fix
        source = 'items.Insert(0, newItem)'
        fixed, fixes = validate_and_fix("test", source)
        assert "table.insert(items, 0 + 1, newItem)" in fixed

    def test_list_index_of(self):
        from converter.luau_validator import validate_and_fix
        source = 'local idx = items.IndexOf(target)'
        fixed, fixes = validate_and_fix("test", source)
        assert "table.find(items, target)" in fixed

    def test_add_multiple_in_sequence(self):
        from converter.luau_validator import validate_and_fix
        source = 'inventory.Add(rock)\ninventory.Add(sweet)\ninventory.Add(item)'
        fixed, fixes = validate_and_fix("test", source)
        assert fixed.count("table.insert(inventory,") == 3
        assert ".Add(" not in fixed

    def test_dict_add_two_args(self):
        from converter.luau_validator import validate_and_fix
        source = 'dict.Add(key, value)'
        fixed, fixes = validate_and_fix("test", source)
        assert "dict[key] = value" in fixed
        assert "table.insert" not in fixed

    def test_null_invoke_has_end(self):
        from converter.luau_validator import validate_and_fix
        source = 'myEvent?.Invoke(arg1, arg2)'
        fixed, fixes = validate_and_fix("test", source)
        assert "if myEvent then" in fixed
        assert ":Fire(arg1, arg2)" in fixed
        assert "end" in fixed


class TestExpandedTranspilerFeatures:
    """Test newly added transpiler features: casts, typeof, using, lock, default."""

    def test_uint_cast_stripped(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile('local x = (uint)value;')
        assert '(uint)' not in luau
        assert 'value' in luau

    def test_byte_cast_stripped(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile('local b = (byte)data;')
        assert '(byte)' not in luau

    def test_unity_type_cast_stripped(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile('local t = (Transform)component;')
        assert '(Transform)' not in luau

    def test_typeof_class_to_string(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile('local t = typeof(PlayerController);')
        assert '"PlayerController"' in luau
        assert 'typeof(' not in luau

    def test_typeof_preserves_luau_typeof(self):
        """Luau's typeof(obj) (lowercase argument) should not be converted."""
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile('if typeof(obj) == "string" then')
        # Luau typeof with lowercase arg should remain
        assert 'typeof(obj)' in luau

    def test_default_type(self):
        from converter.code_transpiler import _rule_based_transpile
        luau, _, _ = _rule_based_transpile('local v = default(Vector3);')
        assert 'nil' in luau
        assert 'default(' not in luau

    def test_using_block_stripped(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'using (var stream = new FileStream("a.txt")) {\nstream.Read();\n}'
        luau, _, _ = _rule_based_transpile(csharp)
        assert 'using' not in luau

    def test_lock_block_stripped(self):
        from converter.code_transpiler import _rule_based_transpile
        csharp = 'lock (syncRoot) {\nx = 1;\n}'
        luau, _, _ = _rule_based_transpile(csharp)
        assert 'lock' not in luau
        assert 'x = 1' in luau


class TestDependencyInjection:
    """Test cross-script require() injection."""

    def test_inject_require_calls(self):
        from converter.script_coherence import inject_require_calls
        from core.roblox_types import RbxScript
        script_a = RbxScript(
            name="PlayerController",
            source="local Players = game:GetService('Players')\nlocal speed = 10\n",
            script_type="Script",
        )
        script_b = RbxScript(
            name="InputReader",
            source="local UIS = game:GetService('UserInputService')\nlocal InputReader = {}\nreturn InputReader\n",
            script_type="ModuleScript",
        )
        dep_map = {"PlayerController": ["InputReader"]}
        injected = inject_require_calls([script_a, script_b], dep_map)
        assert injected == 1
        assert 'require(' in script_a.source
        assert 'InputReader' in script_a.source

    def test_inject_reclassifies_target(self):
        from converter.script_coherence import inject_require_calls
        from core.roblox_types import RbxScript
        script_a = RbxScript(name="A", source="local x = 1\n", script_type="Script")
        script_b = RbxScript(name="B", source="local y = 2\n", script_type="Script")
        dep_map = {"A": ["B"]}
        inject_require_calls([script_a, script_b], dep_map)
        assert script_b.script_type == "ModuleScript"
        assert "return B" in script_b.source

    def test_no_self_require(self):
        from converter.script_coherence import inject_require_calls
        from core.roblox_types import RbxScript
        script_a = RbxScript(name="A", source="local x = 1\n", script_type="Script")
        dep_map = {"A": ["A"]}
        injected = inject_require_calls([script_a], dep_map)
        assert injected == 0

    def test_analyzer_extracts_references(self):
        from unity.script_analyzer import analyze_script
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.cs', delete=False) as f:
            f.write('public class Foo : MonoBehaviour {\n')
            f.write('  private InputReader _reader;\n')
            f.write('  private QuestManager _quests;\n')
            f.write('  void Start() { }\n')
            f.write('}\n')
            f.flush()
            info = analyze_script(f.name)
        os.unlink(f.name)
        assert 'InputReader' in info.referenced_types
        assert 'QuestManager' in info.referenced_types
        assert 'MonoBehaviour' not in info.referenced_types
        assert 'Foo' not in info.referenced_types


class TestScriptToPartBinding:
    """Test that script-to-part binding works for scene node MonoBehaviours."""

    def test_pipeline_binding_runs(self):
        """The _bind_scripts_to_parts method should exist and be callable."""
        from converter.pipeline import Pipeline
        assert hasattr(Pipeline, '_bind_scripts_to_parts')


class TestZeroBasedLoopFix:
    """Test 0-based for loop → 1-based conversion."""

    def test_simple_zero_based_loop(self):
        from converter.luau_validator import validate_and_fix
        source = '    for i = 0, #items - 1 do\n        items[i] = 1\n    end'
        fixed, fixes = validate_and_fix("test", source)
        assert 'for i = 1, #items do' in fixed

    def test_numeric_zero_based_loop(self):
        from converter.luau_validator import validate_and_fix
        source = '    for i = 0, 4 - 1 do\n        arr[i] = 1\n    end'
        fixed, fixes = validate_and_fix("test", source)
        assert 'for i = 1, 4 do' in fixed

    def test_preserves_non_zero_loop(self):
        from converter.luau_validator import validate_and_fix
        source = '    for i = 1, #items do\n        items[i] = 1\n    end'
        fixed, _ = validate_and_fix("test", source)
        assert 'for i = 1, #items do' in fixed


class TestBareReceiverFixes:
    """Test bare receiver → script.Parent fixes."""

    def test_bare_property_at_line_start(self):
        from converter.luau_validator import validate_and_fix
        source = '    .Position = Vector3.new(1, 2, 3)'
        fixed, _ = validate_and_fix("test", source)
        assert 'script.Parent.Position' in fixed

    def test_bare_property_after_operator(self):
        from converter.luau_validator import validate_and_fix
        source = '    local x = .Position.y'
        fixed, _ = validate_and_fix("test", source)
        assert 'script.Parent.Position' in fixed

    def test_while_bare_receiver(self):
        from converter.luau_validator import validate_and_fix
        source = '    while .Position.y > 5 do\n        task.wait()\n    end'
        fixed, _ = validate_and_fix("test", source)
        assert 'while script.Parent.Position.Y' in fixed


class TestUnassignedCFrameFix:
    """Test unassigned CFrame expressions → proper mutations."""

    def test_cframe_angles_standalone(self):
        from converter.luau_validator import validate_and_fix
        source = '        CFrame.Angles(Vector3.yAxis * dt * speed)'
        fixed, _ = validate_and_fix("test", source)
        assert 'script.Parent.CFrame = script.Parent.CFrame * CFrame.Angles(' in fixed

    def test_cframe_new_standalone(self):
        from converter.luau_validator import validate_and_fix
        source = '        CFrame.new(-Vector3.yAxis / 2 * dt)'
        fixed, _ = validate_and_fix("test", source)
        assert 'script.Parent.CFrame = script.Parent.CFrame + ' in fixed

    def test_cframe_angles_in_assignment_preserved(self):
        from converter.luau_validator import validate_and_fix
        source = '        local rot = CFrame.Angles(1, 2, 3)'
        fixed, _ = validate_and_fix("test", source)
        assert 'local rot = CFrame.Angles(' in fixed


class TestKeyCodeFix:
    """Test KeyCode.X → Enum.KeyCode.X conversion."""

    def test_keycode_escape(self):
        from converter.luau_validator import validate_and_fix
        source = 'if UserInputService:IsKeyDown(KeyCode.Escape) then'
        fixed, _ = validate_and_fix("test", source)
        assert 'Enum.KeyCode.Escape' in fixed

    def test_enum_keycode_not_doubled(self):
        from converter.luau_validator import validate_and_fix
        source = 'if UserInputService:IsKeyDown(Enum.KeyCode.Escape) then'
        fixed, _ = validate_and_fix("test", source)
        assert 'Enum.Enum.KeyCode' not in fixed

    def test_multiple_keycodes(self):
        from converter.luau_validator import validate_and_fix
        source = 'KeyCode.F1 KeyCode.F2 KeyCode.Space'
        fixed, _ = validate_and_fix("test", source)
        assert fixed.count('Enum.KeyCode') == 3


class TestListInitFix:
    """Test list initialization fix."""

    def test_new_list_comment_to_table(self):
        from converter.luau_validator import validate_and_fix
        source = 'local items = --[[ new List ]] (4)'
        fixed, _ = validate_and_fix("test", source)
        assert 'local items = {}' in fixed


class TestDottedPathContains:
    """Test that dotted paths work correctly with .Contains() etc."""

    def test_dotted_contains(self):
        from converter.luau_validator import validate_and_fix
        source = 'player.hasItems.Contains(itemName)'
        fixed, _ = validate_and_fix("test", source)
        assert 'table.find(player.hasItems, itemName)' in fixed

    def test_dotted_add(self):
        from converter.luau_validator import validate_and_fix
        source = 'player.items.Add(item)'
        fixed, _ = validate_and_fix("test", source)
        assert 'table.insert(player.items, item)' in fixed


class TestMissingEndInsertion:
    """Test insertion of missing 'end' for single-statement if blocks."""

    def test_if_return_missing_end(self):
        from converter.luau_validator import validate_and_fix
        source = '    if x then\n        return\n\n    y = 1'
        fixed, _ = validate_and_fix("test", source)
        # Should have 'end' between return and y = 1
        lines = fixed.split('\n')
        found_end_after_return = False
        saw_return = False
        for line in lines:
            if 'return' in line:
                saw_return = True
            if saw_return and line.strip() == 'end':
                found_end_after_return = True
                break
        assert found_end_after_return

    def test_if_single_statement_missing_end(self):
        from converter.luau_validator import validate_and_fix
        source = '    if x > 5 then\n        x = 5\n\n    y = x + 1'
        fixed, _ = validate_and_fix("test", source)
        lines = fixed.split('\n')
        found_end = False
        for line in lines:
            if line.strip() == 'end':
                found_end = True
                break
        assert found_end


class TestPositionCasingFix:
    """Test .position → .Position PascalCase conversion."""

    def test_position_lowercase(self):
        from converter.luau_validator import validate_and_fix
        source = 'local pos = obj.position'
        fixed, _ = validate_and_fix("test", source)
        assert 'obj.Position' in fixed

    def test_name_lowercase(self):
        from converter.luau_validator import validate_and_fix
        source = 'local n = part.name'
        fixed, _ = validate_and_fix("test", source)
        assert 'part.Name' in fixed

    def test_workspace_current_camera(self):
        from converter.luau_validator import validate_and_fix
        source = 'workspace.Current:ScreenPointToRay(args)'
        fixed, _ = validate_and_fix("test", source)
        assert 'workspace.CurrentCamera:ScreenPointToRay' in fixed


class TestCommentEmbeddedVarFix:
    """Test fixing comment-embedded variable names from type mapping."""

    def test_m_navmeshagent_assignment(self):
        from converter.luau_validator import validate_and_fix
        source = 'm_-- NavMeshAgent: use Roblox PathfindingService = script.Parent:FindFirstChildOfClass()'
        fixed, _ = validate_and_fix("test", source)
        assert '_agent' in fixed
        assert 'm_--' not in fixed

    def test_m_navmeshagent_property_access(self):
        from converter.luau_validator import validate_and_fix
        source = 'm_-- NavMeshAgent: use Roblox PathfindingService.speed = 5'
        fixed, _ = validate_and_fix("test", source)
        assert '_agent.speed' in fixed

    def test_broken_generic_angle_bracket(self):
        from converter.luau_validator import validate_and_fix
        source = 'x = script.Parent:FindFirstChildOfClass<-- NavMeshAgent: use Roblox PathfindingService>()'
        fixed, _ = validate_and_fix("test", source)
        assert '<--' not in fixed
        assert 'FindFirstChildOfClass("BasePart")' in fixed

    def test_brace_comment_block(self):
        from converter.luau_validator import validate_and_fix
        source = '{--ignore damage if already dead\n    return\nend'
        fixed, _ = validate_and_fix("test", source)
        assert '{--' not in fixed
        assert '--ignore' in fixed

    def test_try_method_syntax(self):
        from converter.luau_validator import validate_and_fix
        source = 'if obj.Try:FindFirstChild("x") then'
        fixed, _ = validate_and_fix("test", source)
        assert '.Try:' not in fixed
        assert ':FindFirstChild' in fixed

    def test_ref_parameter_stripped(self):
        from converter.luau_validator import validate_and_fix
        source = 'SetVelocity(ref velocity.x, input.x)'
        fixed, _ = validate_and_fix("test", source)
        assert 'ref ' not in fixed
        assert 'velocity.X' in fixed

    def test_tuple_unpacking(self):
        from converter.luau_validator import validate_and_fix
        source = '(float absVel, float absInput) = (math.abs(x), math.abs(y))'
        fixed, _ = validate_and_fix("test", source)
        assert 'local absVel, absInput' in fixed
        assert 'float' not in fixed


class TestTagComparison:
    """Test .tag == → CollectionService:HasTag() conversion."""

    def test_tag_equals(self):
        from converter.luau_validator import validate_and_fix
        source = 'if obj.tag == "Player" then'
        fixed, _ = validate_and_fix("test", source)
        assert 'HasTag(obj, "Player")' in fixed
        assert '.tag ==' not in fixed

    def test_tag_not_equals(self):
        from converter.luau_validator import validate_and_fix
        source = 'if obj.tag ~= "Enemy" then'
        fixed, _ = validate_and_fix("test", source)
        assert 'not game:GetService("CollectionService"):HasTag(obj, "Enemy")' in fixed

    def test_compare_tag(self):
        from converter.luau_validator import validate_and_fix
        source = 'if obj.CompareTag("Player") then'
        fixed, _ = validate_and_fix("test", source)
        assert 'HasTag(obj, "Player")' in fixed


class TestBlockBalanceFix:
    """Test stack-based block balance fix for missing end keywords."""

    def test_nested_if_missing_end(self):
        from converter.luau_validator import validate_and_fix
        source = '''script.Parent.Touched:Connect(function(otherPart)
        if otherPart.tag == "Player" then
            if otherPart.hasKey then
                ToggleDoor(true)
    end'''
        fixed, _ = validate_and_fix("test", source)
        # Should have 3 'end' keywords (inner if, outer if, function)
        assert fixed.count('end') >= 3

    def test_function_with_if_elseif_missing_end(self):
        from converter.luau_validator import validate_and_fix
        source = '''    local function Bypass()
        if x then
            a = 1
        elseif y then
            b = 2
    end'''
        fixed, _ = validate_and_fix("test", source)
        # Should have separate end for if chain and function
        assert fixed.count('end') >= 2

    def test_single_statement_if_missing_end(self):
        from converter.luau_validator import validate_and_fix
        source = '''    local function Test()
        if x > 5 then
            x = 5
        y = x + 1
    end'''
        fixed, _ = validate_and_fix("test", source)
        # The 'if' should get its own 'end' before 'y = x + 1'
        assert fixed.count('end') >= 2

    def test_balanced_code_unchanged(self):
        from converter.luau_validator import validate_and_fix
        source = '''if x then
    y = 1
end'''
        fixed, _ = validate_and_fix("test", source)
        assert fixed.count('end') == 1


class TestPhysicsFixes:
    """Test Rigidbody/physics conversion fixes."""

    def test_attached_rigidbody_removed(self):
        from converter.luau_validator import validate_and_fix
        source = 'if col.attachedRigidbody ~= nil then'
        fixed, _ = validate_and_fix("test", source)
        assert 'attachedRigidbody' not in fixed
        assert 'col ~= nil' in fixed

    def test_velocity_to_assembly(self):
        from converter.luau_validator import validate_and_fix
        source = 'local vel = obj.velocity'
        fixed, _ = validate_and_fix("test", source)
        assert 'AssemblyLinearVelocity' in fixed

    def test_add_force_at_position(self):
        from converter.luau_validator import validate_and_fix
        source = 'rb.AddForceAtPosition(force, pos)'
        fixed, _ = validate_and_fix("test", source)
        assert ':ApplyImpulseAtPosition(' in fixed

    def test_collider_removed(self):
        from converter.luau_validator import validate_and_fix
        source = 'local tag = hit.collider.Name'
        fixed, _ = validate_and_fix("test", source)
        assert '.collider' not in fixed
        assert 'hit.Name' in fixed


class TestValidatorNewFixes:
    """Test new validator fixes for script quality issues."""

    def test_double_dot_bracket_access(self):
        """Fix ]..Property → ].Property (array access + property)."""
        from converter.luau_validator import validate_and_fix
        source = 'local pos = m_Cols[n]..Position'
        fixed, _ = validate_and_fix("test", source)
        assert ']..Position' not in fixed
        assert '].Position' in fixed

    def test_debris_additem_missing_object(self):
        """Debris:AddItem( time) → Debris:AddItem(script.Parent, time)."""
        from converter.luau_validator import validate_and_fix
        source = 'game:GetService("Debris"):AddItem( fadeTime)'
        fixed, _ = validate_and_fix("test", source)
        assert 'script.Parent, fadeTime' in fixed

    def test_broken_ternary_comparison(self):
        """expr > (if VALUE then A else B) → (if expr > VALUE then A else B)."""
        from converter.luau_validator import validate_and_fix
        source = 'rotateDir = x > (if 0.5 then Vector3.yAxis else -Vector3.yAxis)'
        fixed, _ = validate_and_fix("test", source)
        assert '(if x > 0.5 then' in fixed

    def test_broken_ternary_misplaced_paren(self):
        """func(args, (if VAL) > COMP then A else B) → (if func(args, VAL) > COMP then A else B)."""
        from converter.luau_validator import validate_and_fix
        source = 'rotateDir = math.random(0, (if 1) > 0.5 then Vector3.yAxis else -Vector3.yAxis)'
        fixed, _ = validate_and_fix("test", source)
        assert '(if math.random(0, 1) > 0.5 then' in fixed

    def test_getchildren_call_indexing(self):
        """GetChildren()(0) → GetChildren()[1] (0-based to 1-based)."""
        from converter.luau_validator import validate_and_fix
        source = 'local child = script.Parent:GetChildren()(0)'
        fixed, _ = validate_and_fix("test", source)
        assert 'GetChildren()[1]' in fixed
        assert 'GetChildren()(0)' not in fixed

    def test_obj_game_getservice(self):
        """obj.game:GetService → game:GetService."""
        from converter.luau_validator import validate_and_fix
        source = 'if otherPart.game:GetService("CollectionService"):HasTag(x, "Player") then'
        fixed, _ = validate_and_fix("test", source)
        assert 'otherPart.game:' not in fixed
        assert 'game:GetService("CollectionService")' in fixed

    def test_comment_embedded_condition(self):
        """if control-- comment: text then → if control then."""
        from converter.luau_validator import validate_and_fix
        source = 'if control-- isGrounded: use Humanoid:GetState() then'
        fixed, _ = validate_and_fix("test", source)
        assert 'if control then' in fixed

    def test_mangled_method_name(self):
        """FindFirstChildOfClasssInChildren → GetDescendants."""
        from converter.luau_validator import validate_and_fix
        source = 'local systems = script.Parent:FindFirstChildOfClasssInChildren()'
        fixed, _ = validate_and_fix("test", source)
        assert 'GetDescendants' in fixed
        assert 'FindFirstChildOfClasssInChildren' not in fixed

    def test_stray_type_prefix(self):
        """TypeName.local var = ... → local var = ..."""
        from converter.luau_validator import validate_and_fix
        source = '				ParticleSystem.local mainModule = system.main'
        fixed, _ = validate_and_fix("test", source)
        assert 'ParticleSystem.local' not in fixed
        assert 'local mainModule' in fixed

    def test_zero_based_random_array_index(self):
        """math.random(0, #arr) → math.random(1, #arr)."""
        from converter.luau_validator import validate_and_fix
        source = 'local prefab = items[math.random(0, #items)]'
        fixed, _ = validate_and_fix("test", source)
        assert 'math.random(1, #items)' in fixed
        assert 'math.random(0, #items)' not in fixed

    def test_new_dictionary(self):
        """new System.Collections.Generic.Dictionary() → {}."""
        from converter.luau_validator import validate_and_fix
        source = 'b.markerClips = new System.Collections.Generic.Dictionary()'
        fixed, _ = validate_and_fix("test", source)
        assert '{}' in fixed
        assert 'new ' not in fixed

    def test_for_in_loop_parens(self):
        """for _, x in expr( do) → for _, x in expr do."""
        from converter.luau_validator import validate_and_fix
        source = 'for _, c in script.Parent:GetDescendants( do)\n    print(c)\nend'
        fixed, _ = validate_and_fix("test", source)
        assert '( do)' not in fixed
        assert ':GetDescendants do' in fixed or ':GetDescendants() do' in fixed

    def test_connect_function_end_closure(self):
        """end after :Connect(function() block → end)."""
        from converter.luau_validator import validate_and_fix
        # Needs enough structure for depth tracking to work
        source = (
            'local function main()\n'
            '    RunService.Heartbeat:Connect(function(dt)\n'
            '        print(dt)\n'
            '    end\n'
            'end\n'
        )
        fixed, _ = validate_and_fix("test", source)
        assert 'end)' in fixed

    def test_trailing_end_removal(self):
        """Excess trailing end keywords removed."""
        from converter.luau_validator import validate_and_fix
        source = 'local function foo()\n    print("hi")\nend\nend\nend'
        fixed, _ = validate_and_fix("test", source)
        # Should have only 1 end (for the function)
        assert fixed.count('\nend') <= 1 or 'Removed' in str(_)

    def test_multi_var_declaration(self):
        """int a, b, c → local a, b, c = nil, nil, nil."""
        from converter.luau_validator import validate_and_fix
        source = '    float a0, a1, a2'
        fixed, _ = validate_and_fix("test", source)
        assert 'local a0, a1, a2 = nil, nil, nil' in fixed

    def test_remaining_csharp_ternary(self):
        """condition() ? a : b → (if condition() then a else b)."""
        from converter.luau_validator import validate_and_fix
        source = '    state = ShouldTransition() ? _targetState : nil'
        fixed, _ = validate_and_fix("test", source)
        assert '(if ShouldTransition() then _targetState else nil)' in fixed
        assert '?' not in fixed

    def test_using_after_blank_line(self):
        """using System after blank line gets commented out."""
        from converter.luau_validator import validate_and_fix
        source = 'local x = 1\n\nusing System\n-- using System.Collections;'
        fixed, _ = validate_and_fix("test", source)
        assert '-- using System' in fixed
        # Should not have bare 'using System'
        for line in fixed.split('\n'):
            if line.strip().startswith('using '):
                assert False, f"Bare using statement remains: {line}"

    def test_value_property_capitalization(self):
        """obj.value → obj.Value (Roblox PascalCase)."""
        from converter.luau_validator import validate_and_fix
        source = '    health.value = percentage'
        fixed, _ = validate_and_fix("test", source)
        assert '.Value' in fixed
        assert '.value' not in fixed

    def test_raycast_api_3args(self):
        """workspace:Raycast(ray, hit, range) → workspace:Raycast(ray.Origin, ray.Direction * range)."""
        from converter.luau_validator import validate_and_fix
        source = 'if workspace:Raycast(ray, hit, shootRange) then'
        fixed, _ = validate_and_fix("test", source)
        assert 'ray.Origin' in fixed
        assert 'ray.Direction' in fixed
        assert 'shootRange' in fixed

    def test_raycast_api_4args(self):
        """workspace:Raycast(origin, dir, hit, range) → workspace:Raycast(origin, dir * range)."""
        from converter.luau_validator import validate_and_fix
        source = 'if workspace:Raycast(tBase.Position, dir.normalized, hit, sightRadius) then'
        fixed, _ = validate_and_fix("test", source)
        assert 'tBase.Position' in fixed
        assert 'dir.Unit * sightRadius' in fixed

    def test_math_acos_two_args(self):
        """math.acos(a, b) → math.deg(math.acos(a.Unit:Dot(b.Unit)))."""
        from converter.luau_validator import validate_and_fix
        source = 'local angle = math.acos(dir, tBase.forward)'
        fixed, _ = validate_and_fix("test", source)
        assert 'dir.Unit:Dot(tBase.CFrame.LookVector.Unit)' in fixed
        assert 'math.deg' in fixed

    def test_bare_forward_to_lookvector(self):
        """obj.forward → obj.CFrame.LookVector."""
        from converter.luau_validator import validate_and_fix
        source = 'local dir = tBase.forward.Unit'
        fixed, _ = validate_and_fix("test", source)
        assert 'tBase.CFrame.LookVector.Unit' in fixed

    def test_bare_right_to_rightvector(self):
        """obj.right → obj.CFrame.RightVector."""
        from converter.luau_validator import validate_and_fix
        source = 'local side = cam.right'
        fixed, _ = validate_and_fix("test", source)
        assert 'cam.CFrame.RightVector' in fixed

    def test_bare_up_to_upvector(self):
        """obj.up → obj.CFrame.UpVector."""
        from converter.luau_validator import validate_and_fix
        source = 'local y = part.up'
        fixed, _ = validate_and_fix("test", source)
        assert 'part.CFrame.UpVector' in fixed

    def test_ray_new_to_table(self):
        """Ray.new(origin, dir) → {Origin=, Direction=}."""
        from converter.luau_validator import validate_and_fix
        source = 'local ray = Ray.new(cam.Position, dir.normalized)'
        fixed, _ = validate_and_fix("test", source)
        assert 'Origin = cam.Position' in fixed
        assert 'Direction = dir.Unit' in fixed
        assert 'Ray.new' not in fixed

    def test_getchild_zero_based(self):
        """:GetChild(0) → :GetChildren()[1]."""
        from converter.luau_validator import validate_and_fix
        source = 'local w = tBase:GetChild(0)'
        fixed, _ = validate_and_fix("test", source)
        assert ':GetChildren()[1]' in fixed

    def test_getchild_variable_index(self):
        """:GetChild(n) → :GetChildren()[n + 1]."""
        from converter.luau_validator import validate_and_fix
        source = 'local c = obj:GetChild(idx)'
        fixed, _ = validate_and_fix("test", source)
        assert ':GetChildren()[idx + 1]' in fixed

    def test_gizmos_commented_out(self):
        """Gizmos.* lines → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    Gizmos.color = Color.new(0, 1, 0)\n    Gizmos.DrawLine(a, b)'
        fixed, _ = validate_and_fix("test", source)
        assert '-- Gizmos.' in fixed  # .color may become .Color before commenting
        assert '-- Gizmos.DrawLine' in fixed

    def test_text_property_casing(self):
        """.text → .Text for UI elements."""
        from converter.luau_validator import validate_and_fix
        source = 'label.text = "hello"'
        fixed, _ = validate_and_fix("test", source)
        assert 'label.Text' in fixed

    def test_text_after_paren(self):
        """).text → ).Text."""
        from converter.luau_validator import validate_and_fix
        source = 'obj:FindFirstChild("X").text = "hi"'
        fixed, _ = validate_and_fix("test", source)
        assert ').Text' in fixed

    def test_rotation_to_cframe(self):
        """.rotation → .CFrame (except Random.rotation)."""
        from converter.luau_validator import validate_and_fix
        source = 'part.rotation = CFrame.new()'
        fixed, _ = validate_and_fix("test", source)
        assert 'part.CFrame' in fixed

    def test_random_rotation_preserved(self):
        """Random.rotation should NOT become Random.CFrame."""
        from converter.luau_validator import validate_and_fix
        source = 'local rot = Random.rotation'
        fixed, _ = validate_and_fix("test", source)
        assert 'Random.rotation' in fixed or 'CFrame.Angles' in fixed

    def test_point_to_object_space(self):
        """:PointToObjectSpace → .CFrame:PointToObjectSpace."""
        from converter.luau_validator import validate_and_fix
        source = 'local p = part:PointToObjectSpace(pos)'
        fixed, _ = validate_and_fix("test", source)
        assert 'part.CFrame:PointToObjectSpace(pos)' in fixed

    def test_lerp_on_script_parent(self):
        """script.Parent:Lerp → script.Parent.CFrame:Lerp."""
        from converter.luau_validator import validate_and_fix
        source = 'script.Parent:Lerp(target, 0.5)'
        fixed, _ = validate_and_fix("test", source)
        assert 'script.Parent.CFrame:Lerp(target, 0.5)' in fixed

    def test_rotate_to_cframe(self):
        """:Rotate(dir, speed) → CFrame rotation."""
        from converter.luau_validator import validate_and_fix
        source = 'tBase:Rotate(rotateDir, dt * rotationSpeed)'
        fixed, _ = validate_and_fix("test", source)
        assert 'CFrame' in fixed
        assert ':Rotate(' not in fixed

    def test_lookat_to_cframe_lookat(self):
        """:LookAt(target) → CFrame.lookAt."""
        from converter.luau_validator import validate_and_fix
        source = 'tWeapon:LookAt(target)'
        fixed, _ = validate_and_fix("test", source)
        assert 'CFrame.lookAt' in fixed
        assert 'tWeapon.Position' in fixed

    def test_type_tostring(self):
        """Player.tostring(x) → tostring(x)."""
        from converter.luau_validator import validate_and_fix
        source = 'label.Text = Player.tostring(maxAmmo)'
        fixed, _ = validate_and_fix("test", source)
        assert 'tostring(maxAmmo)' in fixed
        assert 'Player.tostring' not in fixed

    def test_math_deg_1_fix(self):
        """math.deg(1) → 180/math.pi."""
        from converter.luau_validator import validate_and_fix
        source = 'local angle = math.atan2(x, z) * math.deg(1)'
        fixed, _ = validate_and_fix("test", source)
        assert '(180 / math.pi)' in fixed
        assert 'math.deg(1)' not in fixed

    def test_setactive_method_to_function(self):
        """.setActive(obj, bool) → setActive(obj, bool) function call."""
        from converter.luau_validator import validate_and_fix
        source = 'controls.setActive(script.Parent, true)'
        fixed, _ = validate_and_fix("test", source)
        assert 'setActive(script.Parent, true)' in fixed

    def test_float_positive_infinity(self):
        """float.PositiveInfinity → math.huge."""
        from converter.luau_validator import validate_and_fix
        source = 'local dist = float.PositiveInfinity'
        fixed, _ = validate_and_fix("test", source)
        assert 'math.huge' in fixed
        assert 'float.PositiveInfinity' not in fixed

    def test_float_negative_infinity(self):
        """float.NegativeInfinity → -math.huge."""
        from converter.luau_validator import validate_and_fix
        source = 'local min = float.NegativeInfinity'
        fixed, _ = validate_and_fix("test", source)
        assert '-math.huge' in fixed

    def test_float_max_value(self):
        """float.MaxValue → math.huge."""
        from converter.luau_validator import validate_and_fix
        source = 'local max = float.MaxValue'
        fixed, _ = validate_and_fix("test", source)
        assert 'math.huge' in fixed

    def test_dont_destroy_on_load(self):
        """DontDestroyOnLoad → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    DontDestroyOnLoad(script.Parent)'
        fixed, _ = validate_and_fix("test", source)
        assert 'DontDestroyOnLoad' in fixed
        assert '--' in fixed

    def test_dont_destroy_dotted(self):
        """Dont.DestroyOnLoad → commented out."""
        from converter.luau_validator import validate_and_fix
        source = 'Dont.DestroyOnLoad(script.Parent)'
        fixed, _ = validate_and_fix("test", source)
        assert '--' in fixed

    def test_double_invocation(self):
        """obj:Play()() → obj:Play()."""
        from converter.luau_validator import validate_and_fix
        source = 'm_AnimationTrack:Play()()'
        fixed, _ = validate_and_fix("test", source)
        assert ':Play()' in fixed
        assert '()()' not in fixed

    def test_syncvar_attribute_stripped(self):
        """[SyncVar(...)] stripped from code."""
        from converter.luau_validator import validate_and_fix
        source = '[SyncVar(hook="Net_OnIdChanged")] local m_net_pedId = -1'
        fixed, _ = validate_and_fix("test", source)
        assert '[SyncVar' not in fixed
        assert 'local m_net_pedId = -1' in fixed

    def test_watched_attribute_stripped(self):
        """[Watched] stripped from code."""
        from converter.luau_validator import validate_and_fix
        source = '[Watched] local DragScale = 1 / 100'
        fixed, _ = validate_and_fix("test", source)
        assert '[Watched]' not in fixed
        assert 'local DragScale = 1 / 100' in fixed

    def test_anonymous_object_syntax(self):
        """() { → { (table literal)."""
        from converter.luau_validator import validate_and_fix
        source = 'table.insert(states, () { position = p, rotation = r })'
        fixed, _ = validate_and_fix("test", source)
        assert '() {' not in fixed
        assert '{ position = p' in fixed

    def test_light_visible_to_enabled(self):
        """.Visible → .Enabled for Light objects."""
        from converter.luau_validator import validate_and_fix
        source = 'staffLight.Visible = true'
        fixed, _ = validate_and_fix("test", source)
        assert 'staffLight.Enabled = true' in fixed

    def test_remove_range(self):
        """.RemoveRange → table.remove loop."""
        from converter.luau_validator import validate_and_fix
        source = 'savedStates.RemoveRange(0, count)'
        fixed, _ = validate_and_fix("test", source)
        assert 'table.remove' in fixed
        assert '.RemoveRange' not in fixed

    def test_generic_in_function_params(self):
        """Generic types stripped from function parameters."""
        from converter.luau_validator import validate_and_fix
        source = 'local function Initialize(inst, Dictionary<Instance, dict)\nend'
        fixed, _ = validate_and_fix("test", source)
        assert 'Dictionary<' not in fixed

    def test_math_lerp_to_utility(self):
        """math.lerp → mathLerp utility function."""
        from converter.luau_validator import validate_and_fix
        source = 'local x = math.lerp(a, b, t)'
        fixed, fixes = validate_and_fix("test", source)
        assert 'mathLerp(a, b, t)' in fixed
        assert 'math.lerp' not in fixed
        assert 'local function mathLerp' in fixed

    def test_renderer_visible_to_enabled(self):
        """.Visible → .Enabled for Renderer objects."""
        from converter.luau_validator import validate_and_fix
        source = 'systemRenderer.Visible = not systemRenderer.Visible'
        fixed, _ = validate_and_fix("test", source)
        assert 'systemRenderer.Enabled = not systemRenderer.Enabled' in fixed
        assert '.Visible' not in fixed

    def test_emission_visible_to_enabled(self):
        """.Visible → .Enabled for emission/particle objects."""
        from converter.luau_validator import validate_and_fix
        source = 'emission.Visible = false'
        fixed, _ = validate_and_fix("test", source)
        assert 'emission.Enabled = false' in fixed

    def test_connect_closure_nested_if(self):
        """Connect(function) with nested ifs gets end)."""
        from converter.luau_validator import validate_and_fix
        source = (
            'script.Parent.Touched:Connect(function(otherPart)\n'
            '    if true then\n'
            '        if true then\n'
            '            print("hi")\n'
            '        end\n'
            '    end\n'
            'end\n'
        )
        fixed, _ = validate_and_fix("test", source)
        assert fixed.rstrip().endswith('end)')

    def test_connect_closure_missing_end(self):
        """Connect(function) with missing end still gets end)."""
        from converter.luau_validator import validate_and_fix
        source = (
            'script.Parent.Touched:Connect(function(otherPart)\n'
            '    if true then\n'
            '        print("hi")\n'
            '    end\n'
            'end\n'
        )
        fixed, _ = validate_and_fix("test", source)
        assert 'end)' in fixed

    def test_connect_closure_swapped_end(self):
        """end) at wrong depth gets swapped with end."""
        from converter.luau_validator import validate_and_fix
        source = (
            'workspace.DescendantAdded:Connect(function(obj)\n'
            '    if obj:IsA("BasePart") then\n'
            '        task.defer(setup, obj)\n'
            'end)\n'
            'end\n'
        )
        fixed, _ = validate_and_fix("test", source)
        lines = fixed.rstrip().split('\n')
        # end should close the if, end) should close the Connect function
        assert lines[-1].strip() == 'end)'
        assert lines[-2].strip() == 'end'

    def test_endparen_recognized_as_closer(self):
        """end) is recognized as block closer in missing-end analysis."""
        from converter.luau_validator import validate_and_fix
        source = (
            'workspace.DescendantAdded:Connect(function(obj)\n'
            '    if obj:IsA("BasePart") then\n'
            '        task.defer(setup, obj)\n'
            '    end\n'
            'end)\n'
        )
        fixed, _ = validate_and_fix("test", source)
        # Count end lines (not substrings)
        end_lines = [l.strip() for l in fixed.split('\n') if l.strip() in ('end', 'end)')]
        assert len(end_lines) == 2

    def test_task_delay_closure_end(self):
        """task.delay(time, function() gets end) closure fix."""
        from converter.luau_validator import validate_and_fix
        source = (
            'task.delay(5, function()\n'
            '    for _, player in Players:GetPlayers() do\n'
            '        player:LoadCharacter()\n'
            '    end\n'
            'end\n'
        )
        fixed, _ = validate_and_fix("test", source)
        lines = [l.strip() for l in fixed.rstrip().split('\n') if l.strip()]
        # The last line should be end) to close task.delay callback
        assert lines[-1] == 'end)'

    def test_task_spawn_closure_end(self):
        """task.spawn(function() gets end) closure fix."""
        from converter.luau_validator import validate_and_fix
        source = (
            'task.spawn(function()\n'
            '    print("hello")\n'
            'end\n'
        )
        fixed, _ = validate_and_fix("test", source)
        lines = [l.strip() for l in fixed.rstrip().split('\n') if l.strip()]
        assert lines[-1] == 'end)'

    def test_elseif_depth_tracking(self):
        """elseif doesn't inflate block depth."""
        from converter.luau_validator import validate_and_fix
        source = (
            'local function foo(obj)\n'
            '    if obj:IsA("A") then\n'
            '        print("a")\n'
            '    elseif obj:IsA("B") then\n'
            '        print("b")\n'
            '    end\n'
            'end\n'
            '\n'
            'workspace.DescendantAdded:Connect(function(obj)\n'
            '    task.defer(foo, obj)\n'
            'end)\n'
        )
        fixed, _ = validate_and_fix("test", source)
        # Should not have trailing orphaned end
        assert fixed.rstrip().endswith('end)')

    def test_undefined_module_return(self):
        """Scripts ending with return ClassName get table definition added."""
        from converter.luau_validator import validate_and_fix
        source = (
            'local Players = game:GetService("Players")\n'
            '\n'
            'local function doStuff()\n'
            '    print("hi")\n'
            'end\n'
            '\n'
            'return MyModule\n'
        )
        fixed, _ = validate_and_fix("test", source)
        assert 'local MyModule = {}' in fixed
        assert fixed.index('local MyModule = {}') < fixed.index('return MyModule')

    def test_magnitude_method_to_property(self):
        """Magnitude() as method call → .Magnitude property."""
        from converter.luau_validator import validate_and_fix
        source = 'local dist = (pos1 - pos2):Magnitude()'
        fixed, _ = validate_and_fix("test", source)
        assert ':Magnitude()' not in fixed
        assert '.Magnitude' in fixed

    def test_raycast_broken_origin_direction(self):
        """Raycast with .Origin/.Direction on Vector3 gets fixed."""
        from converter.luau_validator import validate_and_fix
        source = (
            'local origin = camera.CFrame.Position\n'
            'local direction = camera.CFrame.LookVector * 100\n'
            'local result = workspace:Raycast(origin.Origin, origin.Direction * rayParams)\n'
        )
        fixed, _ = validate_and_fix("test", source)
        assert '.Origin' not in fixed
        assert '.Direction' not in fixed
        assert 'Raycast(origin, direction, rayParams)' in fixed

    def test_raycast_skips_rayparams_second_arg(self):
        """Raycast(origin, rayParams, x) is NOT wrongly converted."""
        from converter.luau_validator import validate_and_fix
        source = 'local result = workspace:Raycast(origin, rayParams, dist)\n'
        fixed, _ = validate_and_fix("test", source)
        assert 'origin.Origin' not in fixed

    def test_ternary_inside_function_call(self):
        """Ternary inside function call args gets properly converted."""
        from converter.luau_validator import validate_and_fix
        source = 'x = mathLerp(a, func(0) ? maxVal : minVal, dt)'
        fixed, _ = validate_and_fix("test", source)
        assert '?' not in fixed
        assert '(if func(0) then maxVal else minVal)' in fixed

    def test_tab_normalization(self):
        """Tabs are normalized to spaces in block analysis."""
        from converter.luau_validator import validate_and_fix
        source = 'for _, v in items do\n\t\tprint(v)\nend'
        fixed, _ = validate_and_fix("test", source)
        assert '\t' not in fixed
        assert 'for _, v in items do' in fixed
        assert 'end' in fixed

    def test_excess_end_removal(self):
        """Excess end/end) at negative depth are removed."""
        from converter.luau_validator import validate_and_fix
        source = (
            'RunService.Heartbeat:Connect(function(dt)\n'
            '    print(dt)\n'
            'end)\n'
            'end)\n'
            'print("done")\n'
        )
        fixed, _ = validate_and_fix("test", source)
        assert fixed.count('end)') == 1

    def test_multiline_if_then_depth(self):
        """Multi-line if where then is on continuation line."""
        from converter.luau_validator import validate_and_fix
        source = (
            'local function test()\n'
            '    if condition1 or\n'
            '        condition2 then\n'
            '        print("yes")\n'
            '    end\n'
            'end\n'
        )
        fixed, _ = validate_and_fix("test", source)
        # Should be balanced — no extra end inserted
        assert fixed.count('end') == 2  # one for if, one for function

    def test_utility_function_no_double_paren(self):
        """Utility function API mappings don't produce double parens."""
        from converter.code_transpiler import _rule_based_transpile
        from converter.api_mappings import API_CALL_MAP
        source = 'float angle = Vector3.SignedAngle(a, b, c);'
        luau, _, _ = _rule_based_transpile(source, API_CALL_MAP)
        assert 'vec3SignedAngle((' not in luau
        assert 'vec3SignedAngle(a' in luau

    def test_ternary_not_matching_func_call_parens(self):
        """Ternary regex doesn't match (0) from function calls."""
        from converter.code_transpiler import _rule_based_transpile
        from converter.api_mappings import API_CALL_MAP
        source = 'x = Mathf.Lerp(m_Power, Input.GetMouseButton(0) ? maxPower : minPower, dt);'
        luau, _, _ = _rule_based_transpile(source, API_CALL_MAP)
        # The (0) should NOT be treated as a ternary condition
        assert 'if (0) then' not in luau

    def test_eof_end_appending(self):
        """Missing end at EOF gets appended."""
        from converter.luau_validator import validate_and_fix
        source = (
            'local function foo()\n'
            '    local function bar()\n'
            '        print("nested")\n'
            '    end\n'
        )
        fixed, fixes = validate_and_fix("test", source)
        # foo() was never closed — should append end at EOF
        assert fixed.rstrip().endswith('end')
        assert fixed.count('end') >= 2

    def test_broken_table_find_lambda(self):
        """Malformed table.find lambda gets restructured."""
        from converter.luau_validator import validate_and_fix
        source = 'if (table.find(items, function(x) ~= nil) return x=="GasCan" end)) then'
        fixed, _ = validate_and_fix("test", source)
        assert 'function(x) return x=="GasCan" end)' in fixed
        assert '~= nil' in fixed


class TestValidatorBatch11:
    """Tests for batch 11 validator fixes: float literals, math methods,
    Matrix4x4, Unity enums, :Dot(), .CompareTo(), etc."""

    def test_csharp_float_suffix_stripped(self):
        """C# float literal suffixes (F, f, d, D) are stripped."""
        from converter.luau_validator import validate_and_fix
        source = 'local x = 1.0F\nlocal y = 2F * 3.5f\nlocal z = 0.07F'
        fixed, fixes = validate_and_fix("test", source)
        assert '1.0F' not in fixed
        assert '2F' not in fixed
        assert '3.5f' not in fixed
        assert '0.07F' not in fixed
        assert '1.0' in fixed
        assert '3.5' in fixed

    def test_broken_numeric_property_access(self):
        """script.Parent.02 → 0.02."""
        from converter.luau_validator import validate_and_fix
        source = 'local step = script.Parent.02\nlocal y = script.Parent.033'
        fixed, fixes = validate_and_fix("test", source)
        assert 'script.Parent.02' not in fixed
        assert '0.02' in fixed
        assert '0.033' in fixed

    def test_math_round_to_int(self):
        """math.roundToInt → math.round, etc."""
        from converter.luau_validator import validate_and_fix
        source = 'local a = math.roundToInt(x)\nlocal b = math.floorToInt(y)\nlocal c = math.ceilToInt(z)'
        fixed, fixes = validate_and_fix("test", source)
        assert 'math.round(x)' in fixed
        assert 'math.floor(y)' in fixed
        assert 'math.ceil(z)' in fixed
        assert 'ToInt' not in fixed

    def test_compare_to_sort(self):
        """.Sort with .CompareTo → table.sort with Luau comparison."""
        from converter.luau_validator import validate_and_fix
        source = 'objectives.Sort((function(A, B) return A.Name.CompareTo(B.Name end))'
        fixed, fixes = validate_and_fix("test", source)
        assert 'table.sort(objectives' in fixed
        assert '.CompareTo' not in fixed

    def test_unity_enum_removal(self):
        """Unity-only enum references are removed."""
        from converter.luau_validator import validate_and_fix
        source = (
            'local x = m_Rigidbody.interpolation = RigidbodyInterpolation.Interpolate\n'
            'rb:ApplyImpulse(force, ForceMode.Impulse)\n'
            'local m = m_Animator.updateMode = AnimatorUpdateMode.AnimatePhysics\n'
        )
        fixed, fixes = validate_and_fix("test", source)
        assert 'RigidbodyInterpolation' not in fixed or '-- [Unity]' in fixed
        assert 'ForceMode.Impulse)' not in fixed
        assert 'rb:ApplyImpulse(force)' in fixed

    def test_matrix4x4_commented_out(self):
        """Matrix4x4 operations are commented out."""
        from converter.luau_validator import validate_and_fix
        source = (
            '    local m = Matrix4x4.TRS(pos, rot, scale)\n'
            '    reflectionMat.m00 = (1 - 2 * plane[0])\n'
            '    reflectionMat.m01 = (-2 * plane[0] * plane[1])\n'
        )
        fixed, fixes = validate_and_fix("test", source)
        assert '-- [Unity]' in fixed
        assert 'Matrix4x4' in fixed  # Still present in comment

    def test_bare_dot_fixed(self):
        """Bare :Dot(a, b) → a:Dot(b)."""
        from converter.luau_validator import validate_and_fix
        source = 'local d = -:Dot(normal, pos)'
        fixed, fixes = validate_and_fix("test", source)
        assert ':Dot(normal, pos)' not in fixed or 'normal:Dot(pos)' in fixed

    def test_script_parent_dot(self):
        """script.Parent:Dot(a, b) → a:Dot(b)."""
        from converter.luau_validator import validate_and_fix
        source = 'local d = script.Parent:Dot(Vector3.yAxis, msg.direction)'
        fixed, fixes = validate_and_fix("test", source)
        assert 'Vector3.yAxis:Dot(msg.direction)' in fixed

    def test_destroy_obj_pattern(self):
        """:Destroy()(obj) → obj:Destroy()."""
        from converter.luau_validator import validate_and_fix
        source = ':Destroy()(reflectionTexture)'
        fixed, fixes = validate_and_fix("test", source)
        assert 'reflectionTexture:Destroy()' in fixed

    def test_missing_then_inserted(self):
        """Missing 'then' after if condition gets added."""
        from converter.luau_validator import validate_and_fix
        source = (
            'if (x > 0)\n'
            '    print("yes")\n'
            'end\n'
        )
        fixed, fixes = validate_and_fix("test", source)
        assert 'then' in fixed

    def test_missing_then_not_added_to_multiline(self):
        """Don't add 'then' if continuation line already has it."""
        from converter.luau_validator import validate_and_fix
        source = (
            'if condition1 or\n'
            '    condition2 then\n'
            '    print("yes")\n'
            'end\n'
        )
        fixed, fixes = validate_and_fix("test", source)
        # Should NOT have double 'then'
        assert 'or then' not in fixed

    def test_sort_method(self):
        """.Sort() → table.sort()."""
        from converter.luau_validator import validate_and_fix
        source = 'items.Sort()\n'
        fixed, fixes = validate_and_fix("test", source)
        assert 'table.sort(items)' in fixed

    def test_graphics_draw_mesh_commented(self):
        """Unity Graphics.DrawMesh is commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    Graphics.DrawMesh(mesh, matrix, material, layer)\n'
        fixed, fixes = validate_and_fix("test", source)
        assert '-- [Unity render]' in fixed

    def test_shader_set_global_commented(self):
        """Unity Shader.SetGlobal* is commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    Shader.SetGlobalMatrix("_Matrix", m)\n'
        fixed, fixes = validate_and_fix("test", source)
        assert '-- [Unity render]' in fixed


class TestValidatorBatch12:
    """Tests for batch 12: Vector3 property fixes, shorthand floats."""

    def test_normalized_to_unit(self):
        """.normalized → .Unit."""
        from converter.luau_validator import validate_and_fix
        source = 'local d = dir.normalized * speed'
        fixed, _ = validate_and_fix("test", source)
        assert '.Unit' in fixed
        assert '.normalized' not in fixed

    def test_magnitude_case_fix(self):
        """.magnitude → .Magnitude."""
        from converter.luau_validator import validate_and_fix
        source = 'local d = vec.magnitude'
        fixed, _ = validate_and_fix("test", source)
        assert '.Magnitude' in fixed
        assert '.magnitude' not in fixed

    def test_sqr_magnitude(self):
        """.sqrMagnitude → :Dot(self)."""
        from converter.luau_validator import validate_and_fix
        source = 'if vec.sqrMagnitude > 1 then'
        fixed, _ = validate_and_fix("test", source)
        assert '.sqrMagnitude' not in fixed
        assert 'vec:Dot(vec)' in fixed

    def test_euler_angles(self):
        """.eulerAngles → CFrame:ToEulerAnglesXYZ()."""
        from converter.luau_validator import validate_and_fix
        source = 'local angles = obj.eulerAngles'
        fixed, _ = validate_and_fix("test", source)
        assert '.eulerAngles' not in fixed
        assert 'ToEulerAnglesXYZ' in fixed

    def test_local_position(self):
        """.localPosition → .Position."""
        from converter.luau_validator import validate_and_fix
        source = 'local p = obj.localPosition'
        fixed, _ = validate_and_fix("test", source)
        assert '.Position' in fixed
        assert '.localPosition' not in fixed

    def test_local_scale(self):
        """.localScale → .Size."""
        from converter.luau_validator import validate_and_fix
        source = 'obj.localScale = Vector3.new(1, 1, 1)'
        fixed, _ = validate_and_fix("test", source)
        assert '.Size' in fixed
        assert '.localScale' not in fixed

    def test_shorthand_float_leading_zero(self):
        """.02 shorthand float gets leading zero."""
        from converter.luau_validator import validate_and_fix
        source = 'local x = .02\nlocal y = .5'
        fixed, _ = validate_and_fix("test", source)
        assert '0.02' in fixed
        assert '0.5' in fixed

    def test_single_line_if_then_end(self):
        """Single-line C# if: 'if (cond) stmt' → 'if (cond) then stmt end'."""
        from converter.luau_validator import validate_and_fix
        source = '    if (not eventFired) task.defer(OnComplete)'
        fixed, _ = validate_and_fix("test", source)
        assert 'then' in fixed
        assert 'end' in fixed
        assert 'task.defer(OnComplete)' in fixed

    def test_single_line_if_return(self):
        """Single-line C# if with return."""
        from converter.luau_validator import validate_and_fix
        source = '    if (objectives[i].name == name) return false'
        fixed, _ = validate_and_fix("test", source)
        assert 'then return false end' in fixed

    def test_single_line_if_no_false_match(self):
        """Don't match already-complete if statements."""
        from converter.luau_validator import validate_and_fix
        source = 'if (x > 0) then print("yes") end'
        fixed, _ = validate_and_fix("test", source)
        assert fixed.count('then') == 1  # Not doubled

    def test_set_parent(self):
        """.SetParent(parent) → .Parent = parent."""
        from converter.luau_validator import validate_and_fix
        source = 'obj.SetParent(workspace)'
        fixed, _ = validate_and_fix("test", source)
        assert '.Parent = workspace' in fixed

    def test_child_count(self):
        """.childCount → #:GetChildren()."""
        from converter.luau_validator import validate_and_fix
        source = 'local n = obj.childCount'
        fixed, _ = validate_and_fix("test", source)
        assert '#obj:GetChildren()' in fixed

    def test_get_sibling_index(self):
        """.GetSiblingIndex() → table.find."""
        from converter.luau_validator import validate_and_fix
        source = 'local idx = obj.GetSiblingIndex()'
        fixed, _ = validate_and_fix("test", source)
        assert 'table.find' in fixed

    def test_is_kinematic(self):
        """.isKinematic → .Anchored."""
        from converter.luau_validator import validate_and_fix
        source = 'm_Rigidbody.isKinematic = true'
        fixed, _ = validate_and_fix("test", source)
        assert '.Anchored = true' in fixed

    def test_physics_overlap_sphere(self):
        """Physics.OverlapSphere → workspace:GetPartBoundsInRadius."""
        from converter.luau_validator import validate_and_fix
        source = 'local cols = Physics.OverlapSphere(pos, radius)'
        fixed, _ = validate_and_fix("test", source)
        assert 'workspace:GetPartBoundsInRadius(pos, radius)' in fixed

    def test_new_array_initializer(self):
        """C# new[] { ... } → { ... }."""
        from converter.luau_validator import validate_and_fix
        source = 'local arr = new[] { 1, 2, 3 }'
        fixed, _ = validate_and_fix("test", source)
        assert 'new[]' not in fixed
        assert '{ 1, 2, 3 }' in fixed


class TestValidatorBatch14:
    """Tests for new validator fixes: StringToHash, typed fields, |=, etc."""

    def test_incomplete_assignment_stringtohash(self):
        """local x = -- StringToHash comment → extract name from variable."""
        from converter.luau_validator import validate_and_fix
        source = 'local m_HashAirborneVerticalSpeed = -- StringToHash: use string name directly as attribute key'
        fixed, _ = validate_and_fix("test", source)
        assert '= --' not in fixed or '= nil --' in fixed
        assert '"AirborneVerticalSpeed"' in fixed

    def test_incomplete_assignment_generic_comment(self):
        """local x = -- some comment → local x = nil -- some comment."""
        from converter.luau_validator import validate_and_fix
        source = 'local myVar = -- frameCount: no direct Roblox equivalent'
        fixed, _ = validate_and_fix("test", source)
        assert 'local myVar = nil -- frameCount' in fixed

    def test_typed_field_bool(self):
        """bool canAttack; → local canAttack = nil."""
        from converter.luau_validator import validate_and_fix
        source = '    bool canAttack;'
        fixed, _ = validate_and_fix("test", source)
        assert 'local canAttack = nil' in fixed
        assert 'bool' not in fixed

    def test_typed_field_float_with_comment(self):
        """float m_Speed;  -- comment → local m_Speed = nil  -- comment."""
        from converter.luau_validator import validate_and_fix
        source = '    float m_Speed;  -- How fast'
        fixed, _ = validate_and_fix("test", source)
        assert 'local m_Speed = nil' in fixed
        assert '-- How fast' in fixed

    def test_typed_field_multi_variable(self):
        """UnityEvent OnDeath, OnDamage → local OnDeath, OnDamage = nil, nil."""
        from converter.luau_validator import validate_and_fix
        source = '    UnityEvent OnDeath, OnDamage'
        fixed, _ = validate_and_fix("test", source)
        assert 'local OnDeath, OnDamage = nil, nil' in fixed

    def test_bitwise_or_equals(self):
        """|= operator → or expression."""
        from converter.luau_validator import validate_and_fix
        source = 'inputBlocked |= m_NextState'
        fixed, _ = validate_and_fix("test", source)
        assert '|=' not in fixed
        assert 'inputBlocked = inputBlocked or (m_NextState)' in fixed

    def test_default_param_bool(self):
        """function Foo(true) → function Foo(_defaultParam)."""
        from converter.luau_validator import validate_and_fix
        source = 'local function Detect(detector, true)'
        fixed, _ = validate_and_fix("test", source)
        assert 'true)' not in fixed or 'true' in fixed.split('(')[0]

    def test_missing_receiver_m_colon(self):
        """m_:SetAttribute() → script.Parent:SetAttribute()."""
        from converter.luau_validator import validate_and_fix
        source = 'm_:SetAttribute("speed", 10)'
        fixed, _ = validate_and_fix("test", source)
        assert 'm_:' not in fixed
        assert 'script.Parent:SetAttribute("speed", 10)' in fixed

    def test_double_invocation_with_args(self):
        """Play()(hash) → Play(hash)."""
        from converter.luau_validator import validate_and_fix
        source = 'm_AnimationTrack:Play()(m_HashMeleeAttack)'
        fixed, _ = validate_and_fix("test", source)
        assert '()(' not in fixed
        assert ':Play(m_HashMeleeAttack)' in fixed

    def test_mangled_table_clear(self):
        """vartable.clear → table.clear(var)."""
        from converter.luau_validator import validate_and_fix
        source = 'inventoryItemstable.clear'
        fixed, _ = validate_and_fix("test", source)
        assert 'table.clear(inventoryItems)' in fixed

    def test_mangled_table_insert(self):
        """vartable.insert(x) → table.insert(var, x)."""
        from converter.luau_validator import validate_and_fix
        source = 'm_FreeIdxtable.insert(i)'
        fixed, _ = validate_and_fix("test", source)
        assert 'table.insert(m_FreeIdx, i)' in fixed

    def test_parent_setparent_conversion(self):
        """.Parent =(nil, true) → .Parent = nil."""
        from converter.luau_validator import validate_and_fix
        source = 'obj.Parent =(nil, true)'
        fixed, _ = validate_and_fix("test", source)
        assert '.Parent = nil' in fixed

    def test_parent_setparent_with_object(self):
        """.Parent =(workspace, false) → .Parent = workspace."""
        from converter.luau_validator import validate_and_fix
        source = 'obj.Parent =(workspace, false)'
        fixed, _ = validate_and_fix("test", source)
        assert '.Parent = workspace' in fixed

    def test_csharp_cast_strip(self):
        """(Damageable.DamageMessage)data → data."""
        from converter.luau_validator import validate_and_fix
        source = 'local damageData = (Damageable.DamageMessage)data'
        fixed, _ = validate_and_fix("test", source)
        assert 'Damageable.DamageMessage' not in fixed
        assert 'damageData' in fixed

    def test_cframe_lookvector_assignment(self):
        """CFrame.LookVector = dir → CFrame.lookAt()."""
        from converter.luau_validator import validate_and_fix
        source = 'script.Parent.CFrame.LookVector = -pushForce.Unit'
        fixed, _ = validate_and_fix("test", source)
        assert 'CFrame.LookVector =' not in fixed
        assert 'CFrame.lookAt' in fixed

    def test_cframe_lookvector_from_forward(self):
        """.forward = dir (via structural fix) → CFrame.lookAt()."""
        from converter.luau_validator import validate_and_fix
        source = 'script.Parent.transform.forward = -pushForce.Unit'
        fixed, _ = validate_and_fix("test", source)
        assert 'CFrame.LookVector =' not in fixed
        assert 'CFrame.lookAt' in fixed

    def test_normalize_to_unit(self):
        """.Normalize() → .Unit assignment."""
        from converter.luau_validator import validate_and_fix
        source = 'moveInput.Normalize()'
        fixed, _ = validate_and_fix("test", source)
        assert '.Normalize()' not in fixed
        assert '.Unit' in fixed

    def test_find_first_child_instance(self):
        """FindFirstChildWhichIsA("Instance") → FindFirstChildOfClass("BasePart")."""
        from converter.luau_validator import validate_and_fix
        source = 'local x = script.Parent:FindFirstChildWhichIsA("Instance")'
        fixed, _ = validate_and_fix("test", source)
        assert '"Instance"' not in fixed
        assert 'FindFirstChildOfClass("BasePart")' in fixed

    def test_is_trigger_true(self):
        """.isTrigger = true → .CanCollide = false."""
        from converter.luau_validator import validate_and_fix
        source = 'collider.isTrigger = true'
        fixed, _ = validate_and_fix("test", source)
        assert '.CanCollide = false' in fixed

    def test_use_gravity_false(self):
        """.useGravity = false → .Anchored = true."""
        from converter.luau_validator import validate_and_fix
        source = 'm_Rigidbody.useGravity = false'
        fixed, _ = validate_and_fix("test", source)
        assert '.Anchored = true' in fixed

    def test_detect_collisions(self):
        """.detectCollisions = false → .CanCollide = false."""
        from converter.luau_validator import validate_and_fix
        source = 'm_Rigidbody.detectCollisions = false'
        fixed, _ = validate_and_fix("test", source)
        assert '.CanCollide = false' in fixed

    def test_debug_draw_commented(self):
        """Debug.DrawLine(...) → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    Debug.DrawLine(pos, target, Color3.new(1,0,0))'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity editor]' in fixed

    def test_workspace_gravity_dot(self):
        """workspace.Gravity:Dot() → numeric multiplication."""
        from converter.luau_validator import validate_and_fix
        source = 'local g2 = workspace.Gravity:Dot(workspace.Gravity)'
        fixed, _ = validate_and_fix("test", source)
        assert 'workspace.Gravity * workspace.Gravity' in fixed


class TestValidatorBatch15:
    """Tests for round 2 fixes: Camera.current, Animator API, transform, etc."""

    def test_camera_current(self):
        """Camera.current → workspace.CurrentCamera."""
        from converter.luau_validator import validate_and_fix
        source = 'local cam = Camera.current'
        fixed, _ = validate_and_fix("test", source)
        assert 'workspace.CurrentCamera' in fixed

    def test_animator_set_bool(self):
        """animator:SetBool(hash, val) → :SetAttribute(hash, val)."""
        from converter.luau_validator import validate_and_fix
        source = 'm_Animator:SetBool(hashGrounded, true)'
        fixed, _ = validate_and_fix("test", source)
        assert ':SetAttribute(hashGrounded, true)' in fixed

    def test_animator_set_trigger(self):
        """animator:SetTrigger(hash) → :SetAttribute(hash, true)."""
        from converter.luau_validator import validate_and_fix
        source = 'm_Animator:SetTrigger(hashSpotted)'
        fixed, _ = validate_and_fix("test", source)
        assert ':SetAttribute(hashSpotted, true)' in fixed

    def test_animator_get_bool(self):
        """animator:GetBool(hash) → :GetAttribute(hash)."""
        from converter.luau_validator import validate_and_fix
        source = 'local grounded = m_Animator:GetBool(hashGrounded)'
        fixed, _ = validate_and_fix("test", source)
        assert ':GetAttribute(hashGrounded)' in fixed

    def test_bare_transform_receiver(self):
        """transform.Position → script.Parent.Position."""
        from converter.luau_validator import validate_and_fix
        source = 'local pos = transform.Position'
        fixed, _ = validate_and_fix("test", source)
        assert 'script.Parent.Position' in fixed

    def test_bare_transform_method(self):
        """transform:FindFirstChild() → script.Parent:FindFirstChild()."""
        from converter.luau_validator import validate_and_fix
        source = 'local child = transform:FindFirstChild("x")'
        fixed, _ = validate_and_fix("test", source)
        assert 'script.Parent:FindFirstChild("x")' in fixed

    def test_bare_transform_preserves_local(self):
        """local transform = ... should not be replaced."""
        from converter.luau_validator import validate_and_fix
        source = 'local transform = script.Parent\nlocal pos = transform.Position'
        fixed, _ = validate_and_fix("test", source)
        assert 'local transform = script.Parent' in fixed

    def test_leading_dot_space(self):
        """. obj:Destroy() → obj:Destroy()."""
        from converter.luau_validator import validate_and_fix
        source = '    . m_Texture:Destroy()'
        fixed, _ = validate_and_fix("test", source)
        assert '. m_Texture' not in fixed
        assert 'm_Texture:Destroy()' in fixed

    def test_broken_ternary_assignment(self):
        """local (if x = cond then A else B) → local x = (if cond then A else B)."""
        from converter.luau_validator import validate_and_fix
        source = 'local (if acceleration = IsMoveInput then fast else slow)'
        fixed, _ = validate_and_fix("test", source)
        assert 'local acceleration = (if IsMoveInput then fast else slow)' in fixed

    def test_update_position_commented(self):
        """.updatePosition = false → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '_agent.updatePosition = false'
        fixed, _ = validate_and_fix("test", source)
        assert '-- updatePosition' in fixed

    def test_named_parameter_stripped(self):
        """func(arg, name: (if ...)) → func(arg, (if ...))."""
        from converter.luau_validator import validate_and_fix
        source = 'player.PlayRandomClip(surface, bankId: (if speed < 4 then 0 else 1))'
        fixed, _ = validate_and_fix("test", source)
        assert 'bankId:' not in fixed


class TestValidatorBatch16:
    """Tests for round 3 fixes: Unity APIs, rendering, collision, etc."""

    def test_find_first_child_of_class_in_children(self):
        """FindFirstChildOfClassInChildren() → FindFirstChildOfClass."""
        from converter.luau_validator import validate_and_fix
        source = 'local ctrl = script.Parent:FindFirstChildOfClassInChildren()'
        fixed, _ = validate_and_fix("test", source)
        assert 'FindFirstChildOfClassInChildren' not in fixed
        assert 'FindFirstChildOfClass' in fixed

    def test_gl_api_commented(self):
        """GL.invertCulling → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    GL.invertCulling = true'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity render]' in fixed

    def test_quality_settings_commented(self):
        """QualitySettings.pixelLightCount → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    QualitySettings.pixelLightCount = 0'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity]' in fixed

    def test_collider_property(self):
        """.collider → part itself."""
        from converter.luau_validator import validate_and_fix
        source = 'local part = hit.collider'
        fixed, _ = validate_and_fix("test", source)
        assert '.collider' not in fixed

    def test_contacts_normal(self):
        """.contacts[0].normal → .Normal."""
        from converter.luau_validator import validate_and_fix
        source = 'local n = col.contacts[0].normal'
        fixed, _ = validate_and_fix("test", source)
        assert '.Normal' in fixed

    def test_relative_velocity(self):
        """.relativeVelocity → .AssemblyLinearVelocity."""
        from converter.luau_validator import validate_and_fix
        source = 'local vel = col.relativeVelocity'
        fixed, _ = validate_and_fix("test", source)
        assert '.AssemblyLinearVelocity' in fixed

    def test_delta_position(self):
        """.deltaPosition → Vector3.zero."""
        from converter.luau_validator import validate_and_fix
        source = 'local dp = m_Animator.deltaPosition'
        fixed, _ = validate_and_fix("test", source)
        assert 'Vector3.zero' in fixed

    def test_move_position(self):
        """.MovePosition(pos) → .Position = pos."""
        from converter.luau_validator import validate_and_fix
        source = 'rb.MovePosition(newPos)'
        fixed, _ = validate_and_fix("test", source)
        assert '.Position = newPos' in fixed

    def test_sweep_test(self):
        """SweepTest → workspace:Raycast."""
        from converter.luau_validator import validate_and_fix
        source = 'm_Rigidbody.SweepTest(dir, hit, dist)'
        fixed, _ = validate_and_fix("test", source)
        assert 'workspace:Raycast' in fixed

    def test_get_instance_id(self):
        """GetInstanceID() → tostring()."""
        from converter.luau_validator import validate_and_fix
        source = 'local id = cam:GetInstanceID()'
        fixed, _ = validate_and_fix("test", source)
        assert 'tostring(cam)' in fixed

    def test_scene_linked_smb_commented(self):
        """SceneLinkedSMB.Initialise → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    SceneLinkedSMB.Initialise(m_Animator, script.Parent)'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity SMB]' in fixed


class TestValidatorBatch17:
    """Tests for batch 17 fixes: Vector3 immutable, bare constructors, end/elseif, etc."""

    def test_vector3_y_assignment(self):
        """vec.y = 0 → vec = Vector3.new(vec.X, 0, vec.Z)."""
        from converter.luau_validator import validate_and_fix
        source = '    pushForce.y = 0'
        fixed, _ = validate_and_fix("test", source)
        assert '.y = 0' not in fixed
        assert 'Vector3.new(pushForce.X, 0, pushForce.Z)' in fixed

    def test_vector3_x_assignment(self):
        """vec.x = val → vec = Vector3.new(val, vec.Y, vec.Z)."""
        from converter.luau_validator import validate_and_fix
        source = '    dir.x = speed * dt'
        fixed, _ = validate_and_fix("test", source)
        assert '.x = ' not in fixed
        assert 'Vector3.new(speed * dt, dir.Y, dir.Z)' in fixed

    def test_vector3_z_assignment(self):
        """vec.z = val → vec = Vector3.new(vec.X, vec.Y, val)."""
        from converter.luau_validator import validate_and_fix
        source = '    pos.z = 10'
        fixed, _ = validate_and_fix("test", source)
        assert 'Vector3.new(pos.X, pos.Y, 10)' in fixed

    def test_bare_constructor(self):
        """= () → = nil."""
        from converter.luau_validator import validate_and_fix
        source = 'm_PropertyBlock = ()'
        fixed, _ = validate_and_fix("test", source)
        assert '= nil' in fixed
        assert '= ()' not in fixed

    def test_end_before_elseif(self):
        """end followed by elseif → elseif (merged)."""
        from converter.luau_validator import validate_and_fix
        source = 'if x then\n    foo()\nend\nelseif y then\n    bar()\nend'
        fixed, _ = validate_and_fix("test", source)
        assert 'end\nelseif' not in fixed
        assert 'elseif y then' in fixed

    def test_end_before_else(self):
        """end followed by else → else (merged)."""
        from converter.luau_validator import validate_and_fix
        source = 'if x then\n    foo()\nend\nelse\n    bar()\nend'
        fixed, _ = validate_and_fix("test", source)
        assert 'end\nelse' not in fixed
        assert 'else' in fixed

    def test_collider_to_basepart(self):
        """FindFirstChildWhichIsA("Collider") → "BasePart"."""
        from converter.luau_validator import validate_and_fix
        source = 'local c = script.Parent:FindFirstChildWhichIsA("Collider")'
        fixed, _ = validate_and_fix("test", source)
        assert '"BasePart"' in fixed
        assert '"Collider"' not in fixed

    def test_color_constants(self):
        """Color.white/black/clear → Color3.new(...)."""
        from converter.luau_validator import validate_and_fix
        source = 'local c = Color.white\nlocal d = Color.clear'
        fixed, _ = validate_and_fix("test", source)
        assert 'Color3.new(1, 1, 1)' in fixed
        assert 'Color3.new(0, 0, 0)' in fixed

    def test_layermask_commented(self):
        """LayerMask bitwise ops → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    if 0 ~= (layers.Value & 1 << otherPart.CollisionGroup) then'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity LayerMask]' in fixed or '-- [C# bitwise]' in fixed

    def test_dot_to_colon_with_space(self):
        """obj.PlayRandomClip () → obj:PlayRandomClip()."""
        from converter.luau_validator import validate_and_fix
        source = 'hitAudio.PlayRandomClip ()'
        fixed, _ = validate_and_fix("test", source)
        assert ':PlayRandomClip(' in fixed
        assert '.PlayRandomClip' not in fixed

    def test_stray_brace(self):
        """Stray { on its own line → removed."""
        from converter.luau_validator import validate_and_fix
        source = 'if x then\n{\n    foo()\nend'
        fixed, _ = validate_and_fix("test", source)
        assert '\n{\n' not in fixed

    def test_collider_to_otherpart(self):
        """Undefined collider variable → otherPart."""
        from converter.luau_validator import validate_and_fix
        source = 'if collider.Name == "Player" then\n    collider:Destroy()\nend'
        fixed, _ = validate_and_fix("test", source)
        assert 'otherPart.Name' in fixed
        assert 'otherPart:Destroy' in fixed

    def test_debug_draw_commented(self):
        """Debug.DrawRay → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    Debug.DrawRay(origin, direction, Color3.new(1,0,0))'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity editor]' in fixed

    def test_m_monobehaviour_injected(self):
        """m_MonoBehaviour → local lookup injected."""
        from converter.luau_validator import validate_and_fix
        source = 'm_MonoBehaviour.controller.ClearForce()'
        fixed, _ = validate_and_fix("test", source)
        assert 'local m_MonoBehaviour = script.Parent:FindFirstChildWhichIsA' in fixed

    def test_expanded_type_stripping(self):
        """NavMeshData, ParticleSystem etc. → local var = nil."""
        from converter.luau_validator import validate_and_fix
        source = '    NavMeshData m_NavMeshData'
        fixed, _ = validate_and_fix("test", source)
        assert 'local m_NavMeshData' in fixed
        assert 'NavMeshData m_NavMeshData' not in fixed

    def test_color_red_green_blue(self):
        """Color.red/green/blue → Color3.new(...)."""
        from converter.luau_validator import validate_and_fix
        source = 'local r = Color.red\nlocal g = Color.green\nlocal b = Color.blue'
        fixed, _ = validate_and_fix("test", source)
        assert 'Color3.new(1, 0, 0)' in fixed
        assert 'Color3.new(0, 1, 0)' in fixed
        assert 'Color3.new(0, 0, 1)' in fixed


class TestValidatorBatch18:
    """Tests for batch 18: C# remnant fixes, material APIs, block comments, etc."""

    def test_get_server_time_now_as_double(self):
        """workspace:GetServerTimeNow()AsDouble → workspace:GetServerTimeNow()."""
        from converter.luau_validator import validate_and_fix
        source = 'local t = workspace:GetServerTimeNow()AsDouble'
        fixed, _ = validate_and_fix("test", source)
        assert 'GetServerTimeNow()' in fixed
        assert 'AsDouble' not in fixed

    def test_get_server_time_now_since_level_load(self):
        """workspace:GetServerTimeNow()SinceLevelLoad → workspace:GetServerTimeNow()."""
        from converter.luau_validator import validate_and_fix
        source = 'local t = workspace:GetServerTimeNow()SinceLevelLoad / 20.0'
        fixed, _ = validate_and_fix("test", source)
        assert 'GetServerTimeNow() / 20.0' in fixed

    def test_error_new_system_exception(self):
        """error(new) System.ArgumentException("msg") → error("msg")."""
        from converter.luau_validator import validate_and_fix
        source = 'error(new) System.ArgumentException("Command handler must be provided")'
        fixed, _ = validate_and_fix("test", source)
        assert 'error("Command handler must be provided")' in fixed
        assert 'System' not in fixed

    def test_block_comment_conversion(self):
        """C# /* */ → Luau --[[ ]]."""
        from converter.luau_validator import validate_and_fix
        source = '/* this is a comment */'
        fixed, _ = validate_and_fix("test", source)
        assert '--[[' in fixed
        assert ']]' in fixed
        assert '/*' not in fixed

    def test_math_ieee_remainder(self):
        """Math.IEEERemainder(x, y) → x % y."""
        from converter.luau_validator import validate_and_fix
        source = 'local result = Math.IEEERemainder(waveSpeed * t, 1.0)'
        fixed, _ = validate_and_fix("test", source)
        assert '% (1.0)' in fixed
        assert 'IEEERemainder' not in fixed

    def test_material_get_vector(self):
        """material.GetVector("name") → Vector3.zero."""
        from converter.luau_validator import validate_and_fix
        source = 'local waveSpeed = material.GetVector("WaveSpeed")'
        fixed, _ = validate_and_fix("test", source)
        assert 'Vector3.zero' in fixed

    def test_material_get_float(self):
        """material:GetFloat("name") → 1.0."""
        from converter.luau_validator import validate_and_fix
        source = 'local waveScale = material:GetFloat("_WaveScale")'
        fixed, _ = validate_and_fix("test", source)
        assert '1.0' in fixed

    def test_for_infinite_loop(self):
        """for (;;) → while true do."""
        from converter.luau_validator import validate_and_fix
        source = 'for (; ; )\n    foo()\nend'
        fixed, _ = validate_and_fix("test", source)
        assert 'while true do' in fixed
        assert 'for' not in fixed

    def test_nil_param_name(self):
        """function Foo(nil) → function Foo(_param)."""
        from converter.luau_validator import validate_and_fix
        source = 'local function GetClampedZoomSelector(nil)\n    return 0\nend'
        fixed, _ = validate_and_fix("test", source)
        assert '_param' in fixed
        assert 'function GetClampedZoomSelector(nil)' not in fixed

    def test_base_constructor_call(self):
        """: base(header, stream) → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    : base(header, stream)'
        fixed, _ = validate_and_fix("test", source)
        assert 'base constructor' in fixed
        assert ': base(header' not in fixed

    def test_prefix_decrement_as_comment(self):
        """--zoomSelector (with matching ++) → zoomSelector = zoomSelector - 1."""
        from converter.luau_validator import validate_and_fix
        source = '    ++zoomSelector\n    --zoomSelector'
        fixed, _ = validate_and_fix("test", source)
        assert 'zoomSelector = zoomSelector - 1' in fixed


class TestValidatorBatch19:
    """Tests for batch 19: new constructor expansion, for-loop variants, sizeof, &&/||."""

    def test_new_dictionary_generic(self):
        """new Dictionary<K,V>() → {}."""
        from converter.luau_validator import validate_and_fix
        source = 'local d = new Dictionary<string, List<int>>()'
        fixed, _ = validate_and_fix("test", source)
        assert '{}' in fixed
        assert 'Dictionary' not in fixed

    def test_return_new_type(self):
        """return new SyncData → return {}."""
        from converter.luau_validator import validate_and_fix
        source = '    return new SyncData'
        fixed, _ = validate_and_fix("test", source)
        assert 'return {}' in fixed

    def test_for_loop_postfix_increment(self):
        """for (local i = 0; i < N; i++) → for i = ..., ... do."""
        from converter.luau_validator import validate_and_fix
        source = 'for (local i = 0; i < count; i++)'
        fixed, _ = validate_and_fix("test", source)
        # May be 0-based or 1-based depending on later fix passes
        assert 'for i =' in fixed
        assert 'do' in fixed
        assert 'i++' not in fixed

    def test_for_loop_decrement(self):
        """for (local i = N; i >= 0; i--) → for i = N, 0, -1 do."""
        from converter.luau_validator import validate_and_fix
        source = 'for (local i = 10; i >= 0; i--)'
        fixed, _ = validate_and_fix("test", source)
        assert 'for i = 10, 0, -1 do' in fixed

    def test_sizeof_int(self):
        """sizeof(int) → 4."""
        from converter.luau_validator import validate_and_fix
        source = 'local size = sizeof(int)'
        fixed, _ = validate_and_fix("test", source)
        assert '4' in fixed
        assert 'sizeof' not in fixed

    def test_and_operator(self):
        """&& → and."""
        from converter.luau_validator import validate_and_fix
        source = 'if x ~= nil && y > 0 then'
        fixed, _ = validate_and_fix("test", source)
        assert ' and ' in fixed
        assert '&&' not in fixed

    def test_or_operator(self):
        """|| → or."""
        from converter.luau_validator import validate_and_fix
        source = 'if x == nil || y == nil then'
        fixed, _ = validate_and_fix("test", source)
        assert ' or ' in fixed
        assert '||' not in fixed

    def test_not_paren(self):
        """!(expr) → not (expr)."""
        from converter.luau_validator import validate_and_fix
        source = 'if !(IsBankAvailable(bankIndex)) then'
        fixed, _ = validate_and_fix("test", source)
        assert 'not (' in fixed
        assert '!(' not in fixed

    def test_new_with_angle_brackets_and_args(self):
        """new List<int>({ -1 }) → {}."""
        from converter.luau_validator import validate_and_fix
        source = 'local items = new List<int>({ -1 })'
        fixed, _ = validate_and_fix("test", source)
        assert '{}' in fixed

    def test_assign_new_type_eol(self):
        """= new Vehicle.VehicleInput → = {}."""
        from converter.luau_validator import validate_and_fix
        source = '    m_vehicle.Input = new Vehicle.VehicleInput'
        fixed, _ = validate_and_fix("test", source)
        assert '= {}' in fixed

    # --- New fixes: systemic Luau quality issues ---

    def test_workspace_gravity_dot(self):
        """workspace.Gravity:Dot() → Vector3 form."""
        from converter.luau_validator import validate_and_fix
        source = 'local d = toTarget:Dot(workspace.Gravity)'
        fixed, _ = validate_and_fix("test", source)
        assert 'Vector3.new(0, -workspace.Gravity, 0)' in fixed

    def test_workspace_gravity_vector_arithmetic(self):
        """workspace.Gravity * dt in vector context → Vector3."""
        from converter.luau_validator import validate_and_fix
        source = 'm_ExternalForce = m_ExternalForce + workspace.Gravity * dt'
        fixed, _ = validate_and_fix("test", source)
        assert 'Vector3.new(0, -workspace.Gravity, 0)' in fixed

    def test_cframe_lookat_single_arg(self):
        """CFrame.lookAt(dir) → CFrame.lookAt(origin, origin + dir)."""
        from converter.luau_validator import validate_and_fix
        source = 'obj.CFrame = CFrame.lookAt(forward)'
        fixed, _ = validate_and_fix("test", source)
        assert 'script.Parent.Position' in fixed
        assert 'script.Parent.Position + forward' in fixed

    def test_cframe_lookat_two_args_unchanged(self):
        """CFrame.lookAt(pos, target) should not be changed."""
        from converter.luau_validator import validate_and_fix
        source = 'obj.CFrame = CFrame.lookAt(pos, target)'
        fixed, _ = validate_and_fix("test", source)
        assert fixed.strip() == source.strip()

    def test_and_then_fix(self):
        """'and then' at line break → 'then'."""
        from converter.luau_validator import validate_and_fix
        source = 'if a > 0 and then\n    doSomething()\nend'
        fixed, _ = validate_and_fix("test", source)
        assert 'and then' not in fixed
        assert 'then' in fixed

    def test_incomplete_bitshift(self):
        """1 << -- comment → commented out."""
        from converter.luau_validator import validate_and_fix
        source = 'local layer = 1 << -- LayerMask: use CollisionGroups'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity LayerMask]' in fixed or '-- [C# bitwise]' in fixed

    def test_incomplete_assignment_comment(self):
        """var = -- comment: text → var = nil -- comment."""
        from converter.luau_validator import validate_and_fix
        source = 'local speed = -- PlayerInput: use UserInputService'
        fixed, _ = validate_and_fix("test", source)
        assert '= nil' in fixed

    def test_ternary_assignment_eq(self):
        """(if x = val then → (if x == val then."""
        from converter.luau_validator import validate_and_fix
        source = 'local y = (if x = true then 1 else 0)'
        fixed, _ = validate_and_fix("test", source)
        assert '==' in fixed

    def test_animator_state_info_commented(self):
        """GetCurrentAnimatorStateInfo → commented out."""
        from converter.luau_validator import validate_and_fix
        source = 'local info = m_Animator.GetCurrentAnimatorStateInfo(0)'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity Animator]' in fixed

    def test_short_name_hash_commented(self):
        """.shortNameHash → commented out."""
        from converter.luau_validator import validate_and_fix
        source = 'if info.shortNameHash == hashIdle then'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity Animator]' in fixed

    def test_warp_to_position(self):
        """.Warp(pos) → .Position = pos."""
        from converter.luau_validator import validate_and_fix
        source = '_agent.Warp(m_Rigidbody.Position)'
        fixed, _ = validate_and_fix("test", source)
        assert '.Position = m_Rigidbody.Position' in fixed

    def test_cframe_identity(self):
        """CFrame.identity → CFrame.new()."""
        from converter.luau_validator import validate_and_fix
        source = 'local cf = CFrame.identity'
        fixed, _ = validate_and_fix("test", source)
        assert 'CFrame.new()' in fixed

    def test_reset_path_commented(self):
        """.ResetPath() → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '_agent.ResetPath()'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity NavMesh]' in fixed

    def test_set_color_commented(self):
        """Material .SetColor() → commented out."""
        from converter.luau_validator import validate_and_fix
        source = 'mat.SetColor("_Color2", Color3.new(1, 0, 0))'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity Material]' in fixed

    def test_vector3_new_array(self):
        """Vector3.new[N] → table.create(N)."""
        from converter.luau_validator import validate_and_fix
        source = 'm_WorldDirection = Vector3.new[arcsCount]'
        fixed, _ = validate_and_fix("test", source)
        assert 'table.create(arcsCount' in fixed

    def test_undefined_controller_fix(self):
        """controller → m_Controller when undefined."""
        from converter.luau_validator import validate_and_fix
        source = 'local m_Controller = nil\ncontroller.grounded = true'
        fixed, _ = validate_and_fix("test", source)
        assert 'm_Controller.grounded' in fixed

    def test_non_alloc_methods(self):
        """GetPartBoundsInRadiusNonAlloc → GetPartBoundsInRadius."""
        from converter.luau_validator import validate_and_fix
        source = 'workspace:GetPartBoundsInRadiusNonAlloc(pos, r, cache)'
        fixed, _ = validate_and_fix("test", source)
        assert 'GetPartBoundsInRadius(' in fixed
        assert 'NonAlloc' not in fixed

    def test_contacts_commented(self):
        """Unity .contacts → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    local count = collision.contacts.length'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity Collision]' in fixed or 'contacts' not in fixed.replace('--', '')

    def test_render_texture_commented(self):
        """RenderTexture → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    local rt = RenderTexture.new(w, h, 16)'
        fixed, _ = validate_and_fix("test", source)
        # May be caught by [Unity render] or [Unity camera] - either is valid
        assert '--' in fixed and 'RenderTexture' in fixed

    def test_tuple_to_table(self):
        """(a, b, c, d) assignment → {a, b, c, d}."""
        from converter.luau_validator import validate_and_fix
        source = '    local blendedRotation = (0, 0, 0, 0)'
        fixed, _ = validate_and_fix("test", source)
        assert '{0, 0, 0, 0}' in fixed

    def test_tuple_with_expressions(self):
        """(expr, expr) assignment → {expr, expr}."""
        from converter.luau_validator import validate_and_fix
        source = '    local bounds = (script.Parent.Position, Vector3.zero)'
        fixed, _ = validate_and_fix("test", source)
        assert '{script.Parent.Position, Vector3.zero}' in fixed

    def test_message_type_to_string(self):
        """MessageType.DEAD → "DEAD"."""
        from converter.luau_validator import validate_and_fix
        source = 'if msg == MessageType.DEAD then'
        fixed, _ = validate_and_fix("test", source)
        assert '"DEAD"' in fixed

    def test_continue_preserved(self):
        """continue is valid Luau — should be preserved."""
        from converter.luau_validator import validate_and_fix
        source = '    for i = 1, 10 do\n        continue\n    end'
        fixed, _ = validate_and_fix("test", source)
        assert 'continue' in fixed
        assert '-- continue' not in fixed

    def test_projection_matrix_commented(self):
        """.projectionMatrix → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    cam.projectionMatrix = matrix'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity camera]' in fixed

    def test_null_coalescing_complex(self):
        """Complex ?? expression → or-pattern."""
        from converter.luau_validator import validate_and_fix
        source = 'local data = obj.GetData(key) ?? ""'
        fixed, _ = validate_and_fix("test", source)
        assert '??' not in fixed
        assert 'or ""' in fixed

    def test_cframe_angles_vector(self):
        """CFrame.Angles(vector) → CFrame.Angles(vec.X, vec.Y, vec.Z)."""
        from converter.luau_validator import validate_and_fix
        source = 'obj.CFrame = obj.CFrame * CFrame.Angles(rotAxis * speed)'
        fixed, _ = validate_and_fix("test", source)
        assert '.X)' in fixed or '.X,' in fixed

    def test_cframe_angles_three_args_unchanged(self):
        """CFrame.Angles(x, y, z) should not be changed."""
        from converter.luau_validator import validate_and_fix
        source = 'obj.CFrame = CFrame.Angles(0, math.rad(90), 0)'
        fixed, _ = validate_and_fix("test", source)
        assert 'CFrame.Angles(0, math.rad(90), 0)' in fixed

    def test_dt_initialization(self):
        """dt used before task.wait() → initialized before loop."""
        from converter.luau_validator import validate_and_fix
        source = 'while true do\n    pos = pos + dir * dt\n    dt = task.wait()\nend'
        fixed, _ = validate_and_fix("test", source)
        assert 'local dt = 0' in fixed


class TestValidatorBatch20:
    """Tests for batch 20 validator fixes."""

    def test_inline_materials_comment_assignment(self):
        """Inline -- materials: comment on assignment line → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    m_CoreMaterial = coreRenderer-- materials: use SurfaceAppearance[1]'
        fixed, fixes = validate_and_fix("test", source)
        assert '-- [Unity material' in fixed or 'materials' not in fixed.split('--')[0]

    def test_inline_materials_comment_for_loop(self):
        """Inline -- materials: comment in for loop → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    for _, material in overrides[i]-- materials: use SurfaceAppearance do'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity material' in fixed or fixed.lstrip().startswith('--')

    def test_end_reserved_word_variable(self):
        """local end = expr → local endPos = expr."""
        from converter.luau_validator import validate_and_fix
        source = 'local end = Vector3.zAxis\nlocal pos = start:Lerp(end, t)'
        fixed, fixes = validate_and_fix("test", source)
        assert 'local endPos' in fixed
        assert 'local end ' not in fixed

    def test_trailing_comma_in_function_call(self):
        """func(a, b,) → func(a, b)."""
        from converter.luau_validator import validate_and_fix
        source = 'workspace:Raycast(origin, dir, dist,)'
        fixed, _ = validate_and_fix("test", source)
        assert ',)' not in fixed
        assert 'dist)' in fixed

    def test_assignment_in_if_condition(self):
        """if count = value then → if count == value then."""
        from converter.luau_validator import validate_and_fix
        source = 'if count = collections[name] then\n    print(count)\nend'
        fixed, _ = validate_and_fix("test", source)
        assert '==' in fixed
        # Ensure we didn't break assignment lines
        assert 'if count ==' in fixed or 'count  == ' in fixed

    def test_assignment_in_if_no_false_positive(self):
        """if x == y then should not be changed."""
        from converter.luau_validator import validate_and_fix
        source = 'if x == y then\n    print(x)\nend'
        fixed, _ = validate_and_fix("test", source)
        assert 'x == y' in fixed

    def test_math_random_zero_base(self):
        """math.random(0, #arr) → math.random(1, #arr)."""
        from converter.luau_validator import validate_and_fix
        source = 'local idx = math.random(0, #clips)'
        fixed, _ = validate_and_fix("test", source)
        assert 'math.random(1, #clips)' in fixed

    def test_vector_set_2arg(self):
        """.Set(x, y) on Vector2 → Vector2.new assignment."""
        from converter.luau_validator import validate_and_fix
        source = 'm_Movement.Set(h, v)'
        fixed, _ = validate_and_fix("test", source)
        assert 'Vector2.new(h, v)' in fixed

    def test_vector_set_3arg(self):
        """.Set(x, y, z) on Vector3 → Vector3.new assignment."""
        from converter.luau_validator import validate_and_fix
        source = 'position.Set(x, y, z)'
        fixed, _ = validate_and_fix("test", source)
        assert 'Vector3.new(x, y, z)' in fixed

    def test_csharp_attribute_strip(self):
        """[HelpBox] before declaration → stripped."""
        from converter.luau_validator import validate_and_fix
        source = '[HelpBox] local helpString = "test"'
        fixed, _ = validate_and_fix("test", source)
        assert '[HelpBox]' not in fixed
        assert 'local helpString' in fixed

    def test_bounds_size(self):
        """.bounds.size → .Size."""
        from converter.luau_validator import validate_and_fix
        source = 'local s = m_Renderer.bounds.size'
        fixed, _ = validate_and_fix("test", source)
        assert 'm_Renderer.Size' in fixed

    def test_bounds_center(self):
        """.bounds.center → .Position."""
        from converter.luau_validator import validate_and_fix
        source = 'local c = m_Renderer.bounds.center'
        fixed, _ = validate_and_fix("test", source)
        assert 'm_Renderer.Position' in fixed

    def test_attached_rigidbody(self):
        """.attachedRigidbody → part itself."""
        from converter.luau_validator import validate_and_fix
        source = 'local rb = col.attachedRigidbody'
        fixed, _ = validate_and_fix("test", source)
        assert 'col' in fixed
        assert 'attachedRigidbody' not in fixed

    def test_is_trigger_assignment(self):
        """.isTrigger = true → .CanCollide = false."""
        from converter.luau_validator import validate_and_fix
        source = 'part.isTrigger = true'
        fixed, _ = validate_and_fix("test", source)
        assert 'CanCollide = false' in fixed

    def test_cframe_angles_2arg(self):
        """CFrame.Angles(axis, speed) → expanded 3-arg form."""
        from converter.luau_validator import validate_and_fix
        source = 'script.Parent.CFrame = script.Parent.CFrame * CFrame.Angles(axis, speed * dt)'
        fixed, _ = validate_and_fix("test", source)
        assert 'axis.X' in fixed
        assert 'axis.Y' in fixed
        assert 'axis.Z' in fixed

    def test_humanoid_move_preserved(self):
        """Humanoid:Move(dir) is valid Roblox API — should not be rewritten."""
        from converter.luau_validator import validate_and_fix
        source = 'control:Move(moveDirection * speed)'
        fixed, _ = validate_and_fix("test", source)
        assert ':Move(' in fixed

    def test_object_clone_with_arg(self):
        """obj:Clone(prefab) → prefab:Clone()."""
        from converter.luau_validator import validate_and_fix
        source = 'local copy = pool:Clone(prefab)'
        fixed, _ = validate_and_fix("test", source)
        assert 'prefab:Clone()' in fixed

    def test_property_block_commented(self):
        """GetPropertyBlock/SetPropertyBlock → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    m_Renderer.GetPropertyBlock(m_Block)'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity material]' in fixed

    def test_math_move_towards_mapping(self):
        """Mathf.MoveTowards should map to mathMoveTowards utility."""
        from converter.api_mappings import API_CALL_MAP
        assert API_CALL_MAP["Mathf.MoveTowards"] == "mathMoveTowards"

    def test_vec3_move_towards_mapping(self):
        """Vector3.MoveTowards should map to vec3MoveTowards utility."""
        from converter.api_mappings import API_CALL_MAP
        assert API_CALL_MAP["Vector3.MoveTowards"] == "vec3MoveTowards"

    def test_math_move_towards_utility(self):
        """mathMoveTowards utility function exists."""
        from converter.api_mappings import UTILITY_FUNCTIONS
        assert "mathMoveTowards" in UTILITY_FUNCTIONS
        assert "math.sign" in UTILITY_FUNCTIONS["mathMoveTowards"]

    def test_vec3_move_towards_utility(self):
        """vec3MoveTowards utility function exists."""
        from converter.api_mappings import UTILITY_FUNCTIONS
        assert "vec3MoveTowards" in UTILITY_FUNCTIONS
        assert "Magnitude" in UTILITY_FUNCTIONS["vec3MoveTowards"]

    def test_broken_table_insertion(self):
        """Broken table insertion tbl[{k=v] = k2 = v2} → table.insert."""
        from converter.luau_validator import validate_and_fix
        source = 'savedStates[{ position = player.Position] = rotation = player.CFrame }'
        fixed, _ = validate_and_fix("test", source)
        assert 'table.insert(savedStates' in fixed
        assert 'position = player.Position' in fixed
        assert 'rotation = player.CFrame' in fixed

    def test_broken_removerange_for_loop(self):
        """RemoveRange for-loop with do inside math.max → fixed."""
        from converter.luau_validator import validate_and_fix
        source = 'for _i = 1, math.max(0, #savedStates - 8 do table.remove(savedStates, 0 + 1) end -- RemoveRange)'
        fixed, _ = validate_and_fix("test", source)
        assert 'math.max(0, #savedStates - 8)' in fixed
        assert 'table.remove(savedStates, 1)' in fixed

    def test_pause_input_mapping(self):
        """IsKeyDown("Pause") → IsKeyDown(Enum.KeyCode.P)."""
        from converter.luau_validator import validate_and_fix
        source = 'm_Pause = UserInputService:IsKeyDown("Pause")'
        fixed, _ = validate_and_fix("test", source)
        assert 'Enum.KeyCode.P' in fixed

    def test_generic_string_keydown(self):
        """IsKeyDown("SomeName") → IsKeyDown(Enum.KeyCode.SomeName)."""
        from converter.luau_validator import validate_and_fix
        source = 'local x = UserInputService:IsKeyDown("Pickup")'
        fixed, _ = validate_and_fix("test", source)
        assert 'Enum.KeyCode.Pickup' in fixed
        assert '"Pickup"' not in fixed

    def test_add_with_table_literal(self):
        """.Add({key=val}) should produce table.insert, not dict assignment."""
        from converter.luau_validator import validate_and_fix
        source = 'list.Add({name = "test", value = 42})'
        fixed, _ = validate_and_fix("test", source)
        assert 'table.insert(list' in fixed

    def test_missing_module_return(self):
        """Module script with local Name = {} should get return Name appended."""
        from converter.luau_validator import validate_and_fix
        source = '-- class Foo\nlocal Foo = {}\nlocal x = 1\n'
        fixed, fixes = validate_and_fix("test", source)
        assert 'return Foo' in fixed

    def test_module_return_not_duplicated(self):
        """Module script that already has return Name should not get duplicate."""
        from converter.luau_validator import validate_and_fix
        source = 'local Foo = {}\nlocal x = 1\nreturn Foo\n'
        fixed, _ = validate_and_fix("test", source)
        assert fixed.count('return Foo') == 1

    def test_module_return_prefers_script_name(self):
        """When multiple local X = {} exist, prefer the one matching script name."""
        from converter.luau_validator import validate_and_fix
        source = 'local Helper = {}\nlocal GameManager = {}\n'
        fixed, _ = validate_and_fix("GameManager", source)
        assert 'return GameManager' in fixed

    def test_not_neq_nil_precedence_fix(self):
        """not expr ~= nil → expr == nil (precedence bug)."""
        from converter.luau_validator import validate_and_fix
        source = 'if not m_Cameras[cam] ~= nil then\n    m_Cameras[cam] = false\nend'
        fixed, _ = validate_and_fix("test", source)
        assert 'm_Cameras[cam] == nil' in fixed
        assert 'not' not in fixed.split('then')[0]

    def test_attached_rigidbody_bracket_access(self):
        """.attachedRigidbody on bracket-accessed array → stripped."""
        from converter.luau_validator import validate_and_fix
        source = 'm_Cols[n].attachedRigidbody:ApplyImpulse(force)'
        fixed, _ = validate_and_fix("test", source)
        assert 'attachedRigidbody' not in fixed
        assert 'm_Cols[n]:ApplyImpulse(force)' in fixed


class TestValidatorBatch21:
    """Tests for batch 21 validator fixes — syntax error reduction."""

    def test_bare_property_after_if_keyword(self):
        """Bare `.Parent` after `if` keyword → `script.Parent.Parent`."""
        from converter.luau_validator import validate_and_fix
        source = '    if .Parent then\n        x = 1\n    end'
        fixed, _ = validate_and_fix("test", source)
        assert 'script.Parent.Parent' in fixed

    def test_bare_method_after_not_keyword(self):
        """Bare `:Method()` after `not` → `not script.Parent:Method()`."""
        from converter.luau_validator import validate_and_fix
        source = '    if not :FindFirstChildWhichIsA("BasePart") then\n        return\n    end'
        fixed, _ = validate_and_fix("test", source)
        assert 'not script.Parent:FindFirstChildWhichIsA' in fixed

    def test_unbalanced_double_paren_in_if(self):
        """Double open paren in if → balanced."""
        from converter.luau_validator import validate_and_fix
        source = '    if ((x > 0) then\n        y = 1\n    end'
        fixed, _ = validate_and_fix("test", source)
        assert fixed.count('((') == 0 or '(x > 0) then' in fixed

    def test_multiline_tuple_joined(self):
        """Multi-line tuple assignment → joined and converted to table."""
        from converter.luau_validator import validate_and_fix
        source = '    local x = (func(a, 1),\n        func(b, 2))'
        fixed, _ = validate_and_fix("test", source)
        assert '{' in fixed

    def test_tuple_with_nested_calls(self):
        """Tuple with nested function calls → table literal."""
        from converter.luau_validator import validate_and_fix
        source = '    local x = (mathRepeat(a, 1.0), mathRepeat(b, 1.0))'
        fixed, _ = validate_and_fix("test", source)
        assert '{mathRepeat(a, 1.0), mathRepeat(b, 1.0)}' in fixed

    def test_return_tuple_to_table(self):
        """Return tuple → return table."""
        from converter.luau_validator import validate_and_fix
        source = '    return (a, b, c)'
        fixed, _ = validate_and_fix("test", source)
        assert 'return {a, b, c}' in fixed

    def test_commented_constructor_no_args(self):
        """--[[ new Type ]] () → nil."""
        from converter.luau_validator import validate_and_fix
        source = '    local x = --[[ new DamageBehaviour ]] ()'
        fixed, _ = validate_and_fix("test", source)
        assert '= nil' in fixed

    def test_if_expression_end_removed(self):
        """(if cond then A else B end) → (if cond then A else B)."""
        from converter.luau_validator import validate_and_fix
        source = '    local x = (if a then b else c end)'
        fixed, _ = validate_and_fix("test", source)
        assert 'end)' not in fixed
        assert '(if a then b else c)' in fixed

    def test_csharp_interface_method_commented(self):
        """C# interface method signature → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '        DataSettings GetDataSettings()'
        fixed, _ = validate_and_fix("test", source)
        assert fixed.strip().startswith('--')

    def test_generic_type_declaration_with_assignment(self):
        """Dictionary<K,V> varName = {} → local varName = {}."""
        from converter.luau_validator import validate_and_fix
        source = '        Dictionary<GameCommandType, List<System.Action>> handlers = {}'
        fixed, _ = validate_and_fix("test", source)
        assert 'local handlers = {}' in fixed

    def test_function_as_variable_name(self):
        """`function` as arg → `_func`."""
        from converter.luau_validator import validate_and_fix
        source = '    table.insert(s_ProcessList, function)'
        fixed, _ = validate_and_fix("test", source)
        assert '_func' in fixed
        assert ', function)' not in fixed

    def test_radius_assignment_commented(self):
        """.radius = value on LHS → commented out."""
        from converter.luau_validator import validate_and_fix
        # Requires .center to be present to trigger collider fix
        source = '    local pos = m_Sphere.center\n    m_Sphere.radius = effectDistance*0.5'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity physics]' in fixed

    def test_where_constraint_commented(self):
        """C# `where T : Base` → commented."""
        from converter.luau_validator import validate_and_fix
        source = '        where TMonoBehaviour : MonoBehaviour'
        fixed, _ = validate_and_fix("test", source)
        assert fixed.strip().startswith('--')

    def test_shader_enable_keyword_commented(self):
        """Shader.EnableKeyword → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    Shader.EnableKeyword("WATER_REFLECTIVE")'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity render]' in fixed

    def test_if_comment_before_then(self):
        """if (cond) -- comment then → if (cond) then -- comment."""
        from converter.luau_validator import validate_and_fix
        source = '    if (not x) -- explanation here then\n        y = 1\n    end'
        fixed, _ = validate_and_fix("test", source)
        assert 'then --' in fixed or 'then\n' in fixed

    def test_inline_function_block_depth(self):
        """Inline function(x) return ... end) counted correctly for block depth."""
        from converter.luau_validator import validate_and_fix
        source = '''script.Parent.Touched:Connect(function(otherPart)
    if (table.find(items, function(x) return x=="Gas" end) ~= nil) then
        part:Destroy()
    end
end)
end

return X'''
        fixed, _ = validate_and_fix("test", source)
        # The orphaned `end` before `return` should be removed
        lines = [l.strip() for l in fixed.split('\n') if l.strip()]
        # Should not have orphaned end before return
        for i, line in enumerate(lines):
            if line == 'return X' and i > 0:
                assert lines[i-1] != 'end', "Orphaned end before return should be removed"


class TestValidatorBatch18:
    """Tests for semicolons+braces, Rigidbody removal, StringToHash, prose, etc."""

    def test_semicolon_brace_removal(self):
        """stmt;    } → stmt (remove semicolon and trailing brace)."""
        from converter.luau_validator import validate_and_fix
        source = '    local function CreatePlayable(graph)\n        return playable;    }\n    end'
        fixed, _ = validate_and_fix("test", source)
        assert ';}' not in fixed
        assert 'return playable' in fixed
        assert 'return playable;' not in fixed

    def test_rigidbody_find_removal(self):
        """FindFirstChildWhichIsA("Rigidbody") → removed (part is its own physics)."""
        from converter.luau_validator import validate_and_fix
        source = 'script.Parent:FindFirstChildWhichIsA("Rigidbody").Anchored = true'
        fixed, _ = validate_and_fix("test", source)
        assert ':FindFirstChildWhichIsA("Rigidbody")' not in fixed
        assert '.Anchored = true' in fixed

    def test_string_to_hash_passthrough(self):
        """Animator.StringToHash(expr) → expr directly."""
        from converter.luau_validator import validate_and_fix
        source = 'local hash = Animator.StringToHash("Attack")'
        fixed, _ = validate_and_fix("test", source)
        assert 'StringToHash' not in fixed
        assert '"Attack"' in fixed

    def test_string_to_hash_variable_arg(self):
        """StringToHash(variable) → variable."""
        from converter.luau_validator import validate_and_fix
        source = 'lookup[StringToHash(eventName)] = events[i]'
        fixed, _ = validate_and_fix("test", source)
        assert 'StringToHash' not in fixed
        assert 'lookup[eventName]' in fixed

    def test_comment_in_bracket_access(self):
        """lookup[-- comment(expr)] → lookup[expr]."""
        from converter.luau_validator import validate_and_fix
        source = 'm_EventLookup[-- StringToHash: use string name directly as attribute key(events[i].eventName)] = events[i]'
        fixed, _ = validate_and_fix("test", source)
        assert 'm_EventLookup[events[i].eventName]' in fixed

    def test_prose_eg_commented(self):
        """Lines starting with (e.g. ...) are commented out."""
        from converter.luau_validator import validate_and_fix
        source = '(e.g. the Enemy layer does not collide with the Player layer)'
        fixed, _ = validate_and_fix("test", source)
        assert fixed.strip().startswith('--')

    def test_if_expression_equals_fix(self):
        """if-expression with = → == in condition."""
        from converter.luau_validator import validate_and_fix
        source = '(if input.Volume = target then a else b)'
        fixed, _ = validate_and_fix("test", source)
        assert '==' in fixed
        assert 'input.Volume  ==  target' in fixed or 'input.Volume == target' in fixed

    def test_standalone_if_expression_commented(self):
        """Standalone (if cond then a else b) → commented as dead code."""
        from converter.luau_validator import validate_and_fix
        source = '    (if state == inverted then not state else state)'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [dead code]' in fixed

    def test_cursor_lockstate_mapping(self):
        """Cursor.lockState → UserInputService.MouseBehavior (not a comment)."""
        from converter.luau_validator import validate_and_fix
        source = 'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter'
        fixed, _ = validate_and_fix("test", source)
        assert 'UserInputService.MouseBehavior' in fixed
        assert '-- Cursor' not in fixed

    def test_for_in_dotted_path_parens(self):
        """for _, r in script.Parent:GetDescendants do → ... do (add parens)."""
        from converter.luau_validator import validate_and_fix
        source = 'for _, r in script.Parent:GetDescendants do\n    print(r)\nend'
        fixed, _ = validate_and_fix("test", source)
        assert 'GetDescendants()' in fixed

    def test_indexer_with_space_commented(self):
        """string script.Parent [string key] → commented out."""
        from converter.luau_validator import validate_and_fix
        source = '        string script.Parent [string key]'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [C#]' in fixed

    def test_multi_variable_init_split(self):
        """local x1 = 0, x2 = 0 → separate local declarations."""
        from converter.luau_validator import validate_and_fix
        source = '    local x1 = 0, x2 = 0, y1 = 0, y2 = 0'
        fixed, _ = validate_and_fix("test", source)
        assert 'local x1 = 0' in fixed
        assert 'local x2 = 0' in fixed
        assert 'local y1 = 0' in fixed
        assert 'local y2 = 0' in fixed

    def test_where_constraint_on_method_line(self):
        """bool Method() where T : Base → commented."""
        from converter.luau_validator import validate_and_fix
        source = '        bool RepresentsState() where T : IState'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [C#]' in fixed

    def test_invoke_repeating_conversion(self):
        """InvokeRepeating('method', delay, interval) → task.spawn loop."""
        from converter.luau_validator import validate_and_fix
        source = '    InvokeRepeating("CheckArea", 1, 2)'
        fixed, _ = validate_and_fix("test", source)
        assert 'task.spawn' in fixed
        assert 'task.wait(2)' in fixed
        assert 'task.wait(1)' in fixed

    def test_script_parent_param_after_gameobject_replacement(self):
        """script.Parent as function param (from gameObject→script.Parent) is fixed."""
        from converter.luau_validator import validate_and_fix
        source = 'local function Dummy(gameObject)\n    if gameObject == nil then return end\n    return gameObject'
        fixed, _ = validate_and_fix("test", source)
        assert 'function Dummy(obj)' in fixed
        assert 'script.Parent' not in fixed.split('\n')[0]

    def test_named_parameter_stripping(self):
        """C# named parameters `name: value` stripped from function calls."""
        from converter.luau_validator import validate_and_fix
        source = '    clip = InternalPlayRandomClip(nil, bankId: 0)'
        fixed, _ = validate_and_fix("test", source)
        assert 'bankId:' not in fixed
        assert 'InternalPlayRandomClip(nil, 0)' in fixed

    def test_broken_if_paren_comment(self):
        """if (-- comment) is commented out as broken condition."""
        from converter.luau_validator import validate_and_fix
        source = '    if (-- (type comment removed)\n        continue'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [broken condition]' in fixed

    def test_remove_all_commented(self):
        """.RemoveAll(predicate) is commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    modifiers.RemoveAll(function(x) return not x.isActiveAndEnabled end)'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [C# RemoveAll]' in fixed

    def test_animation_curve_commented(self):
        """AnimationCurve.* calls are commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    local m_LinearCurve = AnimationCurve.Linear(0, 0, 1, 1)'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [Unity AnimationCurve]' in fixed

    def test_broken_keyframe_constructor(self):
        """Broken local function Keyframe(number, ...) commented out or fixed."""
        from converter.luau_validator import validate_and_fix
        source = '        local function Keyframe(1, 1, 0, 0)\n            end'
        fixed, _ = validate_and_fix("test", source)
        # Either commented out as Unity Keyframe or params replaced with _defaultParam
        assert '-- [Unity Keyframe]' in fixed or 'Keyframe(1' not in fixed

    def test_override_property_commented(self):
        """Broken C# override property pattern is commented out."""
        from converter.luau_validator import validate_and_fix
        source = '    AnimatorParameterActionSO function(_originSO) return (AnimatorParameterActionSO end)base.OriginSO;'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [C# override property]' in fixed

    def test_interface_method_commented(self):
        """C# interface method declarations are commented out."""
        from converter.luau_validator import validate_and_fix
        source = '        T Create()\n        T Request()'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [C# interface]' in fixed

    def test_operator_overload_commented(self):
        """C# operator overloads are commented out."""
        from converter.luau_validator import validate_and_fix
        source = '        local operator ==(AudioCueKey x, AudioCueKey y)'
        fixed, _ = validate_and_fix("test", source)
        assert '-- [C# operator]' in fixed

    def test_namespaced_attribute_stripped(self):
        """C# attributes with namespace prefix are stripped."""
        from converter.luau_validator import validate_and_fix
        source = '    [UnityEngine.Serialization.FormerlySerializedAs("_x")] GameState gs'
        fixed, _ = validate_and_fix("test", source)
        assert 'FormerlySerializedAs' not in fixed
