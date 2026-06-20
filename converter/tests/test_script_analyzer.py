"""Tests for unity.script_analyzer, specifically the PR1 _RE_CLASS
strengthening that lets base-less helper classes register.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from unity.script_analyzer import _RE_CLASS, analyze_script


class TestReClassBaseless:
    """The PR1 contract: ``_RE_CLASS`` matches both base-less helper
    classes and the existing ``class X : Base`` shape.
    """

    def test_baseless_class_is_captured(self):
        m = _RE_CLASS.search("public static class MathHelpers { }")
        assert m is not None
        assert m.group(1) == "MathHelpers"
        # Base group is None when omitted — analyze_script normalizes to "".
        assert m.group(2) is None

    def test_class_with_base_still_captures_both(self):
        m = _RE_CLASS.search("public class Player : MonoBehaviour { }")
        assert m is not None
        assert m.group(1) == "Player"
        assert m.group(2) == "MonoBehaviour"

    def test_internal_modifier_still_matches(self):
        # `internal` isn't explicitly in the prefix group, but `re.search`
        # finds the `class` keyword anywhere in the string so this
        # naturally works.
        m = _RE_CLASS.search("internal class Helpers { }")
        assert m is not None
        assert m.group(1) == "Helpers"

    def test_generic_class_baseless(self):
        m = _RE_CLASS.search("public class Pool<T> { }")
        assert m is not None
        assert m.group(1) == "Pool"
        assert m.group(2) is None


class TestAnalyzeScriptBaseless:
    """End-to-end through ``analyze_script`` — the public surface that
    feeds the planner's modules table.
    """

    def test_baseless_helper_yields_class_name_and_empty_base(self, tmp_path: Path):
        cs = tmp_path / "MathHelpers.cs"
        cs.write_text(
            "public static class MathHelpers {\n"
            "    public static float Lerp(float a, float b, float t)\n"
            "    {\n"
            "        return a + (b - a) * t;\n"
            "    }\n"
            "}\n"
        )
        info = analyze_script(cs)
        assert info.class_name == "MathHelpers"
        assert info.base_class == ""
        # No lifecycle hooks + no MonoBehaviour base → ModuleScript.
        assert info.suggested_type == "ModuleScript"

    def test_monobehaviour_subclass_keeps_old_semantics(self, tmp_path: Path):
        cs = tmp_path / "Player.cs"
        cs.write_text(
            "public class Player : MonoBehaviour {\n"
            "    void Awake() {}\n"
            "}\n"
        )
        info = analyze_script(cs)
        assert info.class_name == "Player"
        assert info.base_class == "MonoBehaviour"
        # The Awake hook + MonoBehaviour base still routes to Script —
        # PR1 must not regress the legacy classifier.
        assert info.suggested_type == "Script"


class TestReferencedTypesGlobalLookupExclusion:
    """``referenced_types`` must NOT count the type arg of a GLOBAL
    scene-lookup generic (``FindObjectOfType<T>``) as a dependency — that
    locates an already-existing T, creating no edge and no ``require()``
    need. Counting it poisons ``dependency_map`` and misroutes the target
    in storage classification (TODO_archive.md "Transpiler false-positive
    require() injection").

    But COMPONENT-lookup generics (``GetComponent<T>`` / ``AddComponent<T>``)
    ARE real peer edges the reachability consumers need — they must STILL be
    captured (Codex review 2026-06-01: dropping them would orphan a
    component referenced only that way).

    test_findobjectoftype FAILS against the pre-fix regex (captured the
    arg); the rest guard the boundaries — global-lookup excluded, but
    component-lookup / collection / genuine deps preserved.
    """

    def _refs(self, tmp_path: Path, name: str, body: str) -> list[str]:
        cs = tmp_path / f"{name}.cs"
        cs.write_text(
            f"using UnityEngine;\npublic class {name} : MonoBehaviour {{\n"
            f"{body}\n}}\n",
            encoding="utf-8",
        )
        return analyze_script(cs).referenced_types

    def test_findobjectoftype_arg_is_not_a_dependency(self, tmp_path: Path):
        # The Plane→GameManager false edge: a global scene lookup, not a
        # dependency.
        refs = self._refs(
            tmp_path, "Plane",
            "  void Start() { var gm = FindObjectOfType<GameManager>(); }",
        )
        assert "GameManager" not in refs

    def test_all_global_lookup_variants_excluded(self, tmp_path: Path):
        # Every global-finder spelling must be skipped — incl. the plural
        # ``FindObjectsOfTypeAll`` (Codex review: the singular form was a
        # typo that left it poisoning) and the Unity 2023+ ``ByType`` APIs.
        for call in (
            "FindObjectOfType<Singleton>()",
            "FindObjectsOfType<Singleton>()",
            "Resources.FindObjectsOfTypeAll<Singleton>()",
            "FindFirstObjectByType<Singleton>()",
            "FindAnyObjectByType<Singleton>()",
            "FindObjectsByType<Singleton>(FindObjectsSortMode.None)",
        ):
            refs = self._refs(
                tmp_path, "Finder", f"  void Start() {{ var s = {call}; }}",
            )
            assert "Singleton" not in refs, call

    def test_getcomponent_arg_is_still_a_dependency(self, tmp_path: Path):
        # Component-lookup: a REAL peer edge the caller_graph / reachability
        # consumers need. Must NOT be dropped (Codex review 2026-06-01).
        refs = self._refs(
            tmp_path, "Mover",
            "  void Start() { var r = GetComponent<Movement>(); }",
        )
        assert "Movement" in refs

    def test_addcomponent_arg_is_still_a_dependency(self, tmp_path: Path):
        # AddComponent literally CREATES the peer at runtime — a real edge.
        refs = self._refs(
            tmp_path, "Rig",
            "  void Start() { gameObject.AddComponent<Health>(); }",
        )
        assert "Health" in refs

    def test_collection_generic_arg_still_captured(self, tmp_path: Path):
        # Don't over-tighten: a collection generic IS a real type reference.
        refs = self._refs(
            tmp_path, "Inventory",
            "  private System.Collections.Generic.List<ItemDef> items;",
        )
        assert "ItemDef" in refs

    def test_type_required_elsewhere_still_captured(self, tmp_path: Path):
        # Referenced BOTH via a global lookup AND a real ``new`` — the
        # ``new`` path must still register it as a dependency.
        refs = self._refs(
            tmp_path, "Spawner",
            "  void Start() {\n"
            "    var gm = FindObjectOfType<GameManager>();\n"
            "    var fresh = new GameManager();\n"
            "  }",
        )
        assert "GameManager" in refs


class TestClassNameNotMatchedInComments:
    """Regression: ``_RE_CLASS.search`` on RAW source matched the word after
    the first ``class`` token *anywhere*, including doc-comment prose. The real
    Trash Dash failure: ``/// This class allows us …`` -> class_name "allows".
    Comments/strings are now stripped before the match.
    """

    def _info(self, tmp_path: Path, name: str, body: str):
        cs = tmp_path / f"{name}.cs"
        cs.write_text(body)
        return analyze_script(cs)

    def test_doc_comment_class_prose_is_ignored(self, tmp_path: Path):
        info = self._info(
            tmp_path, "Pooler",
            "/// This class allows us to create multiple instances.\n"
            "public class Pooler\n{\n}\n",
        )
        assert info.class_name == "Pooler"  # pre-fix: "allows"

    def test_line_comment_class_prose_is_ignored(self, tmp_path: Path):
        info = self._info(
            tmp_path, "ShopList",
            "// Base class for any list in the shop\n"
            "public abstract class ShopList : MonoBehaviour\n{\n}\n",
        )
        assert info.class_name == "ShopList"  # pre-fix: "for"
        assert info.base_class == "MonoBehaviour"

    def test_abstract_class_prose_then_real_decl(self, tmp_path: Path):
        info = self._info(
            tmp_path, "Missions",
            "/// Base abstract class used to define a mission.\n"
            "public abstract class MissionBase\n{\n}\n",
        )
        assert info.class_name == "MissionBase"  # pre-fix: "used"

    def test_string_literal_class_is_ignored(self, tmp_path: Path):
        info = self._info(
            tmp_path, "Greeter",
            'const string msg = "class Imposter wins";\n'
            "public class Greeter\n{\n}\n",
        )
        assert info.class_name == "Greeter"


class TestStaticEventEnumeration:
    """``analyze_script`` surfaces ``public static event`` member names — the
    deterministic upstream signal for the static-event channel-identity fix. The
    member name IS the Luau module-table field the converter lowers the event to.
    NEVER derived from the AI Luau output.
    """

    def _info(self, tmp_path: Path, name: str, body: str):
        cs = tmp_path / f"{name}.cs"
        cs.write_text(body)
        return analyze_script(cs)

    def test_simplefps_player_four_static_events(self, tmp_path: Path):
        info = self._info(
            tmp_path, "Player",
            "public class Player : MonoBehaviour\n{\n"
            "    public delegate void HealthUpdateHandler(int curHealth);\n"
            "    public static event HealthUpdateHandler HealthUpdate;\n"
            "    public delegate void AmmoUpdateHandler(int curAmmo);\n"
            "    public static event AmmoUpdateHandler AmmoUpdate;\n"
            "    public delegate void ItemUpdateHandler(string itemName);\n"
            "    public static event ItemUpdateHandler ItemUpdate;\n"
            "    public delegate void PauseHandler(bool pause);\n"
            "    public static event PauseHandler PauseEvent;\n"
            "}\n",
        )
        assert info.static_events == [
            "HealthUpdate", "AmmoUpdate", "ItemUpdate", "PauseEvent",
        ]

    def test_instance_event_excluded(self, tmp_path: Path):
        # Only STATIC events are type-level shared fields; an instance event
        # is per-object and must NOT surface.
        info = self._info(
            tmp_path, "A",
            "public class A { public event Handler OnThing; }\n",
        )
        assert info.static_events == []

    def test_static_modifier_either_order(self, tmp_path: Path):
        for src in (
            "public class A { public static event H Foo; }",
            "public class A { static public event H Foo; }",
        ):
            info = self._info(tmp_path, "A", src)
            assert info.static_events == ["Foo"], src

    def test_generic_and_qualified_handler_types(self, tmp_path: Path):
        info = self._info(
            tmp_path, "A",
            "public class A {\n"
            "  public static event EventHandler<int> Generic;\n"
            "  public static event System.Action Qualified;\n"
            "}\n",
        )
        assert info.static_events == ["Generic", "Qualified"]

    def test_multi_declarator_event(self, tmp_path: Path):
        # A SINGLE ``static event`` declaration may bind MULTIPLE members
        # via a comma list. ``public static event H Foo, Bar;`` declares BOTH
        # ``Foo`` AND ``Bar`` of type ``H``; the analyzer must surface every
        # member, not just the last (else the dropped member never reaches the
        # channel plan or the rendezvous verifier — a silent fail).
        info = self._info(
            tmp_path, "A",
            "public class A { public static event H Foo, Bar; }",
        )
        assert info.static_events == ["Foo", "Bar"]

    def test_multi_declarator_generic_handler(self, tmp_path: Path):
        # Generic/qualified handler type + multi-declarator: the handler-type
        # capture must not swallow the declarator list.
        info = self._info(
            tmp_path, "A",
            "public class A { public static event EventHandler<int> A1, B2, C3; }",
        )
        assert info.static_events == ["A1", "B2", "C3"]

    def test_event_with_initializer(self, tmp_path: Path):
        info = self._info(
            tmp_path, "A",
            "public class A { public static event H Init = null; }",
        )
        assert info.static_events == ["Init"]

    def test_comment_and_string_decoys_ignored(self, tmp_path: Path):
        info = self._info(
            tmp_path, "A",
            "public class A {\n"
            "  /* public static event H Fake; */\n"
            '  string s = "public static event H AlsoFake;";\n'
            "  public static event H Real;\n"
            "}\n",
        )
        assert info.static_events == ["Real"]

    def test_static_field_is_not_an_event(self, tmp_path: Path):
        info = self._info(
            tmp_path, "A",
            "public class A { public static int count; }",
        )
        assert info.static_events == []

    def test_no_static_events_default_empty(self, tmp_path: Path):
        info = self._info(
            tmp_path, "A", "public class A : MonoBehaviour { }",
        )
        assert info.static_events == []


class TestDestroysSelfOnContact:
    """``destroys_self_on_contact`` — the deterministic C# signal that a body is
    a hit-and-vanish contact body (projectile / contact-damage Rigidbody), which
    drives the non-colliding (CanCollide=false) conversion so it doesn't shove
    the Roblox character (Unity's CharacterController player is never pushed by
    rigidbody collisions)."""

    def _info(self, tmp_path: Path, name: str, body: str):
        cs = tmp_path / f"{name}.cs"
        cs.write_text(body)
        return analyze_script(cs)

    def test_turretbullet_shape_oncollision_guarded_destroy(self, tmp_path: Path):
        # OnCollisionEnter with a tag guard then Destroy(gameObject) — TurretBullet.
        info = self._info(
            tmp_path, "TurretBullet",
            "public class TurretBullet : MonoBehaviour {\n"
            "  void OnCollisionEnter(Collision other) {\n"
            '    if (other.collider.tag == "Player") {\n'
            '      other.collider.SendMessage("TakeDamage", 10);\n'
            "      Destroy(gameObject);\n"
            "    }\n"
            "  }\n"
            "}\n",
        )
        assert info.destroys_self_on_contact is True

    def test_planebullet_shape_oncollision_unconditional_destroy(self, tmp_path: Path):
        # OnCollisionEnter that explodes + Destroy(gameObject) unconditionally — PlaneBullet.
        info = self._info(
            tmp_path, "PlaneBullet",
            "public class PlaneBullet : MonoBehaviour {\n"
            "  void OnCollisionEnter(Collision other) {\n"
            "    Instantiate(explosion, transform.position, Quaternion.identity);\n"
            "    Destroy(gameObject);\n"
            "  }\n"
            "}\n",
        )
        assert info.destroys_self_on_contact is True

    def test_this_gameobject_variant(self, tmp_path: Path):
        info = self._info(
            tmp_path, "B",
            "public class B : MonoBehaviour {\n"
            "  void OnTriggerEnter(Collider o) { Destroy(this.gameObject); }\n"
            "}\n",
        )
        assert info.destroys_self_on_contact is True

    def test_mine_shape_destroy_in_separate_method_abstains(self, tmp_path: Path):
        # Mine: OnTriggerEnter only Invokes; the Destroy lives in Explode(). The
        # body does not destroy itself IN the contact handler -> abstain (and the
        # Mine is already a trigger -> CanCollide=false anyway).
        info = self._info(
            tmp_path, "Mine",
            "public class Mine : MonoBehaviour {\n"
            '  void OnTriggerEnter(Collider o) { Invoke("Explode", 1f); }\n'
            "  void Explode() { Destroy(gameObject); }\n"
            "}\n",
        )
        assert info.destroys_self_on_contact is False

    def test_collision_handler_without_self_destroy_abstains(self, tmp_path: Path):
        # A contact handler that reacts but persists (no self-destroy) must NOT
        # be flagged — it is a legitimately-colliding body.
        info = self._info(
            tmp_path, "Bumper",
            "public class Bumper : MonoBehaviour {\n"
            "  void OnCollisionEnter(Collision o) { audioSource.Play(); }\n"
            "}\n",
        )
        assert info.destroys_self_on_contact is False

    def test_destroy_self_outside_contact_handler_abstains(self, tmp_path: Path):
        # Destroy(gameObject) in Update (timed despawn), no contact handler at
        # all -> not a contact body.
        info = self._info(
            tmp_path, "Decay",
            "public class Decay : MonoBehaviour {\n"
            "  void Update() { if (t > 5f) Destroy(gameObject); }\n"
            "}\n",
        )
        assert info.destroys_self_on_contact is False

    def test_destroy_other_object_in_handler_abstains(self, tmp_path: Path):
        # Destroying a DIFFERENT object (a collectible the player picks up) is not
        # self-destruction of THIS body -> not flagged.
        info = self._info(
            tmp_path, "Collector",
            "public class Collector : MonoBehaviour {\n"
            "  void OnCollisionEnter(Collision o) { Destroy(o.gameObject); }\n"
            "}\n",
        )
        assert info.destroys_self_on_contact is False

    def test_default_false_no_hooks(self, tmp_path: Path):
        info = self._info(
            tmp_path, "Plain", "public class Plain : MonoBehaviour { }",
        )
        assert info.destroys_self_on_contact is False


class TestSelfLaunchesProjectileGuard:
    """``self_launches`` is the projectile discriminator paired with
    ``destroys_self_on_contact`` to keep the non-colliding override OFF a
    breakable platform / crate that is meant to stay solid until it shatters
    (adversarial review false positive)."""

    def _info(self, tmp_path: Path, name: str, body: str):
        cs = tmp_path / f"{name}.cs"
        cs.write_text(body)
        return analyze_script(cs)

    def test_bullet_self_launches(self, tmp_path: Path):
        info = self._info(
            tmp_path, "TurretBullet",
            "public class TurretBullet : MonoBehaviour {\n"
            "  void Start() { rb.AddRelativeForce(Vector3.forward * force, ForceMode.Impulse); }\n"
            "  void OnCollisionEnter(Collision o) { Destroy(gameObject); }\n"
            "}\n",
        )
        assert info.self_launches is True
        assert info.destroys_self_on_contact is True

    def test_breakable_platform_is_not_a_projectile(self, tmp_path: Path):
        # The codex-found false positive: a non-kinematic body that is SOLID
        # (player stands on it) until it shatters on contact. It destroys itself
        # on contact but NEVER self-launches -> must NOT be flagged as a
        # non-colliding projectile (else the player falls through it).
        info = self._info(
            tmp_path, "BreakablePlatform",
            "public class BreakablePlatform : MonoBehaviour {\n"
            "  void OnCollisionEnter(Collision c) {\n"
            '    if (c.gameObject.CompareTag(\"Player\")) Destroy(gameObject);\n'
            "  }\n"
            "}\n",
        )
        assert info.destroys_self_on_contact is True   # it does destroy on contact
        assert info.self_launches is False              # but it does NOT self-launch
        # => the pipeline conjunction (both required) leaves it COLLIDING.

    def test_plain_body_neither_signal(self, tmp_path: Path):
        info = self._info(
            tmp_path, "Plain", "public class Plain : MonoBehaviour { }",
        )
        assert info.self_launches is False
        assert info.destroys_self_on_contact is False
