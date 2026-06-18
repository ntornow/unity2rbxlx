"""Phase 1 consumable materialization — host-level behavioral tests for
``SceneRuntime.seedConsumableDatabases`` in ``runtime/scene_runtime.luau`` plus
the autogen entrypoint wiring.

The shim half drives the REAL host runtime under standalone ``luau`` with a
focused service surface (``resolveModule`` keyed by module path; ``warn``
captured). Each scenario mirrors the REAL contract:
  * a database table whose declared array field starts empty (filled by the
    shim) and a once-only ``Load()`` drain that calls a method on each element;
  * subclass modules with a ``.new(config)`` that reads scalars and IGNORES
    asset-ref fields (host-injected post-construction), exactly as the
    transpiled ``Consumable.new`` does.

Covers (slice 1.2 acceptance):
  * N elements → ``db[array_field]`` holds N instances answering
    ``:GetConsumableType()`` (NEVER strings).
  * constructor contract — scalars arrive via ``.new(config)``; asset-ref
    fields are assigned POST-construction; ``.gameObject`` is the prefab id.
  * a missing-``.new`` (or pruned) subclass element is DROPPED + warned, not
    stringed.
  * empty seed → empty array, no crash; absent plan key → generic no-op.
  * idempotency — a SECOND shim call on the same db is a no-op via the
    ``db._consumablesSeeded`` once-guard (contents + identity unchanged).

The autogen half asserts the entrypoint emission: both client + server
entrypoints call ``seedConsumableDatabases`` AFTER ``SceneRuntime.new`` and
BEFORE ``engine:start`` (the load-bearing order — the array must hold objects
before the consumer's once-only ``Load()`` runs).
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

        -- A ConsumableDatabase mirroring the REAL transpiled drain: ``consumbales``
        -- starts empty at module scope (the shim fills it), and a once-only
        -- ``Load()`` builds a dict keyed by each element's GetConsumableType().
        -- Crucially, Load() CALLS a METHOD on each element — so if the array held
        -- a string (the pre-fix bug) Load() would crash, exactly like OnEnable.
        local function makeConsumableDatabase()
            local db = {{}}
            db.consumbales = {{}}
            local dict = nil
            function db.Load()
                if dict == nil then
                    dict = {{}}
                    for _, c in ipairs(db.consumbales) do
                        dict[c:GetConsumableType()] = c   -- method call on element
                    end
                end
            end
            function db.GetConsumableType(key)
                if dict == nil then return nil end
                return dict[key]
            end
            function db.count()
                local n = 0
                for _ in pairs(dict or {{}}) do n = n + 1 end
                return n
            end
            return db
        end

        -- A subclass module with the REAL constructor contract: ``.new(config)``
        -- reads ONLY scalars (duration / canBeSpawned); it does NOT read the
        -- asset-ref fields (icon / ActivatedParticleReference) — those are
        -- host-injected post-construction. Each instance answers the data
        -- methods with the coded constant for ``typeName``.
        local function makeSubclass(typeName, priceConst)
            local Cls = {{}}
            Cls.__index = Cls
            function Cls.new(config)
                local self = setmetatable({{}}, Cls)
                -- scalars read THROUGH .new (the typed ctor; it does the 0/1
                -- coercion in real code — here we just record what it received)
                self.duration = config.duration
                self.ctorSawCanBeSpawned = config.canBeSpawned
                -- .new explicitly does NOT read asset-ref fields:
                self.icon = nil
                self.ActivatedParticleReference = nil
                return self
            end
            function Cls:GetConsumableType() return typeName end
            function Cls:GetPrice() return priceConst end
            return Cls
        end

        local function servicesFor(modules)
            return {{
                warn = logWarn,
                resolveModule = function(_id, path) return modules[path] end,
            }}
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


# A plan with two materializable elements (CoinMagnet + ExtraLife shapes). The
# ``modules`` registry maps each subclass stem -> its module_path (the shim
# joins class_stem -> module_path through this exact registry, as production
# does). The element ``fields_literal`` is a STRING holding a Luau table literal
# — exactly what ``_plan_to_luau`` emits (it quotes the Python str the resolver
# produced) — so the shim must realize it via loadstring.
_TWO_ELEMENT_PLAN = """\
local db = makeConsumableDatabase()
local CoinMagnet = makeSubclass("COIN_MAG", 750)
local ExtraLife = makeSubclass("EXTRALIFE", 2000)
local modules = {
    ["ServerStorage.ConsumableDatabase"] = db,
    ["ServerStorage.CoinMagnet"] = CoinMagnet,
    ["ServerStorage.ExtraLife"] = ExtraLife,
}
local plan = {
    -- the stem -> module_path registry the shim joins through
    modules = {
        guidA = { stem = "CoinMagnet", class_name = "CoinMagnet",
                  module_path = "ServerStorage.CoinMagnet" },
        guidB = { stem = "ExtraLife", class_name = "ExtraLife",
                  module_path = "ServerStorage.ExtraLife" },
        guidDb = { stem = "ConsumableDatabase", class_name = "ConsumableDatabase",
                  module_path = "ServerStorage.ConsumableDatabase" },
    },
    consumable_db_seeds = {
        {
            db_module_path = "ServerStorage.ConsumableDatabase",
            array_field = "consumbales",
            elements = {
                {
                    class_stem = "CoinMagnet",
                    prefab_id = "g1:Assets/CoinMagnet.prefab",
                    fields_literal = "{ duration = 10, canBeSpawned = 1, icon = \\"rbxassetid://111\\" }",
                },
                {
                    class_stem = "ExtraLife",
                    prefab_id = "g2:Assets/ExtraLife.prefab",
                    fields_literal = "{ duration = 0.01, canBeSpawned = 0, ActivatedParticleReference = \\"g3:Assets/FX.prefab\\" }",
                },
            },
        },
    },
}
"""


@_luau_marker
def test_n_elements_materialize_to_instances_not_strings():
    """N elements → ``db.consumbales`` holds N live objects that answer
    ``:GetConsumableType()`` (NEVER strings). Self-satisfaction-proof: the DB's
    own ``Load()`` CALLS the method on each element, so a string slot would
    crash — the pre-fix OnEnable bug."""
    out = _run(_TWO_ELEMENT_PLAN + """
SceneRuntime.seedConsumableDatabases(plan, servicesFor(modules))
print("LEN=" .. tostring(#db.consumbales))
print("E1_IS_STRING=" .. tostring(type(db.consumbales[1]) == "string"))
print("E2_IS_STRING=" .. tostring(type(db.consumbales[2]) == "string"))
db.Load()   -- would crash if any slot were a string
print("CM_PRESENT=" .. tostring(db.GetConsumableType("COIN_MAG") ~= nil))
print("XL_PRESENT=" .. tostring(db.GetConsumableType("EXTRALIFE") ~= nil))
print("CM_PRICE=" .. tostring(db.GetConsumableType("COIN_MAG"):GetPrice()))
""")
    assert "LEN=2" in out
    assert "E1_IS_STRING=false" in out
    assert "E2_IS_STRING=false" in out
    assert "CM_PRESENT=true" in out
    assert "XL_PRESENT=true" in out
    assert "CM_PRICE=750" in out


@_luau_marker
def test_constructor_field_contract_scalars_via_new_assetrefs_post_construction():
    """Constructor contract: scalars (duration / canBeSpawned) arrive via
    ``.new(config)``; asset-ref fields (icon / ActivatedParticleReference) — NOT
    read by ``.new`` — are assigned POST-construction onto the instance; and
    ``.gameObject`` is the element's prefab id."""
    out = _run(_TWO_ELEMENT_PLAN + """
SceneRuntime.seedConsumableDatabases(plan, servicesFor(modules))
local cm = db.consumbales[1]
local xl = db.consumbales[2]
-- scalars flowed THROUGH .new (ctor recorded what it received):
print("CM_CTOR_DURATION=" .. tostring(cm.ctorSawCanBeSpawned ~= nil and cm.duration))
print("CM_CTOR_SAW_SPAWN=" .. tostring(cm.ctorSawCanBeSpawned))
-- asset-ref injected POST-construction (.new set it nil; shim assigned it):
print("CM_ICON=" .. tostring(cm.icon))
print("XL_FX=" .. tostring(xl.ActivatedParticleReference))
-- gameObject is the prefab id:
print("CM_GO=" .. tostring(cm.gameObject))
print("XL_GO=" .. tostring(xl.gameObject))
""")
    # scalars reached the constructor
    assert "CM_CTOR_DURATION=10" in out
    assert "CM_CTOR_SAW_SPAWN=1" in out
    # asset-refs present on the instance (the only path that carries them)
    assert "CM_ICON=rbxassetid://111" in out
    assert "XL_FX=g3:Assets/FX.prefab" in out
    # gameObject = prefab id
    assert "CM_GO=g1:Assets/CoinMagnet.prefab" in out
    assert "XL_GO=g2:Assets/ExtraLife.prefab" in out


@_luau_marker
def test_missing_new_class_element_dropped_not_stringed():
    """An element whose subclass module fails to resolve (pruned, or no ``.new``)
    is DROPPED + warned — never left as a prefab-id string in the array."""
    # Point ExtraLife's module at a non-table (resolve miss) -> its element drops.
    scenario = _TWO_ELEMENT_PLAN.replace(
        '["ServerStorage.ExtraLife"] = ExtraLife,',
        '["ServerStorage.ExtraLife"] = nil,  -- resolve miss',
    ) + """
SceneRuntime.seedConsumableDatabases(plan, servicesFor(modules))
print("LEN=" .. tostring(#db.consumbales))
print("ONLY_CM=" .. tostring(db.consumbales[1]:GetConsumableType()))
print("NO_STRING=" .. tostring(type(db.consumbales[1]) ~= "string"))
db.Load()   -- must not crash: no string slot survived
dumpLogs()
"""
    out = _run(scenario)
    assert "LEN=1" in out                       # the missing element was dropped
    assert "ONLY_CM=COIN_MAG" in out
    assert "NO_STRING=true" in out
    assert any(
        line.startswith("WARN_LINE=[consumable-seed] subclass module did not resolve")
        for line in out.splitlines()
    ), out


@_luau_marker
def test_no_new_method_drops_element():
    """A resolved subclass module that lacks ``.new`` (not constructable) is
    dropped + warned (the ``Cls.new`` function-type guard)."""
    scenario = _TWO_ELEMENT_PLAN.replace(
        'local ExtraLife = makeSubclass("EXTRALIFE", 2000)',
        'local ExtraLife = { GetConsumableType = function() return "X" end }  -- no .new',
    ) + """
SceneRuntime.seedConsumableDatabases(plan, servicesFor(modules))
print("LEN=" .. tostring(#db.consumbales))
dumpLogs()
"""
    out = _run(scenario)
    assert "LEN=1" in out
    assert any(
        line.startswith("WARN_LINE=[consumable-seed] subclass module did not resolve")
        for line in out.splitlines()
    ), out


@_luau_marker
def test_empty_elements_seeds_empty_array_no_crash():
    """A seed with zero elements assigns an EMPTY array (``Load()`` builds an
    empty dict instead of iterating a stale string array — no crash)."""
    scenario = """
local db = makeConsumableDatabase()
db.consumbales = { "g:Assets/Stale.prefab" }  -- a stale pre-seed string slot
local plan = {
    modules = {},
    consumable_db_seeds = {
        {
            db_module_path = "ServerStorage.ConsumableDatabase",
            array_field = "consumbales",
            elements = {},
        },
    },
}
local modules = { ["ServerStorage.ConsumableDatabase"] = db }
SceneRuntime.seedConsumableDatabases(plan, servicesFor(modules))
print("LEN=" .. tostring(#db.consumbales))   -- the stale string was REPLACED by {}
db.Load()
print("COUNT=" .. tostring(db.count()))
print("OK=true")
"""
    out = _run(scenario)
    assert "LEN=0" in out          # fresh empty array, stale string gone
    assert "COUNT=0" in out
    assert "OK=true" in out


@_luau_marker
def test_generic_noop_when_no_seeds():
    """An absent or empty ``consumable_db_seeds`` is a clean generic no-op."""
    out = _run("""
local services = servicesFor({})
SceneRuntime.seedConsumableDatabases({}, services)
SceneRuntime.seedConsumableDatabases({ consumable_db_seeds = {} }, services)
print("NOOP_OK=true")
""")
    assert "NOOP_OK=true" in out


@_luau_marker
def test_idempotency_second_call_is_a_noop_via_once_guard():
    """The per-DB ``_consumablesSeeded`` once-guard makes a SECOND shim call a
    no-op: the array identity AND contents are unchanged, and post-``Load()``
    state is not re-materialized (a second call must not rebuild fresh
    instances or clobber the drained array)."""
    out = _run(_TWO_ELEMENT_PLAN + """
SceneRuntime.seedConsumableDatabases(plan, servicesFor(modules))
local firstArray = db.consumbales
local firstCm = db.consumbales[1]
db.Load()   -- consumer drains; post-Load state must be preserved
-- second invocation (simulating the other entrypoint / a resume / a retry):
SceneRuntime.seedConsumableDatabases(plan, servicesFor(modules))
print("SAME_ARRAY=" .. tostring(db.consumbales == firstArray))   -- not reassigned
print("SAME_INSTANCE=" .. tostring(db.consumbales[1] == firstCm)) -- not rebuilt
print("LEN=" .. tostring(#db.consumbales))
print("GUARD=" .. tostring(db._consumablesSeeded))
""")
    assert "SAME_ARRAY=true" in out
    assert "SAME_INSTANCE=true" in out
    assert "LEN=2" in out
    assert "GUARD=true" in out


@_luau_marker
def test_warns_when_db_module_does_not_resolve_to_a_table():
    """Fail-loud sibling branch: a DB module that resolves to a non-table is
    skipped + warned (never indexed)."""
    out = _run("""
local plan = {
    modules = {},
    consumable_db_seeds = {
        { db_module_path = "ServerStorage.ConsumableDatabase",
          array_field = "consumbales", elements = {} },
    },
}
local modules = { ["ServerStorage.ConsumableDatabase"] = 42 }  -- non-table
SceneRuntime.seedConsumableDatabases(plan, servicesFor(modules))
print("NO_CRASH=true")
dumpLogs()
""")
    assert "NO_CRASH=true" in out
    assert any(
        line.startswith("WARN_LINE=[consumable-seed] database module did not resolve to a table:")
        for line in out.splitlines()
    ), out


@_luau_marker
def test_malformed_fields_literal_degrades_to_no_overrides_not_crash():
    """A ``fields_literal`` that does not evaluate to a table degrades to "no
    overrides" + WARN — the element is still materialized (``.new({})``), never a
    boot crash."""
    scenario = _TWO_ELEMENT_PLAN.replace(
        'fields_literal = "{ duration = 10, canBeSpawned = 1, icon = \\"rbxassetid://111\\" }",',
        'fields_literal = "this is not = a table",',
    ) + """
SceneRuntime.seedConsumableDatabases(plan, servicesFor(modules))
print("LEN=" .. tostring(#db.consumbales))
print("CM_TYPE=" .. tostring(db.consumbales[1]:GetConsumableType()))
dumpLogs()
"""
    out = _run(scenario)
    assert "LEN=2" in out                       # element NOT dropped
    assert "CM_TYPE=COIN_MAG" in out            # still constructed
    assert any(
        "[consumable-seed] fields literal did not" in line
        for line in out.splitlines()
    ), out


# ---------------------------------------------------------------------------
# Autogen entrypoint wiring (Python-side; no luau needed).
# ---------------------------------------------------------------------------

def _entrypoint_sources() -> tuple[str, str]:
    from converter import autogen

    client = autogen.generate_scene_runtime_client_entrypoint().source
    server = autogen.generate_scene_runtime_server_entrypoint().source
    return client, server


def test_both_entrypoints_call_seed_consumable_databases():
    client, server = _entrypoint_sources()
    assert "SceneRuntime.seedConsumableDatabases(Plan, services)" in client
    assert "SceneRuntime.seedConsumableDatabases(Plan, services)" in server


def test_seed_call_emitted_after_new_and_before_engine_start():
    """The load-bearing boot order: ``seedConsumableDatabases`` runs AFTER
    ``SceneRuntime.new`` (the engine + plan exist) and BEFORE ``engine:start``
    (the consumer's once-only ``Load()`` fires inside start's lifecycle, so the
    array must already hold materialized objects)."""
    for domain, source in zip(("client", "server"), _entrypoint_sources()):
        i_new = source.index("SceneRuntime.new(services, Plan)")
        i_seed = source.index("SceneRuntime.seedConsumableDatabases(Plan, services)")
        i_start = source.index(f'engine:start("{domain}")')
        assert i_new < i_seed < i_start, (
            f"{domain} entrypoint boot order wrong: "
            f"new={i_new} seed={i_seed} start={i_start}"
        )


def test_seed_consumable_after_seed_addressable():
    """Both shims run at boot; consumable seeding sits right after addressable
    seeding (the established slot), still before start."""
    for source in _entrypoint_sources():
        i_addr = source.index("SceneRuntime.seedAddressableDatabases(Plan, services)")
        i_cons = source.index("SceneRuntime.seedConsumableDatabases(Plan, services)")
        assert i_addr < i_cons


def test_consumable_db_seeds_in_plan_key_allowlist():
    """The plan key must be allowlisted (slice 1.1's edit) or the recomputed
    seed is elided from the emitted plan and the shim sees ``{}``."""
    from converter import autogen

    assert "consumable_db_seeds" in autogen._PLAN_KEYS_FOR_HOST


@_luau_marker
def test_real_emitted_plan_round_trips_through_shim():
    """Producer→consumer seam: render the seed through the REAL plan encoder
    (``generate_scene_runtime_plan_module`` → ``_plan_to_luau``, which QUOTES
    ``fields_literal`` as a string), then ``require`` that emitted plan inside the
    luau harness and drive the REAL shim against it. Reds if the encoder stops
    quoting the literal the way the shim's ``loadstring`` expects, or if the shim
    stops realizing it — locking the cross-slice contract, not a hand-built
    fixture."""
    from converter.autogen import generate_scene_runtime_plan_module

    sr = {
        "consumable_db_seeds": [
            {
                "db_module_path": "ServerStorage.ConsumableDatabase",
                "array_field": "consumbales",
                "elements": [
                    {
                        "class_stem": "CoinMagnet",
                        "prefab_id": "g1:Assets/CoinMagnet.prefab",
                        "fields_literal":
                            '{ duration = 10, canBeSpawned = 1, '
                            'icon = "rbxassetid://111" }',
                    },
                ],
            },
        ],
        "modules": {
            "guidA": {
                "stem": "CoinMagnet", "class_name": "CoinMagnet",
                "module_path": "ServerStorage.CoinMagnet",
            },
        },
    }
    plan_source = generate_scene_runtime_plan_module(sr).source
    # Embed the REAL emitted plan ModuleScript and require it in the harness.
    # Lua long-string delimiters only allow ``=`` between the brackets.
    delim = "===="
    while f"]{delim}]" in plan_source or f"[{delim}[" in plan_source:
        delim += "="
    embedded_plan = f"[{delim}[\n{plan_source}\n]{delim}]"
    scenario = f"""
local PLAN_SOURCE = {embedded_plan}
local plan = assert(loadstring(PLAN_SOURCE, "SceneRuntimePlan"))()

local db = makeConsumableDatabase()
local CoinMagnet = makeSubclass("COIN_MAG", 750)
local modules = {{
    ["ServerStorage.ConsumableDatabase"] = db,
    ["ServerStorage.CoinMagnet"] = CoinMagnet,
}}
SceneRuntime.seedConsumableDatabases(plan, servicesFor(modules))
print("LEN=" .. tostring(#db.consumbales))
db.Load()
local cm = db.GetConsumableType("COIN_MAG")
print("CM_PRESENT=" .. tostring(cm ~= nil))
print("CM_DURATION=" .. tostring(cm and cm.duration))
print("CM_ICON=" .. tostring(cm and cm.icon))
print("CM_GO=" .. tostring(cm and cm.gameObject))
"""
    out = _run(scenario)
    assert "LEN=1" in out
    assert "CM_PRESENT=true" in out
    assert "CM_DURATION=10" in out          # scalar realized from the quoted literal
    assert "CM_ICON=rbxassetid://111" in out  # asset-ref post-injected
    assert "CM_GO=g1:Assets/CoinMagnet.prefab" in out
