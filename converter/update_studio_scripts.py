"""Generate Luau to update all script sources in Studio from the extracted JSON."""

import json
from pathlib import Path

scripts = json.loads(Path("output/SimpleFPS_v6/script_sources.json").read_text())

lines = []
lines.append('local SSS = game:GetService("ServerScriptService")')
lines.append('local SPS = game:GetService("StarterPlayer"):FindFirstChild("StarterPlayerScripts")')
lines.append("local updated = 0")

for s in scripts:
    name = s["name"]
    source = s["source"]
    # Escape for embedding in a Luau [=[ ]=] string (safe for any content)
    # Just make sure source doesn't contain ]=]
    delim = "="
    while f"]{delim}]" in source:
        delim += "="

    parent = "SPS" if s["class"] == "LocalScript" else "SSS"
    lines.append("do")
    lines.append(f'  local s = {parent}:FindFirstChild("{name}")')
    lines.append(f"  if s then s.Source = [{delim}[{source}]{delim}]; updated = updated + 1 end")
    lines.append("end")

lines.append('return "Updated " .. updated .. " scripts"')

output = "\n".join(lines)
Path("output/SimpleFPS_v6/update_scripts.luau").write_text(output)
print(f"Generated update script ({len(output)} bytes) for {len(scripts)} scripts")
