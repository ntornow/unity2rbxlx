"""
luau_place_builder.py -- Generate a Luau script that reconstructs a Roblox place
from an RbxPlace object, for execution via the Open Cloud Luau Execution API.

The generated script:
1. Clears workspace (except Terrain and Camera)
2. Creates all Parts with properties (CFrame, Size, Color, Material, etc.)
3. Uses AssetService:CreateMeshPartAsync() for MeshParts
4. Creates Scripts with source code
5. Sets Lighting properties and post-processing effects
6. Creates UI (ScreenGuis)
7. Writes terrain if small enough, otherwise relies on TerrainGenerator
8. Calls AssetService:SavePlaceAsync() at the end
"""

from __future__ import annotations

import logging
from typing import Any

from core.roblox_types import (
    RbxCFrame,
    RbxLight,
    RbxPart,
    RbxParticleEmitter,
    RbxPlace,
    RbxPostProcessing,
    RbxScript,
    RbxScreenGui,
    RbxSound,
    RbxSurfaceAppearance,
    RbxUIElement,
)

log = logging.getLogger(__name__)

# Maximum output size (Luau Execution API limit)
_MAX_SCRIPT_BYTES = 4 * 1024 * 1024  # 4 MB

# Material name -> Enum.Material member name
_MATERIAL_MAP = {
    "Plastic": "Plastic", "SmoothPlastic": "SmoothPlastic", "Neon": "Neon",
    "Wood": "Wood", "WoodPlanks": "WoodPlanks", "Marble": "Marble",
    "Basalt": "Basalt", "Slate": "Slate", "CrackedLava": "CrackedLava",
    "Concrete": "Concrete", "Limestone": "Limestone", "Pavement": "Pavement",
    "Granite": "Granite", "Brick": "Brick", "Pebble": "Pebble",
    "Cobblestone": "Cobblestone", "Rock": "Rock", "Sandstone": "Sandstone",
    "CorrodedMetal": "CorrodedMetal", "DiamondPlate": "DiamondPlate",
    "Foil": "Foil", "Metal": "Metal", "Grass": "Grass",
    "LeafyGrass": "LeafyGrass", "Sand": "Sand", "Fabric": "Fabric",
    "Snow": "Snow", "Mud": "Mud", "Ground": "Ground", "Asphalt": "Asphalt",
    "Salt": "Salt", "Ice": "Ice", "Glacier": "Glacier", "Glass": "Glass",
    "ForceField": "ForceField",
}


def _f(v: float) -> str:
    """Format a float compactly."""
    if v == int(v):
        return str(int(v))
    return f"{v:.4g}"


def _cf(c: RbxCFrame) -> str:
    """Emit CFrame.new(x,y,z, r00..r22)."""
    return (
        f"CFrame.new({_f(c.x)},{_f(c.y)},{_f(c.z)},"
        f"{_f(c.r00)},{_f(c.r01)},{_f(c.r02)},"
        f"{_f(c.r10)},{_f(c.r11)},{_f(c.r12)},"
        f"{_f(c.r20)},{_f(c.r21)},{_f(c.r22)})"
    )


def _v3(x: float, y: float, z: float) -> str:
    return f"Vector3.new({_f(x)},{_f(y)},{_f(z)})"


def _c3(r: float, g: float, b: float) -> str:
    return f"Color3.new({_f(r)},{_f(g)},{_f(b)})"


def _c3u8(r: float, g: float, b: float) -> str:
    """Color from 0-1 floats via fromRGB (0-255)."""
    ri = int(r * 255) if r <= 1.0 else int(r)
    gi = int(g * 255) if g <= 1.0 else int(g)
    bi = int(b * 255) if b <= 1.0 else int(b)
    return f"Color3.fromRGB({ri},{gi},{bi})"


def _luau_str(s: str) -> str:
    """Escape a string for Luau. Uses long brackets if it contains quotes/newlines."""
    if "\n" not in s and '"' not in s and "\\" not in s:
        return f'"{s}"'
    # Find a bracket level that doesn't clash
    level = 0
    while f"]{'=' * level}]" in s:
        level += 1
    eq = "=" * level
    return f"[{eq}[{s}]{eq}]"


def _mat_enum(mat: str | int) -> str:
    """Convert material to Enum.Material.X."""
    if isinstance(mat, int):
        # Reverse-lookup from token value is complex; just use Plastic
        return "Enum.Material.Plastic"
    name = _MATERIAL_MAP.get(mat, "Plastic")
    return f"Enum.Material.{name}"


class _LuauBuilder:
    """Accumulates Luau source lines with indentation."""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._indent = 0

    def line(self, text: str = "") -> None:
        if text:
            self._lines.append("\t" * self._indent + text)
        else:
            self._lines.append("")

    def indent(self) -> None:
        self._indent += 1

    def dedent(self) -> None:
        self._indent = max(0, self._indent - 1)

    def block(self, header: str) -> None:
        """Start a block like 'do', 'if ... then', etc."""
        self.line(header)
        self.indent()

    def end(self, suffix: str = "") -> None:
        self.dedent()
        self.line(f"end{suffix}")

    def build(self) -> str:
        return "\n".join(self._lines)


def generate_place_luau(
    place: RbxPlace,
    mesh_cache: dict[str, str] | None = None,
) -> str:
    """Generate a Luau script that reconstructs the entire place.

    The script:
    1. Clears workspace (except Terrain and Camera)
    2. Creates all Parts with their properties
    3. For MeshParts, uses AssetService:CreateMeshPartAsync()
    4. Creates all Scripts with source code
    5. Sets up Lighting properties and post-processing
    6. Creates UI ScreenGuis
    7. Skips terrain SmoothGrid if too large (relies on TerrainGenerator)
    8. Calls AssetService:SavePlaceAsync() at the end

    Args:
        place: The RbxPlace object from the converter
        mesh_cache: Optional mapping of mesh URLs to resolved real MeshIds

    Returns:
        A Luau script string (should be under 4MB)
    """
    mesh_cache = mesh_cache or {}
    b = _LuauBuilder()

    # Header / services
    b.line("-- Auto-generated place reconstruction script")
    b.line("-- Execute via Open Cloud Luau Execution API")
    b.line("local AssetService=game:GetService('AssetService')")
    b.line("local Lighting=game:GetService('Lighting')")
    b.line("local SSS=game:GetService('ServerScriptService')")
    b.line("local SS=game:GetService('ServerStorage')")
    b.line("local RS=game:GetService('ReplicatedStorage')")
    b.line("local SP=game:GetService('StarterPlayer')")
    b.line("local SG=game:GetService('StarterGui')")
    b.line("local WS=game.Workspace")
    b.line("local terrain=WS.Terrain")
    b.line()

    # Helper: safe CreateMeshPartAsync wrapper.
    # ``collFid`` is an optional Enum.CollisionFidelity; when set, it's
    # passed to CreateMeshPartAsync as the `CollisionFidelity` option
    # field. The MeshPart is *cooked* with that fidelity at creation
    # time — assigning ``mp.CollisionFidelity = …`` after the fact does
    # not re-cook the mesh, so Studio silently snaps the property back
    # to whatever shape Roblox actually built (typically Box). This is
    # the only path that preserves PreciseConvexDecomposition for
    # concave collision proxies (e.g. Door's frame_col with the
    # doorway hole) through the headless publish.
    b.line("local function mkMesh(meshId,cf,sz,col,mat,tr,anch,collFid)")
    b.indent()
    b.line("local ok,mp=pcall(function()")
    b.indent()
    b.line("if collFid then")
    b.indent()
    b.line("return AssetService:CreateMeshPartAsync(meshId,{CollisionFidelity=collFid})")
    b.end()
    b.line("return AssetService:CreateMeshPartAsync(meshId)")
    b.end(")")
    b.block("if ok and mp then")
    b.line("mp.CFrame=cf")
    b.line("mp.Size=sz")
    b.line("mp.Color=col")
    b.line("mp.Material=mat")
    b.line("if tr and tr>0 then mp.Transparency=tr end")
    b.line("mp.Anchored=anch~=false")
    b.line("mp.TopSurface=Enum.SurfaceType.Smooth")
    b.line("mp.BottomSurface=Enum.SurfaceType.Smooth")
    b.line("return mp")
    b.end()
    b.line("warn('CreateMeshPartAsync failed for '..meshId..' : '..tostring(mp))")
    b.line("local fb=Instance.new('Part')")
    b.line("fb.CFrame=cf fb.Size=sz fb.Color=col fb.Material=mat fb.Anchored=anch~=false")
    b.line("fb.TopSurface=Enum.SurfaceType.Smooth fb.BottomSurface=Enum.SurfaceType.Smooth")
    b.line("if tr and tr>0 then fb.Transparency=tr end")
    b.line("fb.Name='MeshFallback'")
    b.line("return fb")
    b.end()
    b.line()

    # Helper: create script instance
    b.line("local function mkScript(parent,cls,name,src)")
    b.indent()
    b.line("local s=Instance.new(cls)")
    b.line("s.Name=name")
    b.line("s.Source=src")
    b.line("s.Parent=parent")
    b.line("return s")
    b.end()
    b.line()

    # Clear workspace
    b.block("for _,c in WS:GetChildren() do")
    b.block("if c~=terrain and c.ClassName~='Camera' and c.ClassName~='Terrain' then")
    b.line("c:Destroy()")
    b.end()
    b.end()
    b.line()

    # Clear server script service, starter gui, etc.
    b.line("for _,c in SSS:GetChildren() do c:Destroy() end")
    b.line("for _,c in SG:GetChildren() do c:Destroy() end")
    b.line("for _,c in SS:GetChildren() do c:Destroy() end")
    b.line("for _,c in RS:GetChildren() do c:Destroy() end")
    b.line()

    # --- Lighting ---
    _emit_lighting(b, place.lighting, place.post_processing)
    b.line()

    # --- Skybox ---
    if place.skybox:
        sky = place.skybox
        b.block("do")
        b.line("local s=Instance.new('Sky')")
        b.line("s.Parent=Lighting")
        if sky.front:
            b.line(f"s.SkyboxFt={_luau_str(sky.front)}")
        if sky.back:
            b.line(f"s.SkyboxBk={_luau_str(sky.back)}")
        if sky.left:
            b.line(f"s.SkyboxLf={_luau_str(sky.left)}")
        if sky.right:
            b.line(f"s.SkyboxRt={_luau_str(sky.right)}")
        if sky.up:
            b.line(f"s.SkyboxUp={_luau_str(sky.up)}")
        if sky.down:
            b.line(f"s.SkyboxDn={_luau_str(sky.down)}")
        b.end()
        b.line()

    # --- Workspace parts ---
    part_counter = [0]
    for part in place.workspace_parts:
        _emit_part(b, part, "WS", mesh_cache, part_counter)

    # --- ServerStorage parts ---
    for part in place.server_storage_parts:
        _emit_part(b, part, "SS", mesh_cache, part_counter)

    # --- ReplicatedStorage prefab templates ---
    # rbxlx_writer.py emits these under ReplicatedStorage.Templates so
    # gameplay scripts can ``ReplicatedStorage.Templates:WaitForChild(...):Clone()``.
    # The headless path needs the same folder for two reasons:
    #   1. PrefabSpawner.luau (in place.scripts) calls
    #      ``ReplicatedStorage:WaitForChild("Templates")`` — without the
    #      folder it stalls forever.
    #   2. Phase 5.9 attaches prefab-scoped animation scripts under
    #      ``template.scripts`` (see Pipeline._reparent_prefab_scoped_animation_scripts).
    #      _emit_part recurses into ``part.scripts`` so the animation
    #      driver clones with the template.
    templates = getattr(place, "replicated_templates", None) or []
    if templates:
        b.block("do")
        b.line("local TF=Instance.new('Folder')")
        b.line("TF.Name='Templates'")
        b.line("TF.Parent=RS")
        for template in templates:
            _emit_part(b, template, "TF", mesh_cache, part_counter)
        b.end()

    b.line()

    # --- Auto-create RemoteEvents referenced by scripts -----------------
    # Mirror rbxlx_writer's auto-create scan: scripts reference cross-
    # trust-boundary RemoteEvents (e.g. PickupItemEvent) via
    # ``ReplicatedStorage:FindFirstChild/WaitForChild("Foo")``. Without
    # creating them here, FireClient/OnClientEvent silently no-op on the
    # headlessly-published place even though the rbxlx file works in Studio.
    _emit_remote_events(b, place)
    b.line()

    # --- Scripts ---
    _emit_scripts(b, place.scripts)
    b.line()

    # --- ScreenGuis ---
    global _ui_counter
    _ui_counter = 0
    for gui in place.screen_guis:
        _emit_screen_gui(b, gui)

    # --- Terrain ---
    _emit_terrain(b, place)
    b.line()

    # --- Water regions ---
    # Roblox Terrain:FillBlock has a 2048-stud-per-axis cap; oversized regions
    # silently no-op. Split each water region into a grid of <=MAX_FILL chunks.
    import math as _math
    MAX_FILL = 2048.0
    for wr in place.water_regions:
        sx = min(abs(wr.size[0]), MAX_FILL * 20)
        sy = min(abs(wr.size[1]), MAX_FILL)
        sz = min(abs(wr.size[2]), MAX_FILL * 20)
        nx = max(1, _math.ceil(sx / MAX_FILL))
        nz = max(1, _math.ceil(sz / MAX_FILL))
        chunk_sx = sx / nx
        chunk_sz = sz / nz
        start_x = wr.position[0] - sx / 2 + chunk_sx / 2
        start_z = wr.position[2] - sz / 2 + chunk_sz / 2
        for ix in range(nx):
            for iz in range(nz):
                cx = start_x + ix * chunk_sx
                cz = start_z + iz * chunk_sz
                b.line(
                    f"terrain:FillBlock(CFrame.new({_f(cx)},{_f(wr.position[1])},{_f(cz)}),"
                    f"{_v3(chunk_sx, sy, chunk_sz)},Enum.Material.Water)"
                )
    b.line()

    # Save
    b.line("AssetService:SavePlaceAsync()")
    b.line("print('Place reconstruction complete. Parts: '..#WS:GetDescendants())")

    script = b.build()

    # Size check
    size = len(script.encode("utf-8"))
    if size > _MAX_SCRIPT_BYTES:
        log.warning(
            "Generated Luau script is %.1f MB (limit 4 MB). "
            "Consider reducing part count or skipping terrain.",
            size / (1024 * 1024),
        )

    return script


# ---------------------------------------------------------------------------
# Lighting
# ---------------------------------------------------------------------------

def _emit_lighting(b: _LuauBuilder, cfg: Any, pp: RbxPostProcessing | None) -> None:
    b.line(f"Lighting.Brightness={_f(cfg.brightness)}")
    b.line(f"Lighting.Ambient={_c3(*cfg.ambient)}")
    b.line(f"Lighting.OutdoorAmbient={_c3(*cfg.outdoor_ambient)}")
    b.line(f"Lighting.ClockTime={_f(cfg.clock_time)}")
    b.line(f"Lighting.GeographicLatitude={_f(cfg.geographic_latitude)}")
    b.line(f"Lighting.FogColor={_c3(*cfg.fog_color)}")
    b.line(f"Lighting.FogStart={_f(cfg.fog_start)}")
    b.line(f"Lighting.FogEnd={_f(cfg.fog_end)}")
    b.line("pcall(function() Lighting.Technology=Enum.Technology.Future end)")
    b.line("pcall(function() Lighting.EnvironmentDiffuseScale=1 end)")
    b.line("pcall(function() Lighting.EnvironmentSpecularScale=1 end)")

    if pp is None:
        return

    # Bloom
    if pp.bloom_enabled:
        b.block("do")
        b.line("local e=Instance.new('BloomEffect')")
        b.line(f"e.Intensity={_f(pp.bloom_intensity)}")
        b.line(f"e.Size={_f(pp.bloom_size)}")
        b.line(f"e.Threshold={_f(pp.bloom_threshold)}")
        b.line("e.Parent=Lighting")
        b.end()

    # Color correction
    if pp.color_correction_enabled:
        b.block("do")
        b.line("local e=Instance.new('ColorCorrectionEffect')")
        b.line(f"e.Brightness={_f(pp.cc_brightness)}")
        b.line(f"e.Contrast={_f(pp.cc_contrast)}")
        b.line(f"e.Saturation={_f(pp.cc_saturation)}")
        b.line(f"e.TintColor={_c3(*pp.cc_tint_color)}")
        b.line("e.Parent=Lighting")
        b.end()

    # Depth of field
    if pp.dof_enabled:
        b.block("do")
        b.line("local e=Instance.new('DepthOfFieldEffect')")
        b.line(f"e.FarIntensity={_f(pp.dof_far_intensity)}")
        b.line(f"e.FocusDistance={_f(pp.dof_focus_distance)}")
        b.line(f"e.InFocusRadius={_f(pp.dof_in_focus_radius)}")
        b.line(f"e.NearIntensity={_f(pp.dof_near_intensity)}")
        b.line("e.Parent=Lighting")
        b.end()

    # Sun rays
    if pp.sun_rays_enabled:
        b.block("do")
        b.line("local e=Instance.new('SunRaysEffect')")
        b.line(f"e.Intensity={_f(pp.sun_rays_intensity)}")
        b.line(f"e.Spread={_f(pp.sun_rays_spread)}")
        b.line("e.Parent=Lighting")
        b.end()

    # Atmosphere
    if pp.atmosphere_enabled:
        b.block("do")
        b.line("local e=Instance.new('Atmosphere')")
        b.line(f"e.Density={_f(pp.atmosphere_density)}")
        b.line(f"e.Offset={_f(pp.atmosphere_offset)}")
        b.line(f"e.Color={_c3(*pp.atmosphere_color)}")
        b.line(f"e.Decay={_c3(*pp.atmosphere_decay_color)}")
        b.line(f"e.Glare={_f(pp.atmosphere_glare)}")
        b.line(f"e.Haze={_f(pp.atmosphere_haze)}")
        b.line("e.Parent=Lighting")
        b.end()


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------

def _emit_part(
    b: _LuauBuilder,
    part: RbxPart,
    parent_var: str,
    mesh_cache: dict[str, str],
    counter: list[int],
) -> None:
    """Emit Luau for one part (and recurse into children)."""
    cls = part.class_name or "Part"
    name = part.name or "Part"
    var = f"p{counter[0]}"
    counter[0] += 1

    if cls == "Model":
        b.block("do")
        b.line(f"local {var}=Instance.new('Model')")
        b.line(f"{var}.Name={_luau_str(name)}")
        if part.cframe:
            b.line(f"{var}.WorldPivot={_cf(part.cframe)}")
        _emit_attributes(b, part.attributes or {}, var)
        b.line(f"{var}.Parent={parent_var}")
        # Children
        for child in part.children or []:
            _emit_part(b, child, var, mesh_cache, counter)
        for script in part.scripts or []:
            _emit_script_inline(b, script, var)
        b.end()
        return

    is_mesh = cls == "MeshPart"
    mesh_url = part.mesh_id or ""
    resolved_mesh = mesh_cache.get(mesh_url, mesh_url) if mesh_url else ""

    # For MeshParts with a valid mesh URL, use CreateMeshPartAsync
    if is_mesh and resolved_mesh:
        b.block("do")
        cf_str = _cf(part.cframe) if part.cframe else "CFrame.new()"
        sz = part.size or (4, 1, 2)
        col = part.color or (0.63, 0.63, 0.63)
        mat = _mat_enum(part.material) if part.material else "Enum.Material.Plastic"
        tr = part.transparency or 0
        anchored = "true" if part.anchored else "false"

        # CollisionFidelity must be passed to CreateMeshPartAsync so the
        # mesh is cooked with the right collision shape; assignment after
        # the fact silently snaps back to whatever Roblox actually built
        # (typically Box), which destroys concave hole geometry like
        # door frames. The Python ``part.collision_fidelity`` is the
        # enum's int value (Default=0, Hull=1, Box=2, Precise=3).
        coll_fid_arg = "nil"
        cfid = getattr(part, "collision_fidelity", None)
        if cfid is not None and cfid != 0:
            _names = {1: "Hull", 2: "Box", 3: "PreciseConvexDecomposition"}
            n = _names.get(int(cfid))
            if n:
                coll_fid_arg = f"Enum.CollisionFidelity.{n}"

        b.line(
            f"local {var}=mkMesh({_luau_str(resolved_mesh)},"
            f"{cf_str},{_v3(*sz)},{_c3u8(*col)},{mat},{_f(tr)},{anchored},"
            f"{coll_fid_arg})"
        )
        b.line(f"{var}.Name={_luau_str(name)}")

        # Additional MeshPart properties
        _emit_part_extras(b, part, var)

        # Note: part.size already has the correct final size from the converter.
        # The mkMesh function sets mp.Size = sz (the pre-computed size).
        # The _ScaleX/Y/Z attributes are only used by the MeshLoader runtime
        # fallback where mp.Size starts as MeshSize (native) and needs scaling.
        # Here we DON'T apply _Scale — it would double the scaling.

        # TextureID
        if part.texture_id:
            b.line(f"{var}.TextureID={_luau_str(part.texture_id)}")

        # SurfaceAppearance
        if part.surface_appearance:
            _emit_surface_appearance(b, part.surface_appearance, var)

        b.line(f"{var}.Parent={parent_var}")

        # Children
        for child in part.children or []:
            _emit_part(b, child, var, mesh_cache, counter)
        for script in part.scripts or []:
            _emit_script_inline(b, script, var)
        # Lights, sounds, etc.
        _emit_attachments(b, part, var)
        b.end()
    else:
        # Regular Part (or MeshPart without mesh URL)
        b.block("do")
        inst_cls = "SpawnLocation" if part.class_name == "SpawnLocation" else "Part"
        b.line(f"local {var}=Instance.new('{inst_cls}')")
        b.line(f"{var}.Name={_luau_str(name)}")

        if part.cframe:
            b.line(f"{var}.CFrame={_cf(part.cframe)}")

        sz = part.size or (4, 1, 2)
        sx = min(2048.0, max(0.05, sz[0]))
        sy = min(2048.0, max(0.05, sz[1]))
        szz = min(2048.0, max(0.05, sz[2]))
        b.line(f"{var}.Size={_v3(sx, sy, szz)}")

        if part.color:
            b.line(f"{var}.Color={_c3u8(*part.color)}")

        if part.material:
            b.line(f"{var}.Material={_mat_enum(part.material)}")

        if part.transparency and part.transparency > 0:
            b.line(f"{var}.Transparency={_f(part.transparency)}")

        b.line(f"{var}.Anchored={'true' if part.anchored else 'false'}")
        b.line(f"{var}.CanCollide={'true' if part.can_collide else 'false'}")
        b.line(f"{var}.TopSurface=Enum.SurfaceType.Smooth")
        b.line(f"{var}.BottomSurface=Enum.SurfaceType.Smooth")

        _emit_part_extras(b, part, var)

        if part.shape is not None:
            shape_names = {1: "Ball", 2: "Block", 3: "Cylinder"}
            sn = shape_names.get(part.shape, "Block")
            b.line(f"{var}.Shape=Enum.PartType.{sn}")

        if inst_cls == "SpawnLocation":
            b.line(f"{var}.Duration=0")
            b.line(f"{var}.Neutral=true")

        b.line(f"{var}.Parent={parent_var}")

        # Children
        for child in part.children or []:
            _emit_part(b, child, var, mesh_cache, counter)
        for script in part.scripts or []:
            _emit_script_inline(b, script, var)
        _emit_attachments(b, part, var)
        b.end()


_REMOTE_EVENT_SKIP = {
    "PlayerGui", "HUD", "Module", "ItemModule", "Pause", "Crosshair",
    "Health", "Fill", "Ammo", "Cur", "Back", "CurHealth", "Label",
    "ResumeButton", "Frame", "Background", "Checkmark",
    "HumanoidRootPart", "Humanoid", "Head", "Torso", "Character",
    "Backpack", "PlayerScripts", "leaderstats",
    "Collider", "Trigger", "TriggerZone", "Detector",
    "Sensor", "Hitbox", "Range", "ProximityVolume",
    "PickupTouchDetector",
}


def _emit_remote_events(b: _LuauBuilder, place) -> None:
    """Auto-create RemoteEvents in ReplicatedStorage that scripts reference.

    Mirrors the scan in rbxlx_writer.py so FireClient / OnClientEvent
    pairs across server Scripts and LocalScripts (e.g. PickupItemEvent)
    actually have an instance to bind to in the headless publish path.
    """
    import re as _re

    # Walk every script anywhere in the place: top-level + part-attached + recursive children
    all_scripts: list = list(getattr(place, "scripts", None) or [])
    def _walk(parts):
        for p in parts or []:
            all_scripts.extend(getattr(p, "scripts", None) or [])
            _walk(getattr(p, "children", None) or [])
    _walk(getattr(place, "workspace_parts", None) or [])
    _walk(getattr(place, "server_storage_parts", None) or [])
    _walk(getattr(place, "replicated_templates", None) or [])

    candidates: set[str] = set()
    for s in all_scripts:
        src = getattr(s, "source", "") or ""
        for m in _re.finditer(r'(?:FindFirstChild|WaitForChild)\s*\(\s*"([^"]+)"\s*\)', src):
            candidates.add(m.group(1))

    # Reserve names occupied by ModuleScripts and prefab templates
    reserved = set(_REMOTE_EVENT_SKIP)
    if getattr(place, "replicated_templates", None):
        reserved.add("Templates")
        for tmpl in place.replicated_templates:
            tname = getattr(tmpl, "name", None)
            if tname:
                reserved.add(tname)
    for s in all_scripts:
        if getattr(s, "script_type", None) == "ModuleScript":
            tn = getattr(s, "name", None)
            if tn:
                reserved.add(tn)

    # BindableEvents created via ``Instance.new("BindableEvent"); ev.Name="X"``
    bindables: set[str] = set()
    for s in all_scripts:
        src = getattr(s, "source", "") or ""
        if "BindableEvent" in src:
            for m in _re.finditer(r'\.Name\s*=\s*"([^"]+)"', src):
                bindables.add(m.group(1))
    reserved |= bindables

    for name in sorted(candidates - reserved):
        is_remote = any(
            f'"{name}"' in (getattr(s, "source", "") or "") and
            any(api in (getattr(s, "source", "") or "") for api in (
                "FireServer", "OnServerEvent", "FireClient",
                "OnClientEvent", "FireAllClients",
            ))
            for s in all_scripts
        )
        if not is_remote:
            continue
        b.block("do")
        b.line("local re=Instance.new('RemoteEvent')")
        b.line(f"re.Name={_luau_str(name)}")
        b.line("re.Parent=RS")
        b.end()


def _emit_attributes(b: _LuauBuilder, attrs: dict, var: str) -> None:
    """Emit SetAttribute calls for each entry in *attrs*.

    rbxlx_writer serializes these into AttributesSerialize blobs; the
    headless builder needs the same data via the runtime API so gameplay
    scripts that read ``script.Parent:GetAttribute("itemName")`` etc.
    actually find the value.
    """
    if not attrs:
        return
    for key, value in attrs.items():
        if isinstance(value, bool):
            v = "true" if value else "false"
        elif isinstance(value, (int, float)):
            v = _f(float(value))
        elif isinstance(value, str):
            v = _luau_str(value)
        else:
            continue  # skip unsupported types (Vector3, Color3, etc.)
        b.line(f"{var}:SetAttribute({_luau_str(key)},{v})")


def _emit_part_extras(b: _LuauBuilder, part: RbxPart, var: str) -> None:
    """Emit optional part properties (shared between Part and MeshPart paths)."""
    # MeshParts come back from ``mkMesh`` (CreateMeshPartAsync wrapper) with
    # CanCollide defaulting to true. The plain-Part branch sets CanCollide
    # explicitly via _emit_part, but the MeshPart branch only calls this
    # extras helper — so a converter-side ``part.can_collide=False`` (e.g.
    # the trigger heuristic in _bind_scripts_to_parts that flips Door's
    # invisible ``base``) never lands in the live place. Always emit
    # CanCollide=false here so MeshPart triggers actually let the player
    # walk through. (CanCollide=true is the wrapper default, so we only
    # need to write it when it's false.)
    if not part.can_collide:
        b.line(f"{var}.CanCollide=false")
    if not part.can_query:
        b.line(f"{var}.CanQuery=false")
    if not part.can_touch:
        b.line(f"{var}.CanTouch=false")
    if not part.cast_shadow:
        b.line(f"{var}.CastShadow=false")
    if part.massless:
        b.line(f"{var}.Massless=true")
    if part.reflectance and part.reflectance > 0:
        b.line(f"{var}.Reflectance={_f(part.reflectance)}")
    _emit_attributes(b, part.attributes or {}, var)

    # CustomPhysicalProperties
    cpp = part.custom_physical_properties
    if cpp is not None:
        density, friction, elasticity, fw, ew = cpp
        b.line(
            f"{var}.CustomPhysicalProperties="
            f"PhysicalProperties.new({_f(density)},{_f(friction)},{_f(elasticity)},{_f(fw)},{_f(ew)})"
        )


def _emit_surface_appearance(
    b: _LuauBuilder, sa: RbxSurfaceAppearance, parent_var: str
) -> None:
    """Emit SurfaceAppearance child with Texture fallback.

    SurfaceAppearance properties (ColorMap, etc.) require Plugin capability
    which is unavailable in headless Luau execution. Falls back to Texture
    instances which DO work headlessly.
    """
    color_map = sa.color_map if sa.color_map and "rbxassetid" in sa.color_map else ""
    normal_map = sa.normal_map if sa.normal_map and "rbxassetid" in sa.normal_map else ""
    metalness_map = sa.metalness_map if sa.metalness_map and "rbxassetid" in sa.metalness_map else ""
    roughness_map = sa.roughness_map if sa.roughness_map and "rbxassetid" in sa.roughness_map else ""

    # Skip emitting the SurfaceAppearance entirely when no map references
    # survive validation. An empty SurfaceAppearance child overrides the
    # MeshPart's own TextureID and forces a flat gray render — worse than
    # the un-decorated mesh would render on its own. The "Skip if empty"
    # guard fixes the doors/dock/rifle "no color" symptom users hit when
    # the material had no usable texture URLs (e.g. all maps were dropped
    # by upload errors or pointed at non-rbxassetid:// values).
    if not (color_map or normal_map or metalness_map or roughness_map):
        return

    # Try SurfaceAppearance first (works in Studio, not headless)
    b.line("do local saOk=pcall(function()")
    b.line("local sa=Instance.new('SurfaceAppearance')")
    if color_map:
        b.line(f"sa.ColorMap={_luau_str(color_map)}")
    if normal_map:
        b.line(f"sa.NormalMap={_luau_str(normal_map)}")
    if metalness_map:
        b.line(f"sa.MetalnessMap={_luau_str(metalness_map)}")
    if roughness_map:
        b.line(f"sa.RoughnessMap={_luau_str(roughness_map)}")
    if sa.alpha_mode and sa.alpha_mode != "Overlay":
        b.line(f"sa.AlphaMode=Enum.AlphaMode.{sa.alpha_mode}")
    b.line(f"sa.Parent={parent_var}")
    b.line("end)")
    # Fallback when SurfaceAppearance creation fails (headless execution
    # blocks the Plugin capability on SA property writes). Prefer
    # MeshPart.TextureID, which renders the texture via the mesh's own UV
    # map — the right look. The previous fallback (six face-Textures with
    # the same image) tiled the asset across each cardinal face without
    # UVs, so doors / panels / rifles looked like flat-gray meshes with
    # mismatched stamps. Only fall back to face-Textures when the parent
    # part is not a MeshPart (e.g. a basic Part where TextureID isn't a
    # property).
    if color_map:
        b.line("if not saOk then")
        b.line(f"if {parent_var}:IsA('MeshPart') then")
        b.line(f"{parent_var}.TextureID={_luau_str(color_map)}")
        b.line("else")
        b.line(f"for _,face in ipairs(Enum.NormalId:GetEnumItems()) do")
        b.line(f"local t=Instance.new('Texture')")
        b.line(f"t.Texture={_luau_str(color_map)}")
        b.line(f"t.Face=face")
        b.line(f"t.StudsPerTileU=8 t.StudsPerTileV=8")
        b.line(f"t.Parent={parent_var}")
        b.line("end")
        b.line("end")
        b.line("end")
    b.line("end")  # close the outer do block


def _emit_attachments(b: _LuauBuilder, part: RbxPart, var: str) -> None:
    """Emit lights, sounds, particle emitters, constraints, trails, beams, Motor6Ds."""
    for light in part.lights or []:
        _emit_light(b, light, var)
    for sound in part.sounds or []:
        _emit_sound(b, sound, var)
    for pe in part.particle_emitters or []:
        _emit_particle(b, pe, var)
    for constraint in part.constraints or []:
        _emit_constraint(b, constraint, var)
    for trail in part.trails or []:
        _emit_trail(b, trail, var)
    for beam in part.beams or []:
        _emit_beam(b, beam, var)
    for motor in part.motor6ds or []:
        _emit_motor6d(b, motor, var)
    for reverb in part.reverb_effects or []:
        _emit_reverb(b, reverb, var)
    for vf in part.video_frames or []:
        _emit_video_frame(b, vf, var)
    for decal in part.decals or []:
        _emit_decal(b, decal, var)
    # Sprite texture → Decal (from _SpriteTextureId attribute)
    sprite_tex = (part.attributes or {}).get("_SpriteTextureId", "")
    if sprite_tex and "rbxassetid" in sprite_tex:
        b.line(f"do local d=Instance.new('Decal')")
        b.line(f"d.Texture={_luau_str(sprite_tex)}")
        b.line(f"d.Face=Enum.NormalId.Front")
        b.line(f"d.Parent={var} end")


def _emit_light(b: _LuauBuilder, light: RbxLight, parent_var: str) -> None:
    lt = light.light_type or "PointLight"
    b.line(f"do local l=Instance.new('{lt}')")
    b.line(f"l.Brightness={_f(light.brightness)}")
    b.line(f"l.Color={_c3(*light.color)}")
    b.line(f"l.Range={_f(light.range)}")
    if lt == "SpotLight" and light.angle:
        b.line(f"l.Angle={_f(light.angle)}")
    if light.shadows:
        b.line("l.Shadows=true")
    b.line(f"l.Parent={parent_var} end")


def _emit_sound(b: _LuauBuilder, sound: RbxSound, parent_var: str) -> None:
    if not sound.sound_id:
        return
    b.line(f"do local s=Instance.new('Sound')")
    b.line(f"s.SoundId={_luau_str(sound.sound_id)}")
    b.line(f"s.Volume={_f(sound.volume)}")
    if sound.looped:
        b.line("s.Looped=true")
    if sound.playing:
        b.line("s.Playing=true")
    b.line(f"s.RollOffMaxDistance={_f(sound.roll_off_max_distance)}")
    b.line(f"s.RollOffMinDistance={_f(sound.roll_off_min_distance)}")
    b.line(f"s.Parent={parent_var} end")


def _emit_particle(b: _LuauBuilder, pe: RbxParticleEmitter, parent_var: str) -> None:
    b.line("do local e=Instance.new('ParticleEmitter')")
    b.line(f"e.Rate={_f(pe.rate)}")
    b.line(f"e.Lifetime=NumberRange.new({_f(pe.lifetime_min)},{_f(pe.lifetime_max)})")
    b.line(f"e.Speed=NumberRange.new({_f(pe.speed_min)},{_f(pe.speed_max)})")
    b.line(f"e.Color=ColorSequence.new({_c3(*pe.color)})")
    if pe.texture and "rbxassetid" in pe.texture:
        b.line(f"e.Texture={_luau_str(pe.texture)}")
    if pe.light_emission > 0:
        b.line(f"e.LightEmission={_f(pe.light_emission)}")
    if not pe.enabled:
        b.line("e.Enabled=false")
    b.line(f"e.Parent={parent_var} end")


def _emit_constraint(b: _LuauBuilder, c: "RbxConstraint", parent_var: str) -> None:
    ct = c.constraint_type
    b.line(f"do local c=Instance.new('{ct}')")
    if ct == "HingeConstraint" and c.limits_enabled:
        b.line("c.LimitsEnabled=true")
        b.line(f"c.LowerAngle={_f(c.lower_angle)}")
        b.line(f"c.UpperAngle={_f(c.upper_angle)}")
    elif ct == "SpringConstraint":
        b.line(f"c.Stiffness={_f(c.stiffness)}")
        b.line(f"c.Damping={_f(c.damping)}")
        b.line(f"c.FreeLength={_f(c.free_length)}")
    elif ct == "BallSocketConstraint" and c.twist_limits_enabled:
        b.line("c.TwistLimitsEnabled=true")
        b.line(f"c.UpperAngle={_f(c.upper_twist_angle)}")
    b.line(f"c.Parent={parent_var} end")


def _emit_trail(b: _LuauBuilder, t: "RbxTrail", parent_var: str) -> None:
    b.line("do local t=Instance.new('Trail')")
    b.line(f"t.Lifetime={_f(t.lifetime)}")
    b.line(f"t.Color=ColorSequence.new({_c3(*t.color)})")
    if t.transparency > 0:
        b.line(f"t.Transparency=NumberSequence.new({_f(t.transparency)})")
    b.line(f"t.MinLength={_f(t.min_length)}")
    if t.texture and "rbxassetid" in t.texture:
        b.line(f"t.Texture={_luau_str(t.texture)}")
    if t.light_emission > 0:
        b.line(f"t.LightEmission={_f(t.light_emission)}")
    b.line(f"t.Parent={parent_var} end")


def _emit_beam(b: _LuauBuilder, bm: "RbxBeam", parent_var: str) -> None:
    b.line("do local b=Instance.new('Beam')")
    b.line(f"b.Color=ColorSequence.new({_c3(*bm.color)})")
    b.line(f"b.Width0={_f(bm.width0)}")
    b.line(f"b.Width1={_f(bm.width1)}")
    b.line(f"b.Segments={bm.segments}")
    if bm.texture and "rbxassetid" in bm.texture:
        b.line(f"b.Texture={_luau_str(bm.texture)}")
    if bm.light_emission > 0:
        b.line(f"b.LightEmission={_f(bm.light_emission)}")
    b.line(f"b.Parent={parent_var} end")


def _emit_motor6d(b: _LuauBuilder, m: "RbxMotor6D", parent_var: str) -> None:
    b.line(f"do local m=Instance.new('Motor6D')")
    b.line(f"m.Name={_luau_str(m.name)}")
    # Part0/Part1 resolved by name search in parent hierarchy
    if m.part0_name:
        b.line(f"m.Part0={parent_var}:FindFirstChild({_luau_str(m.part0_name)},true)")
    if m.part1_name:
        b.line(f"m.Part1={parent_var}:FindFirstChild({_luau_str(m.part1_name)},true)")
    cf0 = m.c0
    b.line(f"m.C0=CFrame.new({_f(cf0.x)},{_f(cf0.y)},{_f(cf0.z)})")
    cf1 = m.c1
    b.line(f"m.C1=CFrame.new({_f(cf1.x)},{_f(cf1.y)},{_f(cf1.z)})")
    b.line(f"m.Parent={parent_var} end")


def _emit_reverb(b: _LuauBuilder, r: "RbxReverbSoundEffect", parent_var: str) -> None:
    b.line("do local r=Instance.new('ReverbSoundEffect')")
    b.line(f"r.DecayTime={_f(r.decay_time)}")
    b.line(f"r.Density={_f(r.density)}")
    b.line(f"r.Diffusion={_f(r.diffusion)}")
    b.line(f"r.DryLevel={_f(r.dry_level)}")
    b.line(f"r.WetLevel={_f(r.wet_level)}")
    b.line(f"r.Parent={parent_var} end")


def _emit_decal(b: _LuauBuilder, d: "RbxDecal", parent_var: str) -> None:
    if not d.texture:
        return
    face_map = {"Top": "Top", "Bottom": "Bottom", "Front": "Front",
                "Back": "Back", "Left": "Left", "Right": "Right"}
    face = face_map.get(d.face, "Front")
    b.line(f"do local d=Instance.new('Decal')")
    b.line(f"d.Texture={_luau_str(d.texture)}")
    b.line(f"d.Face=Enum.NormalId.{face}")
    if d.transparency > 0:
        b.line(f"d.Transparency={_f(d.transparency)}")
    b.line(f"d.Parent={parent_var} end")


def _emit_video_frame(b: _LuauBuilder, vf: "RbxVideoFrame", parent_var: str) -> None:
    if not vf.video:
        return
    b.line("do local sg=Instance.new('SurfaceGui')")
    b.line(f"sg.Parent={parent_var}")
    b.line("local vf=Instance.new('VideoFrame')")
    b.line(f"vf.Video={_luau_str(vf.video)}")
    if vf.looped:
        b.line("vf.Looped=true")
    b.line(f"vf.Volume={_f(vf.volume)}")
    b.line("vf.Size=UDim2.new(1,0,1,0)")
    b.line("vf.Parent=sg end")


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------

def _emit_scripts(b: _LuauBuilder, scripts: list[RbxScript]) -> None:
    """Emit top-level scripts into appropriate containers."""
    for script in scripts:
        parent = _script_parent(script)
        _emit_script_inline(b, script, parent)


def _script_parent(script: RbxScript) -> str:
    """Determine the parent variable for a script based on type and path."""
    pp = script.parent_path or ""
    if script.script_type == "LocalScript":
        # LocalScripts go to StarterPlayer.StarterPlayerScripts or StarterGui
        if "StarterGui" in pp:
            return "SG"
        return "SP:FindFirstChild('StarterPlayerScripts') or SP"
    if script.script_type == "ModuleScript":
        if "ReplicatedStorage" in pp:
            return "RS"
        if "ServerStorage" in pp:
            return "SS"
        return "RS"
    # Server scripts
    return "SSS"


def _emit_script_inline(b: _LuauBuilder, script: RbxScript, parent_var: str) -> None:
    """Emit a single script creation."""
    cls = script.script_type or "Script"
    name = script.name or "Script"
    src = script.source or ""
    # Use long brackets for source to avoid escaping issues
    level = 0
    while f"]{'=' * level}]" in src:
        level += 1
    eq = "=" * level
    b.line(f"mkScript({parent_var},{_luau_str(cls)},{_luau_str(name)},[{eq}[{src}]{eq}])")


# ---------------------------------------------------------------------------
# ScreenGuis
# ---------------------------------------------------------------------------

def _emit_screen_gui(b: _LuauBuilder, gui: RbxScreenGui) -> None:
    b.block("do")
    b.line("local g=Instance.new('ScreenGui')")
    b.line(f"g.Name={_luau_str(gui.name)}")
    if gui.reset_on_spawn:
        b.line("g.ResetOnSpawn=true")
    else:
        b.line("g.ResetOnSpawn=false")
    for elem in gui.elements:
        _emit_ui_element(b, elem, "g")
    b.line("g.Parent=SG")
    b.end()


_ui_counter = 0

def _emit_ui_element(b: _LuauBuilder, elem: RbxUIElement, parent_var: str) -> None:
    global _ui_counter
    _ui_counter += 1
    var = f"u{_ui_counter}"
    cls = elem.class_name or "Frame"
    b.block("do")
    b.line(f"local {var}=Instance.new('{cls}')")
    b.line(f"{var}.Name={_luau_str(elem.name)}")
    # Position
    px = elem.position
    b.line(f"{var}.Position=UDim2.new({_f(px[0])},{int(px[1])},{_f(px[2])},{int(px[3])})")
    # Size
    sx = elem.size
    b.line(f"{var}.Size=UDim2.new({_f(sx[0])},{int(sx[1])},{_f(sx[2])},{int(sx[3])})")
    b.line(f"{var}.BackgroundColor3={_c3(*elem.background_color)}")
    if elem.background_transparency > 0:
        b.line(f"{var}.BackgroundTransparency={_f(elem.background_transparency)}")
    if not elem.visible:
        b.line(f"{var}.Visible=false")
    if cls in ("TextLabel", "TextButton") and elem.text:
        b.line(f"{var}.Text={_luau_str(elem.text)}")
        b.line(f"{var}.TextColor3={_c3(*elem.text_color)}")
        b.line(f"{var}.TextSize={elem.text_size}")
    if cls == "ImageLabel" and elem.image:
        b.line(f"{var}.Image={_luau_str(elem.image)}")
    # Layout child
    if elem.layout_type:
        b.line(f"local lay=Instance.new('{elem.layout_type}')")
        if elem.layout_type == "UIListLayout":
            b.line(f"lay.FillDirection=Enum.FillDirection.{elem.layout_direction}")
            b.line(f"lay.Padding=UDim.new(0,{elem.layout_padding})")
            b.line(f"lay.HorizontalAlignment=Enum.HorizontalAlignment.{elem.layout_h_alignment}")
            b.line(f"lay.VerticalAlignment=Enum.VerticalAlignment.{elem.layout_v_alignment}")
        elif elem.layout_type == "UIGridLayout":
            cs = elem.layout_cell_size
            b.line(f"lay.CellSize=UDim2.new(0,{cs[0]},0,{cs[1]})")
            b.line(f"lay.CellPadding=UDim2.new(0,{elem.layout_padding},0,{elem.layout_padding})")
        b.line(f"lay.Parent={var}")
    for child in elem.children:
        _emit_ui_element(b, child, var)
    b.line(f"{var}.Parent={parent_var}")
    b.end()


# ---------------------------------------------------------------------------
# Terrain
# ---------------------------------------------------------------------------

def _emit_terrain(b: _LuauBuilder, place: RbxPlace) -> None:
    """Emit terrain generation via FillBlock calls.

    Since the Luau Execution API cannot set BinaryString properties
    (SmoothGrid), we inline FillBlock bodies directly. The pipeline
    populates ``place.headless_terrain_scripts`` with one Luau body per
    terrain (kept off ``place.scripts`` so they don't get emitted into
    the rbxlx — running them at Studio load would wipe the embedded
    SmoothGrid via ``t:Clear()``).
    """
    if not place.terrains:
        return
    bodies = getattr(place, "headless_terrain_scripts", None) or []
    if not bodies:
        b.line("-- No terrain generator available")
        return
    for i, body in enumerate(bodies):
        b.line(f"-- Terrain generation [{i + 1}/{len(bodies)}]")
        b.line("do")
        for line in body.split("\n"):
            b._lines.append(line)
        b.line("end")


# ---------------------------------------------------------------------------
# Entry point validation
# ---------------------------------------------------------------------------

def generate_place_luau_chunked(
    place: RbxPlace,
    mesh_cache: dict[str, str] | None = None,
    max_chunk_bytes: int = 3_500_000,
) -> list[str]:
    """Generate Luau scripts for a place, in execute_luau-sized chunks.

    Returns a list of script strings. When the generated script fits
    under ``max_chunk_bytes`` the list has one entry; otherwise the
    list contains a single oversized entry that the caller's per-chunk
    size guard (``place_publisher._publish_chunks``) will reject with
    ``PublishResult.exceeded_limit=True`` and a clear error.

    Real tree-splitting chunking is unimplemented (see comment below).
    Honesty matters: we surface the single oversized entry so the
    upstream guard fires, rather than pretending we'd successfully
    chunked the payload.
    """
    full_script = generate_place_luau(place, mesh_cache)
    if len(full_script.encode("utf-8")) <= max_chunk_bytes:
        return [full_script]

    log.warning(
        "Generated place builder is %.1f MB — exceeds the per-chunk "
        "%.1f MB target. Tree-aware splitting is not yet implemented; "
        "place_publisher will reject this payload with exceeded_limit.",
        len(full_script.encode("utf-8")) / (1024 * 1024),
        max_chunk_bytes / (1024 * 1024),
    )
    return [full_script]


def _validate_output(script: str) -> None:
    """Basic sanity checks on generated output."""
    size = len(script.encode("utf-8"))
    if size > _MAX_SCRIPT_BYTES:
        log.warning(
            "Generated script is %.1f MB, exceeds 4 MB limit. "
            "Large projects may need multiple execution batches.",
            size / (1024 * 1024),
        )
