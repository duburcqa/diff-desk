"""Line up the old and new versions of Python files by what their statements are rather than where they stand.

A line diff pairs whatever lines happen to sit at the same place, so a class moved up a file, a method moved to
another class, or a docstring rewritten around unchanged code reads as everything removed and everything added again.
Here both versions are parsed and their statements matched across every file at once, in passes from the surest to
the loosest:

1. identical statements, the largest first, anywhere in the files given;
2. statements identical once docstrings are left out, then once identifiers are consistently renamed;
3. classes and functions of a file still carrying their qualified name;
4. classes, functions and blocks, by the share of their statements already matched, a same name counting in favour;
5. what is left inside each matched pair: docstring to docstring, same name, then by similarity of their tokens;
6. what is left anywhere, by token similarity alone.

What comes out is laid out for a two-column view (see `align`): rows line up on the matched statements that kept their
order, code moved elsewhere is shown once, at its new place and beside its old version, and runs of old lines facing
nothing fold behind a bar saying what they held and where it went.
"""

import ast
import bisect
import difflib
import re
from collections import Counter
from dataclasses import dataclass, field

COMPOUND = (
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.TryStar,
    ast.ExceptHandler,
)
NAMED = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
IMPORTS = (ast.Import, ast.ImportFrom)
# Which field of a node holds an identifier, for telling a statement apart from the same one renamed (see 'keys_of').
IDENTIFIER = {
    ast.Name: "id",
    ast.Attribute: "attr",
    ast.arg: "arg",
    ast.keyword: "arg",
    ast.FunctionDef: "name",
    ast.AsyncFunctionDef: "name",
    ast.ClassDef: "name",
    ast.alias: "asname",
}
TOKEN = re.compile(r"\w+|[^\w\s]")
# Below this many characters a statement reads the same in too many places to be matched across the files on its own:
# `return None` is everywhere. It can still be matched inside a container already matched.
GLOBAL_WEIGHT = 40
# A rename is only said of a statement long enough for it to be telling: two one-liners are always a rename of
# each other.
RENAME_WEIGHT = 80
SIMILAR = 0.5
FUZZY = 0.75
# Runs of unchanged lines longer than this fold, keeping a few lines of context either side.
SAME_RUN = 8
SAME_KEPT = 3
# Old lines facing nothing fold once there are more of them than this.
LEFT_RUN = 6
# A gap where both sides hold more than this many unmatched lines pairs nothing: rows side by side would only pair
# whatever lines happen to come first.
PAIRED_GAP = 3


@dataclass(eq=False)
class Statement:
    """One statement of one version of one file, with what matching it needs."""

    side: int
    path: str
    tree: ast.AST
    parent: "Statement | None"
    start: int
    end: int
    head_end: int
    kind: str
    name: str | None
    qual: str
    text: str
    exact: int
    undocumented: int
    renamed: int
    is_docstring: bool
    depth: int
    children: list = field(default_factory=list)
    match: "Statement | None" = None

    @property
    def weight(self):
        return len(" ".join(self.text.split()))

    def descendants(self):
        for child in self.children:
            yield child
            yield from child.descendants()

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent


def inner_statements(tree):
    return [
        item
        for name in ("body", "handlers", "orelse", "finalbody")
        for item in getattr(tree, name, None) or []
        if isinstance(item, ast.stmt | ast.ExceptHandler)
    ]


def is_docstring(tree, parent):
    if not (isinstance(tree, ast.Expr) and isinstance(tree.value, ast.Constant) and isinstance(tree.value.value, str)):
        return False
    body = getattr(parent, "body", None)
    return isinstance(parent, (ast.Module, *NAMED)) and bool(body) and body[0] is tree


def keys_of(module):
    """Three hashes of every statement: exact, without docstrings, and without docstrings up to consistent renaming.

    Worked out in one pass from the leaves up. The renaming-proof hash covers the shape with every identifier blanked
    and the order in which identifiers first appear, so two statements share it exactly when one is the other with
    its names consistently replaced. The name of a class or function is kept as it is: a reader takes it for what the
    code is, so a class renamed along with its insides is another class.
    """
    keys = {}

    def visit(node):
        exact, undocumented, shape, names = [type(node).__name__], [type(node).__name__], [type(node).__name__], []
        named = IDENTIFIER.get(type(node))
        for label, value in ast.iter_fields(node):
            if isinstance(value, ast.AST):
                one = visit(value)
                exact.append(one[0])
                undocumented.append(one[1])
                shape.append(one[2])
                names.extend(one[3])
            elif isinstance(value, list):
                items = [visit(item) if isinstance(item, ast.AST) else (item, item, item, []) for item in value]
                exact.append(hash(tuple(item[0] for item in items)))
                kept = items[1:] if label == "body" and value and is_docstring(value[0], node) else items
                undocumented.append(hash(tuple(item[1] for item in kept)))
                shape.append(hash(tuple(item[2] for item in kept)))
                for item in kept:
                    names.extend(item[3])
            elif label == named and value is not None and not isinstance(node, NAMED):
                exact.append(value)
                undocumented.append(value)
                shape.append("_")
                names.append(value)
            else:
                exact.append(value)
                undocumented.append(value)
                shape.append(value)
        said = (hash(tuple(exact)), hash(tuple(undocumented)), hash(tuple(shape)), names)
        if isinstance(node, ast.stmt | ast.ExceptHandler):
            first = {}
            order = tuple(first.setdefault(name, len(first)) for name in names)
            keys[id(node)] = (said[0], said[1], hash((said[2], order)))
        return said

    for statement in module.body:
        visit(statement)
    return keys


def renames(old, new):
    """The identifiers that differ between two statements equal up to renaming, old name to new, in order met."""
    found = {}
    for one, other in zip(ast.walk(old), ast.walk(new), strict=False):
        named = IDENTIFIER.get(type(one))
        if named is None or type(other) is not type(one):
            continue
        before, after = getattr(one, named, None), getattr(other, named, None)
        if before is not None and after is not None and before != after:
            found.setdefault(before, after)
    return [[before, after] for before, after in found.items()]


def parse(side, path, source):
    """Every statement of a file, outermost first, with the top-level ones apart."""
    lines = source.split("\n")
    module = ast.parse(source)
    keys = keys_of(module)
    every = []

    def visit(tree, parent, parent_tree, prefix, depth):
        start = min([tree.lineno] + [mark.lineno for mark in getattr(tree, "decorator_list", [])])
        kids = inner_statements(tree) if isinstance(tree, COMPOUND) else []
        # The head of a block runs up to its first inner statement, decorators of that statement excluded.
        head_end = tree.end_lineno
        if kids:
            first = kids[0]
            head_end = min([first.lineno] + [mark.lineno for mark in getattr(first, "decorator_list", [])]) - 1
        name = tree.name if isinstance(tree, NAMED) else None
        qual = f"{prefix}.{name}" if name and prefix else (name or prefix)
        exact, undocumented, renamed = keys[id(tree)]
        node = Statement(
            side=side,
            path=path,
            tree=tree,
            parent=parent,
            start=start,
            end=tree.end_lineno,
            head_end=head_end,
            kind=type(tree).__name__,
            name=name,
            qual=qual,
            text="\n".join(lines[start - 1 : tree.end_lineno]),
            exact=exact,
            undocumented=undocumented,
            renamed=renamed,
            is_docstring=is_docstring(tree, parent_tree),
            depth=depth,
        )
        every.append(node)
        node.children = [visit(kid, node, tree, qual, depth + 1) for kid in kids]
        return node

    tops = [visit(statement, None, module, "", 0) for statement in module.body]
    return tops, every, lines


class Matcher:
    """The matching of every statement of the old files against every statement of the new ones."""

    def __init__(self, olds, news, old_tops, new_tops):
        self.olds, self.news = olds, news
        self.old_tops, self.new_tops = old_tops, new_tops
        self.tokens = {}
        self.scores = {}

    def run(self):
        self.by_key("exact", GLOBAL_WEIGHT, any_block=True)
        self.by_key("undocumented", GLOBAL_WEIGHT, any_block=True)
        self.by_key("renamed", RENAME_WEIGHT, any_block=False)
        self.by_name()
        # Each container matched lets its insides be matched, and each inside matched counts towards its container.
        for _ in range(4):
            changed = self.containers()
            changed |= self.recover(self.matched_pairs())
            for path, tops in self.old_tops.items():
                if self.new_tops.get(path):
                    changed |= self.recover_children(tops, self.new_tops[path])
            if not changed:
                break
        self.fuzzy()
        self.recover(self.matched_pairs())

    @staticmethod
    def link(old, new):
        old.match, new.match = new, old

    def link_whole(self, old, new):
        self.link(old, new)
        for one, other in zip(old.children, new.children, strict=False):
            if one.match is None and other.match is None:
                self.link_whole(one, other)

    def by_key(self, key, weight, any_block):
        """Pair statements sharing a key, the heaviest first, preferring the same name, then file, then order.

        A block below the weight still counts when 'any_block' says so: its shape alone is telling while its code is
        compared, and says nothing once its names are not, two small getters being renames of each other.
        """
        groups = {}
        for node in self.olds + self.news:
            if node.match is None and (node.weight >= weight or (any_block and node.children)):
                groups.setdefault(getattr(node, key), ([], []))[node.side].append(node)
        for sharing in sorted(groups.values(), key=lambda group: -max(node.weight for node in group[0] + group[1])):
            olds = [node for node in sharing[0] if node.match is None]
            news = [node for node in sharing[1] if node.match is None]
            if not olds or not news:
                continue
            scored = sorted(
                (
                    ((one.qual != other.qual, one.path != other.path, abs(i - j)), one, other)
                    for i, one in enumerate(olds)
                    for j, other in enumerate(news)
                    if not (one.path != other.path and isinstance(one.tree, IMPORTS))
                ),
                key=lambda item: item[0],
            )
            for _, one, other in scored:
                if one.match is None and other.match is None:
                    self.link_whole(one, other)

    def by_name(self):
        """Pair the classes and functions of a file that still carry the same qualified name, one of each.

        A class whose methods all moved to another class would otherwise follow them there, its insides being all that
        container matching weighs.
        """
        named = {}
        for node in self.news:
            if node.match is None and node.name is not None:
                named.setdefault((node.path, node.kind, node.qual), []).append(node)
        for node in self.olds:
            if node.match is None and node.name is not None:
                found = named.get((node.path, node.kind, node.qual), [])
                if len(found) == 1 and found[0].match is None:
                    self.link(node, found[0])

    def containers(self):
        """Match unmatched containers, innermost first, by the share of their insides matched into one another."""
        changed = False
        for old in sorted((node for node in self.olds if node.match is None and node.children), key=lambda n: -n.depth):
            inside = list(old.descendants())
            common = Counter()
            for node in inside:
                if node.match is not None:
                    for holder in node.match.ancestors():
                        if holder.match is None and holder.kind == old.kind:
                            common[holder] += 1
            best, best_score = None, 0.0
            for new, shared in common.items():
                score = 2.0 * shared / (len(inside) + sum(1 for _ in new.descendants()))
                if old.name is not None and old.name == new.name:
                    score += 0.3 + 0.2 * (old.qual == new.qual)
                if score > best_score:
                    best, best_score = new, score
            if best is not None and best_score >= SIMILAR:
                self.link(old, best)
                changed = True
        return changed

    def matched_pairs(self):
        return [(node, node.match) for node in self.olds if node.match is not None and node.children]

    def recover(self, pairs):
        changed = False
        stack = list(pairs)
        while stack:
            old, new = stack.pop()
            changed |= self.recover_children(old.children, new.children)
            stack.extend((kid, kid.match) for kid in old.children if kid.children and kid.match in new.children)
        return changed

    def recover_children(self, olds, news):
        """Match what is left among the insides of two matched containers."""
        olds = [node for node in olds if node.match is None]
        news = [node for node in news if node.match is None]
        changed = False
        for one in olds:
            for other in news:
                if one.match is None and other.match is None:
                    paired = (one.is_docstring and other.is_docstring) or (
                        one.name is not None and one.kind == other.kind and one.name == other.name
                    )
                    if paired:
                        self.link(one, other)
                        changed = True
        candidates = sorted(
            (
                (self.similarity(one, other), one, other)
                for one in olds
                for other in news
                if one.kind == other.kind and one.match is None and other.match is None
            ),
            key=lambda item: -item[0],
        )
        for score, one, other in candidates:
            if score >= SIMILAR and one.match is None and other.match is None:
                self.link(one, other)
                changed = True
        return changed

    def fuzzy(self):
        """Pair leftover statements of a kind anywhere, by token similarity of at least FUZZY.

        The similarity is bounded by how much the two bags of tokens share, itself bounded by the ratio of their
        lengths, so both bounds prune the candidates before the sequences are compared.
        """
        free = {}
        for node in self.news:
            if node.match is None and node.weight >= GLOBAL_WEIGHT:
                free.setdefault(node.kind, []).append((len(self.tokens_of(node)[0]), node))
        for bucket in free.values():
            bucket.sort(key=lambda item: item[0])
        stretch = FUZZY / (2.0 - FUZZY)
        candidates = []
        for old in self.olds:
            if old.match is not None or old.weight < GLOBAL_WEIGHT:
                continue
            sequence, bag = self.tokens_of(old)
            bucket = free.get(old.kind, [])
            low = bisect.bisect_left(bucket, len(sequence) * stretch, key=lambda item: item[0])
            high = bisect.bisect_right(bucket, len(sequence) / stretch, key=lambda item: item[0])
            for _, new in bucket[low:high]:
                if old.path != new.path and isinstance(old.tree, IMPORTS):
                    continue
                other_sequence, other_bag = self.tokens_of(new)
                if 2.0 * sum((bag & other_bag).values()) < FUZZY * (len(sequence) + len(other_sequence)):
                    continue
                score = self.similarity(old, new)
                if score >= FUZZY:
                    candidates.append((score, old, new))
        for _, old, new in sorted(candidates, key=lambda item: -item[0]):
            if old.match is None and new.match is None:
                self.link(old, new)

    def tokens_of(self, node):
        held = self.tokens.get(id(node))
        if held is None:
            sequence = TOKEN.findall(node.text)
            held = self.tokens[id(node)] = (sequence, Counter(sequence))
        return held

    def similarity(self, old, new):
        held = self.scores.get((id(old), id(new)))
        if held is None:
            (one, one_bag), (other, other_bag) = self.tokens_of(old), self.tokens_of(new)
            total = len(one) + len(other)
            # Bounded by the ratio of lengths, then by the shared bag of tokens, each cheaper than what it spares.
            if (
                not total
                or 2.0 * min(len(one), len(other)) < SIMILAR * total
                or 2.0 * sum((one_bag & other_bag).values()) < SIMILAR * total
            ):
                held = 0.0
            else:
                held = difflib.SequenceMatcher(None, one, other, autojunk=False).ratio()
            self.scores[(id(old), id(new))] = held
        return held


def verdict(old):
    """What changed between a matched pair: nothing, its docstrings, its names, or its code."""
    new = old.match
    if old.exact == new.exact:
        return "identical"
    if old.undocumented == new.undocumented:
        return "docstring"
    if old.renamed == new.renamed and old.weight >= RENAME_WEIGHT:
        return "renamed"
    return "edited"


def longest_rising(pairs):
    """The longest run of pairs, ordered by their first member, whose second member keeps rising."""
    pairs = sorted(pairs)
    tails, ends, before = [], [], [None] * len(pairs)
    for at, (_, second) in enumerate(pairs):
        spot = bisect.bisect_left(tails, second)
        if spot == len(tails):
            tails.append(second)
            ends.append(at)
        else:
            tails[spot] = second
            ends[spot] = at
        before[at] = ends[spot - 1] if spot else None
    run, at = [], ends[-1] if ends else None
    while at is not None:
        run.append(pairs[at])
        at = before[at]
    return run[::-1]


def token_spans(old_text, new_text):
    """The character spans of the tokens that differ between two texts, on each side."""
    found = [[(hit.start(), hit.end(), hit.group()) for hit in TOKEN.finditer(text)] for text in (old_text, new_text)]
    spans = ([], [])
    words = [[token[2] for token in side] for side in found]
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, *words, autojunk=False).get_opcodes():
        if op != "equal":
            spans[0].extend(found[0][k][:2] for k in range(i1, i2))
            spans[1].extend(found[1][k][:2] for k in range(j1, j2))
    return spans


class Layout:
    """The two-column layout of every file, from a finished matching."""

    def __init__(self, files):
        self.files = files
        self.kept = set()
        self.spans = {}
        self.line_maps = {}
        for old, new in files.values():
            if old and new:
                self.keep_order(old[0], new[0])

    def keep_order(self, olds, news):
        """Mark the statements that stayed in place: matched into the same container and in the same order there."""
        place = {id(node): spot for spot, node in enumerate(news)}
        pairs = [(spot, place[id(node.match)]) for spot, node in enumerate(olds) if id(node.match) in place]
        for spot, _ in longest_rising(pairs):
            node = olds[spot]
            self.kept.add(id(node))
            if node.children and node.match.children:
                self.keep_order(node.children, node.match.children)

    def moved(self, node):
        old = node if node.side == 0 else node.match
        return old.path != old.match.path or any(id(one) not in self.kept for one in [old, *old.ancestors()])

    def spans_of(self, node):
        held = self.spans.get(id(node))
        if held is None:
            old, new = (node, node.match) if node.side == 0 else (node.match, node)
            self.spans[id(old)], self.spans[id(new)] = token_spans(old.text, new.text)
            held = self.spans[id(node)]
        return held

    def line_spans(self, node, number, line):
        offset = sum(len(row) + 1 for row in node.text.split("\n")[: number - node.start])
        return [
            [low - offset, high - offset] for low, high in self.spans_of(node) if offset <= low < offset + len(line) + 1
        ]

    def facing(self, node, number):
        """The line of the matched statement facing a line of this one, or None."""
        other = node.match
        if node.children and number > node.head_end:
            # A comment between inner statements faces the same comment, wherever it is in the other container.
            wanted = self.files[node.path][node.side][2][number - 1].strip()
            for shift, row in enumerate(other.text.split("\n")):
                if row.strip() == wanted:
                    return other.start + shift
            return None
        if node.children:
            spot = other.start + number - node.start
            return spot if spot <= other.head_end else None
        table = self.line_maps.get(id(node))
        if table is None:
            table = {}
            ours, theirs = node.text.split("\n"), other.text.split("\n")
            matcher = difflib.SequenceMatcher(
                None, [r.strip() for r in ours], [r.strip() for r in theirs], autojunk=False
            )
            for _, i1, i2, j1, j2 in matcher.get_opcodes():
                for step in range(min(i2 - i1, j2 - j1)):
                    table[i1 + step] = j1 + step
            self.line_maps[id(node)] = table
        shift = table.get(number - node.start)
        return None if shift is None else other.start + shift

    def owners(self, every, count):
        held = [None] * (count + 2)
        for node in sorted(every, key=lambda one: one.depth):
            for number in range(node.start, node.end + 1):
                held[number] = node
        return held

    def describe(self, side, lines, owners, other_lines):
        """What each line of one side is: its class, the spans of what changed in it, and the badge it carries."""
        said = []
        loose = {row.strip() for row in other_lines}
        for number, line in enumerate(lines, start=1):
            node = owners[number]
            if not line.strip():
                said.append(["blank", None, None])
            elif node is None:
                said.append(["same" if line.strip() in loose else ("add" if side else "del"), None, None])
            elif node.match is None:
                said.append(["add" if side else "del", None, None])
            else:
                said.append(self.describe_matched(node, number, line))
        return said

    def describe_matched(self, node, number, line):
        """What a line of a matched statement is, from what changed between the statement and its match."""
        moved = self.moved(node)
        if number > node.head_end and node.children:
            # A comment between inner statements reads the same when the other container holds it anywhere.
            kept = {row.strip() for row in node.match.text.split("\n")}
            kind = "same" if line.strip() in kept else ("add" if node.side else "del")
            return ["moved" if moved and kind == "same" else kind, None, None]
        state = verdict(node if node.side == 0 else node.match)
        if node.children:
            ours = node.text.split("\n")[: node.head_end - node.start + 1]
            theirs = node.match.text.split("\n")[: node.match.head_end - node.match.start + 1]
            changed = " ".join(" ".join(ours).split()) != " ".join(" ".join(theirs).split())
        else:
            changed = state != "identical"
        kind = "edit" if changed else "same"
        if moved:
            kind = "moved edit" if changed else "moved"
        spans = self.line_spans(node, number, line) if changed else None
        badge = None
        if number == node.start and (moved or state != "identical"):
            badge = self.badge(node, state, moved and not self.moves_along(node))
        return [kind, spans, badge]

    def moves_along(self, node):
        """Whether the statement before this one moved to just before where this one went, saying the move already."""
        siblings = node.parent.children if node.parent is not None else None
        if not siblings:
            return False
        spot = siblings.index(node)
        if spot == 0 or siblings[spot - 1].match is None:
            return False
        before, target = siblings[spot - 1].match, node.match
        return before.parent is target.parent and before.path == target.path and before.end < target.start

    def badge(self, node, state, moved):
        other = node.match
        said = {}
        if moved:
            said["moved"] = [other.path, other.start]
        if state == "renamed":
            old, new = (node, other) if node.side == 0 else (other, node)
            said["renamed"] = renames(old.tree, new.tree)
        elif state == "docstring":
            said["docstring"] = True
        elif state == "edited" and node.children:
            said["edited"] = True
        return said or None

    def target(self, node):
        """Where the outermost moved statement holding a line went, as [path, line, name]."""
        while node.parent is not None and node.parent.match is not None and self.moved(node.parent):
            node = node.parent
        other = node.match
        return [other.path, other.start, other.qual or other.text.strip().split("\n")[0][:40]]

    def lay_out(self, path):
        old_side, new_side = self.files[path]
        old_every, old_lines = (old_side[1], old_side[2]) if old_side else ([], [])
        new_every, new_lines = (new_side[1], new_side[2]) if new_side else ([], [])
        old_owners = self.owners(old_every, len(old_lines))
        new_owners = self.owners(new_every, len(new_lines))
        old_info = self.describe(0, old_lines, old_owners, new_lines)
        new_info = self.describe(1, new_lines, new_owners, old_lines)

        # Rows line up on the statements of this file that stayed in place, and inside edited ones on their lines.
        anchors = set()
        for node in old_every:
            if node.match is None or node.match.path != path or self.moved(node):
                continue
            anchors.update({(node.start, node.match.start), (node.end + 1, node.match.end + 1)})
            if not node.children and node.exact != node.match.exact:
                for number in range(node.start, node.end + 1):
                    facing = self.facing(node, number)
                    if facing is not None:
                        anchors.add((number, facing))
        anchors = longest_rising(anchors)

        def away(number):
            return old_info[number - 1][0].startswith("moved")

        def arriving(number):
            node = new_owners[number]
            return new_info[number - 1][0].startswith("moved") and node is not None and node.match is not None

        rows = []
        old_at, new_at = 1, 1
        for old_to, new_to in [*anchors, (len(old_lines) + 1, len(new_lines) + 1)]:
            if old_to < old_at or new_to < new_at:
                continue
            left = self.left_run(range(old_at, old_to), old_info, away)
            right = [("ghost" if arriving(number) else "line", number) for number in range(new_at, new_to)]
            plain_left = sum(item[0] == "line" for item in left)
            plain_right = sum(item[0] == "line" for item in right)
            if plain_left > PAIRED_GAP or plain_right > PAIRED_GAP:
                rows.extend(self.away(item[1], old_owners) if item[0] == "away" else {"old": item[1]} for item in left)
                rows.extend(
                    self.ghost(item[1], new_owners) if item[0] == "ghost" else {"new": item[1]} for item in right
                )
            else:
                rows.extend(self.zip(left, right, old_owners, new_owners))
            old_at, new_at = old_to, new_to
        return {
            "old": old_lines,
            "new": new_lines,
            "oldInfo": old_info,
            "newInfo": new_info,
            "rows": self.fold(rows, old_info, new_info, old_owners),
        }

    @staticmethod
    def left_run(numbers, info, away):
        """The old lines of a gap, a run of moved lines (blank lines inside it included) taken together."""
        said, run = [], []
        numbers = list(numbers)
        for spot, number in enumerate(numbers):
            inside = info[number - 1][0] == "blank" and run and any(away(n) for n in numbers[spot + 1 : spot + 2])
            if away(number) or inside:
                run.append(number)
                continue
            if run:
                said.append(("away", run))
                run = []
            said.append(("line", number))
        if run:
            said.append(("away", run))
        return said

    def zip(self, left, right, old_owners, new_owners):
        said = []
        left, right = list(left), list(right)
        while left or right:
            if left and left[0][0] == "away":
                said.append(self.away(left.pop(0)[1], old_owners))
            elif right and right[0][0] == "ghost":
                said.append(self.ghost(right.pop(0)[1], new_owners))
            else:
                row = {}
                if left:
                    row["old"] = left.pop(0)[1]
                if right:
                    row["new"] = right.pop(0)[1]
                said.append(row)
        return said

    def away(self, numbers, owners):
        targets = []
        for number in numbers:
            node = owners[number]
            while node is not None and node.match is None:
                node = node.parent
            if node is not None:
                spot = self.target(node)
                if spot not in targets:
                    targets.append(spot)
        return {"away": numbers, "to": targets}

    def ghost(self, number, owners):
        node = owners[number]
        facing = self.facing(node, number)
        row = {"new": number}
        if facing is not None:
            row["from"] = [node.match.path, facing]
        return row

    def fold(self, rows, old_info, new_info, old_owners):
        """Fold long unchanged runs, and long runs of old lines facing nothing behind a bar saying what they held."""

        # An edited line facing nothing stays in sight: it belongs to a statement whose two versions are compared.
        def lone(row):
            return "away" in row or ("new" not in row and old_info[row["old"] - 1][0] in ("del", "blank"))

        def same(row):
            return (
                all(
                    row.get(side) is None or info[row[side] - 1][0] in ("same", "blank")
                    for side, info in (("old", old_info), ("new", new_info))
                )
                and "from" not in row
                and "away" not in row
            )

        said, spot = [], 0
        while spot < len(rows):
            row = rows[spot]
            test = lone if lone(row) else same if same(row) else None
            end = spot + 1
            if test is not None:
                while end < len(rows) and test(rows[end]) and not (test is same and lone(rows[end])):
                    end += 1
            run = rows[spot:end]
            if test is lone and self.counted(run, old_info) > LEFT_RUN:
                said.append(self.summary(run, old_info, old_owners))
            elif test is same and len(run) > SAME_RUN:
                said.extend(run[:SAME_KEPT])
                said.append({"same": run[SAME_KEPT:-SAME_KEPT]})
                said.extend(run[-SAME_KEPT:])
            else:
                said.extend(run)
            spot = end
        return said

    @staticmethod
    def old_numbers(run):
        return [number for row in run for number in (row["away"] if "away" in row else [row["old"]])]

    def counted(self, run, info):
        return sum(1 for number in self.old_numbers(run) if info[number - 1][0] != "blank")

    def summary(self, run, info, owners):
        numbers = self.old_numbers(run)
        kinds = Counter(info[number - 1][0].split()[0] for number in numbers)
        names, seen = [], set()
        for number in numbers:
            node = owners[number]
            while node is not None:
                if (
                    node.name
                    and node.match is None
                    and numbers[0] <= node.start <= numbers[-1]
                    and id(node) not in seen
                ):
                    seen.add(id(node))
                    names.append(node.name)
                node = node.parent
        targets = []
        for row in run:
            for spot in row.get("to", []):
                if spot not in targets:
                    targets.append(spot)
        return {
            "region": run,
            "lines": [numbers[0], numbers[-1]],
            "deleted": kinds["del"],
            "moved": kinds["moved"],
            "names": names,
            "to": targets,
        }


def align(texts):
    """The two-column layout of each Python file, from its old and new text ("" for a side it does not exist on).

    Answered per path: both texts as lines, what each line is ('oldInfo'/'newInfo': its class among same, blank, add,
    del, edit, moved and moved edit, the character spans that changed in it, and the badge its statement carries),
    and 'rows'. A row holds an 'old' line number, a 'new' one or both; a new line that moved here carries 'from',
    the path and old line facing it; 'away' is a run of old lines moved elsewhere, with where they went in 'to';
    'same' holds a fold of unchanged rows, and 'region' a fold of old lines facing nothing, summed up. A file that does
    not parse on either side is left out, for the page to show as a line diff.
    """
    files, olds, news, old_tops, new_tops = {}, [], [], {}, {}
    for path, sides in texts.items():
        try:
            parsed = [parse(side, path, text) if text else None for side, text in enumerate(sides)]
        except (SyntaxError, ValueError):
            continue
        files[path] = parsed
        for side, held in enumerate(parsed):
            if held is not None:
                (olds if side == 0 else news).extend(held[1])
                (old_tops if side == 0 else new_tops)[path] = held[0]
    Matcher(olds, news, old_tops, new_tops).run()
    layout = Layout(files)
    return {path: layout.lay_out(path) for path in files}
