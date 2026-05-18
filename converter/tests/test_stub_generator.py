"""
test_stub_generator.py -- Tests for C#-to-Luau stub generation.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.stub_generator import generate_stub, _convert_default, _convert_params


def _info(stem="Fallback"):
    """Minimal script_info with the .path.stem attribute generate_stub reads."""
    return SimpleNamespace(path=SimpleNamespace(stem=stem))


class TestConvertDefault:
    def test_bool_passthrough(self):
        assert _convert_default("bool", "true") == "true"
        assert _convert_default("bool", "false") == "false"

    def test_bool_invalid_defaults_false(self):
        assert _convert_default("bool", "garbage") == "false"

    def test_numeric_strips_f_suffix(self):
        assert _convert_default("float", "1.5f") == "1.5"

    def test_numeric_invalid_defaults_zero(self):
        assert _convert_default("int", "notanumber") == "0"

    def test_int_passthrough(self):
        assert _convert_default("int", "42") == "42"

    def test_string_quoted_passthrough(self):
        assert _convert_default("string", '"hello"') == '"hello"'

    def test_string_unquoted_defaults_empty(self):
        assert _convert_default("string", "Identifier") == '""'

    def test_null_becomes_nil(self):
        assert _convert_default("GameObject", "null") == "nil"

    def test_complex_type_non_literal_becomes_nil(self):
        # For non-primitive types, anything that isn't a bool/nil literal or
        # a parseable number collapses to "nil" -- a `new Vector3(...)` ctor
        # is not a literal, so it does not survive.
        assert _convert_default("Vector3", "new Vector3(1,2,3)") == "nil"

    def test_complex_type_numeric_literal_survives(self):
        assert _convert_default("MyType", "3.5") == "3.5"

    def test_complex_type_bool_literal_survives(self):
        assert _convert_default("MyType", "true") == "true"

    def test_unknown_type_unparseable_becomes_nil(self):
        assert _convert_default("SomeType", "SomeExpr") == "nil"


class TestConvertParams:
    def test_empty(self):
        assert _convert_params("") == ""
        assert _convert_params("   ") == ""

    def test_typed_params_keep_names(self):
        assert _convert_params("int x, float y") == "x, y"

    def test_single_typed_param(self):
        assert _convert_params("Collider other") == "other"

    def test_untyped_token_kept(self):
        assert _convert_params("x") == "x"


class TestGenerateStubBasics:
    def test_class_and_base_in_header(self):
        src = "public class Player : MonoBehaviour { }"
        out = generate_stub(src, _info())
        assert "-- Converted from Unity C#: Player" in out
        assert "-- Original base class: MonoBehaviour" in out

    def test_no_class_uses_fallback_names(self):
        out = generate_stub("// just a comment", _info("MyFile"))
        assert "UnknownScript" in out
        # print() uses script_info.path.stem when there's no class name.
        assert 'print("MyFile loaded")' in out

    def test_print_uses_class_name_when_present(self):
        out = generate_stub("class Enemy : MonoBehaviour {}", _info("file"))
        assert 'print("Enemy loaded")' in out

    def test_output_has_script_and_part_locals(self):
        out = generate_stub("class A : MonoBehaviour {}", _info())
        assert "local script = script" in out
        assert "local part = script.Parent" in out


class TestGenerateStubLifecycle:
    def test_update_emits_heartbeat_and_runservice(self):
        src = "class C : MonoBehaviour { void Update() { } }"
        out = generate_stub(src, _info())
        assert 'game:GetService("RunService")' in out
        assert "RunService.Heartbeat:Connect(function(dt)" in out

    def test_no_update_no_runservice(self):
        out = generate_stub("class C : MonoBehaviour { void Start() {} }", _info())
        assert "RunService" not in out

    def test_collision_emits_touched_handler(self):
        src = "class C : MonoBehaviour { void OnTriggerEnter(Collider o) {} }"
        out = generate_stub(src, _info())
        assert "part.Touched:Connect(function(otherPart)" in out
        assert 'game:GetService("Players")' in out


class TestGenerateStubFeatures:
    def test_input_usage_emits_userinputservice(self):
        src = "class C : MonoBehaviour { void Update() { Input.GetKey(KeyCode.W); } }"
        out = generate_stub(src, _info())
        assert 'game:GetService("UserInputService")' in out

    def test_fields_become_locals(self):
        src = (
            "class C : MonoBehaviour {\n"
            "  public float speed = 5.0f;\n"
            "  public bool active = true;\n"
            "}"
        )
        out = generate_stub(src, _info())
        assert "local speed = 5.0" in out
        assert "local active = true" in out

    def test_non_lifecycle_methods_become_functions(self):
        src = "class C : MonoBehaviour { public void DoThing(int n) { } }"
        out = generate_stub(src, _info())
        assert "local function DoThing(n)" in out
        assert "-- TODO: implement DoThing" in out

    def test_lifecycle_methods_not_emitted_as_functions(self):
        # Start is a lifecycle hook; it must not appear as a local function.
        src = "class C : MonoBehaviour { void Start() { } }"
        out = generate_stub(src, _info())
        assert "local function Start(" not in out


class TestGenerateStubAlwaysValidShape:
    def test_empty_source_still_produces_output(self):
        out = generate_stub("", _info("Empty"))
        assert out  # non-empty
        assert 'print("Empty loaded")' in out

    def test_no_csharp_braces_leak_through(self):
        # The whole point of a stub: no raw C# syntax should survive.
        src = "public class C : MonoBehaviour { void Start() { int x = 0; } }"
        out = generate_stub(src, _info())
        # Luau comment lines are fine, but no C-style statement terminators
        # should appear in generated code lines.
        assert "int x = 0;" not in out
