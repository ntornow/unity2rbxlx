# PR #148 follow-ups (Phase 1 — scene_runtime_topology)

Phase 1 (PR #148) landed the `scene_runtime_topology/` package, the
`build_topology` coordinator + 6 emit-time invariants, the
`animation_routing` driver narrowing, the
`pipeline._build_and_apply_topology` wire-in, and slice 10/11 unit +
integration tests for the topology authority's emit contract. This
note tracks gaps surfaced while implementing slice 11's SimpleFPS
integration test, ahead of Phase 2.

## Phase 1 narrowing limit: property-based Animator access

`animation_routing.resolve_driver` (slice 7) narrows the driver of an
animation-clip's scope to "THE one MonoBehaviour with a serialized
Animator reference in this prefab/scene". The narrowing walks the
planner's reference list (`scene_runtime_planner._split_config_and_refs`),
which captures any serialized MonoBehaviour field whose Unity-side type
resolves to `Animator` — `[SerializeField] Animator` is the canonical
case but a `public Animator` field also counts. Property / runtime-
getter shapes don't.

MonoBehaviours that read their Animator via a property or runtime
getter — e.g. SimpleFPS's `Door.cs`:

```csharp
private Animator doorAnim {
    get { return transform.parent.Find("door").GetComponent<Animator>(); }
}
```

— produce NO serialized reference, so Phase 1 cannot resolve them.
The driver lands `routing_status="unresolved"` with the server-safe
fallback placement (Script in ServerScriptService).

This is exactly what the `resolve_driver` docstring and the design
doc's Phase 2 prose already commit to: a future Phase 2 source-
narrowing pass walks the C# source for `SetBool`/`SetFloat`/etc. on
Animator references to find the writer. (The design doc names the
narrowing pass in §Phase 1's resolver docstring + §Phase 2 prose
but does NOT bind it specifically to Phase 2a or Phase 2b — both
sub-phases land before the contract verifier in §Phase 3.) Until
that pass lands, **Door** stays server-side on a SimpleFPS cold
conversion.

**HostilePlane and PlaneHolder are a different category, not the
same bug.** The design doc's §Phase 1 root-cause analysis
(`scene-runtime-architecture-ir.md:397-405`) classifies them as
autoplay clips with empty `m_AnimatorParameters` and no
MonoBehaviour driver (PlaneHolder has no MBs at all; HostilePlane.cs
handles audio + spawning but never touches the Animator). Server
placement is the **multiplayer-correct permanent destination** for
these autoplay clips — Phase 2 source narrowing won't move them
anywhere because there's no writer to narrow to. Their
`routing_status="unresolved"` is a Phase-1-honest way of saying
"no driver in scope," and the server fallback Phase 1 ships them to
is structurally fine. Any remaining issues these scripts have at
runtime (target lookup, replication timing) are out of scope for
both Phase 1 and Phase 2; tracked separately.

### Implication for the design doc

The §Phase 1 "What this resolves" bullet ("Bug 3 (door visual):
animation script gets placed in correct domain. All six known broken
Anim_* scripts ...") was aspirational *for Door only* — only Door
will move to a different domain when Phase 2 lands. The
HostilePlane/PlaneHolder bullets in the same list overstate Phase 1's
intent: their unresolved-+-server-fallback state today IS the
intended permanent placement, not a bug pending Phase 2.

**Action**: when Phase 2 lands the source-narrowing pass, fold the
"What this resolves" bullet back into a Phase 1+2 combined statement.
For now, slice 11's integration test docstring
(`test_simplefps_topology_authority_contract_on_cold_conversion`)
documents Phase 1's actual delivery surface.

## Slice 11 integration test: what it validates today

`tests/test_scene_runtime_topology.py::test_simplefps_topology_authority_contract_on_cold_conversion`
asserts five things against a real SimpleFPS `Pipeline.run_all()`:

1. `scene_runtime.topology` emitted under generic mode with non-empty
   `animation_drivers`.
2. `topology.modules` includes Door with `stem="Door"`,
   `domain="client"` — proves classifier-v2 sees Door as client even
   when the driver can't yet be routed to it.
3. Every driver carries an explicit `routing_status` from
   `{"resolved","unresolved","orphan"}` (codex B1 fix held).
4. Every emitted `Anim_*` script's live placement is consistent with
   its driver's topology decision: `resolved+client → LocalScript in
   StarterPlayer.StarterPlayerScripts`; everything else stays
   `Script in ServerScriptService` (Phase 1's safe fallback).
5. No duplicate `Anim_*` names in `rbx_place.scripts` (invariant 3
   structural guarantee).

When Phase 2 source-narrowing lands and Door's driver resolves to
client, extend this test to additionally assert the
LocalScript/StarterPlayerScripts placement for `Anim_Door_*` —
that's the structural pass the user-visible door bug needs.

## E2E status: `door_opens_with_key` fixture — attribute passes, visual does NOT move

The design doc's §Phase 1 Testing also lists a Studio gameplay E2E:
"`door_opens_with_key` fixture passes — door visual moves up 14.28
studs after touch". Verified on this branch via `/e2e-test SimpleFPS
--generic` (run 2026-05-26T04-10-50-0d1d7c): the **`open`
attribute** flips to `true` after the prerequisite cardkey pickup,
but the **door visual does NOT physically move** (`dPos == 0` after
the 2.5s settle). The pre-PR-#148 fixture (`OR open==true OR dPos>1
OR dRot>0.2`) silently passed on the attribute branch alone — the
visual gap had been latent on both `main` and `feat/scene-runtime-
topology-phase-1`. Slice 11 tightens the fixture (drops the
`open==true` branch) so the visual gap is the contract the test
enforces going forward. Phase 1 doesn't break the door — and
doesn't fix the visual either. The door's
behavior is driven not by the animation script's placement but by
Door's coherence-pack pipeline:

1. `pickup_remote_event_server` (and the door-pack widening from
   PR #121) routes `playerHasKey` reads server-side via a RemoteEvent
   bridge.
2. The Door MonoBehaviour sets an `open` attribute on its prefab
   target.
3. `Anim_door_open` ships as a server-side `Script` in
   `ServerScriptService` and tweens the door visual up 14.28 studs.

The door's animation driver IS unresolved under Phase 1's narrowing
(see above), but the placement that Phase 1's safe-fallback delivers
(`Script` in `ServerScriptService`) is the SAME placement the
pre-PR-#148 pipeline produced for this script. Phase 1 doesn't break
the door — and doesn't *fix* the door visual either. The fix lives
in the coherence-pack layer, independently of the topology authority.

### Why the visual doesn't move (root cause, 2026-05-26 investigation)

`Door.luau` is classified `domain="client"` by the v2 classifier
and runs in the client-side `SceneRuntime` host. `ToggleDoor` calls
`doorAnim:SetAttribute("open", value)` on the `door` MeshPart **from
the client**. Roblox attribute writes from a client do NOT replicate
to the server (only server→client; client-side state is local).

`Anim_Door_door_open` ships as a server-side `Script` (Phase 1's
safe fallback for the unresolved Door driver). It binds
`GetAttributeChangedSignal("open")` on workspace door instances —
**server-side**. So the client's `SetAttribute` fires no listener
on the server, and the tween never runs.

The audio path works because `source:Play()` on a Sound instance
replicates the audio playback from the client side (Sounds played
on the client are audible locally), so the user hears the door open
sound even though the visual stays put.

A `PlayerSetSharedFlag` RemoteEvent bridge exists in the converter
(`converter/autogen.py:163`, used by `Player.luau` for `hasKey`),
but the AI prompt that routes attribute writes through it
(`code_transpiler.py:1271`) is scoped to `GetItem`-style pickup
patterns — not generic Animator-attribute writes. Door's
`SetAttribute("open", value)` slips through.

Two ways to close this gap:
1. **Phase 2 source narrowing.** Walk Door.cs's C# source to find
   `doorAnim.SetBool("open", value)`, resolve `doorAnim` to the
   Animator on the prefab's `door` child, and route the
   animation script to client (LocalScript in
   StarterPlayerScripts). Same-domain write→read, no replication
   gap.
2. **Widen the cross-domain bridge.** Generalize the
   `PlayerSetSharedFlag` RemoteEvent (or emit a per-attribute
   bridge per the design doc's §Phase 2b) so any client-domain
   MonoBehaviour writing to an animator-listened attribute mirrors
   server-side.

Path 1 is the architecturally cleaner fix (collapses bridge into
direct same-domain wiring) and matches the doc's Phase 2 narrowing
prose. Tracked for the next slice.

The Phase 2 source-narrowing pass will eventually flip
`Anim_door_open` to a client-side `LocalScript` in
`StarterPlayer.StarterPlayerScripts`. When that lands, the door
should still pass — the coherence packs become unnecessary for this
specific case (server-write→client-read becomes a same-domain
write), and the topology decision becomes the authoritative
placement.
