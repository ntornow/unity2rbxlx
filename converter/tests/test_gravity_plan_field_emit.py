"""AC16 -- gravityDesiredBaseStuds survives plan emission (_PLAN_KEYS_FOR_HOST).

The client clone-site gravity hook (relation #8) reads the scale-faithful base
target accel off the embedded ``SceneRuntimePlan`` ModuleScript. That embedding
goes through ``generate_scene_runtime_plan_module``, which keeps ONLY keys in
``_PLAN_KEYS_FOR_HOST``. This is the load-bearing plan-emission hop: a key not in
that allowlist is silently elided and the client falls back to a frozen 9.81.

Mirrors test_scene_runtime_host_emit.py / test_plan_emits_player_signal.py style
(assert the token in ``.source``). Pure-Python; no luau interpreter. The exact
emitted value is load-bearing, so the float is asserted in ``repr()`` form (the
encoder serializes floats via ``repr``).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.autogen import (
    _PLAN_KEYS_FOR_HOST,
    generate_scene_runtime_plan_module,
)


def test_gravity_field_in_allowlist() -> None:
    """The key must be in _PLAN_KEYS_FOR_HOST or the encoder drops it."""
    assert "gravityDesiredBaseStuds" in _PLAN_KEYS_FOR_HOST


def test_gravity_field_survives_plan_emit() -> None:
    """A stashed gravityDesiredBaseStuds lands in the emitted plan ModuleScript."""
    desired = 9.81 * 3.571
    script = generate_scene_runtime_plan_module(
        {"modules": {}, "gravityDesiredBaseStuds": desired}
    )
    assert "gravityDesiredBaseStuds" in script.source
    assert repr(desired) in script.source


def test_zero_gravity_field_survives_plan_emit() -> None:
    """A stashed 0.0 (zero gravity) survives the filter -- NOT elided as falsy."""
    script = generate_scene_runtime_plan_module(
        {"modules": {}, "gravityDesiredBaseStuds": 0.0}
    )
    assert "gravityDesiredBaseStuds" in script.source
    assert repr(0.0) in script.source


def test_gravity_field_absent_when_not_stashed() -> None:
    """When gravityDesiredBaseStuds is NOT stashed in scene_runtime, the emitted
    plan ModuleScript does NOT carry the field (deterministic fallback: the client
    hook then reads its `or DEFAULT` path). Pins the no-key companion of AC16."""
    script = generate_scene_runtime_plan_module({"modules": {}})
    assert "gravityDesiredBaseStuds" not in script.source
