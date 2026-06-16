"""roster_assembly.py -- Pure assembly of Addressables label rosters.

Phase 1 (producer) of the Addressables Unit-4 roster surface. Builds the
deterministic ``list[RbxRoster]`` channel from ``addressables.by_label`` so both
emit paths (rbxlx_writer + luau_place_builder) can materialize, per member, a
second clonable instance under a dedicated ReplicatedStorage container —
CollectionService-tagged with the label and carrying ``characterName`` as an
attribute (the surface ``CharacterDatabase.LoadDatabase`` reads).

Business logic is PURE: ``assemble_rosters`` returns a value and mutates nothing.
The trigger is structural (every label in ``by_label`` with >=1 resolved,
emitted prefab); there is NO game-specific label literal anywhere.
"""

from __future__ import annotations

import logging

from core.roblox_types import RbxAttrValue, RbxRoster, RbxRosterMember

log = logging.getLogger(__name__)

# Marker attribute carried on a roster member's ROOT part only (never propagated
# to descendants). Both emit paths union it into the CollectionService tag set
# for the root and STRIP it from the emitted attributes — it is a marker, not a
# real Roblox attribute. Keyed here so the producer and both consumers agree.
ROSTER_TAG_MARKER = "_RosterTag"

# Default base name for the dedicated roster container Folder in
# ReplicatedStorage. Disambiguated against reserved RS names by
# ``resolve_roster_container_name``. The container name is NOT the discovery key
# (the CollectionService tag is), so it is free to be suffix-disambiguated and
# is deliberately a fixed generic literal — never the label/group.
DEFAULT_ROSTER_CONTAINER = "RosterMembers"


def resolve_roster_container_name(reserved_names: set[str]) -> str:
    """Return a roster-container name that does not collide with *reserved_names*.

    Pure. Starts from ``DEFAULT_ROSTER_CONTAINER`` and suffix-disambiguates
    (``RosterMembers_1``, ``RosterMembers_2``, ...) until clear of every reserved
    ReplicatedStorage name (Templates / a ModuleScript / a template / a
    ``scriptable_objects/<Group>.luau`` module). The caller adds the returned
    name to its own reserved set so no auto-created RemoteEvent shadows it.
    """
    base = DEFAULT_ROSTER_CONTAINER
    if base not in reserved_names:
        return base
    i = 1
    while f"{base}_{i}" in reserved_names:
        i += 1
    return f"{base}_{i}"


def assemble_rosters(
    by_label: dict[str, list[str]],
    resolved_template_names: dict[str, str],
    emitted_template_names: set[str],
    character_names: dict[str, str],
) -> list[RbxRoster]:
    """Build the deterministic roster surface from ``by_label``. PURE.

    Args:
        by_label: ``PrefabAddressables.by_label`` (label -> [prefab_id]). Built
            via ``setdefault().append()`` with NO dedup upstream, so a prefab
            under two AddressableEntry rows yields a duplicate prefab_id (E5).
        resolved_template_names: prefab_id -> ReplicatedStorage.Templates child
            name (keyed on prefab_id per D3, NOT name/address).
        emitted_template_names: template names actually present in
            ``place.replicated_templates`` (parity guard, E3).
        character_names: prefab_id -> characterName (pre-resolved by the
            field-presence selector). Omitted prefab_ids carry no attribute.

    Returns:
        One ``RbxRoster`` per label with >=1 surviving member. A label whose
        members all fail the emitted-template check yields NO roster (E2).

    Edge behavior:
        - E1: empty ``by_label`` -> ``[]``.
        - E3: prefab_id whose template was not emitted -> member skipped.
        - E5: duplicate (label, prefab_id) -> member emitted ONCE (first-seen
          order preserved); members then sorted by template_name for stable
          output (idempotency).
        - E9: every value read is narrowed with ``isinstance(v, str)``;
          non-str -> skip-with-warning, never coerce, never Any. A non-str
          characterName is omitted but the member is still tagged + clonable.
    """
    rosters: list[RbxRoster] = []

    for label, prefab_ids in by_label.items():
        # E9: label must be a str to be a CollectionService tag.
        if not isinstance(label, str):
            log.warning(
                "[roster_assembly] skipping non-str label %r (type %s)",
                label, type(label).__name__,
            )
            continue
        if not isinstance(prefab_ids, (list, tuple)):
            log.warning(
                "[roster_assembly] skipping label %r: prefab_ids is %s, not a list",
                label, type(prefab_ids).__name__,
            )
            continue

        members: list[RbxRosterMember] = []
        seen_prefab_ids: set[str] = set()
        for prefab_id in prefab_ids:
            # E9: prefab_id keys the template/characterName lookups.
            if not isinstance(prefab_id, str):
                log.warning(
                    "[roster_assembly] label %r: skipping non-str prefab_id %r",
                    label, prefab_id,
                )
                continue
            # E5: dedup on (label, prefab_id), preserving first-seen order.
            if prefab_id in seen_prefab_ids:
                continue
            seen_prefab_ids.add(prefab_id)

            template_name = resolved_template_names.get(prefab_id)
            # E9: template_name from persisted JSON must be a str.
            if not isinstance(template_name, str):
                if template_name is not None:
                    log.warning(
                        "[roster_assembly] label %r prefab_id %s: non-str "
                        "template_name %r — skipping member",
                        label, prefab_id, template_name,
                    )
                continue
            # E3: only emit a member whose template was actually emitted.
            if template_name not in emitted_template_names:
                log.warning(
                    "[roster_assembly] label %r prefab_id %s: template %r not in "
                    "emitted templates — skipping member",
                    label, prefab_id, template_name,
                )
                continue

            attributes: dict[str, RbxAttrValue] = {}
            char_name = character_names.get(prefab_id)
            if char_name is not None:
                # E9: fail-closed — only a real str becomes the attribute.
                if isinstance(char_name, str):
                    attributes["characterName"] = char_name
                else:
                    log.warning(
                        "[roster_assembly] label %r prefab_id %s: non-str "
                        "characterName %r — omitting attribute (member still "
                        "tagged + clonable)",
                        label, prefab_id, char_name,
                    )

            members.append(
                RbxRosterMember(
                    template_name=template_name,
                    tag=label,
                    attributes=attributes,
                )
            )

        # E2: a label with zero surviving members yields NO roster.
        if not members:
            continue

        # Stable output: sort by template_name (idempotency). Ties keep
        # first-seen order via Python's stable sort.
        members.sort(key=lambda m: m.template_name)
        rosters.append(RbxRoster(label=label, members=members))

    return rosters
