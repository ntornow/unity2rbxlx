"""
test_test_seam_injector.py -- Unit tests for the mouse-delta test seam.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.test_seam_injector import (  # noqa: E402
    _inject_into_source,
    inject_test_seams,
)


class _FakeScript:
    """Minimal RbxScript stand-in — only ``source`` is touched."""
    def __init__(self, source: str, name: str = "Player") -> None:
        self.source = source
        self.name = name


class TestInjectIntoSource:
    def test_noop_when_no_getmousedelta(self) -> None:
        src = "print('hello')\nlocal x = 1\n"
        assert _inject_into_source(src) is None

    def test_injects_helper_after_uis_local(self) -> None:
        src = (
            'local UserInputService = game:GetService("UserInputService")\n'
            "local function rotate(dt)\n"
            "    local d = UserInputService:GetMouseDelta()\n"
            "    yaw = yaw + d.X\n"
            "end\n"
        )
        out = _inject_into_source(src)
        assert out is not None
        # Helper landed right after the UIS local
        uis_pos = out.find('local UserInputService = game:GetService')
        helper_pos = out.find('local function _getMouseDelta')
        assert uis_pos >= 0 and helper_pos > uis_pos
        # User's call site rewritten — the only remaining occurrence of
        # ``UserInputService:GetMouseDelta()`` is the helper's own
        # fallback line.
        assert out.count("UserInputService:GetMouseDelta()") == 1
        assert "local d = _getMouseDelta()" in out

    def test_handles_inline_service_lookup_call(self) -> None:
        src = (
            "local function rotate(dt)\n"
            "    local d = game:GetService('UserInputService'):GetMouseDelta()\n"
            "    yaw = yaw + d.X\n"
            "end\n"
        )
        out = _inject_into_source(src)
        assert out is not None
        # No UIS local → injector prepends its own
        assert "local UserInputService" in out
        assert "_getMouseDelta()" in out
        # Inline service call site rewritten
        assert "GetService('UserInputService'):GetMouseDelta" not in out

    def test_idempotent_on_already_instrumented_source(self) -> None:
        # First pass instruments
        src = (
            'local UserInputService = game:GetService("UserInputService")\n'
            "local d = UserInputService:GetMouseDelta()\n"
        )
        first = _inject_into_source(src)
        assert first is not None
        # Second pass on the already-instrumented source returns None
        assert _inject_into_source(first) is None
        # And only one helper definition exists
        assert first.count("local function _getMouseDelta") == 1

    def test_multiple_call_sites_all_rewritten(self) -> None:
        src = (
            'local UserInputService = game:GetService("UserInputService")\n'
            "local function a() return UserInputService:GetMouseDelta() end\n"
            "local function b() return UserInputService:GetMouseDelta() end\n"
            "local function c()\n"
            "    local d = UserInputService:GetMouseDelta()\n"
            "    return d.X\n"
            "end\n"
        )
        out = _inject_into_source(src)
        assert out is not None
        # 4 occurrences of ``_getMouseDelta()``: 3 rewritten call sites
        # + the helper's own definition line ``local function _getMouseDelta()``.
        assert out.count("_getMouseDelta()") == 4
        # User's calls are gone; helper's fallback is the only survivor.
        assert out.count("UserInputService:GetMouseDelta()") == 1

    def test_helper_falls_back_to_real_call_when_global_nil(self) -> None:
        out = _inject_into_source(
            'local UserInputService = game:GetService("UserInputService")\n'
            "local d = UserInputService:GetMouseDelta()\n"
        )
        # The helper body must still reference the real method so the
        # nil-global production path works.
        assert "UserInputService:GetMouseDelta()" in out


class TestInjectTestSeams:
    def test_returns_count_of_modified_scripts(self) -> None:
        scripts = [
            _FakeScript("print('no input')\n"),
            _FakeScript(
                'local UserInputService = game:GetService("UserInputService")\n'
                "local d = UserInputService:GetMouseDelta()\n"
            ),
            _FakeScript(
                'local UserInputService = game:GetService("UserInputService")\n'
                "local d = UserInputService:GetMouseDelta()\n"
            ),
        ]
        n = inject_test_seams(scripts)
        assert n == 2
        # The no-input script is untouched
        assert scripts[0].source == "print('no input')\n"
        # The other two are instrumented
        for s in scripts[1:]:
            assert "_getMouseDelta()" in s.source

    def test_handles_script_with_no_source_attribute(self) -> None:
        class _Bare:
            pass
        # Should not crash, just skip
        n = inject_test_seams([_Bare()])
        assert n == 0

    def test_handles_empty_source(self) -> None:
        n = inject_test_seams([_FakeScript("")])
        assert n == 0
