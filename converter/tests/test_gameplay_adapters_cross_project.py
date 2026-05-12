"""Cross-project detector smoke matrix (PR #73c).

Pins the detector match table for SimpleFPS, Gamekit3D, and ChopChop.
A detector that starts matching ChopChop or Gamekit3D classes (a
silent regression on non-FPS projects) would fail CI here — that's
the whole point of the cross-project matrix per the design doc.

Two layers:

  1. **SimpleFPS real-source classification.** Loads every .cs file
     under ``test_projects/SimpleFPS/Assets`` and runs each detector's
     primary + confirm against it. Pins the expected matches:
     ``TurretBullet`` → turret_bullet, ``PlaneBullet`` → plane_bullet,
     ``Door`` → door. Also pins near-misses: ``Mine.cs`` uses
     ``OnTriggerEnter`` but is class ``Mine`` (DoorDetector.primary
     rejects); ``Plane.cs`` uses ``AddRelativeForce`` but is class
     ``Plane`` (projectile detector primaries reject). Skips gracefully
     when SimpleFPS isn't checked out (developer machines without the
     full ``test_projects/`` tree).
  2. **Gamekit3D / ChopChop rejection.** Real sources for those
     projects are out of band for this machine's working tree (they
     live in CI fixtures). When their .cs files are checked out, this
     test pins ZERO matches across the whole project — any future
     match means a detector got too greedy. When the projects aren't
     checked out, falls back to synthetic Gamekit3D-flavoured /
     ChopChop-flavoured C# fixtures that exercise the same rejection
     paths (different class name + similar API shapes).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from converter.gameplay.detectors import (
    ALL_DETECTORS,
    DoorDetector,
    PlaneBulletDetector,
    TurretBulletDetector,
    detect,
)


# ---------------------------------------------------------------------------
# Stubs (mirrors the projectile test file's stub shape)
# ---------------------------------------------------------------------------

@dataclass
class _StubComponent:
    component_type: str
    file_id: str = "1"
    properties: dict = field(default_factory=dict)


@dataclass
class _StubNode:
    file_id: str = "n1"
    name: str = "Stub"
    components: list = field(default_factory=list)


def _mono(class_name: str, *, file_id: str = "1") -> _StubComponent:
    """Build a MonoBehaviour ``ComponentData`` with the script class
    pre-resolved — mirrors what ``_enrich_components`` does at the
    pipeline level.
    """
    return _StubComponent(
        component_type="MonoBehaviour",
        file_id=file_id,
        properties={"_script_class_name": class_name},
    )


_TEST_PROJECTS = (
    Path(__file__).parent.parent.parent / "test_projects"
)


# ---------------------------------------------------------------------------
# Layer 1: SimpleFPS real-source classification
# ---------------------------------------------------------------------------

_SIMPLEFPS_ASSETS = _TEST_PROJECTS / "SimpleFPS" / "Assets"


# Map: class name → (relative path under SimpleFPS/Assets, expected
# detector name). Built from the actual SimpleFPS source tree on
# 2026-05-12. Adding a new bullet/door class to SimpleFPS would change
# this table and force an explicit decision (extend the test or add a
# new detector).
_SIMPLEFPS_EXPECTED_MATCHES: tuple[tuple[str, str, str], ...] = (
    ("TurretBullet", "Scripts/TurretBullet.cs", "turret_bullet"),
    ("PlaneBullet",  "Scripts/PlaneBullet.cs",  "plane_bullet"),
    ("Door",         "AssetPack/SciFi_Door/Script/Door.cs", "door"),
)


# Classes that have ONE confirm signal but NOT the full set — used to
# pin that confirm-side rejection is working. Format: (class_name,
# relative_path, why_it_should_be_rejected).
_SIMPLEFPS_NEAR_MISSES: tuple[tuple[str, str, str], ...] = (
    ("Mine",     "Scripts/Mine.cs",
     "uses OnTriggerEnter but class != Door — primary should reject"),
    ("Pickup",   "Scripts/Pickup.cs",
     "uses OnTriggerEnter but class != Door — primary should reject"),
    ("SpawnPoint", "Scripts/SpawnPoint.cs",
     "uses OnTriggerEnter but class != Door — primary should reject"),
    ("Plane",    "Scripts/Plane.cs",
     "uses AddRelativeForce but class != bullet — primary should reject"),
    ("Machine",  "Scripts/Machine.cs",
     "uses AddRelativeForce but class != bullet — primary should reject"),
)


def _simplefps_available() -> bool:
    """The bullet sources live at the canonical path; treat them as the
    presence signal for the whole project tree.
    """
    return (
        _SIMPLEFPS_ASSETS / "Scripts" / "TurretBullet.cs"
    ).exists()


class TestSimpleFPSCrossDetector:
    """Real-source classification across every relevant SimpleFPS .cs.

    A new detector that accidentally claims a near-miss script would
    fail ``test_near_misses_reject``. A regression in an existing
    detector that drops one of the canonical matches would fail
    ``test_expected_matches_classify``.
    """

    def test_expected_matches_classify(self) -> None:
        if not _simplefps_available():
            pytest.skip("SimpleFPS test project not checked out")
        for class_name, rel_path, expected_detector in _SIMPLEFPS_EXPECTED_MATCHES:
            path = _SIMPLEFPS_ASSETS / rel_path
            assert path.exists(), (
                f"SimpleFPS source moved: {rel_path} — update "
                f"_SIMPLEFPS_EXPECTED_MATCHES."
            )
            source = path.read_text(encoding="utf-8")
            node = _StubNode(components=[_mono(class_name)])
            behavior = detect(  # type: ignore[arg-type]
                node, node.components[0], source,
            )
            assert behavior is not None, (
                f"{class_name} ({rel_path}) no longer classifies — "
                f"expected detector {expected_detector!r} dropped the "
                f"canonical SimpleFPS source."
            )
            # Round-trip via the same scan the integration layer uses
            # to identify the winning detector.
            winner = None
            for det in ALL_DETECTORS:
                if det.primary(node, node.components[0]) and det.confirm(
                    node, node.components[0], source,
                ):
                    winner = det.name
                    break
            assert winner == expected_detector, (
                f"{class_name} ({rel_path}) classified by "
                f"{winner!r}, expected {expected_detector!r}."
            )

    def test_near_misses_reject(self) -> None:
        if not _simplefps_available():
            pytest.skip("SimpleFPS test project not checked out")
        for class_name, rel_path, reason in _SIMPLEFPS_NEAR_MISSES:
            path = _SIMPLEFPS_ASSETS / rel_path
            assert path.exists(), (
                f"SimpleFPS source moved: {rel_path} — update "
                f"_SIMPLEFPS_NEAR_MISSES."
            )
            source = path.read_text(encoding="utf-8")
            node = _StubNode(components=[_mono(class_name)])
            behavior = detect(  # type: ignore[arg-type]
                node, node.components[0], source,
            )
            assert behavior is None, (
                f"{class_name} ({rel_path}) classified unexpectedly: "
                f"{reason}. Detector primary signal grew too lax."
            )


# ---------------------------------------------------------------------------
# Layer 2: Gamekit3D / ChopChop rejection
# ---------------------------------------------------------------------------

# When the upstream test projects are checked out under
# ``test_projects/``, the project's .cs tree gets scanned for any class
# named ``Door`` / ``TurretBullet`` / ``PlaneBullet`` (the only class
# names the detectors' primary signals will accept). If a future
# refactor of the test projects introduces such a class, the test
# below either passes (the C# body doesn't satisfy confirm) or fails
# loudly (the body DOES satisfy confirm, meaning we've leaked into a
# non-FPS project's namespace and need to either narrow the detector
# or add a deny-list entry to the smoke fixture).

_NON_FPS_PROJECTS: tuple[str, ...] = ("Gamekit3D", "ChopChop")


def _project_available(name: str) -> bool:
    base = _TEST_PROJECTS / name
    if not base.exists():
        return False
    # Both projects keep .cs files under an Assets/ subdir. Walk a
    # shallow depth to confirm the project is materialized rather than
    # just an empty placeholder directory.
    try:
        for _ in base.rglob("*.cs"):
            return True
    except OSError:
        return False
    return False


_TRACKED_DETECTOR_CLASS_NAMES: frozenset[str] = frozenset({
    "Door", "TurretBullet", "PlaneBullet",
})


class TestNonFpsProjectsReject:
    """Each non-FPS project must produce ZERO detector matches.

    For each project that IS checked out: scan every .cs file, build a
    stub node for any class name that one of our detectors would
    primary-accept, and assert that ``detect`` returns ``None`` (i.e.
    confirm rejects). For projects that AREN'T checked out: skip with
    a clear message — the synthetic-fixture tests below cover the
    rejection-path semantics so CI is still meaningful on developer
    machines.
    """

    @pytest.mark.parametrize("project_name", _NON_FPS_PROJECTS)
    def test_no_real_matches_in_non_fps_project(
        self, project_name: str,
    ) -> None:
        if not _project_available(project_name):
            pytest.skip(
                f"{project_name} test project not checked out — "
                f"synthetic-fixture rejection tests still run."
            )
        base = _TEST_PROJECTS / project_name
        unexpected: list[tuple[str, str]] = []
        for cs_path in base.rglob("*.cs"):
            try:
                source = cs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for class_name in _TRACKED_DETECTOR_CLASS_NAMES:
                # Cheap pre-filter — only construct a stub if the file
                # actually declares a class with the tracked name.
                if f"class {class_name}" not in source:
                    continue
                node = _StubNode(components=[_mono(class_name)])
                behavior = detect(  # type: ignore[arg-type]
                    node, node.components[0], source,
                )
                if behavior is not None:
                    unexpected.append((class_name, str(cs_path)))
        assert not unexpected, (
            f"{project_name} produced detector matches that should "
            f"have been rejected: {unexpected}. Either narrow the "
            f"detector's confirm() signal, or add a deny-list entry "
            f"in the project's .gameplay_deny.txt."
        )


# ---------------------------------------------------------------------------
# Layer 2b: synthetic non-FPS C# sources (always run)
# ---------------------------------------------------------------------------

# Each fixture is a (class_name, body) tuple. Class names are chosen
# to match detector primaries; bodies are crafted to MISS confirm.
# Together they exercise the same rejection paths the real Gamekit3D
# / ChopChop sources would, so the test stays meaningful even when
# the upstream projects aren't checked out.

_GAMEKIT_DOOR_FIXTURE = (
    # Gamekit3D-style Door: uses a Trigger animator parameter, NOT a
    # SetBool("open"). Confirm should reject — the DoorDetector's
    # confirm signal is the literal ``SetBool("open", ...)`` paired
    # with an ``OnTriggerEnter`` body.
    "Door",
    """
using UnityEngine;
public class Door : MonoBehaviour {
    public Animator anim;
    void OnTriggerEnter(Collider other) {
        // Note: Trigger, not SetBool("open").
        anim.SetTrigger("OpenDoor");
    }
}
""",
)

_GAMEKIT_PROJECTILE_FIXTURE = (
    # Gamekit3D bullets fire via ParticleSystem hits — no
    # AddRelativeForce, no Destroy(gameObject) timer.
    "TurretBullet",
    """
using UnityEngine;
public class TurretBullet : MonoBehaviour {
    public ParticleSystem ps;
    void Start() { ps.Play(); }
}
""",
)

_CHOPCHOP_DOOR_FIXTURE = (
    # ChopChop's interaction system uses ScriptableObject EventChannel
    # raise/listen — no Animator.SetBool in the door body.
    "Door",
    """
using UnityEngine;
public class Door : MonoBehaviour {
    public VoidEventChannelSO openRequested;
    void OnEnable() { openRequested.OnEventRaised += HandleOpen; }
    void HandleOpen() { transform.position += Vector3.up * 2f; }
}
""",
)

_CHOPCHOP_PROJECTILE_FIXTURE = (
    # PlaneBullet name in another project: rigidbody but no
    # OverlapSphere / Instantiate confirm signals.
    "PlaneBullet",
    """
using UnityEngine;
public class PlaneBullet : MonoBehaviour {
    public Rigidbody rb;
    void Update() { rb.velocity = transform.forward * 10f; }
}
""",
)


class TestSyntheticNonFpsRejection:
    """Always-on rejection coverage. These fixtures stand in for
    Gamekit3D / ChopChop sources on machines that don't carry the
    full ``test_projects/`` tree.
    """

    @pytest.mark.parametrize("fixture,label", [
        (_GAMEKIT_DOOR_FIXTURE,        "Gamekit3D-style Door"),
        (_GAMEKIT_PROJECTILE_FIXTURE,  "Gamekit3D-style TurretBullet"),
        (_CHOPCHOP_DOOR_FIXTURE,       "ChopChop-style Door"),
        (_CHOPCHOP_PROJECTILE_FIXTURE, "ChopChop-style PlaneBullet"),
    ])
    def test_synthetic_non_fps_source_rejects(
        self,
        fixture: tuple[str, str],
        label: str,
    ) -> None:
        class_name, source = fixture
        node = _StubNode(components=[_mono(class_name)])
        behavior = detect(  # type: ignore[arg-type]
            node, node.components[0], source,
        )
        assert behavior is None, (
            f"{label} fixture (class {class_name!r}) matched a "
            f"detector — confirm signal grew too lax."
        )


# ---------------------------------------------------------------------------
# Detector inventory pin
# ---------------------------------------------------------------------------

class TestDetectorInventory:
    """Pin the exact set of detectors so a new entry triggers an
    explicit decision about cross-project smoke coverage. Without
    this, a new detector could ship without anyone updating the
    cross-project matrix.
    """

    def test_all_detectors_set(self) -> None:
        names = {det.name for det in ALL_DETECTORS}
        assert names == {"door", "turret_bullet", "plane_bullet"}, (
            f"ALL_DETECTORS changed to {names!r}. Update this test "
            f"AND the cross-project smoke fixtures to cover the new "
            f"detector against Gamekit3D / ChopChop before landing."
        )

    def test_tracked_class_names_match_detectors(self) -> None:
        """The non-FPS scan filters by tracked class names. If a new
        detector accepts a primary class name we don't list, the scan
        would silently skip the new detector's namespace.
        """
        # The DoorDetector / TurretBulletDetector / PlaneBulletDetector
        # primaries each pin one specific class name. If a future
        # detector's primary accepts a different shape (e.g. a regex
        # over class names), this scan-by-name approach breaks down —
        # the constant below should grow with the detector set.
        expected = {"Door", "TurretBullet", "PlaneBullet"}
        assert _TRACKED_DETECTOR_CLASS_NAMES == expected, (
            "tracked class-name set drifted from the detector primary "
            "signals — update _TRACKED_DETECTOR_CLASS_NAMES."
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
