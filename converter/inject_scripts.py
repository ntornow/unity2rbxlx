"""Generate Luau to inject all transpiled scripts into Studio."""
from pathlib import Path

scripts_dir = Path("output/SimpleFPS_v2/scripts")
scripts = sorted(scripts_dir.glob("*.luau"))

client_scripts = {"Player", "Hose", "WaterBase"}

lines = [
    'local SSS = game:GetService("ServerScriptService")',
    'local SP = game:GetService("StarterPlayer")',
    'local SPS = SP:FindFirstChild("StarterPlayerScripts")',
    "if not SPS then",
    '    SPS = Instance.new("StarterPlayerScripts")',
    "    SPS.Parent = SP",
    "end",
    "",
]

for script_file in scripts:
    name = script_file.stem
    if name in client_scripts:
        parent = "SPS"
        cls = "LocalScript"
    else:
        parent = "SSS"
        cls = "Script"

    lines.append("do")
    lines.append(f'    local s = Instance.new("{cls}")')
    lines.append(f'    s.Name = "{name}"')
    lines.append("    s.Enabled = false")
    lines.append(f"    s.Parent = {parent}")
    lines.append("end")

lines.append("")
lines.append(
    'return "Created " .. #SSS:GetChildren() .. " server + " .. #SPS:GetChildren() .. " client scripts"'
)

Path("output/SimpleFPS_v2/inject_scripts.luau").write_text("\n".join(lines))
print(f"Generated script injection for {len(scripts)} scripts")
