"""
Screenshot capture from both Unity and Roblox engines.

Provides utilities to position a camera and capture screenshots in Unity
via an Editor script (batch mode), and to capture screenshots from Roblox
via the MCP tools.
"""

from __future__ import annotations

import logging
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Unity Editor C# script template
# ---------------------------------------------------------------------------

_UNITY_CAPTURE_SCRIPT = textwrap.dedent("""\
    using UnityEditor;
    using UnityEditor.SceneManagement;
    using UnityEngine;
    using System.IO;

    public static class ScreenshotCapture
    {
        /// <summary>
        /// Capture a screenshot from the scene's main camera or a custom pose.
        ///
        /// Usage A (use scene camera):
        ///   Unity -batchmode -projectPath <path>
        ///       -executeMethod ScreenshotCapture.Capture
        ///       -- <scenePath> <outputPath>
        ///
        /// Usage B (custom camera pose):
        ///   Unity -batchmode -projectPath <path>
        ///       -executeMethod ScreenshotCapture.Capture
        ///       -- <scenePath> <outputPath> <posX> <posY> <posZ> <rotX> <rotY> <rotZ>
        /// </summary>
        [MenuItem("Tools/Capture Screenshot")]
        public static void Capture()
        {
            string[] args = System.Environment.GetCommandLineArgs();
            int idx = System.Array.IndexOf(args, "--");
            if (idx < 0 || idx + 2 >= args.Length)
            {
                Debug.LogError("ScreenshotCapture: expected at least 2 args after '--': scenePath outputPath");
                EditorApplication.Exit(1);
                return;
            }

            string scenePath = args[idx + 1];
            string outputPath = args[idx + 2];
            bool customPose = (idx + 8 < args.Length);

            // Open the scene
            Debug.Log($"Opening scene: {scenePath}");
            var scene = EditorSceneManager.OpenScene(scenePath, OpenSceneMode.Single);
            if (!scene.IsValid())
            {
                Debug.LogError($"Failed to open scene: {scenePath}");
                EditorApplication.Exit(1);
                return;
            }

            Camera cam = null;

            if (customPose)
            {
                float px = float.Parse(args[idx + 3]);
                float py = float.Parse(args[idx + 4]);
                float pz = float.Parse(args[idx + 5]);
                float rx = float.Parse(args[idx + 6]);
                float ry = float.Parse(args[idx + 7]);
                float rz = float.Parse(args[idx + 8]);

                GameObject camObj = new GameObject("__CaptureCam");
                cam = camObj.AddComponent<Camera>();
                cam.transform.position = new Vector3(px, py, pz);
                cam.transform.eulerAngles = new Vector3(rx, ry, rz);
                cam.clearFlags = CameraClearFlags.Skybox;
            }
            else
            {
                // Use the scene's main camera
                cam = Camera.main;
                if (cam == null)
                {
                    // Find any camera in the scene
                    cam = Object.FindObjectOfType<Camera>();
                }
                if (cam == null)
                {
                    Debug.LogError("No camera found in scene");
                    EditorApplication.Exit(1);
                    return;
                }
                Debug.Log($"Using camera: {cam.name} at {cam.transform.position}");
            }

            int width = 1920;
            int height = 1080;
            RenderTexture rt = new RenderTexture(width, height, 24);
            cam.targetTexture = rt;
            cam.Render();

            RenderTexture.active = rt;
            Texture2D tex = new Texture2D(width, height, TextureFormat.RGB24, false);
            tex.ReadPixels(new Rect(0, 0, width, height), 0, 0);
            tex.Apply();

            byte[] bytes = tex.EncodeToPNG();
            string dir = Path.GetDirectoryName(outputPath);
            if (!string.IsNullOrEmpty(dir) && !Directory.Exists(dir))
                Directory.CreateDirectory(dir);
            File.WriteAllBytes(outputPath, bytes);

            RenderTexture.active = null;
            cam.targetTexture = null;
            Object.DestroyImmediate(rt);

            if (customPose)
                Object.DestroyImmediate(cam.gameObject);

            Debug.Log($"Screenshot saved to {outputPath} ({width}x{height})");
            Debug.Log($"Camera pos: {cam?.transform.position}, rot: {cam?.transform.eulerAngles}");

            EditorApplication.Exit(0);
        }
    }
""")


# ---------------------------------------------------------------------------
# Unity Editor paths (macOS)
# ---------------------------------------------------------------------------

_UNITY_EDITOR_PATHS = [
    "/Applications/Unity/Hub/Editor/2023.2.1f1/Unity.app/Contents/MacOS/Unity",
    "/Applications/Unity/Hub/Editor/6000.3.11f1/Unity.app/Contents/MacOS/Unity",
    "/Applications/Unity/Hub/Editor/2020.3.26f1/Unity.app/Contents/MacOS/Unity",
]


def _find_unity_editor(preferred_version: str = "") -> str | None:
    """Find a Unity Editor binary on macOS."""
    if preferred_version:
        path = f"/Applications/Unity/Hub/Editor/{preferred_version}/Unity.app/Contents/MacOS/Unity"
        if Path(path).exists():
            return path

    for path in _UNITY_EDITOR_PATHS:
        if Path(path).exists():
            return path
    return None


def _get_project_unity_version(project_path: Path) -> str:
    """Read the Unity version from ProjectVersion.txt."""
    version_file = project_path / "ProjectSettings" / "ProjectVersion.txt"
    if version_file.exists():
        for line in version_file.read_text().splitlines():
            if line.startswith("m_EditorVersion:"):
                return line.split(":", 1)[1].strip()
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def setup_unity_capture_script(project_path: str | Path) -> Path:
    """Write the C# Editor script into the Unity project.

    Args:
        project_path: Root of the Unity project (the folder containing Assets/).

    Returns:
        Path to the written C# script file.
    """
    project_path = Path(project_path)
    editor_dir = project_path / "Assets" / "Editor"
    editor_dir.mkdir(parents=True, exist_ok=True)

    script_path = editor_dir / "ScreenshotCapture.cs"
    script_path.write_text(_UNITY_CAPTURE_SCRIPT, encoding="utf-8")
    logger.info("Unity capture script written to %s", script_path)
    return script_path


def run_unity_screenshot(
    unity_project_path: str | Path,
    scene_path: str,
    output_path: str | Path,
    camera_position: Tuple[float, float, float] | None = None,
    camera_rotation: Tuple[float, float, float] | None = None,
    timeout: int = 300,
) -> Path | None:
    """Execute Unity in batch mode to capture a screenshot.

    Args:
        unity_project_path: Root of the Unity project.
        scene_path: Scene path relative to project (e.g. "Assets/Scenes/main.unity").
        output_path: Desired file path for the output PNG.
        camera_position: Optional world-space (x, y, z). If None, uses scene camera.
        camera_rotation: Optional euler angles (x, y, z) in degrees.
        timeout: Max seconds to wait for Unity.

    Returns:
        Path to the screenshot if successful, None otherwise.
    """
    project_path = Path(unity_project_path).resolve()
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Install the capture script
    setup_unity_capture_script(project_path)

    # Find Unity editor
    version = _get_project_unity_version(project_path)
    unity_bin = _find_unity_editor(version)
    if not unity_bin:
        logger.error("No Unity Editor found. Tried version %s", version)
        return None

    # Build command
    cmd = [
        unity_bin,
        "-batchmode",
        "-nographics",
        "-projectPath", str(project_path),
        "-executeMethod", "ScreenshotCapture.Capture",
        "-logFile", str(output_path.parent / "unity_capture.log"),
        "--", scene_path, str(output_path),
    ]

    if camera_position and camera_rotation:
        cmd.extend([
            str(camera_position[0]), str(camera_position[1]), str(camera_position[2]),
            str(camera_rotation[0]), str(camera_rotation[1]), str(camera_rotation[2]),
        ])

    logger.info("Running Unity: %s", " ".join(cmd[:8]) + " ...")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if output_path.exists():
            logger.info("Unity screenshot captured: %s", output_path)
            return output_path
        else:
            logger.error("Unity screenshot not created. Exit code: %d", result.returncode)
            log_file = output_path.parent / "unity_capture.log"
            if log_file.exists():
                log_tail = log_file.read_text()[-2000:]
                logger.error("Unity log tail:\n%s", log_tail)
            return None
    except subprocess.TimeoutExpired:
        logger.error("Unity screenshot timed out after %ds", timeout)
        return None
    except Exception as exc:
        logger.exception("Unity screenshot failed: %s", exc)
        return None


def get_scene_camera_info(
    unity_project_path: str | Path,
    scene_path: str,
) -> dict | None:
    """Parse the Unity scene to find the main camera's position and rotation.

    Returns dict with 'position' (x,y,z) and 'rotation' (x,y,z) euler angles,
    or None if no camera found.
    """
    from pathlib import Path as P
    import sys
    sys.path.insert(0, str(P(__file__).parent.parent))

    try:
        from unity.scene_parser import parse_scene

        project_path = P(unity_project_path)
        full_scene_path = project_path / scene_path
        scene = parse_scene(full_scene_path)

        # Find camera node
        for root in scene.roots:
            cam_info = _find_camera_recursive(root)
            if cam_info:
                return cam_info

        return None
    except Exception as exc:
        logger.warning("Could not parse scene camera: %s", exc)
        return None


def _find_camera_recursive(node) -> dict | None:
    """Recursively find a camera component in the scene tree."""
    for comp in node.components:
        if comp.component_type == "Camera":
            import math
            pos = node.position
            rot = node.rotation  # quaternion (x, y, z, w)
            # Convert quaternion to euler angles (degrees)
            qx, qy, qz, qw = rot
            # Euler from quaternion (ZYX convention)
            sinr = 2.0 * (qw * qx + qy * qz)
            cosr = 1.0 - 2.0 * (qx * qx + qy * qy)
            ex = math.degrees(math.atan2(sinr, cosr))
            sinp = 2.0 * (qw * qy - qz * qx)
            if abs(sinp) >= 1:
                ey = math.copysign(90, sinp)
            else:
                ey = math.degrees(math.asin(sinp))
            siny = 2.0 * (qw * qz + qx * qy)
            cosy = 1.0 - 2.0 * (qy * qy + qz * qz)
            ez = math.degrees(math.atan2(siny, cosy))

            fov = float(comp.properties.get("field of view", 60))

            return {
                "position": tuple(pos),
                "rotation_euler": (ex, ey, ez),
                "rotation_quat": tuple(rot),
                "fov": fov,
                "name": node.name,
            }

    for child in node.children:
        result = _find_camera_recursive(child)
        if result:
            return result
    return None


def capture_roblox_screenshot() -> Optional[Path]:
    """Capture a screenshot from Roblox Studio via the MCP take_screenshot tool.

    Since the actual MCP call is made externally by the orchestrator, this
    function returns a placeholder path that the caller should replace with the
    real screenshot once the MCP tool has executed.

    Returns:
        A placeholder :class:`Path` pointing to the expected screenshot
        location, or ``None`` if preconditions are not met.
    """
    placeholder = Path("roblox_screenshot.png")
    logger.info(
        "Roblox screenshot capture requested. "
        "The orchestrator should invoke mcp__Roblox_Studio__screen_capture "
        "and save the result to %s",
        placeholder,
    )
    return placeholder
