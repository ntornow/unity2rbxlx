# unity2rbxlx

This project converts Unity projects into .rbxlx files. It leverages Claude Code for the C# to Luau conversion and python to coordinate the asset uploads and overall conversion.

Conversion is accomplished with the u2r.py command

Usage: u2r.py [OPTIONS] COMMAND [ARGS]...

  Unity to Roblox Game Converter.

Options:
  -v, --verbose  Enable debug logging
  --help         Show this message and exit.

Commands:
  analyze   Analyze a Unity project without converting.
  compare   Run comparison between Unity and Roblox versions.
  convert   Convert a Unity project to a Roblox experience.
  resolve   Generate Studio resolution scripts for uploaded assets.
  validate  Validate a generated .rbxlx file for Roblox compatibility.

Example of conversion from the command like:

python cli.py convert ../test_projects/SimpleFPS -o ./output/SimpleFPS --api-key-file ../apikey
