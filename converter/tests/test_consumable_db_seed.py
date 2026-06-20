"""Unit tests for the consumable-prototype build-time resolver (Phase 1 §1.A).

Builds REALISTIC synthetic Unity projects (mirroring trash-dash's shapes: an SO
``.asset`` carrying an array of in-prefab MonoBehaviour object-refs, prefabs whose
component anchors carry serialized fields + an ``m_Script`` to a project ``.cs``,
and a database ``.cs`` that drains the array as objects) so the GuidIndex resolves
through the canonical ``build_guid_index`` path — not tautological synthetic
records. Covers: positive resolve, common-base ABSTAIN, consumer-usage ABSTAIN,
unresolvable-element DROP, and the intermediate-base case.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from unity.guid_resolver import build_guid_index
from converter.consumable_db_seed import (
    build_base_by_class,
    common_monobehaviour_base,
    db_drains_field_as_objects,
    field_types_for_class,
    find_component_ref_arrays,
    read_prefab_component,
    resolve_db_seed,
    split_component_fields,
)
from converter.scriptable_object_converter import convert_asset_file


def _module_path_for_stem(stem: str) -> str | None:
    """A trivial collision-free build-time module-path resolver for tests:
    every subclass stem maps to ``ServerStorage.<stem>``. Production builds this
    from ``scene_runtime["modules"]`` with the planner's collision exclusion."""
    return f"ServerStorage.{stem}" if stem else None


# --------------------------------------------------------------------------- #
# Fixture builders — write .cs/.prefab/.asset + .meta so build_guid_index works.
# --------------------------------------------------------------------------- #

# Stable test GUIDs. Real Unity guids are 32 hex chars that ALWAYS contain
# letters, so YAML never coerces them to ints — use a leading letter + digits so
# each is distinct yet stays a string when re-parsed from the .asset/.prefab.
def _g(tag: str) -> str:
    """A distinct 32-char hex guid seeded by ``tag`` (letters guarantee a str)."""
    import hashlib
    return "a" + hashlib.sha256(tag.encode()).hexdigest()[:31]


G_DB_CS = _g("db_cs")            # ConsumableDatabase.cs
G_BASE_CS = _g("base_cs")        # Consumable.cs (abstract base : MonoBehaviour)
G_COINMAGNET_CS = _g("coinmagnet_cs")  # CoinMagnet.cs : Consumable
G_EXTRALIFE_CS = _g("extralife_cs")    # ExtraLife.cs : Consumable
G_COINMAGNET_PREFAB = _g("coinmagnet_prefab")
G_EXTRALIFE_PREFAB = _g("extralife_prefab")
G_ASSET = _g("asset")            # Consumables.asset
G_ICON = _g("icon")              # a Sprite asset (icon ref)

# In-prefab MonoBehaviour anchor fileIDs.
FID_COINMAGNET = 11491712
FID_EXTRALIFE = 114000011351653892


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_meta(asset_path: Path, guid: str) -> None:
    asset_path.with_suffix(asset_path.suffix + ".meta").write_text(
        f"fileFormatVersion: 2\nguid: {guid}\n", encoding="utf-8",
    )


def _cs(root: Path, rel: str, guid: str, source: str) -> None:
    p = root / "Assets" / rel
    _write(p, textwrap.dedent(source))
    _write_meta(p, guid)


def _prefab_with_component(
    root: Path, rel: str, guid: str, fid: int, script_guid: str, extra_fields: str,
) -> None:
    """A prefab whose MonoBehaviour at ``&fid`` has ``m_Script`` -> ``script_guid``
    and the given extra serialized fields. ``extra_fields`` is dedented then each
    line indented two spaces under the MonoBehaviour body."""
    p = root / "Assets" / rel
    field_lines = [
        "  " + ln if ln.strip() else ""
        for ln in textwrap.dedent(extra_fields).splitlines()
    ]
    body = (
        "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
        "--- !u!1 &100\nGameObject:\n"
        "  m_Name: Root\n  m_Component:\n"
        f"  - component: {{fileID: {fid}}}\n"
        f"--- !u!114 &{fid}\nMonoBehaviour:\n"
        "  m_ObjectHideFlags: 0\n"
        "  m_GameObject: {fileID: 100}\n"
        "  m_Enabled: 1\n"
        f"  m_Script: {{fileID: 11500000, guid: {script_guid}, type: 3}}\n"
        "  m_Name:\n"
        + "\n".join(field_lines) + "\n"
    )
    _write(p, body)
    _write_meta(p, guid)


def _asset_with_array(
    root: Path, rel: str, guid: str, script_guid: str, refs: list[tuple[int, str]],
    array_field: str = "consumbales",
) -> Path:
    p = root / "Assets" / rel
    ref_lines = "\n".join(
        f"  - {{fileID: {fid}, guid: {g}, type: 2}}" for fid, g in refs
    )
    body = (
        "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
        "--- !u!114 &11400000\nMonoBehaviour:\n"
        "  m_ObjectHideFlags: 0\n"
        f"  m_Script: {{fileID: 11500000, guid: {script_guid}, type: 3}}\n"
        f"  m_Name: {rel.rsplit('/', 1)[-1].rsplit('.', 1)[0]}\n"
        f"  {array_field}:\n{ref_lines}\n"
    )
    _write(p, body)
    _write_meta(p, guid)
    return p


_DB_CS_DRAINS_OBJECTS = """\
    public class ConsumableDatabase : ScriptableObject
    {
        public Consumable[] consumbales;
        static Dictionary<int, Consumable> _dict;
        public void Load()
        {
            for (int i = 0; i < consumbales.Length; ++i)
                _dict.Add(consumbales[i].GetConsumableType(), consumbales[i]);
        }
    }
"""

_BASE_CS = """\
    public abstract class Consumable : MonoBehaviour
    {
        public float duration;
        public Sprite icon;
        public AssetReference ActivatedParticleReference;
        public bool canBeSpawned = true;
        public abstract int GetConsumableType();
    }
"""

_COINMAGNET_CS = """\
    public class CoinMagnet : Consumable
    {
        public override int GetConsumableType() { return 1; }
    }
"""

_EXTRALIFE_CS = """\
    public class ExtraLife : Consumable
    {
        public override int GetConsumableType() { return 4; }
    }
"""


def _build_trash_dash_like(tmp_path: Path) -> Path:
    """A realistic project: a Consumables.asset with two in-prefab component
    refs (CoinMagnet, ExtraLife) both : Consumable : MonoBehaviour, drained as
    objects by ConsumableDatabase."""
    root = tmp_path / "proj"
    _cs(root, "Scripts/ConsumableDatabase.cs", G_DB_CS, _DB_CS_DRAINS_OBJECTS)
    _cs(root, "Scripts/Consumable.cs", G_BASE_CS, _BASE_CS)
    _cs(root, "Scripts/Types/CoinMagnet.cs", G_COINMAGNET_CS, _COINMAGNET_CS)
    _cs(root, "Scripts/Types/ExtraLife.cs", G_EXTRALIFE_CS, _EXTRALIFE_CS)
    _prefab_with_component(
        root, "Prefabs/CoinMagnet.prefab", G_COINMAGNET_PREFAB, FID_COINMAGNET,
        G_COINMAGNET_CS,
        f"""\
        duration: 15
        icon: {{fileID: 21300028, guid: {G_ICON}, type: 3}}
        ActivatedParticleReference:
          m_AssetGUID: {G_EXTRALIFE_PREFAB}
          m_CachedAsset: {{fileID: 0}}
        canBeSpawned: 1
        """,
    )
    _prefab_with_component(
        root, "Prefabs/ExtraLife.prefab", G_EXTRALIFE_PREFAB, FID_EXTRALIFE,
        G_EXTRALIFE_CS,
        """\
        duration: 0.01
        canBeSpawned: 0
        """,
    )
    _asset_with_array(
        root, "Prefabs/Consumables.asset", G_ASSET, G_DB_CS,
        [(FID_COINMAGNET, G_COINMAGNET_PREFAB), (FID_EXTRALIFE, G_EXTRALIFE_PREFAB)],
    )
    return root


def _asset_body(root: Path, rel: str) -> dict[str, object]:
    from unity.yaml_parser import doc_body, parse_documents
    raw = (root / "Assets" / rel).read_text(encoding="utf-8")
    for class_id, _fid, doc in parse_documents(raw):
        if class_id == 114 and "MonoBehaviour" in doc:
            return doc_body(doc)
    raise AssertionError("no MonoBehaviour doc")


# --------------------------------------------------------------------------- #
# read_prefab_component
# --------------------------------------------------------------------------- #

def test_read_prefab_component_finds_anchor(tmp_path):
    root = _build_trash_dash_like(tmp_path)
    prefab = root / "Assets" / "Prefabs" / "CoinMagnet.prefab"
    comp = read_prefab_component(prefab, FID_COINMAGNET)
    assert comp is not None
    assert comp["duration"] == 15
    m_script = comp["m_Script"]
    assert isinstance(m_script, dict) and m_script["guid"] == G_COINMAGNET_CS


def test_read_prefab_component_missing_anchor_is_none(tmp_path):
    root = _build_trash_dash_like(tmp_path)
    prefab = root / "Assets" / "Prefabs" / "CoinMagnet.prefab"
    assert read_prefab_component(prefab, 999999) is None


# --------------------------------------------------------------------------- #
# Positive case
# --------------------------------------------------------------------------- #

def test_positive_resolves_seed_with_elements(tmp_path):
    root = _build_trash_dash_like(tmp_path)
    guid_index = build_guid_index(root)
    base_by_class = build_base_by_class(guid_index)
    body = _asset_body(root, "Prefabs/Consumables.asset")

    seed = resolve_db_seed(
        db_module_path="ServerStorage.ConsumableDatabase",
        db_cs_source=_DB_CS_DRAINS_OBJECTS,
        asset_body=body,
        guid_index=guid_index,
        base_by_class=base_by_class,
        module_path_for_stem=_module_path_for_stem,
    )
    assert seed is not None
    assert seed["array_field"] == "consumbales"
    assert len(seed["elements"]) == 2
    stems = [e["class_stem"] for e in seed["elements"]]
    assert stems == ["CoinMagnet", "ExtraLife"]
    # prefab ids are "<guid>:<relpath>"
    cm, xl = seed["elements"]
    assert cm["prefab_id"].startswith(G_COINMAGNET_PREFAB + ":")
    # module_path resolved at BUILD time (carried on the element; no runtime scan).
    assert cm["module_path"] == "ServerStorage.CoinMagnet"
    assert xl["module_path"] == "ServerStorage.ExtraLife"
    # CoinMagnet: scalars in ctor_config (canBeSpawned: 1 -> True, declared bool);
    # the ActivatedParticleReference (AssetReference -> a .prefab) RESOLVED into
    # post_fields, never ctor_config. (icon is a Sprite, not a .prefab, so it does
    # not resolve to a prefab id and is dropped — same as the legacy _value_to_lua.)
    assert cm["ctor_config"]["duration"] == 15
    assert cm["ctor_config"]["canBeSpawned"] is True
    assert "ActivatedParticleReference" not in cm["ctor_config"]
    assert "icon" not in cm["ctor_config"]
    apr = cm["post_fields"]["ActivatedParticleReference"]
    assert isinstance(apr, str) and apr.startswith(G_EXTRALIFE_PREFAB + ":")
    # ExtraLife: bool-coerced ``canBeSpawned: 0`` -> Python ``False`` (NOT 0, which
    # is truthy in Luau — the load-bearing P1-2 coercion).
    assert xl["ctor_config"]["canBeSpawned"] is False
    assert xl["ctor_config"]["duration"] == 0.01
    assert xl["post_fields"] == {}


def test_positive_field_literal_is_deterministic(tmp_path):
    root = _build_trash_dash_like(tmp_path)
    guid_index = build_guid_index(root)
    base_by_class = build_base_by_class(guid_index)
    body = _asset_body(root, "Prefabs/Consumables.asset")
    args = dict(
        db_module_path="ServerStorage.ConsumableDatabase",
        db_cs_source=_DB_CS_DRAINS_OBJECTS,
        asset_body=body,
        guid_index=guid_index,
        base_by_class=base_by_class,
        module_path_for_stem=_module_path_for_stem,
    )
    a = resolve_db_seed(**args)
    b = resolve_db_seed(**args)
    assert a == b  # pure + sorted keys -> byte-identical recompute


# --------------------------------------------------------------------------- #
# Gate (4) — common-base ABSTAIN
# --------------------------------------------------------------------------- #

def test_common_base_abstain_mixed_family(tmp_path):
    """An array mixing a Consumable-derived component with a non-component class
    (no shared MonoBehaviour base) contributes NO seed."""
    root = tmp_path / "proj"
    _cs(root, "Scripts/ConsumableDatabase.cs", G_DB_CS, _DB_CS_DRAINS_OBJECTS)
    _cs(root, "Scripts/Consumable.cs", G_BASE_CS, _BASE_CS)
    _cs(root, "Scripts/Types/CoinMagnet.cs", G_COINMAGNET_CS, _COINMAGNET_CS)
    # A plain POCO that does NOT derive from MonoBehaviour.
    G_POCO = _g("poco")
    G_POCO_PREFAB = _g("poco_prefab")
    _cs(root, "Scripts/PlainThing.cs", G_POCO, "public class PlainThing { }\n")
    _prefab_with_component(
        root, "Prefabs/CoinMagnet.prefab", G_COINMAGNET_PREFAB, FID_COINMAGNET,
        G_COINMAGNET_CS, "duration: 15\n",
    )
    _prefab_with_component(
        root, "Prefabs/Plain.prefab", G_POCO_PREFAB, 222, G_POCO, "x: 1\n",
    )
    _asset_with_array(
        root, "Prefabs/Consumables.asset", G_ASSET, G_DB_CS,
        [(FID_COINMAGNET, G_COINMAGNET_PREFAB), (222, G_POCO_PREFAB)],
    )
    guid_index = build_guid_index(root)
    base_by_class = build_base_by_class(guid_index)
    body = _asset_body(root, "Prefabs/Consumables.asset")
    seed = resolve_db_seed(
        db_module_path="ServerStorage.ConsumableDatabase",
        db_cs_source=_DB_CS_DRAINS_OBJECTS,
        asset_body=body,
        guid_index=guid_index,
        base_by_class=base_by_class,
        module_path_for_stem=_module_path_for_stem,
    )
    assert seed is None


def test_common_base_abstain_unrelated_component_families(tmp_path):
    """End-to-end: an array of two UNRELATED components that each derive DIRECTLY
    from MonoBehaviour (no shared project-local base) contributes NO seed — the
    bare-MonoBehaviour shared root is rejected at gate (4)."""
    root = tmp_path / "proj"
    G_ALPHA_CS = _g("alpha_cs")
    G_BETA_CS = _g("beta_cs")
    G_ALPHA_PREFAB = _g("alpha_prefab")
    G_BETA_PREFAB = _g("beta_prefab")
    _cs(root, "Scripts/ConsumableDatabase.cs", G_DB_CS, _DB_CS_DRAINS_OBJECTS)
    # Two unrelated component classes, each : MonoBehaviour, no common subclass.
    _cs(root, "Scripts/Alpha.cs", G_ALPHA_CS,
        "public class Alpha : MonoBehaviour { public int GetConsumableType() { return 1; } }\n")
    _cs(root, "Scripts/Beta.cs", G_BETA_CS,
        "public class Beta : MonoBehaviour { public int GetConsumableType() { return 2; } }\n")
    _prefab_with_component(
        root, "Prefabs/Alpha.prefab", G_ALPHA_PREFAB, 444, G_ALPHA_CS, "x: 1\n",
    )
    _prefab_with_component(
        root, "Prefabs/Beta.prefab", G_BETA_PREFAB, 555, G_BETA_CS, "y: 2\n",
    )
    _asset_with_array(
        root, "Prefabs/Consumables.asset", G_ASSET, G_DB_CS,
        [(444, G_ALPHA_PREFAB), (555, G_BETA_PREFAB)],
    )
    guid_index = build_guid_index(root)
    base_by_class = build_base_by_class(guid_index)
    body = _asset_body(root, "Prefabs/Consumables.asset")
    seed = resolve_db_seed(
        db_module_path="ServerStorage.ConsumableDatabase",
        db_cs_source=_DB_CS_DRAINS_OBJECTS,
        asset_body=body,
        guid_index=guid_index,
        base_by_class=base_by_class,
        module_path_for_stem=_module_path_for_stem,
    )
    assert seed is None


def test_common_monobehaviour_base_unit():
    # Two siblings -> shared Consumable base.
    bbc = {"CoinMagnet": "Consumable", "ExtraLife": "Consumable",
           "Consumable": "MonoBehaviour"}
    assert common_monobehaviour_base(["CoinMagnet", "ExtraLife"], bbc) == "Consumable"
    # A non-component sibling -> None.
    bbc2 = {"CoinMagnet": "Consumable", "Consumable": "MonoBehaviour",
            "PlainThing": ""}
    assert common_monobehaviour_base(["CoinMagnet", "PlainThing"], bbc2) is None


def test_common_base_abstains_on_bare_monobehaviour_shared_root():
    """Two UNRELATED component families whose ONLY shared ancestor is the bare
    ``MonoBehaviour`` base -> ABSTAIN (None). The shared root proves "all are
    components", not "one coherent family"; accepting it reopens the unrelated-
    component-array hole gate (4) exists to close.

    Pre-fix this returned ``"MonoBehaviour"``: ``_full_ancestor_chain`` appends
    the truthy external base, so both chains end in ``MonoBehaviour``, the
    intersection is ``{"MonoBehaviour"}``, and the OLD selection loop returned it
    via ``cls in _COMPONENT_BASE_CLASSES``.
    """
    bbc = {"A": "MonoBehaviour", "B": "MonoBehaviour"}
    assert common_monobehaviour_base(["A", "B"], bbc) is None
    # NetworkBehaviour is rejected the same way.
    bbc_net = {"A": "NetworkBehaviour", "B": "NetworkBehaviour"}
    assert common_monobehaviour_base(["A", "B"], bbc_net) is None


# --------------------------------------------------------------------------- #
# Intermediate-base case (subclass of a subclass still resolves common ancestor)
# --------------------------------------------------------------------------- #

def test_intermediate_base_resolves_common_ancestor():
    # RareConsumable : Consumable; both RareConsumable's subclass and a direct
    # Consumable subclass share the common ancestor Consumable (NOT immediate-
    # base equality, which would wrongly abstain).
    bbc = {
        "CoinMagnet": "Consumable",
        "GoldMagnet": "RareConsumable",
        "RareConsumable": "Consumable",
        "Consumable": "MonoBehaviour",
    }
    base = common_monobehaviour_base(["CoinMagnet", "GoldMagnet"], bbc)
    assert base == "Consumable"


def test_intermediate_base_full_resolve(tmp_path):
    """End-to-end: a direct subclass + an intermediate-subclass-of-subclass still
    resolve a seed (common ancestor Consumable)."""
    root = tmp_path / "proj"
    G_RARE_CS = _g("rare_cs")
    G_GOLD_CS = _g("gold_cs")
    G_GOLD_PREFAB = _g("gold_prefab")
    _cs(root, "Scripts/ConsumableDatabase.cs", G_DB_CS, _DB_CS_DRAINS_OBJECTS)
    _cs(root, "Scripts/Consumable.cs", G_BASE_CS, _BASE_CS)
    _cs(root, "Scripts/Types/CoinMagnet.cs", G_COINMAGNET_CS, _COINMAGNET_CS)
    _cs(root, "Scripts/Types/RareConsumable.cs", G_RARE_CS,
        "public class RareConsumable : Consumable { }\n")
    _cs(root, "Scripts/Types/GoldMagnet.cs", G_GOLD_CS,
        "public class GoldMagnet : RareConsumable { public override int GetConsumableType() { return 9; } }\n")
    _prefab_with_component(
        root, "Prefabs/CoinMagnet.prefab", G_COINMAGNET_PREFAB, FID_COINMAGNET,
        G_COINMAGNET_CS, "duration: 15\n",
    )
    _prefab_with_component(
        root, "Prefabs/GoldMagnet.prefab", G_GOLD_PREFAB, 333, G_GOLD_CS,
        "duration: 20\n",
    )
    _asset_with_array(
        root, "Prefabs/Consumables.asset", G_ASSET, G_DB_CS,
        [(FID_COINMAGNET, G_COINMAGNET_PREFAB), (333, G_GOLD_PREFAB)],
    )
    guid_index = build_guid_index(root)
    base_by_class = build_base_by_class(guid_index)
    body = _asset_body(root, "Prefabs/Consumables.asset")
    seed = resolve_db_seed(
        db_module_path="ServerStorage.ConsumableDatabase",
        db_cs_source=_DB_CS_DRAINS_OBJECTS,
        asset_body=body,
        guid_index=guid_index,
        base_by_class=base_by_class,
        module_path_for_stem=_module_path_for_stem,
    )
    assert seed is not None
    assert [e["class_stem"] for e in seed["elements"]] == ["CoinMagnet", "GoldMagnet"]


# --------------------------------------------------------------------------- #
# Gate (5) — consumer-usage ABSTAIN
# --------------------------------------------------------------------------- #

_DB_CS_INSTANTIATES = """\
    public class PrefabDatabase : ScriptableObject
    {
        public Consumable[] consumbales;
        public void Spawn()
        {
            for (int i = 0; i < consumbales.Length; ++i)
                Instantiate(consumbales[i]);
        }
    }
"""


def test_consumer_usage_abstain_instantiation_path(tmp_path):
    root = _build_trash_dash_like(tmp_path)
    guid_index = build_guid_index(root)
    base_by_class = build_base_by_class(guid_index)
    body = _asset_body(root, "Prefabs/Consumables.asset")
    seed = resolve_db_seed(
        db_module_path="ServerStorage.PrefabDatabase",
        db_cs_source=_DB_CS_INSTANTIATES,  # forwards elements to Instantiate
        asset_body=body,
        guid_index=guid_index,
        base_by_class=base_by_class,
        module_path_for_stem=_module_path_for_stem,
    )
    assert seed is None


def test_drain_pattern_unit():
    assert db_drains_field_as_objects(_DB_CS_DRAINS_OBJECTS, "consumbales") is True
    assert db_drains_field_as_objects(_DB_CS_INSTANTIATES, "consumbales") is False
    # foreach object usage
    foreach_src = (
        "public void Load() { foreach (var c in consumbales) { _dict[c.GetType()] = c; } }"
    )
    assert db_drains_field_as_objects(foreach_src, "consumbales") is True
    # field never dereferenced (only .Length) -> not object usage
    len_only = "public void N() { int n = consumbales.Length; }"
    assert db_drains_field_as_objects(len_only, "consumbales") is False


def test_drain_pattern_prefab_member_instantiate_abstains():
    """Finding #2: ``Instantiate(c.gameObject)`` / ``Instantiate(field[i].member)``
    is PREFAB usage (the element is a prefab carrier), not object usage. A member
    ACCESS whose value is fed to Instantiate must NOT be treated as object usage."""
    # foreach loop var whose .gameObject is instantiated -> prefab usage.
    foreach_proto = (
        "public void Spawn() { foreach (var c in consumbales) Instantiate(c.gameObject); }"
    )
    assert db_drains_field_as_objects(foreach_proto, "consumbales") is False
    # indexed element member instantiated -> prefab usage.
    indexed_proto = (
        "public void Spawn() { for (int i=0;i<consumbales.Length;++i) "
        "Instantiate(consumbales[i].gameObject); }"
    )
    assert db_drains_field_as_objects(indexed_proto, "consumbales") is False


def test_drain_pattern_foreach_instantiate_element_abstains():
    """Finding #6: ``foreach (var c in field) Instantiate(c)`` — the loop var bound
    to an element is handed straight to Instantiate -> prefab usage, abstain."""
    src = "public void Spawn() { foreach (var c in consumbales) Instantiate(c); }"
    assert db_drains_field_as_objects(src, "consumbales") is False


def test_drain_pattern_local_alias_method_call_is_object_usage():
    """Finding #3: a local alias of an element drained as an object
    (``var c = field[i]; c.Method();``) is object usage — the structural check
    must follow the element binding, not only direct ``field[i].Member``."""
    aliased = (
        "public void Load() { for (int i=0;i<consumbales.Length;++i) "
        "{ var c = consumbales[i]; _dict.Add(c.GetConsumableType(), c); } }"
    )
    assert db_drains_field_as_objects(aliased, "consumbales") is True
    # foreach alias method call (the common shape) is object usage too.
    foreach_alias = (
        "public void Load() { foreach (var c in consumbales) { c.Activate(); } }"
    )
    assert db_drains_field_as_objects(foreach_alias, "consumbales") is True


def test_drain_pattern_member_access_only_is_not_object_usage():
    """A bare member ACCESS that is never CALLED is not positive object usage —
    require a method call (finding #2's positive-signal requirement)."""
    # field[i].member read (no call) and not drained as objects elsewhere.
    read_only = (
        "public int Sum() { int t = 0; for (int i=0;i<consumbales.Length;++i) "
        "t += consumbales[i].weight; return t; }"
    )
    assert db_drains_field_as_objects(read_only, "consumbales") is False


# --------------------------------------------------------------------------- #
# Finding #5 — WARN when >1 candidate array passes both gates
# --------------------------------------------------------------------------- #

def test_multiple_passing_arrays_warns_and_seeds_first(tmp_path, caplog):
    """When >1 candidate array on one DB passes both gates, the resolver seeds
    only the first (the shim assigns one array_field per DB) and WARNS so a
    multi-array DB is not silently half-seeded."""
    import logging

    root = tmp_path / "proj"
    G_SECOND_PREFAB = _g("second_prefab")
    # A DB C# that drains BOTH fields as objects.
    db_cs_two = """\
        public class ConsumableDatabase : ScriptableObject
        {
            public Consumable[] consumbales;
            public Consumable[] secondary;
            public void Load()
            {
                for (int i = 0; i < consumbales.Length; ++i)
                    _dict.Add(consumbales[i].GetConsumableType(), consumbales[i]);
                for (int j = 0; j < secondary.Length; ++j)
                    secondary[j].Activate();
            }
        }
    """
    _cs(root, "Scripts/ConsumableDatabase.cs", G_DB_CS, db_cs_two)
    _cs(root, "Scripts/Consumable.cs", G_BASE_CS, _BASE_CS)
    _cs(root, "Scripts/Types/CoinMagnet.cs", G_COINMAGNET_CS, _COINMAGNET_CS)
    _cs(root, "Scripts/Types/ExtraLife.cs", G_EXTRALIFE_CS, _EXTRALIFE_CS)
    _prefab_with_component(
        root, "Prefabs/CoinMagnet.prefab", G_COINMAGNET_PREFAB, FID_COINMAGNET,
        G_COINMAGNET_CS, "duration: 15\n",
    )
    _prefab_with_component(
        root, "Prefabs/ExtraLife.prefab", G_EXTRALIFE_PREFAB, FID_EXTRALIFE,
        G_EXTRALIFE_CS, "duration: 1\n",
    )
    # Build an asset with TWO component-ref array fields.
    p = root / "Assets" / "Prefabs" / "Consumables.asset"
    body_yaml = (
        "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
        "--- !u!114 &11400000\nMonoBehaviour:\n"
        "  m_ObjectHideFlags: 0\n"
        f"  m_Script: {{fileID: 11500000, guid: {G_DB_CS}, type: 3}}\n"
        "  m_Name: Consumables\n"
        f"  consumbales:\n  - {{fileID: {FID_COINMAGNET}, guid: {G_COINMAGNET_PREFAB}, type: 2}}\n"
        f"  secondary:\n  - {{fileID: {FID_EXTRALIFE}, guid: {G_EXTRALIFE_PREFAB}, type: 2}}\n"
    )
    _write(p, body_yaml)
    _write_meta(p, G_ASSET)

    guid_index = build_guid_index(root)
    base_by_class = build_base_by_class(guid_index)
    body = _asset_body(root, "Prefabs/Consumables.asset")
    with caplog.at_level(logging.WARNING, logger="converter.consumable_db_seed"):
        seed = resolve_db_seed(
            db_module_path="ServerStorage.ConsumableDatabase",
            db_cs_source=db_cs_two,
            asset_body=body,
            guid_index=guid_index,
            base_by_class=base_by_class,
            module_path_for_stem=_module_path_for_stem,
        )
    assert seed is not None
    # Seeds only the FIRST passing array (dict order = source order).
    assert seed["array_field"] == "consumbales"
    assert any(
        "passed both gates" in r.getMessage() for r in caplog.records
    ), "expected a >1-candidate WARN"


# --------------------------------------------------------------------------- #
# Unresolvable element is DROPPED, not stringified
# --------------------------------------------------------------------------- #

def test_unresolvable_element_dropped(tmp_path):
    """One ref points at a guid not in the index -> that element is DROPPED; the
    resolvable element still seeds. No string is ever emitted for the dropped
    slot."""
    root = _build_trash_dash_like(tmp_path)
    # Add a bogus ref (guid not present in the project) to the asset array.
    bogus_guid = "0123456789abcdef0123456789abcdef"
    _asset_with_array(
        root, "Prefabs/Consumables.asset", G_ASSET, G_DB_CS,
        [
            (FID_COINMAGNET, G_COINMAGNET_PREFAB),
            (123, bogus_guid),  # unresolvable
            (FID_EXTRALIFE, G_EXTRALIFE_PREFAB),
        ],
    )
    guid_index = build_guid_index(root)
    base_by_class = build_base_by_class(guid_index)
    body = _asset_body(root, "Prefabs/Consumables.asset")
    seed = resolve_db_seed(
        db_module_path="ServerStorage.ConsumableDatabase",
        db_cs_source=_DB_CS_DRAINS_OBJECTS,
        asset_body=body,
        guid_index=guid_index,
        base_by_class=base_by_class,
        module_path_for_stem=_module_path_for_stem,
    )
    assert seed is not None
    # 3 refs in, 1 unresolvable -> 2 elements out (dropped, not stringified).
    assert len(seed["elements"]) == 2
    assert [e["class_stem"] for e in seed["elements"]] == ["CoinMagnet", "ExtraLife"]
    # No element carries a bare prefab-id string in place of a class.
    for e in seed["elements"]:
        assert e["class_stem"] and e["prefab_id"]


# --------------------------------------------------------------------------- #
# find_component_ref_arrays — structural detection
# --------------------------------------------------------------------------- #

def test_find_component_ref_arrays_structural(tmp_path):
    root = _build_trash_dash_like(tmp_path)
    body = _asset_body(root, "Prefabs/Consumables.asset")
    arrays = find_component_ref_arrays(body)
    assert "consumbales" in arrays
    assert len(arrays["consumbales"]) == 2


def test_find_component_ref_arrays_ignores_scalar_and_mixed():
    body = {
        "m_Name": "X",
        "scalars": [1, 2, 3],
        "mixed": [{"fileID": 1, "guid": "a" * 32, "type": 2}, {"foo": 1}],
        "refs": [{"fileID": 1, "guid": "a" * 32, "type": 2}],
        "zeroguid": [{"fileID": 0, "guid": "0" * 32, "type": 2}],
    }
    arrays = find_component_ref_arrays(body)
    assert set(arrays.keys()) == {"refs"}


# --------------------------------------------------------------------------- #
# P1-2 — bool coercion keyed on the C# DECLARED type (never a field name)
# --------------------------------------------------------------------------- #

def test_field_types_for_class_walks_base_chain(tmp_path):
    """``canBeSpawned`` is declared on the BASE ``Consumable``, not the subclass;
    the type map must walk the class+base chain to find it as ``bool``."""
    root = _build_trash_dash_like(tmp_path)
    guid_index = build_guid_index(root)
    base_by_class = build_base_by_class(guid_index)
    types = field_types_for_class("CoinMagnet", base_by_class, guid_index)
    assert types["canBeSpawned"] == "bool"     # found on the base, declared bool
    assert types["duration"] == "float"
    assert types["icon"] == "Sprite"


def test_split_component_fields_coerces_bool_and_resolves_refs(tmp_path):
    """A ``bool``-declared ``0`` becomes Python ``False`` (NOT 0); an asset-ref
    field is RESOLVED into post_fields, never left in ctor_config."""
    root = _build_trash_dash_like(tmp_path)
    guid_index = build_guid_index(root)
    base_by_class = build_base_by_class(guid_index)
    # ExtraLife's component body has duration: 0.01, canBeSpawned: 0.
    prefab = root / "Assets" / "Prefabs" / "ExtraLife.prefab"
    comp = read_prefab_component(prefab, FID_EXTRALIFE)
    assert comp is not None
    types = field_types_for_class("ExtraLife", base_by_class, guid_index)
    ctor, post = split_component_fields(comp, types, guid_index)
    assert ctor["canBeSpawned"] is False        # 0 -> False, not numeric 0
    assert ctor["duration"] == 0.01
    assert post == {}

    # CoinMagnet has an AssetReference field -> resolved into post_fields.
    cm_prefab = root / "Assets" / "Prefabs" / "CoinMagnet.prefab"
    cm = read_prefab_component(cm_prefab, FID_COINMAGNET)
    assert cm is not None
    cm_types = field_types_for_class("CoinMagnet", base_by_class, guid_index)
    cm_ctor, cm_post = split_component_fields(cm, cm_types, guid_index)
    assert cm_ctor["canBeSpawned"] is True       # 1 -> True
    assert "ActivatedParticleReference" not in cm_ctor
    assert isinstance(
        cm_post["ActivatedParticleReference"], str,
    ) and cm_post["ActivatedParticleReference"]


def test_bool_field_not_coerced_without_declared_type(tmp_path):
    """A field with NO resolvable declared type is left VERBATIM (no guess) — the
    coercion is keyed on the C# type, not the value shape."""
    root = _build_trash_dash_like(tmp_path)
    guid_index = build_guid_index(root)
    # An empty type map: nothing is known to be bool, so 0/1 stay numeric.
    comp = {"someFlag": 0, "duration": 2.0}
    ctor, post = split_component_fields(comp, {}, guid_index)
    assert ctor["someFlag"] == 0                 # left verbatim, not coerced
    assert ctor["duration"] == 2.0


# A class whose serialized fields use the idiomatic ``[SerializeField]`` form
# with NO access modifier (defaults to private but IS serialized). Pre-fix the
# type reader required an explicit access modifier, so these matched nothing and
# a serialized ``0``/``1`` stayed numeric (an inverted bool gate). Also carries
# exclusions that must NOT be typed as coercible fields: ``const``, an
# auto-property, an expression-bodied property, and a method.
_SERIALIZEFIELD_CS = """\
    public class SerializedItem : MonoBehaviour
    {
        [SerializeField] bool canBeSpawned;
        [SerializeField] private bool privateFlag;
        [SerializeField] protected bool protectedFlag;
        public bool publicFlag;
        const bool ALWAYS = true;
        static bool shared;
        public bool active { get; set; }
        public bool IsReady => publicFlag;
        public bool Compute() { return true; }
    }
"""

G_SERIALIZED_CS = _g("serialized_cs")


def test_field_types_reads_serializefield_without_access_modifier(tmp_path):
    """``[SerializeField] bool canBeSpawned;`` (NO access modifier) is typed
    ``bool`` — the idiomatic Unity serialized form. The access-modifier and
    ``[SerializeField]`` forms BOTH type-match; the public form keeps working."""
    root = tmp_path / "proj"
    _cs(root, "Scripts/SerializedItem.cs", G_SERIALIZED_CS, _SERIALIZEFIELD_CS)
    guid_index = build_guid_index(root)
    base_by_class = build_base_by_class(guid_index)
    types = field_types_for_class("SerializedItem", base_by_class, guid_index)

    # All four serialized forms (3x SerializeField + 1x public) type as bool.
    assert types["canBeSpawned"] == "bool"   # [SerializeField], no access mod
    assert types["privateFlag"] == "bool"    # [SerializeField] private
    assert types["protectedFlag"] == "bool"  # [SerializeField] protected
    assert types["publicFlag"] == "bool"     # plain public (still works)

    # const / static / auto-property / expression-bodied property / method are
    # NOT coercible fields and must not appear.
    for non_field in ("ALWAYS", "shared", "active", "IsReady", "Compute"):
        assert non_field not in types


def test_serializefield_bool_coerces_zero_to_false(tmp_path):
    """End-to-end: a serialized ``0`` on a ``[SerializeField] bool`` field coerces
    to Python ``False`` (NOT numeric 0, which Luau treats as truthy). Pre-fix the
    field had no resolvable type, so the ``0`` stayed numeric."""
    root = tmp_path / "proj"
    _cs(root, "Scripts/SerializedItem.cs", G_SERIALIZED_CS, _SERIALIZEFIELD_CS)
    guid_index = build_guid_index(root)
    base_by_class = build_base_by_class(guid_index)
    types = field_types_for_class("SerializedItem", base_by_class, guid_index)

    comp = {"canBeSpawned": 0, "publicFlag": 1}
    ctor, post = split_component_fields(comp, types, guid_index)
    assert ctor["canBeSpawned"] is False     # 0 -> False, not numeric 0
    assert ctor["publicFlag"] is True        # 1 -> True


# --------------------------------------------------------------------------- #
# P2 — colliding subclass stem fails closed (element dropped)
# --------------------------------------------------------------------------- #

def test_colliding_module_path_drops_element(tmp_path):
    """A ``module_path_for_stem`` that returns None (collision / pruned) DROPS the
    element + warns — never leaves a string. Here CoinMagnet collides (None) and
    ExtraLife resolves, so only ExtraLife survives."""
    root = _build_trash_dash_like(tmp_path)
    guid_index = build_guid_index(root)
    base_by_class = build_base_by_class(guid_index)
    body = _asset_body(root, "Prefabs/Consumables.asset")

    def resolver(stem: str) -> str | None:
        return None if stem == "CoinMagnet" else f"ServerStorage.{stem}"

    seed = resolve_db_seed(
        db_module_path="ServerStorage.ConsumableDatabase",
        db_cs_source=_DB_CS_DRAINS_OBJECTS,
        asset_body=body,
        guid_index=guid_index,
        base_by_class=base_by_class,
        module_path_for_stem=resolver,
    )
    assert seed is not None
    assert [e["class_stem"] for e in seed["elements"]] == ["ExtraLife"]


# --------------------------------------------------------------------------- #
# Plan encoder renders the structured fields as TABLES, not quoted strings
# --------------------------------------------------------------------------- #

def test_emitted_plan_renders_fields_as_tables_not_strings(tmp_path):
    """The plan encoder (``_plan_to_luau``) renders ``ctor_config``/``post_fields``
    as nested Luau TABLES and a ``bool``-typed 0 as the literal ``false`` (not the
    quoted string ``"0"`` and not numeric ``0``). No ``loadstring`` is needed."""
    from converter.autogen import generate_scene_runtime_plan_module

    root = _build_trash_dash_like(tmp_path)
    guid_index = build_guid_index(root)
    base_by_class = build_base_by_class(guid_index)
    body = _asset_body(root, "Prefabs/Consumables.asset")
    seed = resolve_db_seed(
        db_module_path="ServerStorage.ConsumableDatabase",
        db_cs_source=_DB_CS_DRAINS_OBJECTS,
        asset_body=body,
        guid_index=guid_index,
        base_by_class=base_by_class,
        module_path_for_stem=_module_path_for_stem,
    )
    assert seed is not None
    source = generate_scene_runtime_plan_module(
        {"consumable_db_seeds": [seed]},
    ).source
    # ctor_config is a TABLE literal (``ctor_config = {``), not a quoted string
    # (``ctor_config = "``).
    assert "ctor_config = {" in source
    assert 'ctor_config = "' not in source
    assert "post_fields = {" in source
    # The bool-typed 0 (ExtraLife.canBeSpawned) renders as the literal ``false``.
    assert "canBeSpawned = false" in source
    # And the truthy one renders as ``true`` (CoinMagnet).
    assert "canBeSpawned = true" in source
    # No pre-rendered fields_literal string survives.
    assert "fields_literal" not in source


# --------------------------------------------------------------------------- #
# convert_asset_file still emits the asset (the resolver does not replace it)
# --------------------------------------------------------------------------- #

def test_asset_still_converts(tmp_path):
    root = _build_trash_dash_like(tmp_path)
    guid_index = build_guid_index(root)
    asset = root / "Assets" / "Prefabs" / "Consumables.asset"
    converted = convert_asset_file(asset, guid_index)
    assert converted is not None
    assert converted.asset_name == "Consumables"
