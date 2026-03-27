"""
studio_harness.py -- Test harness for automated Roblox Studio testing.

Opens rbxlx files in Studio, verifies they load correctly via MCP,
and closes them. Uses osascript (macOS) for window management.

SAFETY: Never interacts with "Agas Map of London" Studio instance.

Usage:
    from tests.studio_harness import StudioHarness
    harness = StudioHarness()
    success = harness.test_file_opens("output/test.rbxlx")
"""

from __future__ import annotations

import logging
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

log = logging.getLogger(__name__)

# SAFETY: Studio instances we must NEVER interact with.
_PROTECTED_WINDOWS = {"Agas Map", "Agas Map of London"}


class StudioHarness:
    """Automated test harness for Roblox Studio."""

    def __init__(self):
        pass

    def _is_protected_window(self, window_name: str) -> bool:
        """Check if a window name matches a protected Studio instance."""
        for protected in _PROTECTED_WINDOWS:
            if protected.lower() in window_name.lower():
                return True
        return False

    def open_file(self, rbxlx_path: str | Path, wait: float = 12.0) -> bool:
        """Open an rbxlx file in Studio via command line.

        Returns True if the open command succeeded (doesn't guarantee loading).
        """
        path = Path(rbxlx_path).resolve()
        if not path.exists():
            log.error("File not found: %s", path)
            return False

        # Safety check: don't open files that could interfere with protected instances
        if self._is_protected_window(path.stem):
            log.error("SAFETY: Refusing to open file matching protected window: %s", path.stem)
            return False

        log.info("Opening %s in Studio...", path.name)
        try:
            subprocess.run(
                ["open", "-a", "RobloxStudio", str(path)],
                check=True, timeout=10,
            )
        except Exception as e:
            log.error("Failed to open: %s", e)
            return False

        time.sleep(wait)
        return True

    def close_active_tab(self) -> None:
        """Close the active Studio tab via Cmd+W, then dismiss save dialog.

        SAFETY: Checks that the active window is not a protected instance.
        """
        # Safety: check active window before closing
        windows = self.get_studio_windows()
        if windows:
            active = windows[0]  # First window is typically the active one
            if self._is_protected_window(active):
                log.error("SAFETY: Active window is protected (%s), refusing to close", active)
                return

        try:
            subprocess.run([
                "osascript",
                "-e", 'tell application "RobloxStudio" to activate',
                "-e", 'delay 0.5',
                "-e", 'tell application "System Events" to keystroke "w" using command down',
            ], check=False, timeout=10)
            time.sleep(1)
            # Dismiss "Don't Save" dialog if it appears
            subprocess.run([
                "osascript",
                "-e", 'tell application "System Events"',
                "-e", '  try',
                "-e", '    click button "Don\\\'t Save" of window 1 of process "RobloxStudio"',
                "-e", '  end try',
                "-e", 'end tell',
            ], check=False, timeout=5)
            time.sleep(1)
        except Exception as e:
            log.warning("Close failed: %s", e)

    def get_studio_windows(self) -> list[str]:
        """Get names of all open Studio windows/tabs."""
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of every window of process "RobloxStudio"'],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return [w.strip() for w in result.stdout.strip().split(",")]
        except Exception:
            pass
        return []

    def is_studio_running(self) -> bool:
        """Check if Roblox Studio is running."""
        try:
            result = subprocess.run(
                ["pgrep", "-x", "RobloxStudio"],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def test_file_opens(self, rbxlx_path: str | Path, wait: float = 15.0) -> bool:
        """Test that an rbxlx file opens in Studio without crashing.

        Opens the file, waits, checks if Studio is still responsive,
        then closes the tab.

        Returns True if the file appears to have loaded.
        """
        path = Path(rbxlx_path).resolve()

        # Get window list before opening
        before = self.get_studio_windows()

        if not self.open_file(path, wait=wait):
            return False

        # Check Studio is still running (didn't crash)
        if not self.is_studio_running():
            log.error("Studio crashed while loading %s", path.name)
            return False

        # Check if a new window/tab appeared
        after = self.get_studio_windows()
        new_windows = [w for w in after if w not in before]
        if new_windows:
            log.info("New window: %s", new_windows[0])

        # Check window name for error indicators
        for w in after:
            if path.stem in w:
                log.info("File loaded: %s", w)
                self.close_active_tab()
                return True

        # File might have failed to open (error dialog)
        # Studio would still show the old window
        log.warning("File may not have loaded — no matching window found")
        self.close_active_tab()
        return len(after) > len(before)


def validate_rbxlx(rbxlx_path: str | Path) -> dict:
    """Validate an rbxlx file structure without opening Studio.

    Checks:
    - XML is well-formed
    - Has roblox root element
    - Has Workspace service
    - Has Lighting service
    - Counts parts, scripts, UI elements
    - Checks for common issues (empty scripts, missing properties)

    Returns:
        Dict with validation results.
    """
    path = Path(rbxlx_path).resolve()
    result = {
        "valid": False,
        "path": str(path),
        "errors": [],
        "warnings": [],
        "stats": {},
    }

    if not path.exists():
        result["errors"].append(f"File not found: {path}")
        return result

    # Parse XML
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except ET.ParseError as e:
        result["errors"].append(f"XML parse error: {e}")
        return result

    # Check root element
    if root.tag != "roblox":
        result["errors"].append(f"Root element is '{root.tag}', expected 'roblox'")
        return result

    # Count instances by class
    class_counts: dict[str, int] = {}
    for item in root.iter("Item"):
        cls = item.get("class", "Unknown")
        class_counts[cls] = class_counts.get(cls, 0) + 1

    result["stats"]["total_items"] = sum(class_counts.values())
    result["stats"]["class_counts"] = class_counts

    # Check required services
    services = {item.get("class") for item in root.findall("Item")}
    for required in ("Workspace", "Lighting"):
        if required not in services:
            result["errors"].append(f"Missing required service: {required}")

    # Count specific types
    parts = class_counts.get("Part", 0) + class_counts.get("MeshPart", 0)
    models = class_counts.get("Model", 0)
    scripts = class_counts.get("Script", 0) + class_counts.get("LocalScript", 0)
    screen_guis = class_counts.get("ScreenGui", 0)

    result["stats"]["parts"] = parts
    result["stats"]["models"] = models
    result["stats"]["scripts"] = scripts
    result["stats"]["screen_guis"] = screen_guis

    # Check for new feature types
    result["stats"]["constraints"] = sum(
        class_counts.get(c, 0) for c in
        ("WeldConstraint", "HingeConstraint", "SpringConstraint", "BallSocketConstraint", "RodConstraint")
    )
    result["stats"]["trails"] = class_counts.get("Trail", 0)
    result["stats"]["beams"] = class_counts.get("Beam", 0)
    result["stats"]["post_processing"] = sum(
        class_counts.get(c, 0) for c in
        ("BloomEffect", "ColorCorrectionEffect", "DepthOfFieldEffect", "SunRaysEffect", "Atmosphere")
    )
    result["stats"]["layout"] = sum(
        class_counts.get(c, 0) for c in ("UIListLayout", "UIGridLayout")
    )

    # Warnings for potential issues
    if parts == 0:
        result["warnings"].append("No parts found in Workspace")
    if scripts == 0:
        result["warnings"].append("No scripts found")

    # Check for empty scripts
    empty_scripts = 0
    for item in root.iter("Item"):
        if item.get("class") in ("Script", "LocalScript"):
            source_elem = item.find(".//ProtectedString[@name='Source']")
            if source_elem is not None and (not source_elem.text or source_elem.text.strip() == ""):
                empty_scripts += 1
    if empty_scripts:
        result["warnings"].append(f"{empty_scripts} empty script(s) found")

    result["valid"] = len(result["errors"]) == 0
    return result


def quick_test(rbxlx_path: str | Path) -> bool:
    """Quick test: does this rbxlx file open in Studio?"""
    return StudioHarness().test_file_opens(rbxlx_path)
