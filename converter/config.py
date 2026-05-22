"""
config.py -- Global configuration for the Unity -> Roblox converter.

All modules read from this file; none of them modify it.
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

UNITY_PROJECT_PATH: Path = Path(os.environ.get("UNITY_PROJECT_PATH", "./unity_project"))
OUTPUT_DIR: Path = Path(os.environ.get("OUTPUT_DIR", "./output"))
TEMP_DIR: Path = Path(os.environ.get("TEMP_DIR", "./tmp"))

def _default_studio_path() -> str:
    """Best-effort default for the Roblox Studio executable per platform.

    An empty default resolves to ``Path(".")``, which the launcher then
    tries to exec — yielding a confusing "Permission denied launching
    Studio: ." The /e2e-test skill's Studio-launch step depends on this
    resolving to a real binary, so default to each platform's standard
    install location and let ``ROBLOX_STUDIO_PATH`` override.
    """
    import platform
    system = platform.system()
    if system == "Darwin":
        return "/Applications/RobloxStudio.app/Contents/MacOS/RobloxStudio"
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            # Versioned install dir; the launcher's bootstrapper symlink.
            return os.path.join(local, "Roblox", "Versions", "RobloxStudioBeta.exe")
    return ""


STUDIO_PATH: Path = Path(
    os.environ.get(
        "ROBLOX_STUDIO_PATH",
        _default_studio_path(),
    )
)

# ---------------------------------------------------------------------------
# Anthropic (Claude) API
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL: str = "claude-sonnet-4-6"
ANTHROPIC_MAX_TOKENS: int = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "16384"))

# ---------------------------------------------------------------------------
# Asset extraction options
# ---------------------------------------------------------------------------

ASSET_EXT_TO_KIND: dict[str, str] = {
    # Textures
    ".png": "texture", ".jpg": "texture", ".jpeg": "texture",
    ".tga": "texture", ".bmp": "texture", ".exr": "texture",
    ".hdr": "texture", ".psd": "texture", ".gif": "texture",
    ".tif": "texture", ".tiff": "texture", ".svg": "texture",
    # Meshes
    ".fbx": "mesh", ".obj": "mesh", ".dae": "mesh", ".blend": "mesh",
    ".gltf": "mesh", ".glb": "mesh",
    ".usd": "mesh", ".usda": "mesh", ".usdc": "mesh", ".usdz": "mesh",
    ".ply": "mesh", ".stl": "mesh",
    # Audio
    ".wav": "audio", ".mp3": "audio", ".ogg": "audio",
    ".aiff": "audio", ".aif": "audio", ".flac": "audio",
    # Video
    ".mp4": "video", ".webm": "video", ".mov": "video",
    # Materials / animations / shaders
    ".mat": "material",
    ".anim": "animation", ".controller": "animation",
    ".overrideController": "animation",
    ".mask": "animation",
    ".shader": "shader", ".cginc": "shader", ".hlsl": "shader",
    ".shadergraph": "shader", ".shadersubgraph": "shader",
    ".compute": "shader",
    # Fonts
    ".ttf": "font", ".otf": "font", ".fontsettings": "font",
    # Scene / prefab / script
    ".prefab": "prefab",
    ".unity": "scene",
    ".cs": "script",
    # Assembly definitions
    ".asmdef": "assembly_definition",
    ".asmref": "assembly_definition",
    # Data
    ".asset": "scriptable_object",
    ".json": "data", ".xml": "data", ".yaml": "data", ".yml": "data",
    ".txt": "data", ".csv": "data",
    # Other
    ".preset": "preset",
    ".lighting": "lighting",
    ".terrainlayer": "terrain",
    ".inputactions": "input",
    ".playable": "timeline",
    ".signal": "timeline",
    ".spriteatlas": "texture",
}

SUPPORTED_ASSET_EXTENSIONS: frozenset[str] = frozenset(ASSET_EXT_TO_KIND.keys())

# ---------------------------------------------------------------------------
# Scene / prefab parsing
# ---------------------------------------------------------------------------

UNITY_SCENE_EXT: str = ".unity"
UNITY_PREFAB_EXT: str = ".prefab"
MAX_SCENE_DEPTH: int = 64

# ---------------------------------------------------------------------------
# Code transpilation
# ---------------------------------------------------------------------------

USE_AI_TRANSPILATION: bool = True  # Always use AI as primary, rule-based as fallback
TRANSPILATION_CONFIDENCE_THRESHOLD: float = float(os.environ.get("TRANSPILATION_CONFIDENCE_THRESHOLD", "0.7"))
AI_TRANSPILE_FIRST: bool = True  # Try AI before rule-based

# Per-call wall-clock ceiling for the `claude -p` CLI transpile (and each
# lint/contract reprompt). A large controller script (e.g. SimpleFPS's
# 307-line Player.cs) cold-transpiles well past the old hardcoded 600s,
# so the offline-assembly e2e test — which intentionally runs cold, no
# cache reliance — would time out mid-script and silently fall back to a
# stub. 1200s gives a generous cold-transpile budget; override via env
# for slower machines or even larger scripts.
CLAUDE_CLI_TIMEOUT_SECONDS: int = int(os.environ.get("CLAUDE_CLI_TIMEOUT_SECONDS", "1200"))

# ---------------------------------------------------------------------------
# LLM cache
# ---------------------------------------------------------------------------

LLM_CACHE_ENABLED: bool = True
LLM_CACHE_DIR: Path = Path(os.environ.get("LLM_CACHE_DIR", ".cache/llm"))
LLM_CACHE_TTL_SECONDS: float = 7 * 24 * 3600  # 7 days

# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------

RETRY_MAX_ATTEMPTS: int = 4
RETRY_BASE_DELAY: float = 2.0
RETRY_MAX_DELAY: float = 60.0
RETRY_BACKOFF_FACTOR: float = 2.0

# ---------------------------------------------------------------------------
# Roblox output
# ---------------------------------------------------------------------------

RBXLX_OUTPUT_FILENAME: str = "converted_place.rbxlx"

# Unit conversion: 1 Unity unit = 1 meter ≈ 3.571 Roblox studs (1 stud ≈ 0.28m)
STUDS_PER_METER: float = 3.571

# ---------------------------------------------------------------------------
# Roblox Open Cloud
# ---------------------------------------------------------------------------

ROBLOX_API_KEY: str = ""  # loaded at runtime from apikey file
ROBLOX_UNIVERSE_ID: int | None = None
ROBLOX_PLACE_ID: int | None = None
ROBLOX_CREATOR_ID: int | None = None
ROBLOX_CREATOR_TYPE: str = "User"

# ---------------------------------------------------------------------------
# Mesh decimation
# ---------------------------------------------------------------------------

MESH_DECIMATION_ENABLED: bool = True
MESH_ROBLOX_MAX_FACES: int = 10_000
MESH_TARGET_FACES: int = 8_000
MESH_QUALITY_FLOOR: float = 0.6

# ---------------------------------------------------------------------------
# Material mapper
# ---------------------------------------------------------------------------

TEXTURE_MAX_RESOLUTION: int = 4096
TEXTURE_OUTPUT_FORMAT: str = "png"
GENERATE_UNIFORM_TEXTURES: bool = True
PRE_TILE_MAX_FACTOR: int = 4
FLIP_NORMAL_GREEN_CHANNEL: bool = False

# ---------------------------------------------------------------------------
# Packages (prefab packages for ReplicatedStorage/Templates)
# ---------------------------------------------------------------------------

EMIT_PACKAGES: bool = True
PACKAGES_SUBDIR: str = "packages"

# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

REPORT_FILENAME: str = "conversion_report.json"
REPORT_VERBOSE: bool = True
UNCONVERTED_FILENAME: str = "UNCONVERTED.md"
