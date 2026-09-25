"""The build log is white on black ON THE PAGE, not just in the sheet.

poly-atq9. poly-qqx9.17 pointed the tail at the --term-* tokens and its
tests asserted exactly that: the tail's rule names var(--term-fg), names no
--code-*, and the tokens are dark in all three theme states. Every one of
those was true while the tail rendered as a light code block, because
`.turn-stream .tool-call pre` (0,2,1) matches the same element and beat
`.turn-stream .tool-tail` (0,2,0). A stylesheet assertion cannot see another
rule winning.

So this file does not read the tail's rule. It renders the job page, walks
the real ancestor chain down to the <pre>, collects every rule in the
document that matches it -- the linked sheet AND the page's own inline
<style>, which comes later and so takes ties -- and resolves the cascade for
the two properties the bug was about.

WHAT IT IS NOT is a browser or a screenshot. It reads two declarations on
one element, which is the narrow thing the repo's rule about pictures
("produce them, do not assert on them") leaves room for: not a diff, one
computed value, and provably outside what the stylesheet tests can see.

The resolver is correspondingly narrow -- descendant and child combinators,
classes, tags, no pseudos, no at-rules. What keeps that honest is that a
rule it cannot read is never silently skipped: each one is weakened into a
selector matching a SUPERSET of what it could match and tested against the
tail, and anything that survives fails the run. See
test_no_rule_that_could_paint_the_tail_is_skipped_unproven.
"""

from __future__ import annotations

import re
import sqlite3
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from dportsv3.db import presence
from dportsv3.db.schema import init_db
from dportsv3.tracker.server import create_app

JOB = "job-1"
RUNNER = "builder-A"
CSS = (Path(__file__).resolve().parents[1] / "dportsv3" / "tracker"
       / "static" / "progress.css")

#: The rule that caused the bead, named so one test can prove it still
#: reaches the tail. Without that, deleting the conflict would leave a
#: cascade test passing because there is nothing left to fight.
CODE_BLOCK_RULE = ".turn-stream .tool-call pre"

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}

Node = tuple[str, frozenset[str]]


# --- the page -------------------------------------------------------------


def _seed(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    init_db(db)
    db.execute(
        "INSERT INTO jobs (job_id, state, type, origin, target, dev_env, "
        "bundle_id) VALUES (?, 'patching', 'patch', 'devel/glib20', "
        "'@2026Q3', '2026Q3', 'b-1')", (JOB,),
    )
    db.execute(
        "INSERT INTO activity_log (ts, stage, message, job_id, extra_json) "
        "VALUES ('2026-09-24T10:00:00+00:00', 'attempt_start', 'a', ?, "
        "'{\"attempt\": 1, \"turn\": 1}')", (JOB,),
    )
    db.execute(
        "INSERT INTO activity_log (ts, stage, message, job_id, extra_json) "
        "VALUES ('2026-09-24T10:00:01+00:00', 'tool_start', "
        "'dsynth_build started', ?, "
        "'{\"attempt\": 1, \"turn\": 1, \"tool\": \"dsynth_build\", "
        "\"call_id\": \"c1\"}')", (JOB,),
    )
    db.commit()
    return db


@pytest.fixture
def page(tmp_path: Path) -> Iterator[tuple[str, TestClient]]:
    """The job page with a live tail on it -- the only state in which the
    element under test exists at all."""
    path = tmp_path / "state.db"
    db = _seed(path)
    with TestClient(create_app(path)) as client:
        presence.apply(db, {"event": "heartbeat", "runner_id": RUNNER, "tail": {
            "job_id": JOB, "tool": "dsynth_build",
            "text": "checking for gcc... yes\nconfigure: done\n",
            "lines": 2, "total_bytes": 900_000, "skipped": 0,
            "max_bytes": 32768, "log_mtime": 1_700_000_000.0,
        }})
        yield client.get(f"/agentic/jobs/{JOB}").text, client
    db.close()


# --- walking the served document -----------------------------------------


class _Walk(HTMLParser):
    """Ancestor chains for every <pre class="tool-tail">, plus the inline
    sheets.

    The chain comes from the SERVED HTML, not from the template: which
    rules reach the element is decided by where the element sits, and the
    partial is included from one place today and need not stay that way.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[Node] = []
        self.chains: list[list[Node]] = []
        self.inline_css: list[str] = []
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in VOID:
            return
        classes = frozenset((dict(attrs).get("class") or "").split())
        self.stack.append((tag, classes))
        if tag == "style":
            self._in_style = True
        if tag == "pre" and "tool-tail" in classes:
            self.chains.append(list(self.stack))

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.inline_css.append(data)


def _walk(html: str) -> _Walk:
    walk = _Walk()
    walk.feed(html)
    return walk


# --- the narrow cascade resolver ------------------------------------------

#: `*` or a tag, then classes and ids, and nothing else. A compound this
#: cannot parse is reported, never ignored.
_COMPOUND = re.compile(r"^(\*|[a-z][a-z0-9-]*)?((?:[.#][A-Za-z0-9_-]+)*)$")
_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _rules(css: str, order0: int = 0) -> tuple[list[dict], list[dict]]:
    """(top-level rules, rules nested in an at-rule), in source order.

    Hand-rolled rather than a parser dependency -- the [dev] extras already
    do not install on a DragonFly builder (poly-170) and this needs no
    third one. @keyframes bodies are dropped: their `0%` / `from` are not
    selectors and nothing in them can paint by matching.
    """
    css = _COMMENT.sub(lambda m: " " * len(m.group(0)), css)
    flat: list[dict] = []
    conditional: list[dict] = []
    at_rules: list[str] = []
    order, i, n, prelude_at = order0, 0, len(css), 0
    while i < n:
        char = css[i]
        if char == "{":
            prelude = css[prelude_at:i].strip()
            if prelude.startswith("@"):
                at_rules.append(prelude.lower())
                i, prelude_at = i + 1, i + 1
                continue
            depth, j = 1, i + 1
            while j < n and depth:
                depth += (css[j] == "{") - (css[j] == "}")
                j += 1
            if not any(a.startswith("@keyframes") for a in at_rules):
                rule = {
                    "selectors": [s.strip() for s in prelude.split(",") if s.strip()],
                    "body": css[i + 1:j - 1],
                    "order": order,
                }
                (conditional if at_rules else flat).append(rule)
                order += 1
            i, prelude_at = j, j
            continue
        if char == "}":
            if at_rules:
                at_rules.pop()
            i, prelude_at = i + 1, i + 1
            continue
        if char == ";" and not at_rules:
            i, prelude_at = i + 1, i + 1
            continue
        i += 1
    return flat, conditional


def _specificity(selector: str) -> tuple[int, int, int]:
    ids = len(re.findall(r"#[A-Za-z0-9_-]+", selector))
    classes = len(re.findall(r"\.[A-Za-z0-9_-]+|\[[^\]]*\]|(?<!:):[a-z-]+", selector))
    tags = len(re.findall(r"(?:^|[\s>+~])([a-z][a-z0-9-]*)", selector))
    return ids, classes, tags


def _compound_matches(compound: str, node: Node) -> bool:
    m = _COMPOUND.match(compound)
    if not m:
        raise ValueError(compound)
    tag, rest = m.group(1), m.group(2) or ""
    if tag and tag != "*" and tag != node[0]:
        return False
    for part in re.findall(r"[.#][A-Za-z0-9_-]+", rest):
        if part[0] == "#":
            return False          # the chains carry no ids
        if part[1:] not in node[1]:
            return False
    return True


def _parse(selector: str) -> list[tuple[str | None, str]]:
    """[(combinator to the left, compound), ...], left to right.

    Descendant and child only. A sibling combinator, a pseudo or an
    attribute selector raises, and the honesty test is what decides
    whether that mattered.
    """
    if re.search(r"[+~:\[]", selector):
        raise ValueError(selector)
    seq: list[tuple[str | None, str]] = []
    comb: str | None = None
    for token in selector.replace(">", " > ").split():
        if token == ">":
            comb = ">"
            continue
        seq.append((comb, token))
        comb = " "
    if not seq:
        raise ValueError(selector)
    return seq


def _walk_seq(seq: list[tuple[str | None, str]], chain: list[Node]) -> bool:
    if not seq:
        return True
    if not chain:
        return False
    comb, compound = seq[-1]
    if not _compound_matches(compound, chain[-1]):
        return False
    rest = seq[:-1]
    if not rest:
        return True
    if comb == ">":
        return _walk_seq(rest, chain[:-1])
    return any(_walk_seq(rest, chain[:i]) for i in range(len(chain) - 1, 0, -1))


def _matches(selector: str, chain: list[Node]) -> bool:
    return _walk_seq(_parse(selector), chain)


_PSEUDO = re.compile(r":{1,2}[A-Za-z-]+(?:\([^()]*\))?")
_ATTR = re.compile(r"\[[^\]]*\]")
#: A ::before box is not the element -- its background paints a generated
#: box next to the tail's, never the tail's own. Legacy single-colon
#: spellings included; they mean the same boxes.
_PSEUDO_ELEMENT = re.compile(
    r"::[A-Za-z-]+|:(?:before|after|first-line|first-letter|marker"
    r"|placeholder|selection|backdrop)\b")


def _weaken(selector: str) -> str:
    """The same selector with every narrowing construct dropped.

    :hover, :not(...) and [aria-current] restrict which elements a compound
    reaches, so removing them widens it; a compound left empty means "any
    element", which is `*`. A sibling combinator is not a descendant
    combinator and cannot be rewritten as one, so everything left of the
    rightmost `+` or `~` is dropped instead -- again a widening, since what
    remains constrains strictly less. `:root` is the one pseudo-class
    translated rather than dropped, because it has an exact equivalent and
    dropping it would turn the theme blocks into `*`.

    The point of all this: if the weakened form does not reach the tail,
    the original provably cannot, so skipping it is a proof and not a hope.
    """
    out = re.sub(r":root\b", "html", selector)
    out = re.split(r"[+~]", out)[-1]
    parts: list[str] = []
    for token in out.replace(">", " > ").split():
        if token == ">":
            parts.append(token)
            continue
        parts.append(_ATTR.sub("", _PSEUDO.sub("", token)) or "*")
    while parts and parts[0] == ">":
        parts.pop(0)
    return " ".join(parts) or "*"


def _could_paint(selector: str, chain: list[Node]) -> bool:
    if _PSEUDO_ELEMENT.search(selector):
        return False
    try:
        return _matches(_weaken(selector), chain)
    except ValueError:
        return True               # cannot even weaken it: assume the worst


#: `background` and `background-color` decide the same pixel, so they are
#: one contest. The bug was a `background` shorthand losing.
GROUPS = {"color": ("color",), "background": ("background", "background-color")}


def _declarations(body: str) -> list[tuple[str, str, bool]]:
    out = []
    for raw in body.split(";"):
        prop, sep, value = raw.partition(":")
        if not sep:
            continue
        value = value.strip()
        out.append((prop.strip().lower(),
                    value.removesuffix("!important").strip(),
                    value.endswith("!important")))
    return out


def _winner(rules: list[dict], chain: list[Node], group: str) -> tuple[str, str]:
    """(selector, value) for the declaration that actually paints.

    Only the unconditional rules are passed in; the conditional ones are
    proven unable to reach this element by the honesty test, which is what
    makes leaving them out of the contest legitimate.
    """
    best: tuple[str, str] | None = None
    best_key = None
    for rule in rules:
        for selector in rule["selectors"]:
            try:
                reaches = _matches(selector, chain)
            except ValueError:
                # Unreadable here, and proven harmless over there: every
                # skip is covered by
                # test_no_rule_that_could_paint_the_tail_is_skipped_unproven.
                continue
            if not reaches:
                continue
            for pos, (prop, value, important) in enumerate(
                    _declarations(rule["body"])):
                if prop not in GROUPS[group]:
                    continue
                key = (important, _specificity(selector), rule["order"], pos)
                if best_key is None or key > best_key:
                    best, best_key = (selector, value), key
    assert best is not None, f"nothing in the document sets {group} on the tail"
    return best


def _document(html: str) -> tuple[list[dict], list[dict], list[Node]]:
    """Every rule the served page brings, linked sheet first, plus the
    tail's ancestor chain.

    The inline <style> lands after the sheet, as it does in the document,
    so an inline rule takes a specificity tie -- which is how the job
    page's own head block could quietly re-break this.
    """
    walk = _walk(html)
    flat, conditional = _rules(CSS.read_text())
    for block in walk.inline_css:
        more_flat, more_cond = _rules(block, order0=len(flat) + len(conditional))
        flat += more_flat
        conditional += more_cond
    assert walk.chains, "no tail on the page: the fixture is not rendering one"
    return flat, conditional, walk.chains[0]


# --- the checks -----------------------------------------------------------


def test_the_page_carries_one_tail_and_it_sits_in_a_tool_call(page) -> None:
    """Both halves of the premise this file rests on."""
    html, _ = page
    chains = _walk(html).chains

    assert len(chains) == 1, f"expected one tail <pre>, got {len(chains)}"
    assert any("tool-call" in classes for _, classes in chains[0]), (
        "the tail is no longer inside a .tool-call -- re-read poly-atq9 "
        "before trusting anything below it"
    )


def test_the_tail_is_a_terminal_once_the_cascade_is_resolved(page) -> None:
    """The measurement. Before the fix this resolved to --code-bg /
    --code-fg: #f6f8fa under a light theme, i.e. a code block, which is
    exactly the treatment poly-qqx9.17 was filed to replace."""
    html, _ = page
    flat, _, chain = _document(html)

    bg_selector, bg = _winner(flat, chain, "background")
    fg_selector, fg = _winner(flat, chain, "color")

    assert "--term-bg" in bg, f"the tail's ground comes from {bg_selector}: {bg}"
    assert "--term-fg" in fg, f"the tail's text comes from {fg_selector}: {fg}"
    # And it is the TAIL's rule that paints it. Making .tool-call pre
    # terminal-coloured would satisfy the two assertions above while
    # collapsing the distinction the design source draws ten lines apart:
    # --term-* for the build log, --code-* for a tool's captured stdout.
    assert "tool-tail" in bg_selector and "tool-tail" in fg_selector, (
        f"the terminal treatment is coming from {bg_selector} / "
        f"{fg_selector}, which is not the build log's own rule"
    )


def test_the_code_block_rule_still_reaches_the_tail_and_still_loses(
    page,
) -> None:
    """Guards this file against rotting into a tautology. If the tool-output
    rule stops matching the tail -- the <pre> becomes a <div>, the sheet is
    split -- the test above would pass for a reason unrelated to the bug,
    and this is where that gets said out loud."""
    html, _ = page
    flat, _, chain = _document(html)
    winner, _value = _winner(flat, chain, "background")

    assert _matches(CODE_BLOCK_RULE, chain), (
        f"{CODE_BLOCK_RULE} no longer reaches the tail: the conflict this "
        f"file resolves is gone, so the cascade check above is measuring "
        f"nothing"
    )
    # The winner as the SHEET spells it, not as this file does: the tail has
    # to beat the code-block rule on specificity. Source order is not a fix
    # -- .tool-tail already sat two hundred lines later and still lost, and
    # a rule that wins by where it sits loses the next reorganisation.
    assert _specificity(winner) > _specificity(CODE_BLOCK_RULE), (
        f"{winner} paints the tail but only out-orders {CODE_BLOCK_RULE}"
    )


def test_no_rule_that_could_paint_the_tail_is_skipped_unproven(page) -> None:
    """The honesty clause.

    Two sets of rules are left out of the contest above: the ones whose
    selector the resolver cannot read, and the ones inside an at-rule,
    where whether they apply depends on a viewport and a theme this knows
    nothing about. Every one of them that declares a colour or a ground at
    all is weakened into a superset of itself and tested against the tail,
    and anything that still reaches it fails here. A rule that sets neither
    property cannot decide either contest however it matches.

    A failure is not necessarily a bug in the sheet. It means the sheet grew
    a construct this file cannot judge, and the answer is to teach the
    resolver or to move the rule -- never to widen the skip.
    """
    html, _ = page
    flat, conditional, chain = _document(html)

    skipped = [
        (selector, rule)
        for rule in flat for selector in rule["selectors"]
        if _unreadable(selector, chain)
    ]
    assert [s for s, rule in skipped
            if _paints(rule) and _could_paint(s, chain)] == [], (
        "a rule the resolver cannot parse could paint the tail"
    )
    assert [s for rule in conditional for s in rule["selectors"]
            if _paints(rule) and _could_paint(s, chain)] == [], (
        "a rule inside an at-rule could paint the tail, and whether it "
        "applies depends on the viewport"
    )


def test_the_honesty_clause_is_looking_at_something(page) -> None:
    """An empty skip list would pass the clause above for the wrong reason.
    Both sets are large in this sheet -- a hundred-odd pseudo-class rules and
    every @media body -- so if either comes back empty the collection broke
    rather than the sheet got simple."""
    html, _ = page
    flat, conditional, chain = _document(html)

    unreadable = [s for rule in flat for s in rule["selectors"]
                  if _unreadable(s, chain)]

    assert len(unreadable) > 20, unreadable
    assert len(conditional) > 20, "the parser lost the @media bodies"
    assert any(_declarations(rule["body"]) for rule in conditional), (
        "conditional rules came back with no declarations in them"
    )
    # None of them paints, as it happens: the theme blocks redefine --term-*
    # rather than setting color/background, which is exactly why a token
    # value is the other file's business (test_job_detail_operator_review's
    # test_the_terminal_tokens_are_dark_in_every_theme) and the winning
    # declaration is this one's.
    assert not any(_paints(rule) for rule in conditional), (
        "a conditional rule started painting; it is now in scope for the "
        "clause above and needs proving harmless there, not noted here"
    )


def _paints(rule: dict) -> bool:
    """Whether the rule declares either property under contest."""
    contested = {p for props in GROUPS.values() for p in props}
    return any(prop in contested for prop, _, _ in _declarations(rule["body"]))


def _unreadable(selector: str, chain: list[Node]) -> bool:
    try:
        _matches(selector, chain)
    except ValueError:
        return True
    return False


def test_the_polled_tail_carries_the_class_the_cascade_turns_on(page) -> None:
    """The page is half this element's life. Every 3s the client replaces
    #tool-tail-slot with the fragment's tail_html, in place inside the same
    .tool-call -- so the chain is unchanged and the one thing that could
    break the cascade after a poll is the <pre> arriving without its
    class."""
    html, client = page
    reply = client.get(f"/api/jobs/{JOB}/activity-fragment?since_id=0")
    assert reply.status_code == 200, reply.text

    swapped = _walk(reply.json()["tail_html"]).chains

    assert len(swapped) == 1, "the poll sent no tail <pre>"
    assert swapped[0][-1] == _walk(html).chains[0][-1], (
        "the swapped <pre> does not carry the classes the rendered one does"
    )
