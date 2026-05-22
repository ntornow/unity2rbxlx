"""
test_fps_scaffolding_e2e_channel.py — Pin the E2E mouse-delta input channel
in both places it can reach a running game.

The /e2e-test skill drives mouse-look via workspace attributes because
MCP's user_mouse_input synthesises Delta=(0,0) and so can't exercise
UserInputService:GetMouseDelta() polling. See docs/E2E_INPUT_CHANNEL.md.

The channel must reach the running Player controller two ways:

1. ``converter/scaffolding/fps.py`` — the static FPS scaffolding that
   ships verbatim when a project matches the FPS heuristic and has no
   user-authored Player controller.

2. The ``fps_e2e_mouse_channel`` coherence pack — injects the channel
   into AI-transpiled mouse-look code post-transpile. It is NOT in the
   transpiler prompt: ``_AI_SYSTEM_PROMPT`` is a frozen cache key
   (test_scene_runtime_transpiler.py), so editing it would invalidate
   every legacy project's LLM cache.

Both use the same ack-attribute shape (E2EMouseSeq test-set,
E2EMouseAckSeq client-set) so the runtime behaviour is identical. If the
channel is removed from either, this test fires — without it, mouse-look
gameplay fixtures silently no-op.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.roblox_types import RbxScript  # noqa: E402
from converter.scaffolding.fps import generate_fps_client_script  # noqa: E402
from converter import script_coherence_packs as packs  # noqa: E402


class TestScaffoldingChannel:
    """The static FPS scaffolding script must read the E2E channel."""

    def _script_source(self) -> str:
        return generate_fps_client_script().source

    def test_reads_e2e_mouse_seq(self) -> None:
        src = self._script_source()
        assert 'workspace:GetAttribute("E2EMouseSeq")' in src, (
            "FPS scaffolding updateCamera() must read E2EMouseSeq — "
            "the /e2e-test skill drives mouse-look through this channel. "
            "See docs/E2E_INPUT_CHANNEL.md."
        )

    def test_acks_via_attribute(self) -> None:
        """The client acks via the E2EMouseAckSeq attribute (not an
        upvalue) so consumption is one-shot and reload-safe, matching
        the coherence-pack form."""
        src = self._script_source()
        assert 'workspace:SetAttribute("E2EMouseAckSeq"' in src
        assert 'workspace:GetAttribute("E2EMouseAckSeq")' in src

    def test_reads_both_delta_axes(self) -> None:
        src = self._script_source()
        assert 'workspace:GetAttribute("E2EMouseDeltaX")' in src
        assert 'workspace:GetAttribute("E2EMouseDeltaY")' in src

    def test_is_additive_not_overriding(self) -> None:
        """The injected delta must ADD to the real mouse delta, not
        replace it — otherwise normal mouse-look breaks the moment a
        test attribute is set."""
        src = self._script_source()
        assert "delta.X + ex" in src
        assert "delta.Y + ey" in src


class TestCoherencePackChannel:
    """The fps_e2e_mouse_channel pack injects the channel into
    AI-transpiled mouse-look code."""

    _RAW = (
        'local UserInputService = game:GetService("UserInputService")\n'
        "local function updateCamera()\n"
        "    local d = UserInputService:GetMouseDelta()\n"
        "    yawAngle = yawAngle - d.X * MOUSE_RAD_PER_PIXEL\n"
        "    pitchAngle = math.clamp(pitchAngle - d.Y * MOUSE_RAD_PER_PIXEL, "
        "math.rad(minAngle), math.rad(maxAngle))\n"
        "end\n"
    )

    def test_detector_fires_on_raw_mouse_look(self) -> None:
        s = RbxScript(name="Player", source=self._RAW, script_type="LocalScript")
        assert packs._detect_fps_e2e_mouse_channel([s]) is True

    def test_injects_channel_after_getmousedelta(self) -> None:
        s = RbxScript(name="Player", source=self._RAW, script_type="LocalScript")
        n = packs._fix_fps_e2e_mouse_channel([s])
        assert n == 1
        # Channel present, additive, seq-guarded, acked.
        assert 'workspace:GetAttribute("E2EMouseSeq")' in s.source
        assert 'workspace:SetAttribute("E2EMouseAckSeq"' in s.source
        assert "d.X + (workspace:GetAttribute(\"E2EMouseDeltaX\") or 0)" in s.source
        assert "d.Y + (workspace:GetAttribute(\"E2EMouseDeltaY\") or 0)" in s.source
        # Original mouse-look math is preserved below the injected block.
        assert "yawAngle = yawAngle - d.X * MOUSE_RAD_PER_PIXEL" in s.source

    def test_idempotent(self) -> None:
        s = RbxScript(name="Player", source=self._RAW, script_type="LocalScript")
        packs._fix_fps_e2e_mouse_channel([s])
        first = s.source
        n2 = packs._fix_fps_e2e_mouse_channel([s])
        assert n2 == 0, "second pass must be a no-op"
        assert s.source == first
        # Exactly one injected block (the GetAttribute("E2EMouseSeq") line
        # is unique to the block, so it pins the injection count).
        assert s.source.count('GetAttribute("E2EMouseSeq")') == 1
        assert packs._detect_fps_e2e_mouse_channel([s]) is False

    def test_no_op_on_non_mouse_look_script(self) -> None:
        s = RbxScript(
            name="Door",
            source="local function open() print('opening') end\nreturn true\n",
            script_type="Script",
        )
        assert packs._detect_fps_e2e_mouse_channel([s]) is False
        assert packs._fix_fps_e2e_mouse_channel([s]) == 0

    def test_pack_is_registered(self) -> None:
        names = {p.name for p in packs._REGISTRY}
        assert "fps_e2e_mouse_channel" in names
