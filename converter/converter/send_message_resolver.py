"""Resolve Unity ``recv.SendMessage("M", ...)`` / ``BroadcastMessage`` dispatches
from raw C# source into deterministic per-module FACTS.

Pre-transpile pass for generic mode: before a C# script reaches the AI, every
DIRECT ``SendMessage``/``BroadcastMessage`` call site is parsed into a
``SendMessageDispatchFact`` carrying the method name, the gameplay arguments
(any trailing ``SendMessageOptions.*`` token STRIPPED), and the dispatch kind
(``send`` | ``broadcast``). Slice 1.3 threads the per-module fact subset into the
reprompt loop and the verifier, which assert each fact has a matching
``host:sendMessage``/``host:broadcastMessage`` call in the produced Luau.

The classifier/normalizer (`classify_dispatch`) is this module's SINGLE source of
truth for "given a SendMessage/BroadcastMessage call, what is its ``(kind, method,
gameplay_args)``": ``_resolve_script`` runs it on every call site, so every fact
this producer emits carries that one normalization. The downstream consumers do
NOT re-classify the C#; they bind to the FACTS instead:

  - the verifier (``runtime_contract``) consumes the ``SendMessageDispatchFact``
    tuple and shares the ``SEND``/``BROADCAST`` kind constants, deriving each
    emitted Luau call's gameplay arity as ``len(luau_args) - 2`` (drop the
    receiver + method-name positionals) — algebraically the same value
    ``classify_dispatch`` records as ``len(gameplay_args)``;
  - the slice 1.3 prompt directive (in ``code_transpiler``) restates the same
    normalization (preserve receiver first, method-name string second, strip a
    trailing ``SendMessageOptions.*``) as prose.

So producer, prompt, and checker stay aligned on what a dispatch means via the
shared constants + the fact contract, never a duplicated re-implementation.

OverlapSphere exclusion (alias-traced): the existing ``playersInRadius`` prompt
directive (the #201 mine-instakill fix) authoritatively handles the
``Physics.OverlapSphere(...)`` foreach ``col.SendMessage("TakeDamage", dmg)``
radius shape. This producer therefore emits NO fact for a dispatch whose receiver
is the iteration variable of a ``foreach`` whose collection traces (<=1 local
alias hop) to a ``Physics.OverlapSphere(...)`` initializer — keyed on the SEMANTIC
shape, not one brittle spelling.

Pure: reads sources, returns values. No game-specific names. The C# is raw text
(regex/scan, no Roslyn AST), so matching is code-position-aware
(comments/strings/char-literals are skipped) via the shared ``_cs_pos_is_code``
helper in ``cs_text_scan``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from converter.cs_text_scan import _canon_key, _cs_pos_is_code
from unity.script_analyzer import ScriptInfo

# Dispatch kinds.
SEND = "send"
BROADCAST = "broadcast"

# The two C# dispatch call names, mapped to their fact ``kind``.
_CALL_KIND: dict[str, str] = {
    "SendMessage": SEND,
    "BroadcastMessage": BROADCAST,
}


@dataclass(frozen=True, slots=True)
class SendMessageDispatchFact:
    """One DIRECT ``SendMessage``/``BroadcastMessage`` dispatch obligation.

    ``method`` is the dispatched method name (the first string-literal argument,
    unquoted). ``gameplay_args`` is the trailing argument text in source order
    with any ``SendMessageOptions.*`` token removed — its length is the gameplay
    arity the verifier matches (0/1/2 in the real corpus). ``kind`` is ``send``
    or ``broadcast``.
    """

    source_path: str  # canonical .cs path key (str(path.resolve()) or raw)
    method: str  # the dispatched method name, unquoted ("GetItem")
    gameplay_args: tuple[str, ...]  # trailing args, SendMessageOptions stripped
    kind: str  # SEND | BROADCAST


SendMessageMap = dict[str, tuple[SendMessageDispatchFact, ...]]


# C# matchers --------------------------------------------------------------

# A SendMessage / BroadcastMessage call NAME followed by ``(``. The receiver (if
# any) and the argument list are parsed structurally around this anchor, so the
# matcher itself only needs to find the call token at a code position. ``(?<![\w.])``
# rejects a longer identifier tail (``MySendMessage(``) but ALLOWS a leading
# receiver dot (``recv.SendMessage(``) — the dot is handled by the receiver parse.
_CALL_RE = re.compile(r"(?<![\w])(SendMessage|BroadcastMessage)\s*\(")

# A C# string literal (regular or verbatim) — used to read the FIRST argument as
# the method name. ``@"..."`` doubles quotes to escape; ``"..."`` uses ``\``.
_VERBATIM_STR_RE = re.compile(r'@"(?:[^"]|"")*"')
_REGULAR_STR_RE = re.compile(r'"(?:\\.|[^"\\])*"')

# A trailing ``SendMessageOptions.<Member>`` token to strip from the arg list.
_OPTIONS_RE = re.compile(r"^SendMessageOptions\s*\.\s*[A-Za-z_]\w*$")

# ``Physics.OverlapSphere(`` initializer — the collection an excluded foreach
# iterates over (directly or via <=1 local alias hop).
_OVERLAP_SPHERE_RE = re.compile(r"\bPhysics\s*\.\s*OverlapSphere\s*\(")


def classify_dispatch(call_name: str, raw_args: str) -> tuple[str, str, tuple[str, ...]] | None:
    """SHARED classifier/normalizer — the single source of truth for what a
    ``SendMessage``/``BroadcastMessage`` call dispatches.

    ``call_name`` is the literal call token (``SendMessage`` | ``BroadcastMessage``);
    ``raw_args`` is the source text BETWEEN the call's parentheses (no enclosing
    parens). Returns ``(kind, method, gameplay_args)`` where ``kind`` is
    ``send``/``broadcast``, ``method`` is the unquoted first string-literal
    argument, and ``gameplay_args`` is the remaining argument text in order with
    any trailing ``SendMessageOptions.*`` token removed (so
    ``("TakeDamage", DontRequireReceiver)`` -> arity 0;
    ``("ToggleDoor", true, DontRequireReceiver)`` -> arity 1). Returns ``None``
    when the call is not a recognizable dispatch (unknown call name, or a first
    argument that is not a plain string literal — a dynamic method name the
    verifier cannot key on).

    Pure; no side effects. ``_resolve_script`` calls THIS function for every
    dispatch, so the facts it emits carry one normalization; the verifier then
    consumes those ``SendMessageDispatchFact`` records (and the shared
    ``SEND``/``BROADCAST`` constants) rather than re-classifying the C# itself.
    """
    kind = _CALL_KIND.get(call_name)
    if kind is None:
        return None
    args = _split_args(raw_args)
    if not args:
        return None
    method = _string_literal_value(args[0])
    if method is None:
        return None  # non-literal method name -> not statically keyable
    rest = [a for a in args[1:] if not _OPTIONS_RE.match(a)]
    return kind, method, tuple(rest)


def _split_args(raw_args: str) -> list[str]:
    """Split a C# argument-list string on TOP-LEVEL commas, returning each
    argument trimmed. Respects nesting of ``()``/``[]``/``{}``/``<>`` and skips
    commas inside string/char literals so a generic call
    (``FindAnyObjectByType<A, B>()``) or a nested call argument is one token.
    An all-whitespace / empty input yields ``[]``."""
    if raw_args.strip() == "":
        return []
    parts: list[str] = []
    depth = 0
    angle = 0
    i = 0
    n = len(raw_args)
    start = 0
    while i < n:
        ch = raw_args[i]
        if ch in '"@':
            j = _skip_string(raw_args, i)
            if j != i:
                i = j
                continue
        if ch == "'":
            i = _skip_char_literal(raw_args, i)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "<":
            angle += 1
        elif ch == ">":
            if angle > 0:
                angle -= 1
        elif ch == "," and depth == 0 and angle == 0:
            parts.append(raw_args[start:i].strip())
            start = i + 1
        i += 1
    parts.append(raw_args[start:].strip())
    return [p for p in parts if p != ""]


def _skip_string(text: str, i: int) -> int:
    """If a string literal opens at ``i`` (regular ``"`` or verbatim ``@"``),
    return the index just past its closing quote; else return ``i`` unchanged."""
    if text[i] == "@":
        m = _VERBATIM_STR_RE.match(text, i)
        return m.end() if m else i
    if text[i] == '"':
        m = _REGULAR_STR_RE.match(text, i)
        return m.end() if m else i
    return i


def _skip_char_literal(text: str, i: int) -> int:
    """Return the index just past a ``'...'`` char literal opening at ``i``
    (``\\`` escapes). If unterminated, advance one char to guarantee progress."""
    n = len(text)
    j = i + 1
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "'":
            return j + 1
        j += 1
    return i + 1


def _string_literal_value(arg: str) -> str | None:
    """If ``arg`` is EXACTLY a single C# string literal, return its unquoted
    value; else None (a non-literal expression — a dynamic method name)."""
    m = _VERBATIM_STR_RE.fullmatch(arg)
    if m is not None:
        body = arg[2:-1]
        return body.replace('""', '"')  # verbatim doubles quotes to escape
    m = _REGULAR_STR_RE.fullmatch(arg)
    if m is not None:
        return _unescape_regular(arg[1:-1])
    return None


def _unescape_regular(body: str) -> str:
    """Unescape the common C# regular-string escapes inside a method-name literal
    (``\\"`` ``\\\\`` ``\\n`` ``\\t`` ``\\r`` ``\\0``); leave any other ``\\x`` as
    its literal char. Method names are plain identifiers in practice, so this is
    a faithful-enough decode for the keyed name."""
    out: list[str] = []
    i = 0
    n = len(body)
    simple = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", '"': '"', "\\": "\\"}
    while i < n:
        ch = body[i]
        if ch == "\\" and i + 1 < n:
            out.append(simple.get(body[i + 1], body[i + 1]))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# Argument-list extraction --------------------------------------------------


def _matching_paren(source: str, open_idx: int) -> int | None:
    """Given the index of an opening ``(`` in ``source``, return the index of its
    matching ``)``, skipping string/char literals and nested parens. None if
    unbalanced (the call is malformed/truncated)."""
    depth = 0
    i = open_idx
    n = len(source)
    while i < n:
        ch = source[i]
        if ch in '"@':
            j = _skip_string(source, i)
            if j != i:
                i = j
                continue
        if ch == "'":
            i = _skip_char_literal(source, i)
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


# Receiver parsing ----------------------------------------------------------


def _receiver_before(source: str, dot_or_call_pos: int) -> str | None:
    """The receiver expression immediately to the LEFT of a ``.SendMessage`` /
    ``.BroadcastMessage`` call, or None for a bare (implicit-this) call.

    ``dot_or_call_pos`` is the index of the call NAME's first char. If the
    nearest preceding non-whitespace char is not a ``.``, the call is bare (no
    receiver). Otherwise walk left over the dotted/indexed/parenthesized
    receiver chain (``other``, ``hit.collider``, ``doors[number]``,
    ``FindAnyObjectByType<GameManager>()``) and return its source text trimmed."""
    k = dot_or_call_pos
    while k > 0 and source[k - 1] in " \t\r\n":
        k -= 1
    if k == 0 or source[k - 1] != ".":
        return None  # bare call -> implicit this
    # Walk left from the ``.`` over a receiver chain. Track bracket/paren depth
    # so the WHOLE chain (incl. an index ``[n]`` or a call ``(...)``) is captured.
    end = k - 1  # index of the ``.``
    i = end - 1
    depth = 0
    while i >= 0:
        ch = source[i]
        if ch in ")]}":
            depth += 1
            i -= 1
            continue
        if ch in "([{":
            if depth == 0:
                break
            depth -= 1
            i -= 1
            continue
        if depth > 0:
            i -= 1
            continue
        if ch.isalnum() or ch in "_.<>":
            i -= 1
            continue
        if ch in " \t":
            # whitespace inside a chain (``foo . bar``) is part of it only if a
            # chain char precedes it; stop otherwise.
            j = i
            while j > 0 and source[j - 1] in " \t":
                j -= 1
            if j > 0 and (source[j - 1].isalnum() or source[j - 1] in "_.>)]"):
                i = j - 1
                continue
            break
        break
    recv = source[i + 1 : end].strip()
    return recv or None


# OverlapSphere foreach exclusion ------------------------------------------


@dataclass(frozen=True, slots=True)
class _ForeachBinding:
    """One ``foreach`` loop binding parsed from source: the iteration VARIABLE,
    the COLLECTION expression, the ``header_start`` index of the ``foreach``
    keyword (the upper bound for an in-scope collection declaration), and the
    source span ``[body_start, body_end)`` of the loop body within which the
    variable is in scope."""

    var: str
    collection: str
    header_start: int
    body_start: int
    body_end: int


# foreach header up to (and including) ``in``: ``foreach ( <type|var> <ident> in``.
# The collection EXPRESSION after ``in`` runs to the matching header ``)`` and is
# read by a paren-balanced scan (NOT a regex) so an inline call collection
# (``foreach (var c in Physics.OverlapSphere(p, 2))``) is captured whole.
_FOREACH_HEAD_RE = re.compile(
    r"\bforeach\s*\(\s*(?:var|[\w.<>\[\],\s]*?[\w>\]])\s+([A-Za-z_]\w*)\s+in\s+"
)


def _foreach_bindings(source: str) -> list[_ForeachBinding]:
    """Parse all ``foreach`` loops at code positions, each with its loop body
    span. The body is the brace-delimited block (or single statement) following
    the header; the variable is in scope only within that span. Pure."""
    out: list[_ForeachBinding] = []
    n = len(source)
    for m in _FOREACH_HEAD_RE.finditer(source):
        if not _cs_pos_is_code(source, m.start()):
            continue
        var = m.group(1)
        # The header ``(`` is the first ``(`` after ``foreach``; the collection
        # runs from just past ``in`` to that paren's match. Scan balance-aware.
        open_idx = source.find("(", m.start())
        header_close = _matching_paren(source, open_idx) if open_idx != -1 else None
        if header_close is None or header_close < m.end():
            continue  # malformed header
        collection = source[m.end() : header_close].strip()
        # Find the body span starting just after the header ``)``.
        i = header_close + 1
        while i < n and source[i] in " \t\r\n":
            i += 1
        if i < n and source[i] == "{":
            close = _matching_brace(source, i)
            body_start, body_end = i + 1, (close if close is not None else n)
        else:
            # braceless single statement -> body runs to the next ``;`` (or EOF).
            semi = source.find(";", i)
            body_start, body_end = i, (semi + 1 if semi != -1 else n)
        out.append(
            _ForeachBinding(var, collection, m.start(), body_start, body_end)
        )
    return out


def _matching_brace(source: str, open_idx: int) -> int | None:
    """Index of the ``}`` matching the ``{`` at ``open_idx``, skipping
    string/char literals + nested braces. None if unbalanced."""
    depth = 0
    i = open_idx
    n = len(source)
    while i < n:
        ch = source[i]
        if ch in '"@':
            j = _skip_string(source, i)
            if j != i:
                i = j
                continue
        if ch == "'":
            i = _skip_char_literal(source, i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _encloses(source: str, decl_pos: int, foreach_pos: int) -> bool:
    """True iff the block that lexically contains ``decl_pos`` still encloses
    ``foreach_pos`` — i.e. no ``}`` between the two positions closes a block that
    was open at ``decl_pos``. This makes "in scope for the foreach" precise: a
    declaration inside an already-closed SIBLING block (a different method or an
    earlier ``{ ... }``) does NOT reach the foreach. Counts only ``}`` at code
    positions (string/comment braces are ignored via ``_cs_pos_is_code``).

    Requires ``decl_pos < foreach_pos`` (declared before the foreach)."""
    if decl_pos >= foreach_pos:
        return False
    net_close = 0  # how many enclosing-at-decl blocks have closed before foreach
    i = decl_pos
    while i < foreach_pos:
        ch = source[i]
        if ch == "{":
            if _cs_pos_is_code(source, i):
                net_close -= 1  # a block opened AFTER decl; its close is balanced
        elif ch == "}":
            if _cs_pos_is_code(source, i):
                net_close += 1
                if net_close > 0:
                    return False  # closed a block that enclosed decl_pos
        i += 1
    return True


def _collection_is_overlap_sphere(
    source: str, collection: str, foreach_pos: int
) -> bool:
    """True iff ``collection`` is (or aliases, in <=1 local hop, a local whose
    nearest-preceding in-scope declaration initializes it from) a
    ``Physics.OverlapSphere(...)`` result, FOR the ``foreach`` at ``foreach_pos``.

    Direct: the collection text itself contains ``Physics.OverlapSphere(`` (the
    inline form ``foreach (var c in Physics.OverlapSphere(...))``).

    Aliased (the real corpus shape): the collection is a bare local symbol whose
    NEAREST-PRECEDING in-scope binding is ``<Type|var> <collection> =
    Physics.OverlapSphere(...)``. "In scope" = declared before the foreach AND in
    a block that still encloses it (``_encloses``) — so an unrelated same-named
    ``cols = OverlapSphere(...)`` in a different method/sibling block does NOT
    suppress this dispatch. Only a SINGLE alias hop is followed.

    Conservative bias: a later re-binding of ``collection`` to a NON-OverlapSphere
    value (e.g. ``cols = targets``) that sits nearer the foreach wins, so we KEEP
    (emit the fact) rather than exclude on a stale earlier OverlapSphere decl. We
    exclude only when the confirmed nearest-preceding in-scope binding is an
    OverlapSphere initializer."""
    if _OVERLAP_SPHERE_RE.search(collection) is not None:
        return True
    if not re.fullmatch(r"[A-Za-z_]\w*", collection):
        return False  # not a bare symbol -> no alias to trace
    # Find the NEAREST-PRECEDING in-scope binding of ``collection`` (a declaration
    # ``<Type|var> collection = ...`` or a plain assignment ``collection = ...``),
    # then check whether that one binding's initializer is Physics.OverlapSphere.
    # The LHS must be the BARE local identifier: a member/field assignment such as
    # ``this.cols = ...`` or ``obj.cols = ...`` is a DIFFERENT lvalue and must not
    # be mistaken for the foreach collection's binding (a leading ``.`` or word
    # char before the identifier rejects the member-access and partial-name cases).
    bind_re = re.compile(
        r"(?<![\w.])"
        + r"(?:\b(?:var|[\w.<>\[\],\s]*?[\w>\]])\s+)?"
        + re.escape(collection)
        + r"\s*=\s*(?!=)"
    )
    nearest_start = -1
    nearest_init_at = -1  # index just past the matched ``=`` of the nearest binding
    for m in bind_re.finditer(source):
        if m.end() > foreach_pos:
            break  # bindings are ordered; nothing after the foreach is preceding
        if not _cs_pos_is_code(source, m.start()):
            continue
        if not _encloses(source, m.start(), foreach_pos):
            continue  # in a closed sibling block / different method
        if m.start() > nearest_start:
            nearest_start = m.start()
            nearest_init_at = m.end()
    if nearest_start == -1:
        return False  # no in-scope binding -> cannot confirm the OverlapSphere link
    return _OVERLAP_SPHERE_RE.match(source, nearest_init_at) is not None


def _is_overlap_sphere_iter_receiver(
    source: str, recv: str, call_pos: int, bindings: list[_ForeachBinding]
) -> bool:
    """True iff ``recv`` (the dispatch receiver, at ``call_pos``) is the
    iteration variable of an enclosing ``foreach`` whose collection traces to a
    ``Physics.OverlapSphere(...)`` initializer. This is the EXCLUSION predicate —
    a True drops the fact so ``playersInRadius`` stays authoritative (#201)."""
    if not re.fullmatch(r"[A-Za-z_]\w*", recv):
        return False  # only a bare iteration-var receiver is excludable
    for b in bindings:
        if b.var != recv:
            continue
        if not (b.body_start <= call_pos < b.body_end):
            continue  # the dispatch is outside this loop's body
        if _collection_is_overlap_sphere(source, b.collection, b.header_start):
            return True
    return False


# Per-script + map resolution ----------------------------------------------


def _resolve_script(source: str, source_path: str) -> tuple[SendMessageDispatchFact, ...]:
    """Parse all DIRECT SendMessage/BroadcastMessage dispatches in one C# source
    into facts, EXCLUDING any whose receiver is an OverlapSphere-foreach
    iteration variable. Pure; code-position-aware."""
    bindings = _foreach_bindings(source)
    facts: list[SendMessageDispatchFact] = []
    for m in _CALL_RE.finditer(source):
        if not _cs_pos_is_code(source, m.start()):
            continue
        open_idx = m.end() - 1  # index of the ``(`` (the matcher ends just past it)
        close_idx = _matching_paren(source, open_idx)
        if close_idx is None:
            continue  # unbalanced -> malformed call, skip
        raw_args = source[open_idx + 1 : close_idx]
        classified = classify_dispatch(m.group(1), raw_args)
        if classified is None:
            continue
        kind, method, gameplay_args = classified
        recv = _receiver_before(source, m.start(1))
        if recv is not None and _is_overlap_sphere_iter_receiver(
            source, recv, m.start(), bindings
        ):
            continue  # OverlapSphere-foreach radius case -> playersInRadius owns it
        facts.append(
            SendMessageDispatchFact(
                source_path=source_path,
                method=method,
                gameplay_args=gameplay_args,
                kind=kind,
            )
        )
    return tuple(facts)


def build_send_message_map(script_infos: list[ScriptInfo]) -> SendMessageMap:
    """For every script, parse its DIRECT ``recv.SendMessage("M", ...)`` /
    ``BroadcastMessage`` and bare ``SendMessage(...)`` (implicit-this) call sites
    into ``SendMessageDispatchFact`` records, keyed by canonical .cs path.

    The OverlapSphere-foreach ``TakeDamage`` radius shape is EXCLUDED (the
    existing ``playersInRadius`` prompt directive owns it — no #201 regression).
    A script with >= 1 emitted fact is present in the map; a script with no
    emittable dispatch is absent. Pure."""
    result: SendMessageMap = {}
    for info in script_infos:
        path = getattr(info, "path", None)
        if path is None:
            continue
        key = _canon_key(path)
        try:
            source = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        facts = _resolve_script(source, key)
        if facts:
            result[key] = facts
    return result
