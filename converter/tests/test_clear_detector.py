"""gap#4 — the SOUND clear-intent container detector.

``detect_cleared_containers`` (unity/script_analyzer.py) returns the serialized
container FIELD NAMES a class provably clears-then-populates in a SINGLE method
via the canonical ``foreach (Transform c in container) Destroy(c.gameObject)`` +
``Instantiate(prefab, container)`` shape. It is the replacement for the unsound
"references any asset/prefab → delete the UI host's authored children" gate that
destroyed real UI.

The detector is biased HARD to abstain: over-detection deletes authored UI, so
EVERY ambiguity case returns the empty set. These tests pin both the positive
canonical shape and each abstain case enumerated in the design.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from unity.script_analyzer import analyze_script, detect_cleared_containers


def _wrap(class_name: str, body: str) -> str:
    return f"""
using UnityEngine;

public class {class_name} : MonoBehaviour {{
{body}
}}
"""


# ---------------------------------------------------------------------------
# Positive — the canonical clear-all-then-populate shape.
# ---------------------------------------------------------------------------

def test_canonical_clear_then_spawn_emits_field():
    src = _wrap("InventoryController", """
    public Transform containerTransform;
    public GameObject itemPrefab;

    void Refresh() {
        foreach (Transform c in containerTransform) {
            Destroy(c.gameObject);
        }
        Instantiate(itemPrefab, containerTransform);
    }
""")
    assert detect_cleared_containers(src, "InventoryController") == frozenset(
        {"containerTransform"}
    )


def test_canonical_destroy_no_gameobject_suffix_emits():
    src = _wrap("Grid", """
    public Transform grid;
    public GameObject cell;
    void Build() {
        foreach (Transform t in grid) Destroy(t);
        Instantiate(cell, grid);
    }
""")
    assert detect_cleared_containers(src, "Grid") == frozenset({"grid"})


def test_setparent_spawn_after_clear_emits():
    src = _wrap("ListView", """
    public Transform content;
    public GameObject rowPrefab;
    void Reload() {
        foreach (Transform child in content) Destroy(child.gameObject);
        var row = Instantiate(rowPrefab);
        row.transform.SetParent(content);
    }
""")
    assert detect_cleared_containers(src, "ListView") == frozenset({"content"})


def test_destroychildren_clear_emits():
    src = _wrap("Roster", """
    public Transform listRoot;
    public GameObject entry;
    void Repopulate() {
        listRoot.DestroyChildren();
        Instantiate(entry, listRoot);
    }
""")
    assert detect_cleared_containers(src, "Roster") == frozenset({"listRoot"})


def test_two_containers_each_canonical_union():
    src = _wrap("DualPanel", """
    public Transform left;
    public Transform right;
    public GameObject p;
    void RefreshLeft() {
        foreach (Transform c in left) Destroy(c.gameObject);
        Instantiate(p, left);
    }
    void RefreshRight() {
        foreach (Transform c in right) Destroy(c.gameObject);
        Instantiate(p, right);
    }
""")
    assert detect_cleared_containers(src, "DualPanel") == frozenset(
        {"left", "right"}
    )


# ---------------------------------------------------------------------------
# Abstain cases — each returns the empty set.
# ---------------------------------------------------------------------------

def test_guarded_clear_abstains():
    """The clear is wrapped in an ``if`` (guard asymmetry) → abstain."""
    src = _wrap("Ctrl", """
    public Transform container;
    public GameObject prefab;
    bool refresh;
    void Tick() {
        if (refresh) {
            foreach (Transform c in container) Destroy(c.gameObject);
        }
        Instantiate(prefab, container);
    }
""")
    assert detect_cleared_containers(src, "Ctrl") == frozenset()


def test_range_for_clear_abstains():
    """A range ``for`` clear is not the canonical full-clear shape → abstain."""
    src = _wrap("Ctrl", """
    public Transform container;
    public GameObject prefab;
    void Tick() {
        for (int i = 0; i < container.childCount; i++) {
            Destroy(container.GetChild(i).gameObject);
        }
        Instantiate(prefab, container);
    }
""")
    assert detect_cleared_containers(src, "Ctrl") == frozenset()


def test_filtered_destroy_abstains():
    """A per-child ``if`` filter inside the foreach is a partial clear → abstain."""
    src = _wrap("Ctrl", """
    public Transform container;
    public GameObject prefab;
    void Tick() {
        foreach (Transform c in container) {
            if (c.CompareTag("Keep")) continue;
            Destroy(c.gameObject);
        }
        Instantiate(prefab, container);
    }
""")
    assert detect_cleared_containers(src, "Ctrl") == frozenset()


def test_component_filtered_destroy_abstains():
    """A component-gated destroy is not an unconditional full clear → abstain."""
    src = _wrap("Ctrl", """
    public Transform container;
    public GameObject prefab;
    void Tick() {
        foreach (Transform c in container) {
            if (c.GetComponent<Image>() != null) Destroy(c.gameObject);
        }
        Instantiate(prefab, container);
    }
""")
    assert detect_cleared_containers(src, "Ctrl") == frozenset()


def test_wrong_container_abstains():
    """Clears ``C`` but spawns into ``D`` (no clear of D) → abstain."""
    src = _wrap("Ctrl", """
    public Transform containerC;
    public Transform containerD;
    public GameObject prefab;
    void Tick() {
        foreach (Transform c in containerC) Destroy(c.gameObject);
        Instantiate(prefab, containerD);
    }
""")
    assert detect_cleared_containers(src, "Ctrl") == frozenset()


def test_pre_clear_spawn_abstains():
    """A spawn into ``C`` BEFORE the clear breaks dominance → abstain."""
    src = _wrap("Ctrl", """
    public Transform container;
    public GameObject prefab;
    void Tick() {
        Instantiate(prefab, container);
        foreach (Transform c in container) Destroy(c.gameObject);
        Instantiate(prefab, container);
    }
""")
    assert detect_cleared_containers(src, "Ctrl") == frozenset()


def test_cross_method_clear_and_spawn_abstains():
    """Clear in one method, spawn in another — not a same-method shape → abstain."""
    src = _wrap("Ctrl", """
    public Transform container;
    public GameObject prefab;
    void Clear() {
        foreach (Transform c in container) Destroy(c.gameObject);
    }
    void Spawn() {
        Instantiate(prefab, container);
    }
""")
    assert detect_cleared_containers(src, "Ctrl") == frozenset()


def test_transform_self_target_abstains():
    """A spawn/clear over ``transform`` (the host's own transform, not a
    serialized container field) is excluded."""
    src = _wrap("Ctrl", """
    public GameObject prefab;
    void Tick() {
        foreach (Transform c in transform) Destroy(c.gameObject);
        Instantiate(prefab, transform);
    }
""")
    assert detect_cleared_containers(src, "Ctrl") == frozenset()


def test_lambda_split_abstains():
    """A clear/spawn moved into a lambda body → abstain (deferred scope)."""
    src = _wrap("Ctrl", """
    public Transform container;
    public GameObject prefab;
    void Tick() {
        Schedule(() => {
            foreach (Transform c in container) Destroy(c.gameObject);
            Instantiate(prefab, container);
        });
    }
""")
    assert detect_cleared_containers(src, "Ctrl") == frozenset()


def test_clear_only_no_spawn_abstains():
    """A bare clear with no Instantiate into the container → abstain (not a
    populate)."""
    src = _wrap("Ctrl", """
    public Transform container;
    void Tick() {
        foreach (Transform c in container) Destroy(c.gameObject);
    }
""")
    assert detect_cleared_containers(src, "Ctrl") == frozenset()


# ---------------------------------------------------------------------------
# Wiring — analyze_script populates ScriptInfo.cleared_container_fields.
# ---------------------------------------------------------------------------

def test_analyze_script_populates_field(tmp_path):
    cs = tmp_path / "InventoryController.cs"
    cs.write_text(_wrap("InventoryController", """
    public Transform containerTransform;
    public GameObject itemPrefab;
    void Refresh() {
        foreach (Transform c in containerTransform) Destroy(c.gameObject);
        Instantiate(itemPrefab, containerTransform);
    }
"""), encoding="utf-8")
    info = analyze_script(cs)
    assert info.cleared_container_fields == frozenset({"containerTransform"})


def test_analyze_script_empty_when_no_clear(tmp_path):
    cs = tmp_path / "Plain.cs"
    cs.write_text(_wrap("Plain", """
    public GameObject prefab;
    void Tick() { Instantiate(prefab); }
"""), encoding="utf-8")
    info = analyze_script(cs)
    assert info.cleared_container_fields == frozenset()
