"""Did the attempt's overlay edits reach the build line it was verifying?

poly-7pwa.1. An op inherits the most recently named ``target`` directive
above it, so scope is decided by BYTE POSITION -- and the agent's natural
edit is to append. Both multi-target ports in DeltaPorts end with a
``target @main`` block, so an append lands in ``@main`` whatever the job
was for. A job on ``@2026Q3`` then writes its op, ``validate_dops``
passes (the file is well-formed), compose omits the op because it is
scoped elsewhere, and the build fails exactly as it did before the fix.

THE RULE IS "NOTHING LANDED", NOT "SOMETHING LANDED ELSEWHERE", and the
first version of this module got that wrong in a way that would have made
it useless. Authoring ops for another build line is legitimate and the
convention:

- ``target @2026Q3,@main`` expands to ONE PlanOp PER TARGET (semantic.py),
  so one correctly written line becomes two ops and the non-matching half
  looks stranded. lang/rust really has such a block.
- a per-target split authored in one attempt -- ``V= 1.96.1`` under
  ``@2026Q3``, ``V= 1.98.1`` under ``@main`` -- is exactly how these ports
  are meant to look, and half of it is always "for the other target".
- poly-7pwa.13's re-cut edits the op that already references a patch,
  which may be in another target's block.

Flagging those would make the check fire on correct work, and a check that
is right about noise gets ignored. So the finding is narrow: the attempt
added ops AND NONE of them apply on the target it was building. That is
precisely "the fix did not reach this build line", which is the cost the
bead is about.

NO BASELINE MEANS NO FINDING. "Added" only has meaning against a before.
A port with no overlay at attempt start (the bootstrap/convert flow writes
its header after) would otherwise have every other-target op in a freshly
and correctly authored two-target overlay reported.

Pure functions over overlay TEXT, deliberately: no env, no chroot, so the
rule is testable on its own and the callers keep the I/O.

INTERIM. poly-7pwa.8 replaces this module: its MISSED note replaces this
check's agent note through the state_notes list in attempt_loop.run, and
it moves payload_identity to dportsv3/engine/models.op_identity and
deletes this file.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Returned by a reader that could not read, as distinct from a file that
#: is not there. An unreadable overlay must not be judged as "empty
#: before", which would report every op in it as newly added.
UNREADABLE = "\x00unreadable\x00"

#: Payload keys worth naming in a finding, most specific first.
_SUBJECT_KEYS = ("dst", "name", "src", "path", "file", "var")


@dataclass(frozen=True)
class ScopeDrift:
    """An attempt whose overlay additions all missed its own build line."""

    target: str
    #: One line per added op, none of which apply here.
    stranded: tuple[str, ...] = ()
    #: Set when there was nothing to compare against, or the text could
    #: not be planned. A broken overlay is validate_dops's diagnostic and
    #: this must not invent a second one in different words.
    unavailable: str | None = None

    @property
    def ok(self) -> bool:
        """True when there is nothing to report.

        ``unavailable`` is NOT a problem -- it means the question could not
        be asked (no baseline, an overlay that does not plan), and the right
        response to that is silence, not a finding.
        """
        return not self.stranded

    def message(self) -> str:
        """One paragraph for a log or an operator, or '' when nothing."""
        if self.ok:
            return ""
        n = len(self.stranded)
        return (
            f"this attempt added {n} overlay op{'s' if n != 1 else ''} and "
            f"none of them apply on {self.target}, the build line it is "
            f"building. An op inherits the last `target` block above it, so "
            f"appending at the end of the file puts it in whatever block "
            f"happens to be last:\n"
            + "\n".join(f"  - {s}" for s in self.stranded)
            + f"\nMove them into the `target {self.target}` block. Nothing "
            f"else reports this: the overlay is valid and compose succeeds."
        )

    def note(self) -> str:
        """The same finding, addressed to the agent for its next attempt."""
        if self.ok:
            return ""
        return (
            f"SCOPE: your overlay edits did not reach {self.target}: every "
            f"op this job added is in another build line's block. An op takes "
            f"the scope of the last `target` line above it, so an append at "
            f"the end of the file lands in the last block. Move them into the "
            f"`target {self.target}` block and re-check with "
            f"get_effective_overlay. Added: "
            + "; ".join(self.stranded)
        )


def payload_identity(op) -> tuple:
    """What a PlanOp does, without its scope or its position.

    Read from the PlanOp's fields, never from ``to_dict()``: that spreads
    the payload over ``id``/``target``/``kind``, so a payload key named
    ``target`` would overwrite the op's scope. ``id`` is left out because
    op ids encode position (``op-<ordinal>-<kind>``), so inserting a line
    above an op would otherwise make every op below it look new. Shared
    with steps._build_line_brief.
    """
    return (op.kind, tuple(sorted(
        (k, repr(v)) for k, v in op.payload.items())))


def _key(op) -> tuple:
    """Identity of an op for set-difference purposes: scope plus payload."""
    return (op.target, payload_identity(op))


def _describe(op) -> str:
    scope = op.target or "@any"
    kind = op.kind or "?"
    subject = next(
        (str(op.payload[k]) for k in _SUBJECT_KEYS if op.payload.get(k)), ""
    )
    return f"scope {scope}: {kind}{' ' + subject if subject else ''}"


def _plan_ops(text: str) -> list | None:
    from dportsv3.engine.api import build_plan  # noqa: PLC0415

    try:
        result = build_plan(text, None)
    except Exception:  # noqa: BLE001
        return None
    if not result.ok or result.plan is None:
        return None
    return list(result.plan.ops)


def _added(before_ops: list, after_ops: list) -> list:
    """The ops in ``after_ops`` with no counterpart in ``before_ops``.

    A multiset difference on scope plus payload, so a second copy of an op
    that was already there counts as added once.
    """
    seen: dict[tuple, int] = {}
    for op in before_ops:
        k = _key(op)
        seen[k] = seen.get(k, 0) + 1
    added: list = []
    for op in after_ops:
        k = _key(op)
        if seen.get(k):
            seen[k] -= 1
            continue
        added.append(op)
    return added


def scope_drift(before: str | None, after: str | None, target: str) -> ScopeDrift:
    """Report when every op this attempt added misses ``target``.

    ``before``/``after`` are overlay.dops text. ``None`` means the file was
    absent; :data:`UNREADABLE` means the read failed. Either way there is
    no baseline and therefore no finding.
    """
    if not target:
        return ScopeDrift(target="", unavailable="no compose target for this env")
    if before is None:
        return ScopeDrift(
            target=target,
            unavailable="no overlay existed at attempt start, so nothing to diff",
        )
    if before is UNREADABLE or after is UNREADABLE:
        return ScopeDrift(target=target, unavailable="overlay could not be read")
    if after is None:
        return ScopeDrift(target=target)

    before_ops = _plan_ops(before)
    after_ops = _plan_ops(after)
    if before_ops is None:
        return ScopeDrift(
            target=target,
            unavailable="overlay did not plan cleanly before the attempt",
        )
    if after_ops is None:
        return ScopeDrift(target=target, unavailable="overlay does not plan cleanly")

    added = _added(before_ops, after_ops)
    if not added:
        return ScopeDrift(target=target)
    # ANY effective addition means the fix reached this build line. The
    # other-target ops alongside it are the convention, not drift -- see
    # the module docstring for the three shapes that look like drift and
    # are not.
    if any((op.target or "@any") in ("@any", target) for op in added):
        return ScopeDrift(target=target)
    return ScopeDrift(target=target, stranded=tuple(_describe(op) for op in added))


#: Kinds that replace what they name, so the same name under @any loses.
#: The accumulating kinds -- mk add, mk bump, mk target append -- stack
#: on top of the @any op instead, and two of them are not an override.
_REPLACING = frozenset({
    "mk.var.set", "mk.var.unset", "mk.var.eval", "mk.var.shell",
    "mk.target.set", "mk.target.remove", "mk.target.rename",
    "file.materialize", "file.copy", "file.remove",
})


def _override_subject(op) -> tuple | None:
    """What an op writes, for matching an override to the @any op it beats.

    A make variable, a make target, or a composed file. Text and patch ops
    edit a file without owning it, so they have no subject here.
    """
    import posixpath  # noqa: PLC0415

    p = op.payload
    if op.kind.startswith("mk.var.") and p.get("name"):
        return ("var", p["name"])
    if op.kind.startswith("mk.target."):
        name = p.get("name") or p.get("old")
        return ("mk-target", name) if name else None
    if op.kind in ("file.materialize", "file.copy") and p.get("dst"):
        return ("file", posixpath.normpath(str(p["dst"])))
    if op.kind == "file.remove" and p.get("path"):
        return ("file", posixpath.normpath(str(p["path"])))
    return None


def _overridden(op, shared_ops: list) -> object | None:
    """The @any op ``op`` overrides, or None.

    A replacing op beats any @any op on the same subject. ``mk remove``
    overrides only the @any ``mk add`` of the same token: taking back a
    token @any added. Anything else accumulates.
    """
    subject = _override_subject(op)
    if subject is None:
        return None
    if op.kind == "mk.var.token_remove":
        value = op.payload.get("value")
        for shared in reversed(shared_ops):
            if (shared.kind == "mk.var.token_add"
                    and _override_subject(shared) == subject
                    and shared.payload.get("value") == value):
                return shared
        return None
    if op.kind not in _REPLACING:
        return None
    for shared in reversed(shared_ops):  # the last @any op is the one that runs
        if _override_subject(shared) == subject:
            return shared
    return None


def _render(op) -> str:
    p = op.payload
    if op.kind.startswith("mk.var."):
        value = p.get("value")
        return f"`{op.kind} {p.get('name')}" + (
            f' "{value}"`' if value is not None else "`")
    if op.kind in ("file.materialize", "file.copy"):
        return f"`{op.kind} {p.get('src')} -> {p.get('dst')}`"
    if op.kind == "mk.target.rename":
        return f"`{op.kind} {p.get('old')} -> {p.get('new')}`"
    subject = next((str(p[k]) for k in _SUBJECT_KEYS if p.get(k)), "")
    return f"`{op.kind}{' ' + subject if subject else ''}`"


def any_overrides(before: str | None, after: str | None,
                  target: str) -> tuple[str, ...]:
    """The ops this attempt added on ``target`` that override an @any op.

    poly-7pwa.27, row 5b. An @any op that composes on this line but is wrong
    for it is overridden in this line's block, and every other line keeps
    it. The reviewer is told, one line per override, so a shared value that
    is wrong everywhere can be split by hand. Only ops the attempt added:
    an override that was already there is old news.
    """
    if not target or after is None or UNREADABLE in (before, after):
        return ()
    after_ops = _plan_ops(after)
    before_ops = _plan_ops(before) if before is not None else []
    if after_ops is None or before_ops is None:
        return ()
    shared_ops = [op for op in after_ops if (op.target or "@any") == "@any"]
    out: list[str] = []
    for op in _added(before_ops, after_ops):
        if op.target != target:
            continue
        beaten = _overridden(op, shared_ops)
        if beaten is not None:
            out.append(
                f"`{target}` overrides `@any` {_render(beaten)} "
                f"with {_render(op)}"
            )
    return tuple(out)
