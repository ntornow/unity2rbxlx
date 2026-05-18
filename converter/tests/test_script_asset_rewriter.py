"""
test_script_asset_rewriter.py -- Tests for rewriting local asset paths in Luau
scripts to rbxassetid:// URLs.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.script_asset_rewriter import rewrite_asset_references
from core.roblox_types import RbxScript


def _script(source):
    return RbxScript(name="Test", source=source)


class TestEmptyInputs:
    def test_no_uploaded_assets_returns_zero(self):
        scripts = [_script('local x = "Assets/Textures/Diamond.png"')]
        assert rewrite_asset_references(scripts, {}) == 0
        assert scripts[0].source == 'local x = "Assets/Textures/Diamond.png"'

    def test_no_scripts_returns_zero(self):
        assert rewrite_asset_references([], {"Assets/x.png": "rbxassetid://1"}) == 0


class TestFullPathRewrite:
    def test_full_path_string_replaced(self):
        scripts = [_script('local tex = "Assets/Textures/Diamond.png"')]
        n = rewrite_asset_references(
            scripts, {"Assets/Textures/Diamond.png": "rbxassetid://999"}
        )
        assert n == 1
        assert scripts[0].source == 'local tex = "rbxassetid://999"'

    def test_backslash_path_normalized(self):
        scripts = [_script('local tex = "Assets/Textures/Diamond.png"')]
        # Uploaded key uses Windows separators; the / form must still match.
        n = rewrite_asset_references(
            scripts, {"Assets\\Textures\\Diamond.png": "rbxassetid://7"}
        )
        assert n == 1
        assert "rbxassetid://7" in scripts[0].source


class TestFilenameRewrite:
    def test_bare_filename_with_extension_matched(self):
        scripts = [_script('local icon = "Diamond12.png"')]
        n = rewrite_asset_references(
            scripts, {"Assets/Textures/Diamond12.png": "rbxassetid://55"}
        )
        assert n == 1
        assert scripts[0].source == 'local icon = "rbxassetid://55"'

    def test_resources_load_no_extension_form(self):
        # Unity Resources.Load uses extension-less "Assets/..." paths.
        scripts = [_script('local m = "Assets/Resources/MyModel"')]
        n = rewrite_asset_references(
            scripts, {"Assets/Resources/MyModel.fbx": "rbxassetid://321"}
        )
        assert n == 1
        assert "rbxassetid://321" in scripts[0].source


class TestUnresolvedGuid:
    def test_unknown_path_left_untouched(self):
        original = 'local x = "Assets/Unknown/Missing.png"'
        scripts = [_script(original)]
        n = rewrite_asset_references(
            scripts, {"Assets/Textures/Other.png": "rbxassetid://1"}
        )
        assert n == 0
        assert scripts[0].source == original

    def test_partial_match_only_replaces_known_asset(self):
        scripts = [
            _script('local a = "Assets/Textures/Known.png"'),
            _script('local b = "Assets/Textures/Unknown.png"'),
        ]
        n = rewrite_asset_references(
            scripts, {"Assets/Textures/Known.png": "rbxassetid://42"}
        )
        assert n == 1
        assert "rbxassetid://42" in scripts[0].source
        assert scripts[1].source == 'local b = "Assets/Textures/Unknown.png"'


class TestFalsePositiveAvoidance:
    def test_short_key_not_used(self):
        # Keys shorter than _MIN_KEY_LEN (8) are filtered out.
        scripts = [_script('local s = "a.png"')]
        n = rewrite_asset_references(scripts, {"a.png": "rbxassetid://1"})
        assert n == 0
        assert scripts[0].source == 'local s = "a.png"'

    def test_non_path_string_not_rewritten(self):
        # A plain word string that does not look like a path or exact key
        # is left alone even if a substring coincidentally matches.
        scripts = [_script('local greeting = "hello world here"')]
        n = rewrite_asset_references(
            scripts, {"Assets/Textures/Diamond.png": "rbxassetid://1"}
        )
        assert n == 0


class TestMultipleScripts:
    def test_counts_scripts_changed_not_replacements(self):
        # Return value counts scripts modified, per the implementation.
        scripts = [
            _script('local a = "Assets/Tex/Gem.png"\nlocal b = "Assets/Tex/Gem.png"'),
            _script('local c = "Assets/Tex/Gem.png"'),
            _script('local d = "nothing here"'),
        ]
        n = rewrite_asset_references(
            scripts, {"Assets/Tex/Gem.png": "rbxassetid://88"}
        )
        assert n == 2
        assert scripts[0].source.count("rbxassetid://88") == 2
        assert "rbxassetid://88" in scripts[1].source
        assert scripts[2].source == 'local d = "nothing here"'

    def test_in_place_mutation(self):
        s = _script('local t = "Assets/Long/Texture.png"')
        scripts = [s]
        rewrite_asset_references(scripts, {"Assets/Long/Texture.png": "rbxassetid://5"})
        # Same object mutated in place.
        assert scripts[0] is s
        assert "rbxassetid://5" in s.source
