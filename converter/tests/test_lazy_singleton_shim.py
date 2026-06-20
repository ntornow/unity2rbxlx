"""Phase 2 lazy-singleton boot-instantiation — host-level behavioral tests for
``SceneRuntime.seedLazySingletons`` in ``runtime/scene_runtime.luau`` plus the
autogen entrypoint wiring (slice 2.2).

The shim half drives the REAL host runtime under standalone ``luau``: it builds a
real ``SceneRuntime.new(services, plan)`` engine with a ``plan.modules`` table
GUID-keyed by the seed's ``script_guid`` and seeds through the REAL
``engine:addComponent`` -> ``_buildComponent`` -> ``plan.modules[script_guid]``
-> ``Cls.new`` -> ``_runAwakeEnableStart`` path. This is the load-bearing
acceptance (design §5, codex's lesson): it proves the LIFECYCLE, not existence —
a fake engine that stubs the module lookup would be a green-test-for-wrong-reason
since the BLOCKING bug is precisely the ``plan.modules[scriptId]`` GUID-vs-stem
mismatch.

The CoroutineHandler-shaped module mirrors the REAL converter output: a
SYNTHESIZED ``Awake`` that caches ``Cls.m_Instance = self`` (the backing field),
a static ``getInstance()`` returning that field, and a static
``StartStaticCoroutine(fn)`` that resolves ``getInstance()`` and runs the
coroutine off the seeded instance's ``self.host:startCoroutine`` — i.e. the
generic host scheduler, not a Roblox default.

Covers (slice 2.2 acceptance):
  * a lazy_singletons seed whose module is CoroutineHandler-shaped -> after the
    shim, ``Cls[backing_field]`` (getInstance()) is non-nil (constructed +
    Awoke once through the real addComponent path);
  * the idempotency guard makes a SECOND shim call a no-op (the carried
    backing_field is the canonical guard — same instance, no re-construct);
  * an unresolvable script_guid (or a module without ``.new``) -> warn + skip,
    no crash;
  * the domain filter: a client-domain seed is skipped when the entrypoint side
    is ``"server"``;
  * the DECISIVE criterion (design §5.2): a ``StartStaticCoroutine``-style static
    coroutine SCHEDULES + ADVANCES (observably ticks) after seeding — proving the
    synthetic-host wiring drives the engine's coroutine scheduler deeply enough
    to fix the real bug (``getInstance() ~= nil`` alone is NOT sufficient).

The autogen half asserts the entrypoint emission: both client + server
entrypoints call ``seedLazySingletons`` AFTER the consumable seed and BEFORE
``engine:start`` (the singleton must be Awoken before any consumer's deferred
Start/coroutine use).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

HOST_RUNTIME_PATH = Path(__file__).parent.parent / "runtime" / "scene_runtime.luau"


def _luau_available() -> bool:
    return shutil.which("luau") is not None


_luau_marker = pytest.mark.skipif(
    not _luau_available() or not HOST_RUNTIME_PATH.exists(),
    reason="needs standalone luau interpreter + host runtime file",
)


def _run(scenario: str) -> str:
    host_source = HOST_RUNTIME_PATH.read_text(encoding="utf-8")
    delim = "==="
    while f"]{delim}]" in host_source or f"[{delim}[" in host_source:
        delim += "="
    embedded = f"[{delim}[\n{host_source}\n]{delim}]"
    preamble = textwrap.dedent(f"""\
        local HOST_RUNTIME_SOURCE = {embedded}
        local SceneRuntime
        do
            local chunk, err = loadstring(HOST_RUNTIME_SOURCE, "scene_runtime")
            assert(chunk, "load host runtime failed: " .. tostring(err))
            SceneRuntime = chunk()
        end

        local logs = {{}}
        local function logWarn(...)
            local parts = {{...}}
            for i, p in ipairs(parts) do parts[i] = tostring(p) end
            table.insert(logs, table.concat(parts, " "))
        end

        -- A controllable task scheduler so coroutines can be ADVANCED on a
        -- later "frame" — the decisive lifecycle proof. ``task.spawn`` runs the
        -- function as a coroutine and resumes it once (to its first yield);
        -- ``task.defer`` queues a thunk drained by ``runDeferred()``;
        -- ``tick()`` resumes every spawned coroutine still suspended (a later
        -- frame). This is the minimal real-shaped surface the engine's
        -- ``startCoroutine`` (task.spawn) and ``_scheduleStartIfPending``
        -- (task.defer) need.
        local spawnedThreads = {{}}
        local deferredThunks = {{}}
        local task = {{}}
        function task.spawn(fn, ...)
            local co = coroutine.create(fn)
            table.insert(spawnedThreads, co)
            coroutine.resume(co, ...)
            return co
        end
        function task.defer(fn, ...)
            table.insert(deferredThunks, {{ fn = fn, args = {{...}} }})
        end
        function task.delay(_sec, fn, ...) table.insert(deferredThunks, {{ fn = fn, args = {{...}} }}) end
        function task.wait(_sec) coroutine.yield() return 0 end
        local function runDeferred()
            local pending = deferredThunks
            deferredThunks = {{}}
            for _, t in ipairs(pending) do t.fn(table.unpack(t.args)) end
        end
        local function tick()
            for _, co in ipairs(spawnedThreads) do
                if coroutine.status(co) == "suspended" then
                    coroutine.resume(co)
                end
            end
        end

        -- A minimal but REAL services surface for ``SceneRuntime.new``. The
        -- shim resolves modules by GUID-keyed path; ``getInstanceId`` is never
        -- reached because the shim passes a synthetic STRING ``go`` (addComponent
        -- sets goId = go directly for a string).
        local function servicesFor(modulesByPath)
            return {{
                warn = logWarn,
                task = task,
                resolveModule = function(_id, path) return modulesByPath[path] end,
                getInstanceId = function(_inst) return nil end,
                findFirstChildWhichIsA = function(_inst, _cls) return nil end,
                workspaceFind = function(_id) return nil end,
                heartbeat = {{ Connect = function() return {{ Disconnect = function() end }} end }},
                fixedStep = 0.02,
                now = function() return 0 end,
            }}
        end

        -- A CoroutineHandler-SHAPED module mirroring the REAL converter output:
        --   * an empty ``.new(config)`` constructor,
        --   * a SYNTHESIZED ``Awake`` that caches ``Cls[backingField] = self``
        --     (CoroutineHandler.luau:11 writes ``CoroutineHandler.m_Instance = self``),
        --   * a static ``getInstance()`` returning the backing field,
        --   * a static ``StartStaticCoroutine(fn)`` that resolves getInstance()
        --     and runs the coroutine via the seeded instance's
        --     ``self.host:startCoroutine`` (the generic host scheduler).
        -- ``backingField`` is a parameter so the test proves the shim reads the
        -- CARRIED field name, not a hardcoded ``m_Instance``.
        local function makeCoroutineHandler(backingField)
            local Cls = {{}}
            Cls.__index = Cls
            Cls.awakeCount = 0
            function Cls.new(_config)
                return setmetatable({{}}, Cls)
            end
            function Cls:Awake()
                Cls.awakeCount = Cls.awakeCount + 1
                Cls[backingField] = self   -- the synthesized cache (gap #2 fix)
            end
            function Cls.getInstance()
                return Cls[backingField]
            end
            -- Static entrypoint: resolves the cached instance and runs the
            -- coroutine off ITS host (the seeded instance must carry a live
            -- host surface for this to advance — the decisive wiring proof).
            function Cls.StartStaticCoroutine(fn)
                local inst = Cls.getInstance()
                if inst == nil then return nil end
                return inst.host:startCoroutine(fn)
            end
            return Cls
        end

        local function dumpLogs()
            for _, msg in ipairs(logs) do print("WARN_LINE=" .. msg) end
            print("WARN_COUNT=" .. tostring(#logs))
        end
    """)
    src = preamble + "\n" + scenario
    with tempfile.NamedTemporaryFile("w", suffix=".luau", delete=False) as fh:
        fh.write(src)
        path = fh.name
    try:
        proc = subprocess.run(
            [shutil.which("luau") or "luau", path],
            capture_output=True, text=True, timeout=30,
        )
    finally:
        Path(path).unlink(missing_ok=True)
    assert proc.returncode == 0, f"luau failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


# A plan with ONE CoroutineHandler seed. ``plan.modules`` is GUID-keyed (the
# real planner shape, scene_runtime.luau:1127 looks up plan.modules[scriptId]),
# each row carrying ``module_path``/``stem``/``domain``. The seed's
# ``script_guid`` IS that GUID key — the load-bearing GUID-vs-stem fix: a
# stem/path key would MISS in _buildComponent and nothing would build.
_ONE_SEED_PLAN = """\
local GUID = "abc-coroutine-handler-guid"
local Cls = makeCoroutineHandler("m_Instance")
local modulesByPath = {
    ["ReplicatedStorage.CoroutineHandler"] = Cls,
}
local plan = {
    modules = {
        [GUID] = {
            module_path = "ReplicatedStorage.CoroutineHandler",
            stem = "CoroutineHandler",
            domain = "client",
            runtime_bearing = false,
        },
    },
    lazy_singletons = {
        {
            module_path = "ReplicatedStorage.CoroutineHandler",
            class_stem = "CoroutineHandler",
            domain = "client",
            script_guid = GUID,
            backing_field = "m_Instance",
        },
    },
}
local services = servicesFor(modulesByPath)
local engine = SceneRuntime.new(services, plan)
"""


@_luau_marker
def test_seed_constructs_and_awakes_one_instance_backing_field_live():
    """After the shim, the carried ``backing_field`` (getInstance()) is non-nil:
    the singleton was constructed + Awoke ONCE through the REAL addComponent ->
    _buildComponent -> plan.modules[script_guid] -> Cls.new -> Awake path."""
    out = _run(_ONE_SEED_PLAN + """
print("BEFORE_NIL=" .. tostring(Cls.getInstance() == nil))
SceneRuntime.seedLazySingletons(plan, services, engine, nil)
print("AFTER_NONNIL=" .. tostring(Cls.getInstance() ~= nil))
print("AWAKE_COUNT=" .. tostring(Cls.awakeCount))
-- the cached instance carries a live host surface (Awake ran AFTER injectHostSurface):
print("HAS_HOST=" .. tostring(type(Cls.getInstance().host) == "table"))
dumpLogs()
""")
    assert "BEFORE_NIL=true" in out      # nil before seeding (the bug state)
    assert "AFTER_NONNIL=true" in out    # getInstance() live after seeding
    assert "AWAKE_COUNT=1" in out        # exactly one Awake (one instance)
    assert "HAS_HOST=true" in out
    assert "WARN_COUNT=0" in out


@_luau_marker
def test_static_coroutine_schedules_and_advances_after_seeding():
    """DECISIVE criterion (design §5.2): a StartStaticCoroutine-launched coroutine
    SCHEDULES (reaches its first yield) AND ADVANCES (ticks on a later frame) via
    the REAL path after seeding. getInstance() non-nil alone is NOT sufficient —
    this proves the synthetic-host wiring drives the engine's coroutine scheduler,
    fixing the real ``StartStaticCoroutine`` no-op bug."""
    out = _run(_ONE_SEED_PLAN + """
SceneRuntime.seedLazySingletons(plan, services, engine, nil)
-- A coroutine that records reaching its first yield, then advancing past it.
local reachedFirstYield = false
local advanced = false
CoroutineHandler_probe = function()
    reachedFirstYield = true
    coroutine.yield()        -- first yield: simulates a frame wait
    advanced = true          -- only set when resumed on a LATER frame
end
local handle = Cls.StartStaticCoroutine(CoroutineHandler_probe)
print("SCHEDULED=" .. tostring(handle ~= nil))
print("REACHED_FIRST_YIELD=" .. tostring(reachedFirstYield))
print("ADVANCED_BEFORE_TICK=" .. tostring(advanced))   -- must still be false
tick()                                                  -- a later frame resumes it
print("ADVANCED_AFTER_TICK=" .. tostring(advanced))     -- now true
dumpLogs()
""")
    assert "SCHEDULED=true" in out               # getInstance().host scheduled it
    assert "REACHED_FIRST_YIELD=true" in out     # the coroutine body ran to yield
    assert "ADVANCED_BEFORE_TICK=false" in out   # genuinely suspended, not run-to-completion
    assert "ADVANCED_AFTER_TICK=true" in out     # resumed on a later frame -> the bug is fixed
    assert "WARN_COUNT=0" in out


@_luau_marker
def test_idempotency_second_call_is_a_noop_via_backing_field_guard():
    """The ``Cls[backing_field] ~= nil`` guard makes a SECOND shim call a no-op:
    the cached instance identity is unchanged and Awake does not run again (the
    backing field is the canonical guard — no separate bool needed)."""
    out = _run(_ONE_SEED_PLAN + """
SceneRuntime.seedLazySingletons(plan, services, engine, nil)
local first = Cls.getInstance()
SceneRuntime.seedLazySingletons(plan, services, engine, nil)   -- second call
print("SAME_INSTANCE=" .. tostring(Cls.getInstance() == first))
print("AWAKE_COUNT=" .. tostring(Cls.awakeCount))   -- still 1, not 2
dumpLogs()
""")
    assert "SAME_INSTANCE=true" in out
    assert "AWAKE_COUNT=1" in out       # no double-construct
    assert "WARN_COUNT=0" in out


@_luau_marker
def test_idempotency_skips_when_scene_instance_already_cached():
    """If a scene-placed instance already Awoke (backing field pre-set), the shim
    SKIPS — never double-constructs the "exactly one instance" singleton."""
    out = _run(_ONE_SEED_PLAN + """
-- Simulate a scene-placed instance having already Awoke + cached itself.
local preexisting = Cls.new({})
Cls.m_Instance = preexisting
SceneRuntime.seedLazySingletons(plan, services, engine, nil)
print("UNCHANGED=" .. tostring(Cls.getInstance() == preexisting))
print("AWAKE_COUNT=" .. tostring(Cls.awakeCount))   -- 0: shim never ran Awake
dumpLogs()
""")
    assert "UNCHANGED=true" in out
    assert "AWAKE_COUNT=0" in out
    assert "WARN_COUNT=0" in out


@_luau_marker
def test_unresolvable_script_guid_warns_and_skips_no_crash():
    """A seed whose module path does not resolve to a constructable class is
    warned + skipped — boot does not crash (fail-soft, mirrors the consumable
    shim's drop+warn)."""
    out = _run(_ONE_SEED_PLAN.replace(
        '["ReplicatedStorage.CoroutineHandler"] = Cls,',
        '["ReplicatedStorage.CoroutineHandler"] = nil,  -- resolve miss',
    ) + """
SceneRuntime.seedLazySingletons(plan, services, engine, nil)
print("NO_CRASH=true")
dumpLogs()
""")
    assert "NO_CRASH=true" in out
    assert any(
        line.startswith("WARN_LINE=[lazy-singleton] module did not resolve")
        for line in out.splitlines()
    ), out


@_luau_marker
def test_script_guid_absent_from_plan_modules_warns_and_skips_no_crash():
    """The REAL GUID-keyed failure mode (BLOCKING #1): a seed whose ``script_guid``
    is NOT a key in ``plan.modules``. ``resolveModule`` returns a constructable
    class (so the resolve gate passes), but ``addComponent`` -> ``_buildComponent``
    would index ``plan.modules[script_guid]`` and explode. The shim must check
    presence first -> warn + skip, never crash boot; OTHER seeds still process.

    FAILS against the pre-fix shim (unguarded ``engine:addComponent`` indexes a
    nil ``plan.modules[script_guid]`` and errors -> luau returncode != 0)."""
    out = _run("""
local GUID_GOOD = "guid-good"
local GUID_STALE = "guid-stale-not-in-modules"
local ClsGood = makeCoroutineHandler("m_Instance")
-- A second class whose path resolves, but whose GUID is absent from plan.modules.
local ClsStale = makeCoroutineHandler("_instance")
local modulesByPath = {
    ["ReplicatedStorage.Good"] = ClsGood,
    ["ReplicatedStorage.Stale"] = ClsStale,
}
local plan = {
    modules = {
        [GUID_GOOD] = { module_path = "ReplicatedStorage.Good", stem = "Good",
                        domain = "client", runtime_bearing = false },
        -- NOTE: GUID_STALE intentionally absent from plan.modules.
    },
    lazy_singletons = {
        -- stale seed first so an unguarded crash would skip the good seed:
        { module_path = "ReplicatedStorage.Stale", class_stem = "Stale",
          domain = "client", script_guid = GUID_STALE, backing_field = "_instance" },
        { module_path = "ReplicatedStorage.Good", class_stem = "Good",
          domain = "client", script_guid = GUID_GOOD, backing_field = "m_Instance" },
    },
}
local services = servicesFor(modulesByPath)
local engine = SceneRuntime.new(services, plan)
SceneRuntime.seedLazySingletons(plan, services, engine, nil)
print("NO_CRASH=true")
print("STALE_SKIPPED=" .. tostring(ClsStale.getInstance() == nil))
print("GOOD_STILL_SEEDED=" .. tostring(ClsGood.getInstance() ~= nil))
dumpLogs()
""")
    assert "NO_CRASH=true" in out
    assert "STALE_SKIPPED=true" in out
    assert "GOOD_STILL_SEEDED=true" in out      # other seeds still process
    assert any(
        line.startswith("WARN_LINE=[lazy-singleton] script_guid not present in plan.modules")
        for line in out.splitlines()
    ), out


@_luau_marker
def test_throwing_resolve_module_warns_and_skips_no_crash():
    """BLOCKING #2: a ``resolveModule`` that THROWS (not returns nil) on an
    unresolvable module. The shim pcall-wraps the resolve -> warn + skip, no crash.

    FAILS against the pre-fix shim (the bare ``resolveModule(...)`` call propagates
    the throw -> luau returncode != 0)."""
    out = _run(_ONE_SEED_PLAN.replace(
        "local services = servicesFor(modulesByPath)",
        "local services = servicesFor(modulesByPath)\n"
        "services.resolveModule = function(_id, _path)\n"
        "    error(\"resolver blew up\")\n"
        "end",
    ) + """
SceneRuntime.seedLazySingletons(plan, services, engine, nil)
print("NO_CRASH=true")
print("STILL_NIL=" .. tostring(Cls.getInstance() == nil))
dumpLogs()
""")
    assert "NO_CRASH=true" in out
    assert "STILL_NIL=true" in out
    assert any(
        line.startswith("WARN_LINE=[lazy-singleton] resolveModule errored")
        for line in out.splitlines()
    ), out


@_luau_marker
def test_module_without_new_warns_and_skips():
    """A resolved module that lacks ``.new`` (e.g. a dead-module inert stub) is
    warned + skipped, not constructed."""
    out = _run(_ONE_SEED_PLAN.replace(
        'local Cls = makeCoroutineHandler("m_Instance")',
        'local Cls = { getInstance = function() return nil end }  -- no .new',
    ) + """
SceneRuntime.seedLazySingletons(plan, services, engine, nil)
print("STILL_NIL=" .. tostring(Cls.getInstance() == nil))
dumpLogs()
""")
    assert "STILL_NIL=true" in out
    assert any(
        line.startswith("WARN_LINE=[lazy-singleton] module did not resolve")
        for line in out.splitlines()
    ), out


@_luau_marker
def test_domain_filter_skips_other_side():
    """With the entrypoint side ``"server"``, a client-domain seed is SKIPPED (not
    constructed) — mirroring engine:start's domain filter. With matching side or
    nil filter, it IS seeded."""
    out = _run(_ONE_SEED_PLAN + """
-- server side, client seed -> skipped
SceneRuntime.seedLazySingletons(plan, services, engine, "server")
print("SERVER_SKIPPED=" .. tostring(Cls.getInstance() == nil))
-- matching side -> seeded
SceneRuntime.seedLazySingletons(plan, services, engine, "client")
print("CLIENT_SEEDED=" .. tostring(Cls.getInstance() ~= nil))
dumpLogs()
""")
    assert "SERVER_SKIPPED=true" in out
    assert "CLIENT_SEEDED=true" in out
    assert "WARN_COUNT=0" in out


@_luau_marker
def test_helper_domain_seed_constructed_on_both_entrypoints():
    """A ``helper``-domain seed (a shared ReplicatedStorage utility loaded by BOTH
    VMs — the REAL CoroutineHandler) is constructed on the ``"server"`` entrypoint
    AND the ``"client"`` entrypoint, regardless of ``domainFilter``. The per-VM
    idempotency guard prevents a double-construct within a single VM.

    FAILS against the pre-fix shim (which skipped any seed whose ``domain`` did not
    EQUAL the filter, so a helper seed was constructed on NEITHER side)."""
    helper_plan = _ONE_SEED_PLAN.replace('domain = "client"', 'domain = "helper"')
    # Two fresh engines simulate the two separate VMs (each VM has its own class
    # table -> its own backing field). The helper seed must construct on BOTH.
    out = _run(helper_plan + """
-- VM A: the server entrypoint side.
local ClsServer = makeCoroutineHandler("m_Instance")
local servicesServer = servicesFor({ ["ReplicatedStorage.CoroutineHandler"] = ClsServer })
local engineServer = SceneRuntime.new(servicesServer, plan)
SceneRuntime.seedLazySingletons(plan, servicesServer, engineServer, "server")
print("SERVER_SEEDED=" .. tostring(ClsServer.getInstance() ~= nil))

-- VM B: the client entrypoint side (a distinct class table = a distinct VM).
local ClsClient = makeCoroutineHandler("m_Instance")
local servicesClient = servicesFor({ ["ReplicatedStorage.CoroutineHandler"] = ClsClient })
local engineClient = SceneRuntime.new(servicesClient, plan)
SceneRuntime.seedLazySingletons(plan, servicesClient, engineClient, "client")
print("CLIENT_SEEDED=" .. tostring(ClsClient.getInstance() ~= nil))

-- Per-VM idempotency: a second seed on the SAME VM does not re-construct.
SceneRuntime.seedLazySingletons(plan, servicesServer, engineServer, "server")
print("SERVER_AWAKE_COUNT=" .. tostring(ClsServer.awakeCount))
dumpLogs()
""")
    assert "SERVER_SEEDED=true" in out     # helper constructed on the server side
    assert "CLIENT_SEEDED=true" in out     # AND on the client side
    assert "SERVER_AWAKE_COUNT=1" in out   # per-VM idempotency holds (no double-construct)
    assert "WARN_COUNT=0" in out


@_luau_marker
def test_reads_carried_backing_field_not_hardcoded():
    """The shim reads the CARRIED ``backing_field`` (the name varies across
    projects), never a hardcoded ``m_Instance``. A seed using ``_instance``
    constructs + caches under that field, and getInstance() (which reads the
    same field) is live."""
    out = _run("""
local GUID = "guid-underscore-instance"
local Cls = makeCoroutineHandler("_instance")   -- non-default field name
local modulesByPath = { ["ReplicatedStorage.Other"] = Cls }
local plan = {
    modules = {
        [GUID] = { module_path = "ReplicatedStorage.Other", stem = "Other",
                   domain = "client", runtime_bearing = false },
    },
    lazy_singletons = {
        { module_path = "ReplicatedStorage.Other", class_stem = "Other",
          domain = "client", script_guid = GUID, backing_field = "_instance" },
    },
}
local services = servicesFor(modulesByPath)
local engine = SceneRuntime.new(services, plan)
SceneRuntime.seedLazySingletons(plan, services, engine, nil)
print("UNDERSCORE_LIVE=" .. tostring(Cls._instance ~= nil))
print("GETINSTANCE_LIVE=" .. tostring(Cls.getInstance() ~= nil))
-- a hardcoded m_Instance read would be nil here (the field is _instance):
print("MINSTANCE_NIL=" .. tostring(Cls.m_Instance == nil))
dumpLogs()
""")
    assert "UNDERSCORE_LIVE=true" in out
    assert "GETINSTANCE_LIVE=true" in out
    assert "MINSTANCE_NIL=true" in out
    assert "WARN_COUNT=0" in out


@_luau_marker
def test_generic_noop_when_no_seeds():
    """An absent or empty ``lazy_singletons`` is a clean generic no-op."""
    out = _run("""
local services = servicesFor({})
local engine = SceneRuntime.new(services, { modules = {} })
SceneRuntime.seedLazySingletons({ modules = {} }, services, engine, nil)
SceneRuntime.seedLazySingletons({ modules = {}, lazy_singletons = {} }, services, engine, nil)
print("NOOP_OK=true")
""")
    assert "NOOP_OK=true" in out


@_luau_marker
def test_missing_resolve_module_service_is_noop():
    """No ``resolveModule`` service -> the shim returns early (no crash), like the
    sibling seed shims."""
    out = _run(_ONE_SEED_PLAN.replace(
        "local services = servicesFor(modulesByPath)",
        "local services = { warn = logWarn, task = task }  -- no resolveModule",
    ) + """
SceneRuntime.seedLazySingletons(plan, services, engine, nil)
print("EARLY_RETURN_OK=true")
""")
    assert "EARLY_RETURN_OK=true" in out


# ---------------------------------------------------------------------------
# Autogen entrypoint wiring (Python-side; no luau needed).
# ---------------------------------------------------------------------------

def _entrypoint_sources() -> tuple[str, str]:
    from converter import autogen

    client = autogen.generate_scene_runtime_client_entrypoint().source
    server = autogen.generate_scene_runtime_server_entrypoint().source
    return client, server


def test_both_entrypoints_call_seed_lazy_singletons_with_engine_and_side():
    client, server = _entrypoint_sources()
    assert 'SceneRuntime.seedLazySingletons(Plan, services, engine, "client")' in client
    assert 'SceneRuntime.seedLazySingletons(Plan, services, engine, "server")' in server


def test_lazy_singleton_seed_emitted_after_consumable_and_before_engine_start():
    """The load-bearing boot order: ``seedLazySingletons`` runs AFTER the
    consumable seed and BEFORE ``engine:start`` (the singleton must be Awoken
    before any consumer's deferred Start/coroutine use)."""
    for domain, source in zip(("client", "server"), _entrypoint_sources()):
        i_cons = source.index("SceneRuntime.seedConsumableDatabases(Plan, services)")
        i_lazy = source.index("SceneRuntime.seedLazySingletons(Plan, services, engine")
        i_start = source.index(f'engine:start("{domain}")')
        assert i_cons < i_lazy < i_start, (
            f"{domain} entrypoint boot order wrong: "
            f"consumable={i_cons} lazy={i_lazy} start={i_start}"
        )


def test_lazy_singletons_in_plan_key_allowlist():
    """The plan key must be allowlisted (slice 2.1's edit) or the recomputed seed
    is elided from the emitted plan and the shim sees ``{}``."""
    from converter import autogen

    assert "lazy_singletons" in autogen._PLAN_KEYS_FOR_HOST


# ---------------------------------------------------------------------------
# End-to-end round-trip: detector -> resolver -> REAL plan encoder -> shim.
# ---------------------------------------------------------------------------

# A canonical CoroutineHandler-shaped lazy singleton (static self-typed field +
# self-instantiating getter, no Awake/OnEnable/Start, benign DontDestroyOnLoad).
_E2E_COROUTINE_CS = """\
    using UnityEngine;
    public class CoroutineHandler : MonoBehaviour
    {
        static protected CoroutineHandler m_Instance;
        static public CoroutineHandler instance
        {
            get
            {
                if(m_Instance == null)
                {
                    GameObject o = new GameObject("CoroutineHandler");
                    DontDestroyOnLoad(o);
                    m_Instance = o.AddComponent<CoroutineHandler>();
                }
                return m_Instance;
            }
        }
    }
"""


def _build_e2e_plan_source(tmp_path: Path) -> tuple[str, str, str]:
    """Drive the FULL build half: a real ``.cs`` -> GuidIndex -> detector
    (``analyze_script``) -> the REAL ``resolve_lazy_singletons`` -> the REAL
    ``generate_scene_runtime_plan_module`` encoder. Returns
    ``(encoded_plan_luau, script_guid, module_path)``.

    This is the load-bearing join the two slice tests cover only in halves: the
    seed test stops at the resolver records; the shim test hand-writes a plan
    whose ``script_guid`` matches ``plan.modules`` by construction. Here the
    ``script_guid`` is produced by the resolver off the GuidIndex and carried
    THROUGH the real Luau-literal encoder, so the test proves the GUID join (the
    BLOCKING #1 mismatch) survives a real encode + parse — including the
    bare-vs-bracketed key encoding ``_plan_to_luau`` applies to a GUID key.
    """
    import hashlib

    from unity.guid_resolver import build_guid_index
    from converter.consumable_db_seed import build_base_by_class
    from converter.lazy_singleton_seed import resolve_lazy_singletons
    from converter.autogen import generate_scene_runtime_plan_module

    guid = "a" + hashlib.sha256(b"coroutine-handler-e2e").hexdigest()[:31]
    cs = tmp_path / "Assets" / "Scripts" / "CoroutineHandler.cs"
    cs.parent.mkdir(parents=True, exist_ok=True)
    cs.write_text(textwrap.dedent(_E2E_COROUTINE_CS), encoding="utf-8")
    cs.with_suffix(".cs.meta").write_text(
        f"fileFormatVersion: 2\nguid: {guid}\n", encoding="utf-8",
    )

    guid_index = build_guid_index(tmp_path)
    base_by_class = build_base_by_class(guid_index)
    module_path = "ReplicatedStorage.CoroutineHandler"
    # The GUID-keyed modules registry the planner emits (mirrors the real shape).
    modules: dict[str, object] = {
        guid: {
            "stem": "CoroutineHandler",
            "class_name": "CoroutineHandler",
            "module_path": module_path,
            "domain": "client",
            "runtime_bearing": False,
            "is_component_class": True,
        },
    }
    seeds = resolve_lazy_singletons(
        modules=modules,
        guid_index=guid_index,
        base_by_class=base_by_class,
        module_path_for_stem=lambda stem: (
            f"ReplicatedStorage.{stem}" if stem else None
        ),
    )
    assert len(seeds) == 1, seeds
    assert seeds[0]["script_guid"] == guid
    assert seeds[0]["backing_field"] == "m_Instance"

    scene_runtime = {"modules": modules, "lazy_singletons": seeds}
    plan_module = generate_scene_runtime_plan_module(scene_runtime)
    # ``generate_*`` prepends ``return {...}``; the embed below ``loadstring``s the
    # whole module body, so keep it verbatim (proves it parses as real Luau).
    return plan_module.source, guid, module_path


@_luau_marker
def test_end_to_end_detector_resolver_encoder_shim_round_trip(tmp_path: Path) -> None:
    """The full path joined ONCE: a real ``.cs`` -> detector -> resolver -> the
    REAL plan encoder -> parse -> the REAL shim addComponent path. Proves the
    resolver-produced ``script_guid`` survives the Luau-literal encode and IS a
    key in the encoded ``Plan.modules`` at shim time (the BLOCKING #1 GUID join),
    and that the seeded singleton then constructs + Awakes (getInstance() live).

    The two slice tests cover only halves (seed test stops at records; shim test
    hand-writes a guid-matched plan); this is the only test exercising the encoder
    in the join, so a future encoder rekey or resolver guid-source change is caught.
    """
    plan_source, guid, module_path = _build_e2e_plan_source(tmp_path)

    # Embed the encoded plan module verbatim and ``loadstring`` it -- this is a
    # REAL Luau parse of the encoder output, so a malformed/elided key fails here.
    # The CoroutineHandler-shaped Cls is registered under the seed's module_path
    # (the resolveModule lookup key); the seed's script_guid keys plan.modules.
    # Lua long-bracket levels use ``=`` signs only (``[==[ ... ]==]``); bump the
    # level until the close sequence does not occur in the plan source.
    delim = "=="
    while f"]{delim}]" in plan_source or f"[{delim}[" in plan_source:
        delim += "="
    scenario = textwrap.dedent(f"""\
        local PLAN_SOURCE = [{delim}[
{plan_source}
]{delim}]
        local Plan
        do
            local chunk, err = loadstring(PLAN_SOURCE, "SceneRuntimePlan")
            assert(chunk, "encoded plan did not parse as Luau: " .. tostring(err))
            Plan = chunk()
        end
        local GUID = {guid!r}
        -- The encoded GUID join: the resolver-produced script_guid must be a key
        -- in the encoded plan.modules (bare or bracketed key, same string at
        -- runtime). A rekey to module_path would make this nil -> the shim's
        -- presence guard would warn + skip and the singleton would never awake.
        print("GUID_IN_MODULES=" .. tostring(Plan.modules[GUID] ~= nil))
        print("SEED_GUID_MATCHES=" .. tostring(Plan.lazy_singletons[1].script_guid == GUID))

        local Cls = makeCoroutineHandler("m_Instance")
        local modulesByPath = {{ [{module_path!r}] = Cls }}
        local services = servicesFor(modulesByPath)
        local engine = SceneRuntime.new(services, Plan)
        print("BEFORE_NIL=" .. tostring(Cls.getInstance() == nil))
        SceneRuntime.seedLazySingletons(Plan, services, engine, "client")
        print("AFTER_NONNIL=" .. tostring(Cls.getInstance() ~= nil))
        print("AWAKE_COUNT=" .. tostring(Cls.awakeCount))
        dumpLogs()
    """)
    out = _run(scenario)
    assert "GUID_IN_MODULES=true" in out, out
    assert "SEED_GUID_MATCHES=true" in out, out
    assert "BEFORE_NIL=true" in out      # the bug state before seeding
    assert "AFTER_NONNIL=true" in out    # constructed through the real encoder+shim
    assert "AWAKE_COUNT=1" in out        # exactly one instance
    assert "WARN_COUNT=0" in out         # no fail-soft skip path taken
