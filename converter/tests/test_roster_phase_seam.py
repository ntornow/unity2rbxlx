"""Whole-feature (Phase 1 producer <-> Phase 2 consumer) seam test.

The per-phase tests each exercise ONE half: ``test_roster_assembly`` /
``test_rbxlx_writer`` cover the producer (assemble -> emit a tagged, attributed
roster surface), ``test_roster_consumer_lowering`` covers the consumer (locate +
re-lower the transpiled DB module to read that surface). Neither drives BOTH on
one synthetic plan, so a divergence in the shared contract — the
CollectionService TAG and the identity-ATTRIBUTE key — would slip past both.

This module builds ONE synthetic ``by_label`` plan, runs the real producer
(``assemble_rosters`` -> ``rbxlx_writer``) AND the real consumer
(``find_roster_consumers`` -> ``lower_roster_consumers``) against it, then
asserts the bytes one side EMITS are exactly the bytes the other side READS:

  * the tag the producer writes onto each member root == the literal the
    consumer's ``CollectionService:GetTagged(...)`` reads;
  * the identity attribute key the producer serializes (``characterName``) ==
    the key the consumer's ``op:GetAttribute(...)`` reads.

No game literal: the label and the index key flow from the same ``by_label`` key
and the same C# ``LoadAssetsAsync`` fact both halves derive from.
"""
from __future__ import annotations

import base64
import struct
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.roster_assembly import (  # noqa: E402
    DEFAULT_ROSTER_CONTAINER,
    assemble_rosters,
)
from converter.roster_consumer_lowering import (  # noqa: E402
    RosterConsumerFact,
    find_roster_consumers,
    lower_roster_consumers,
)
from core.roblox_types import RbxPlace  # noqa: E402
from tests._roster_fixtures import (  # noqa: E402
    CHAR_NAME,
    LABEL,
    TEMPLATE_NAME,
    make_model_rooted_template,
)


# The C# the consumer is transpiled FROM — the deterministic upstream fact both
# halves key on. The label literal ``LABEL`` is also the ``by_label`` key the
# producer tags with, so a mismatch here would surface as a dead seam.
_ROSTER_CS = (
    "using UnityEngine; using UnityEngine.AddressableAssets;\n"
    "public class CharacterDatabase {\n"
    "  static System.Collections.Generic.Dictionary<string,Character> m_Dict;\n"
    "  public static System.Collections.IEnumerator LoadDatabase() {\n"
    f'    yield return Addressables.LoadAssetsAsync<GameObject>("{LABEL}", op => {{\n'
    "      Character c = op.GetComponent<Character>();\n"
    "      if (c != null) m_Dict.Add(c.characterName, c);\n"
    "    });\n"
    "  }\n"
    "}\n"
)

# A transpiled DB body in the GetTagged drift shape (any of the 3 shapes lowers
# to the same canonical region; one is enough for the seam check).
_ROSTER_LUAU = (
    "local CharacterDatabase = {}\n"
    "\n"
    "local m_Dict = nil\n"
    "local m_Loaded = false\n"
    "\n"
    "function CharacterDatabase.dictionary()\n"
    "\treturn m_Dict\n"
    "end\n"
    "\n"
    "function CharacterDatabase.loaded()\n"
    "\treturn m_Loaded\n"
    "end\n"
    "\n"
    "function CharacterDatabase.LoadDatabase()\n"
    "\tm_Dict = {}\n"
    "\tlocal CollectionService = game:GetService('CollectionService')\n"
    f'\tfor _, op in CollectionService:GetTagged("{LABEL}") do\n'
    "\t\tm_Dict[op.Name] = op\n"
    "\tend\n"
    "\tm_Loaded = true\n"
    "end\n"
    "\n"
    "function CharacterDatabase.GetCharacter(t)\n"
    "\treturn m_Dict[t]\n"
    "end\n"
    "\n"
    "return CharacterDatabase\n"
)


@dataclass
class _Script:
    source_path: str
    luau_source: str
    csharp_source: str
    roster_binding: object = None


def _decode_tags(binstr_text: str) -> set[str]:
    raw = base64.b64decode(binstr_text)
    return set(raw.decode("utf-8").split("\0")) if raw else set()


def _decode_attributes(binstr_text: str) -> dict[str, object]:
    raw = base64.b64decode(binstr_text)
    out: dict[str, object] = {}
    off = 0
    (count,) = struct.unpack_from("<I", raw, off); off += 4
    for _ in range(count):
        (klen,) = struct.unpack_from("<I", raw, off); off += 4
        key = raw[off:off + klen].decode("utf-8"); off += klen
        tid = raw[off]; off += 1
        if tid == 0x02:  # String
            (vlen,) = struct.unpack_from("<I", raw, off); off += 4
            val: object = raw[off:off + vlen].decode("utf-8"); off += vlen
        elif tid == 0x03:  # Bool
            val = bool(raw[off]); off += 1
        elif tid == 0x06:  # Float64
            (val,) = struct.unpack_from("<d", raw, off); off += 8
        else:
            raise AssertionError(f"unexpected attr type {tid}")
        out[key] = val
    return out


def _emitted_member_root(root: ET.Element) -> ET.Element:
    """The single roster member-root Item under the dedicated roster container
    Folder (the ``RosterMembers``-prefixed one, NOT the Templates folder)."""
    rs = next((i for i in root.iter("Item")
               if i.get("class") == "ReplicatedStorage"), None)
    assert rs is not None
    for item in rs.findall("Item"):
        if item.get("class") != "Folder":
            continue
        props = item.find("Properties")
        nm = None
        if props is not None:
            for s in props.findall("string"):
                if s.get("name") == "Name":
                    nm = s.text
        if not (nm and nm.startswith(DEFAULT_ROSTER_CONTAINER)):
            continue
        members = item.findall("Item")
        assert members, "roster container Folder has no member"
        return members[0]
    raise AssertionError("no dedicated roster container Folder emitted")


def _binstrings(item: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    props = item.find("Properties")
    if props is not None:
        for b in props.findall("BinaryString"):
            out[b.get("name")] = b.text or ""
    return out


def _build_place_from_plan(by_label: dict[str, list[str]]) -> RbxPlace:
    """Run the REAL producer: assemble_rosters off a by_label plan, then attach
    the rosters + the emitted Templates child to an RbxPlace."""
    prefab_id = "pid-cat"
    template = make_model_rooted_template(template_name=TEMPLATE_NAME)
    rosters = assemble_rosters(
        by_label=by_label,
        resolved_template_names={prefab_id: TEMPLATE_NAME},
        emitted_template_names={TEMPLATE_NAME},
        character_names={prefab_id: CHAR_NAME},
    )
    place = RbxPlace(replicated_templates=[template])
    place.rosters = rosters
    return place


def test_producer_tag_and_attr_key_match_consumer_reads(tmp_path):
    """The seam: the tag + attribute key the producer EMITS are exactly the
    literal + key the re-lowered consumer READS."""
    from roblox.rbxlx_writer import write_rbxlx

    by_label = {LABEL: ["pid-cat"]}

    # --- Phase 1 (producer): assemble + emit, then read the emitted bytes. ---
    place = _build_place_from_plan(by_label)
    out = tmp_path / "roster.rbxlx"
    write_rbxlx(place, out)
    member = _emitted_member_root(ET.parse(out).getroot())
    bins = _binstrings(member)
    emitted_tags = _decode_tags(bins["Tags"])
    emitted_attrs = _decode_attributes(bins["AttributesSerialize"])

    # --- Phase 2 (consumer): identify + re-lower off the SAME by_label. ---
    facts = find_roster_consumers({"CharacterDatabase.cs": _ROSTER_CS}, by_label)
    assert facts, "consumer not identified from the shared by_label fact"
    fact = facts["CharacterDatabase.cs"]
    script = _Script(
        source_path="CharacterDatabase.cs",
        luau_source=_ROSTER_LUAU,
        csharp_source=_ROSTER_CS,
    )
    assert lower_roster_consumers([script], facts) == 1
    lowered = script.luau_source

    # SEAM 1: the tag the producer wrote == the literal the consumer reads.
    assert fact.label in emitted_tags, (
        f"producer emitted tags {emitted_tags}, consumer keys on {fact.label!r}"
    )
    assert f'GetTagged("{fact.label}")' in lowered, (
        "re-lowered consumer must GetTagged the same label the producer tagged"
    )

    # SEAM 2: the identity attribute key the producer serialized == the key the
    # consumer's GetAttribute reads.
    assert fact.index_key == "characterName"
    assert fact.index_key in emitted_attrs, (
        f"producer serialized attrs {set(emitted_attrs)}, consumer reads "
        f"{fact.index_key!r}"
    )
    assert emitted_attrs[fact.index_key] == CHAR_NAME
    assert f'GetAttribute("{fact.index_key}")' in lowered, (
        "re-lowered consumer must read the same attribute key the producer wrote"
    )

    # The re-lowering stamped the carrier (dead-module exemption contract).
    assert script.roster_binding == {
        "label": LABEL, "receiver": "CharacterDatabase", "lowered": True,
    }


def test_consumer_abstains_when_producer_emits_no_roster():
    """No by_label surface -> producer emits nothing AND the consumer abstains;
    the seam is consistent on the empty path (no half-lowered orphan)."""
    place = _build_place_from_plan({})
    assert place.rosters == []
    facts = find_roster_consumers({"CharacterDatabase.cs": _ROSTER_CS}, {})
    assert facts == {}
