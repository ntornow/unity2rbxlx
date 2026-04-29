"""Phase 5.2a: u2r.py validate --strict-asset-ids gates on placeholder URLs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the u2r.py module importable as a script.
sys.path.insert(0, str(Path(__file__).parent.parent))

from click.testing import CliRunner

from u2r import main  # noqa: E402


def _write_rbxlx(tmp_path: Path, contents_xml: str) -> Path:
    """Wrap a snippet in the minimal rbxlx envelope."""
    rbxlx = tmp_path / "scene.rbxlx"
    rbxlx.write_text(
        f'<roblox version="4">\n{contents_xml}\n</roblox>',
        encoding="utf-8",
    )
    return rbxlx


class TestValidateStrictAssetIds:
    """Phase 5.2a: --strict-asset-ids exits non-zero on placeholder URLs."""

    def test_no_placeholders_passes(self, tmp_path: Path):
        rbxlx = _write_rbxlx(tmp_path, """
            <Item class="MeshPart" referent="A">
              <Properties>
                <Content name="MeshId">
                  <url>rbxassetid://1234567890</url>
                </Content>
              </Properties>
            </Item>
        """)
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(rbxlx), "--strict-asset-ids"])
        assert result.exit_code == 0, result.output

    def test_placeholder_zero_triggers_failure(self, tmp_path: Path):
        rbxlx = _write_rbxlx(tmp_path, """
            <Item class="MeshPart" referent="A">
              <Properties>
                <Content name="MeshId">
                  <url>rbxassetid://0</url>
                </Content>
              </Properties>
            </Item>
        """)
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(rbxlx), "--strict-asset-ids"])
        assert result.exit_code == 1, result.output
        assert "placeholder" in result.output.lower() or "rbxassetid" in result.output.lower()

    def test_placeholder_warning_without_strict(self, tmp_path: Path):
        """Default mode: report placeholders as a warning, exit 0."""
        rbxlx = _write_rbxlx(tmp_path, """
            <Item class="MeshPart" referent="A">
              <Properties>
                <Content name="MeshId">
                  <url>rbxassetid://0</url>
                </Content>
              </Properties>
            </Item>
        """)
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(rbxlx)])
        assert result.exit_code == 0, result.output
        assert "placeholder" in result.output.lower()

    def test_long_zero_run_recognized_as_placeholder(self, tmp_path: Path):
        """rbxassetid://0000 (any leading-zero-only ID) is also a placeholder."""
        rbxlx = _write_rbxlx(tmp_path, """
            <Item class="MeshPart" referent="A">
              <Properties>
                <Content name="MeshId">
                  <url>rbxassetid://0000</url>
                </Content>
              </Properties>
            </Item>
        """)
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(rbxlx), "--strict-asset-ids"])
        assert result.exit_code == 1, result.output

    def test_rbxassetid_with_query_string_zero_is_placeholder(self, tmp_path: Path):
        """rbxassetid://0?something is still a placeholder (regex covers ?, $, /)."""
        rbxlx = _write_rbxlx(tmp_path, """
            <Item class="Decal" referent="A">
              <Properties>
                <Content name="Texture">
                  <url>rbxassetid://0?cache=foo</url>
                </Content>
              </Properties>
            </Item>
        """)
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(rbxlx), "--strict-asset-ids"])
        assert result.exit_code == 1, result.output

    def test_real_id_with_zero_inside_does_not_match(self, tmp_path: Path):
        """rbxassetid://10000 (real ID with embedded zeros) is NOT a placeholder."""
        rbxlx = _write_rbxlx(tmp_path, """
            <Item class="MeshPart" referent="A">
              <Properties>
                <Content name="MeshId">
                  <url>rbxassetid://10000</url>
                </Content>
              </Properties>
            </Item>
        """)
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(rbxlx), "--strict-asset-ids"])
        assert result.exit_code == 0, result.output
