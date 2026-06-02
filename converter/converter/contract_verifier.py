"""Phase 3 contract verifier.

Build-time enforcement layer that proves every emitted artifact obeys the
topology authority. See the design doc §"Phase 3 — Contract verifier (new
``contract_verifier.py``)" in
``docs/design/scene-runtime-architecture-ir.md``.

**Shadow mode** — every check records warnings only; the per-check flip to
fail-closed lands in slice 4. Checks:

  * smoke (slice 0) — fires iff the topology artifact lacks a ``modules`` key,
    proving the data path reaches the verifier.
  * **check A — consumer compliance (slice 1, this file).** Reconciles each
    module's INDEPENDENT ``domain`` against its emitted (``script_type``,
    container-family of ``parent_path``). This is NOT a "placement == topology"
    comparison: the artifact's ``container``/``module_path`` is mirrored from
    ``RbxScript.parent_path`` (``module_domain.py:1666``), so that would be
    tautological. ``domain`` is the only independent signal (source-derived,
    never reads ``parent_path``/``script_type``), so a domain⟂placement
    mismatch is the real bug (e.g. a server-domain module emitted as a
    LocalScript that never runs server-side). Modules only — animation_drivers
    are deferred (their domain↔script_class is consistent by construction).
  * check B (component availability / GetComponent) — slice 2.
  * check C (cross-domain attribute access) — slice 3.

The module is deliberately import-light (no heavy pipeline imports) so it
stays unit-testable in isolation. ``RbxScript`` and ``TopologyArtifact`` are
imported from their real modules for concrete typing — no ``Any``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from converter.scene_runtime_topology.build_topology import TopologyArtifact
from core.roblox_types import RbxScript


@dataclass(frozen=True)
class ContractViolation:
    """A single contract violation surfaced by the verifier.

    ``identity`` is a STABLE dedup key (e.g. ``f"{check}:{script}:{key}"``).
    The pipeline stash dedupes on it so a ``materialize_and_classify`` resume
    replay does not double-count the same violation.
    """

    check: str
    """Which check produced this: "smoke" (slice 0), then "consumer_compliance"
    / "component_availability" / "cross_domain_attribute" in slices 1-3."""

    severity: str
    """"warning" in slice 0 — every check ships shadow first; the per-check
    flip to fail-closed lands in slice 4."""

    script: str
    """Source path or script name; "" if the violation is global (no single
    owning script)."""

    detail: str
    """Human-readable description of what is wrong."""

    identity: str
    """Stable dedup key. Two violations with the same identity are the same
    violation across runs/resumes."""


@dataclass
class ContractVerifierResult:
    """The result of a single ``verify_contract`` call."""

    violations: list[ContractViolation] = field(default_factory=list)

    def total(self) -> int:
        """Total number of violations."""
        return len(self.violations)

    def counts_by_check(self) -> dict[str, int]:
        """Violation counts grouped by ``check`` name."""
        counts: dict[str, int] = {}
        for v in self.violations:
            counts[v.check] = counts.get(v.check, 0) + 1
        return counts


def verify_contract(
    topology: TopologyArtifact,
    scripts: list[RbxScript],
    *,
    mode: str = "shadow",
) -> ContractVerifierResult:
    """Run the contract verifier over the topology artifact + final scripts.

    Slice 0 runs NO real A/B/C checks. It runs ONE trivial **smoke check**:
    emit a ``ContractViolation(check="smoke", ...)`` iff ``topology`` lacks a
    ``"modules"`` key (i.e. the topology artifact never reached the verifier
    or is empty). When the topology carries modules → zero violations. This
    proves the data path is wired end-to-end.

    ``mode`` is "shadow" and is threaded through for the eventual per-check
    fail-closed flip (slice 4); it does not change behavior yet.
    """
    violations: list[ContractViolation] = []

    # Smoke check: the topology artifact must carry a populated ``modules``
    # block by the time the verifier runs (it is populated inside
    # ``_build_and_apply_topology`` before the hook fires). A missing/empty
    # ``modules`` key means the data never reached us — surface it so the
    # wiring is provable from a violation, not from silence.
    if not topology.get("modules"):
        violations.append(
            ContractViolation(
                check="smoke",
                severity="warning",
                script="",
                detail=(
                    "topology artifact reached the verifier without a "
                    "populated 'modules' block"
                ),
                identity="smoke:missing-modules",
            )
        )

    # Check A — consumer compliance (domain⟂placement consistency).
    violations.extend(_check_consumer_compliance(topology, scripts))

    # Check B — component availability (GetComponent reachability).
    violations.extend(_check_component_availability(topology, scripts))

    # Check C — cross-domain attribute access (Class-1 component-ref edges).
    violations.extend(_check_cross_domain_attribute(topology, scripts))

    return ContractVerifierResult(violations=violations)


# ---------------------------------------------------------------------------
# Check A — consumer compliance (domain ⟂ placement consistency)
# ---------------------------------------------------------------------------

# Container families. The check fires only on a CONFIDENT family mismatch; an
# unrecognized ``parent_path`` (e.g. Workspace, a nested model path) maps to
# "other" and is never flagged.
_SERVER_ONLY_CONTAINERS = frozenset({"ServerScriptService", "ServerStorage"})
_NEUTRAL_CONTAINERS = frozenset({"ReplicatedStorage"})


def _container_family(parent_path: str) -> str:
    """Classify a final ``parent_path`` into "server" | "client" | "neutral"
    | "other".

    ReplicatedStorage is NEUTRAL — requireable by either side, so a module of
    any domain may legitimately live there (the doc's §"storage ≠ domain"
    cases 1-3, 6). Client-only containers are matched by substring so the
    dotted ``StarterPlayer.StarterPlayerScripts`` /
    ``StarterPlayer.StarterCharacterScripts`` forms and a bare
    ``ReplicatedFirst`` all classify as client.
    """
    # Module ``parent_path`` values are always TOP-LEVEL container strings
    # (the storage classifier + reachability path only ever emit
    # "ServerScriptService"/"ServerStorage"/"ReplicatedStorage"/"ReplicatedFirst"
    # /"StarterPlayer.Starter*Scripts"). A dotted "ServerStorage.Foo" would fall
    # through to "other" and escape the check — that is intentional and
    # currently unreachable for modules; do NOT "fix" it into a substring match
    # on the server set (that would re-introduce false positives on names that
    # merely contain a container token).
    if parent_path in _SERVER_ONLY_CONTAINERS:
        return "server"
    if parent_path in _NEUTRAL_CONTAINERS:
        return "neutral"
    if (
        "StarterPlayer" in parent_path
        or "StarterCharacter" in parent_path
        or parent_path == "ReplicatedFirst"
    ):
        return "client"
    return "other"


def _join_name(module: object) -> str:
    """The emitted-script name to join a topology module row to its
    ``RbxScript``.

    Prefer the last segment of ``module_path`` (built as
    ``f"{container}.{script.name}"`` in ``_stamp_container_and_path``, so its
    tail IS ``script.name`` by construction — robust even when the C# class
    name differs from the file stem). Fall back to ``stem`` when no
    ``module_path`` was stamped.
    """
    if not isinstance(module, dict):
        return ""
    module_path = str(module.get("module_path") or "")
    if module_path:
        return module_path.rsplit(".", 1)[-1]
    # Stem fallback: ``module_path`` is unstamped only when the module's
    # ``RbxScript.parent_path`` was empty (``_stamp_container_and_path``
    # requires a truthy container). Then ``stem`` may differ from the emitted
    # ``RbxScript.name`` (C# class name ≠ file stem), risking a mis-join — but an
    # empty ``parent_path`` also makes ``_container_family("") == "other"``, so
    # only the container-independent type rules could fire. Low-risk edge.
    return str(module.get("stem") or "")


def _domain_placement_violation(
    sid: str,
    name: str,
    domain: str,
    script_type: str,
    parent_path: str,
    family: str,
) -> ContractViolation | None:
    """Apply the domain⟂placement consistency table. Returns a warning-severity
    ``ContractViolation`` on a mismatch, else ``None``.

    Does NOT duplicate the storage classifier's hard ``ConstraintViolation``s
    (LocalScript-in-ServerScriptService, ModuleScript-in-ReplicatedFirst,
    ``storage_classifier.py:898``) — those abort before the verifier runs.
    """
    def _mk(reason_key: str, detail: str) -> ContractViolation:
        return ContractViolation(
            check="consumer_compliance",
            severity="warning",
            script=name,
            detail=detail,
            identity=f"consumer_compliance:{sid}:{reason_key}",
        )

    if domain == "server":
        if script_type == "LocalScript":
            return _mk(
                "server-localscript",
                f"server-domain module {name!r} emitted as a LocalScript "
                f"— a LocalScript never runs on the server",
            )
        if family == "client":
            return _mk(
                "server-in-client-container",
                f"server-domain module {name!r} placed in client-only "
                f"container {parent_path!r}",
            )
    elif domain == "client":
        if script_type == "Script" and family == "server":
            return _mk(
                "client-script-in-server-container",
                f"client-domain module {name!r} emitted as an auto-run Script "
                f"in server-only container {parent_path!r}",
            )
    elif domain == "helper":
        if script_type in ("Script", "LocalScript"):
            return _mk(
                "helper-autorun",
                f"helper module {name!r} emitted as auto-run {script_type} "
                f"— helpers are require-only ModuleScripts",
            )
        # Container is intentionally NOT checked for helpers: a reachability
        # hoist can legitimately place a client-reachable helper in a
        # client-only container.
    elif domain == "excluded":
        return _mk(
            "excluded-but-emitted",
            f"module {name!r} is domain=excluded but still emitted a script "
            f"({script_type} in {parent_path!r})",
        )
    return None


def _check_consumer_compliance(
    topology: TopologyArtifact,
    scripts: list[RbxScript],
) -> list[ContractViolation]:
    """Reconcile each module's independent ``domain`` against its emitted
    placement. See the module docstring + design doc §Phase 3 check #1."""
    violations: list[ContractViolation] = []
    modules = topology.get("modules") or {}
    if not modules:
        return violations

    scripts_by_name: dict[str, list[RbxScript]] = {}
    for s in scripts:
        scripts_by_name.setdefault(s.name, []).append(s)

    for sid, module in modules.items():
        if not isinstance(module, dict):
            continue
        domain = str(module.get("domain") or "")
        if domain not in ("client", "server", "helper", "excluded"):
            # No domain / unknown value — nothing independent to reconcile.
            continue
        name = _join_name(module)
        if not name:
            continue
        matches = scripts_by_name.get(name, [])
        if len(matches) != 1:
            # DQ4(a): an ambiguous / missing join is UNVERIFIABLE. Record it
            # (no silent gap) but do NOT raise a real violation — stem/name
            # collisions are already surfaced by the storage classifier, and a
            # verifier should not double-fail on a known-degraded join.
            violations.append(
                ContractViolation(
                    check="consumer_compliance",
                    severity="info",
                    script=name,
                    detail=(
                        f"unverifiable: module {sid!r} (name {name!r}) joined "
                        f"to {len(matches)} emitted script(s); placement not "
                        f"checked"
                    ),
                    identity=f"consumer_compliance:{sid}:unverifiable",
                )
            )
            continue
        script = matches[0]
        parent_path = script.parent_path or ""
        violation = _domain_placement_violation(
            sid,
            name,
            domain,
            script.script_type,
            parent_path,
            _container_family(parent_path),
        )
        if violation is not None:
            violations.append(violation)

    return violations


def violation_to_dict(violation: ContractViolation) -> dict[str, str]:
    """Serialize a ``ContractViolation`` to the plain JSON-able row the
    pipeline stashes on ``ctx.scene_runtime["contract_check_violations"]``."""
    return {
        "check": violation.check,
        "severity": violation.severity,
        "script": violation.script,
        "detail": violation.detail,
        "identity": violation.identity,
    }


def stash_violations(
    existing_rows: list[dict[str, str]],
    result: ContractVerifierResult,
) -> int:
    """Append ``result``'s violations to ``existing_rows`` IN PLACE, deduped
    by ``identity`` against rows already present.

    Mirrors the membership-gated ``contract_fail_closed`` plumbing
    (``pipeline.py:2041``) so a ``materialize_and_classify`` resume replay
    does not double-count. Reads existing identities into a set first, then
    appends only genuinely-new rows.

    Returns the number of rows actually appended (0 on a pure replay).
    """
    seen: set[str] = {
        str(row.get("identity", "")) for row in existing_rows
    }
    appended = 0
    for v in result.violations:
        if v.identity in seen:
            continue
        seen.add(v.identity)
        existing_rows.append(violation_to_dict(v))
        appended += 1
    return appended


# ---------------------------------------------------------------------------
# Check B — component availability (GetComponent reachability)
# ---------------------------------------------------------------------------
#
# Generic mode emits the peer form ``self:GetComponent("X")``
# (code_transpiler.py:1329). At runtime (scene_runtime.luau:752-780) X resolves
# to: a peer converted-MonoBehaviour (by stem/scriptId) -> else
# ``_UNITY_TO_ROBLOX_CLASS[X]`` -> else ``findFirstChildWhichIsA(X)``. An X that
# is none of those returns nil, so any subsequent use/method-call errors. Check
# B flags those unreachable sites.
#
# SCOPE (slice 2): reachability only. Method-validity (X maps to a Roblox class
# that lacks the called method — the CharacterController->BasePart->:Move()
# anecdote) is DEFERRED: the repo has no Roblox class->method database, and the
# transpiler already routes CharacterController.Move/.SimpleMove/.isGrounded
# through a bridge (api_mappings API_CALL_MAP), so that anecdote is largely
# already handled. Documented gap, not silently dropped.
#
# COVERAGE: only STRING-LITERAL args are checked. A non-literal arg
# (``self:GetComponent(typeVar)``) cannot be resolved statically and is skipped
# — so a future fail-closed flip of check B covers literal-arg sites only.

# Matches ``:GetComponent("X")`` with a string-literal arg ONLY. Deliberately
# does NOT match:
#   * the plural ``GetComponents`` (list semantics, different bug class) — a
#     literal "(" must follow "GetComponent", and "GetComponents(" has an "s";
#   * ``GetComponentInChildren`` / ``GetComponentInParent`` — the transpiler
#     lowers those to a GetDescendants()/GetAncestors() hierarchy WALK
#     (code_transpiler.py:1330), not a ``_UNITY_TO_ROBLOX_CLASS`` resolution, so
#     check B's reachability model does not apply to them (review P3).
_GETCOMPONENT_RE = re.compile(
    r""":GetComponent\s*\(\s*['"]([A-Za-z_]\w*)['"]""",
)


def _strip_luau_comments(source: str) -> str:
    """Remove Luau comments so the GetComponent scan doesn't fire on a
    commented-out call (review P2). Strips ``--[[ ... ]]`` block comments first,
    then ``-- ...`` to end-of-line. Imperfect inside string literals (a ``--``
    in a string truncates the line), but that only DROPS a would-be match —
    never creates one — which is the safe direction (we never want to flag a
    GetComponent that lives inside a string anyway)."""
    no_block = re.sub(r"--\[\[.*?\]\]", "", source, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", no_block)

# Roblox classes converted (or hand-edited) code may legitimately pass to
# GetComponent directly — runtime ``findFirstChildWhichIsA`` resolves them, so
# they must NOT be flagged. The runtime map's VALUES already cover the
# transpiler's own outputs; this allowlist guards the fail-closed flip against
# legitimate direct-Roblox-class passes the values set happens to miss. Biased
# to ABSTAIN: an over-broad allowlist only suppresses warnings (fails open),
# which is the safe direction for a shadow→fail-closed check. Never flag a name
# the runtime can resolve.
_ROBLOX_CLASS_ALLOWLIST = frozenset({
    "Humanoid", "HumanoidRootPart", "Seat", "VehicleSeat",
    "ClickDetector", "ProximityPrompt", "Sound", "SoundGroup",
    "Camera", "BasePart", "MeshPart", "Part", "UnionOperation", "Model",
    "ParticleEmitter", "Beam", "Trail", "Light", "PointLight",
    "SpotLight", "SurfaceLight", "Attachment",
    "SurfaceGui", "BillboardGui", "ScreenGui", "Frame",
    "TextLabel", "TextButton", "TextBox", "ImageLabel", "ImageButton",
    "GuiButton", "Decal", "Texture", "Weld", "WeldConstraint", "Motor6D",
    "Highlight", "Folder", "Configuration",
})


@lru_cache(maxsize=1)
def _runtime_class_map() -> tuple[frozenset[str], frozenset[str]]:
    """Parse ``_UNITY_TO_ROBLOX_CLASS`` from ``runtime/scene_runtime.luau`` and
    return ``(keys, values)``.

    This is the single source of truth check B trusts (the locked decision:
    trust the RUNTIME map, which differs from Python ``TYPE_MAP`` — e.g.
    CharacterController -> "BasePart" here vs "Humanoid" there). Parsed, not
    duplicated, so the verifier and the runtime never drift. An EXHAUSTIVE
    guard test (``test_runtime_class_map_*``) pins the full parsed key/value
    set so a runtime-file refactor that drops/renames an entry fails loudly.
    Cached: the file never changes within a process.
    """
    path = Path(__file__).resolve().parent.parent / "runtime" / "scene_runtime.luau"
    text = path.read_text(encoding="utf-8")
    keys: set[str] = set()
    values: set[str] = set()
    # Block-bounded: the table body from ``= {`` to the first line that is a
    # bare ``}`` (the table's close). Avoids matching ``Ident = "Str"`` pairs
    # elsewhere in the file.
    block = re.search(
        r"local\s+_UNITY_TO_ROBLOX_CLASS[^=]*=\s*\{(.*?)\n\}",
        text,
        re.DOTALL,
    )
    if block is not None:
        for key, value in re.findall(r'(\w+)\s*=\s*"([^"]+)"', block.group(1)):
            keys.add(key)
            values.add(value)
    # Sentinel assigns outside the table literal:
    #   _UNITY_TO_ROBLOX_CLASS.Transform = _CLASS_TRANSFORM_SELF
    for key in re.findall(r"_UNITY_TO_ROBLOX_CLASS\.(\w+)\s*=", text):
        keys.add(key)
    sentinel = re.search(r'_CLASS_TRANSFORM_SELF\s*=\s*"([^"]+)"', text)
    if sentinel is not None:
        values.add(sentinel.group(1))
    return frozenset(keys), frozenset(values)


def _check_component_availability(
    topology: TopologyArtifact,
    scripts: list[RbxScript],
) -> list[ContractViolation]:
    """Flag ``GetComponent("X")`` sites whose ``X`` resolves to nil at runtime.
    See the section comment above + design doc §Phase 3 check #3."""
    keys, values = _runtime_class_map()

    # Peer converted-MonoBehaviours are reachable exactly as the runtime
    # resolves them: ``m.stem == name or m.scriptId == name``
    # (scene_runtime.luau:758). ``scriptId`` is the topology ``modules`` dict
    # KEY. (Review P1: an earlier draft used ``class_name``, which the runtime
    # never checks AND which ``TopologyModuleEntry`` never carries — a dead
    # clause. A peer whose stem ≠ its C# class name genuinely does NOT resolve
    # by class name at runtime, so flagging such a GetComponent is correct, not
    # a false positive.)
    peer: set[str] = set()
    modules = topology.get("modules") or {}
    for script_id, module in modules.items():
        if script_id:
            peer.add(str(script_id))
        if not isinstance(module, dict):
            continue
        stem = module.get("stem")
        if isinstance(stem, str) and stem:
            peer.add(stem)

    reachable = peer | set(keys) | set(values) | _ROBLOX_CLASS_ALLOWLIST

    violations: list[ContractViolation] = []
    seen: set[tuple[str, str]] = set()
    for script in scripts:
        source = _strip_luau_comments(script.source or "")
        for match in _GETCOMPONENT_RE.finditer(source):
            x = match.group(1)
            if x in reachable:
                continue
            key = (script.name, x)
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                ContractViolation(
                    check="component_availability",
                    severity="warning",
                    script=script.name,
                    detail=(
                        f"GetComponent({x!r}) resolves to nil — {x!r} is not a "
                        f"converted component, a mapped Unity type, or a known "
                        f"Roblox class; the result will error when used"
                    ),
                    identity=f"component_availability:{script.name}:{x}",
                )
            )
    return violations


# ---------------------------------------------------------------------------
# Check C — cross-domain attribute access (Class-1 component-ref edges)
# ---------------------------------------------------------------------------
#
# SCOPE (slice 3): the STRUCTURAL Class-1 check only. A literal cross-domain
# ``SetAttribute("X")`` writer + ``GetAttribute[ChangedSignal]("X")`` reader
# pair (writer and reader in DIFFERENT domains, same field X) must be covered
# by a ``cross_domain_edges`` entry with ``resolution.strategy ==
# "remote_event_bridge"`` whose ``from_script``/``to_script`` script_ids match
# the writer/reader. Otherwise the write never reaches the reader's process →
# the reader silently sees a stale value.
#
# DEFERRED (Class-2 dynamic shared-flag store mismatch — the door bug): the
# topology does NOT record the reader's OWN store (shared_flag_channels records
# the WRITER funnel dests Character+Player, not what the reader reads from), so
# detecting "reader reads the shared flag from the HumanoidRootPart instead of
# Player/Character" requires parsing the reader's store expression out of the
# Luau. That is (a) a brittle regex-on-AI-output heuristic the project rule
# forbids, and (b) PHANTOM on the validation corpus: the verifier runs
# POST-coherence (pipeline.py:4759, after _subphase_cohere_scripts), and the
# ``door_player_flag_location`` coherence pack has already rewritten the
# wrong-store read to ``player:`` before the verifier scans — so the bug no
# longer exists in the scanned scripts. The would-be ``present == False``
# coverage alternative is vacuous (``present = bool(read_names) or
# fail_open_present`` — read_names non-empty ⟹ present True). Class-2 needs a
# PRE-coherence hook (or to detect the gap the pack itself keys on) + an
# adversarial review; it is recorded as a known deferred false-negative, not
# shipped as a phantom heuristic.

# Authoritative domain for a read/write SITE is the domain of the MODULE whose
# script contains it — read from the topology artifact (the infer_module_domains
# verdict), NOT RbxScript.script_type (coherence flips Script<->LocalScript, so
# script_type disagrees with the domain the edge producer used).
_SETATTR_RE = re.compile(r""":SetAttribute\s*\(\s*['"]([A-Za-z0-9_]+)['"]""")
_GETATTR_RE = re.compile(
    r""":GetAttribute(?:ChangedSignal)?\s*\(\s*['"]([A-Za-z0-9_]+)['"]""",
)


def _name_to_script_id(topology: TopologyArtifact) -> dict[str, str]:
    """``{emitted script name -> script_id}`` from the topology modules block.

    The emitted name is the tail of ``module_path`` (``f"{container}.{name}"``)
    when present, else ``stem`` — the same join check A uses. A name that maps
    to MORE than one script_id is dropped (collision → the caller abstains),
    mirroring the storage classifier's collision-exclusion contract."""
    by_name: dict[str, list[str]] = {}
    modules = topology.get("modules") or {}
    for script_id, module in modules.items():
        # Reuse check A's join (module_path tail else stem) so the two never
        # drift (review P3).
        name = _join_name(module)
        if name:
            by_name.setdefault(name, []).append(str(script_id))
    return {name: ids[0] for name, ids in by_name.items() if len(ids) == 1}


def _check_cross_domain_attribute(
    topology: TopologyArtifact,
    scripts: list[RbxScript],
) -> list[ContractViolation]:
    """Class-1 cross-domain attribute reconciliation. See section comment."""
    modules = topology.get("modules") or {}
    if not modules:
        return []
    name_to_sid = _name_to_script_id(topology)

    def _domain(sid: str) -> str:
        module = modules.get(sid)
        return str(module.get("domain") or "") if isinstance(module, dict) else ""

    # Collect writer/reader sites keyed by field: {field -> {sid, ...}}.
    writers: dict[str, set[str]] = {}
    readers: dict[str, set[str]] = {}
    for script in scripts:
        sid = name_to_sid.get(script.name)
        if sid is None:
            continue  # unjoinable / colliding name → abstain (no silent fail)
        source = _strip_luau_comments(script.source or "")
        for m in _SETATTR_RE.finditer(source):
            writers.setdefault(m.group(1), set()).add(sid)
        for m in _GETATTR_RE.finditer(source):
            readers.setdefault(m.group(1), set()).add(sid)

    # Index covered (from_script, field, to_script) triples from the edges.
    covered: set[tuple[str, str, str]] = set()
    for edge in topology.get("cross_domain_edges") or []:
        if not isinstance(edge, dict):
            continue
        resolution = edge.get("resolution")
        strategy = (
            resolution.get("strategy") if isinstance(resolution, dict) else ""
        )
        if strategy != "remote_event_bridge":
            continue
        field_name = str(edge.get("field") or "")
        covered.add((
            str(edge.get("from_script") or ""),
            field_name,
            str(edge.get("to_script") or ""),
        ))

    violations: list[ContractViolation] = []
    # Each (w_sid, field_name, r_sid) triple is unique by construction
    # (fixed field, set-sourced sids), so no cross-iteration dedup is needed.
    for field_name, writer_sids in writers.items():
        reader_sids = readers.get(field_name)
        if not reader_sids:
            continue
        for w_sid in writer_sids:
            w_domain = _domain(w_sid)
            if not w_domain:
                continue
            for r_sid in reader_sids:
                r_domain = _domain(r_sid)
                if not r_domain or r_domain == w_domain:
                    continue  # same-domain (or unknown) — no bridge needed
                if (w_sid, field_name, r_sid) in covered:
                    continue
                identity = (
                    f"cross_domain_attribute:{w_sid}:{field_name}:{r_sid}"
                )
                violations.append(
                    ContractViolation(
                        check="cross_domain_attribute",
                        severity="warning",
                        script=field_name,
                        detail=(
                            f"attribute {field_name!r} is SetAttribute-written "
                            f"in domain {w_domain!r} and read in domain "
                            f"{r_domain!r} with no remote_event_bridge edge — "
                            f"the cross-process write never reaches the reader"
                        ),
                        identity=identity,
                    )
                )
    return violations
