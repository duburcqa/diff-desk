"""What the semantic view makes of Python files: which statements it pairs, and how it lays the pairs out."""

import pytest

import semantic

pytestmark = pytest.mark.xdist_group("collect")

# Long enough that a match is told by its code rather than by the shape every short function shares.
BODY = ["total = 0", "for value in values:", "    total += value * weight", "return total / max(len(values), 1)"]


def code(*lines):
    return "\n".join(lines) + "\n"


def body(indent, rename=lambda line: line):
    return [" " * indent + rename(line) for line in BODY]


def line_of(text, needle):
    return next(number for number, line in enumerate(text.split("\n"), start=1) if needle in line)


def rows_of(entries):
    """Every row a layout holds, folds opened."""
    for entry in entries:
        if "same" in entry:
            yield from rows_of(entry["same"])
        elif "region" in entry:
            yield from rows_of(entry["region"])
        elif "away" in entry:
            yield from ({"old": number} for number in entry["away"])
        else:
            yield entry


def test_a_method_moved_to_another_class_is_shown_once_at_its_new_place_beside_its_old_version():
    old = code("class Reader:", "    def mean(self, values, weight):", *body(8), "", "", "class Writer:", "    pass")
    scaled = body(8, lambda line: line.replace("weight", "scale"))
    new = code("class Reader:", "    pass", "", "", "class Writer:", "    def mean(self, values, scale):", *scaled)
    lined = semantic.align({"stats.py": (old, new)})["stats.py"]
    was, now = line_of(old, "def mean"), line_of(new, "def mean")
    arrived = next(row for row in rows_of(lined["rows"]) if row.get("new") == now)
    assert arrived["from"] == ["stats.py", was]
    assert lined["newInfo"][now - 1][0].startswith("moved")
    assert lined["newInfo"][now - 1][2]["moved"] == ["stats.py", was]
    # Where it was, it is one fold saying where it went rather than a run of removed lines.
    gone = next(entry for entry in lined["rows"] if "away" in entry or "region" in entry)
    assert ["stats.py", now, "Writer.mean"] in gone["to"]


def test_code_moved_to_another_file_is_found_there():
    moved = code("def mean(values, weight):", *body(4))
    files = semantic.align({"a.py": (moved, "pass\n"), "b.py": ("", moved)})
    assert files["b.py"]["newInfo"][0][2]["moved"] == ["a.py", 1]
    assert files["a.py"]["oldInfo"][0][2]["moved"] == ["b.py", 1]


def test_a_rewritten_docstring_is_said_to_be_all_that_changed_and_its_words_are_marked():
    old = code("def mean(values, weight):", '    """Average the values."""', *body(4))
    new = old.replace("Average the values.", "Weighted mean of the values.")
    lined = semantic.align({"stats.py": (old, new)})["stats.py"]
    assert lined["newInfo"][0][2] == {"docstring": True}
    kind, spans, _ = lined["newInfo"][1]
    assert kind == "edit"
    assert [new.split("\n")[1][low:high] for low, high in spans] == ["Weighted", "mean", "of"]
    # The code under it reads the same on both sides.
    assert all(info[0] in ("same", "blank") for info in lined["newInfo"][2:])


def test_a_consistent_rename_names_what_was_renamed():
    old = code("def mean(values, weight):", *body(4))
    new = old.replace("weight", "scale").replace("total", "acc")
    lined = semantic.align({"stats.py": (old, new)})["stats.py"]
    assert lined["newInfo"][0][2]["renamed"] == [["weight", "scale"], ["total", "acc"]]


def test_a_removed_function_folds_behind_a_bar_naming_it():
    kept = code("def mean(values, weight):", *body(4))
    # Longer than the few lines a removal may stay unfolded at, so the fold is what shows it.
    spread = [line.replace("weight", "power") for line in BODY] * 2
    dropped = code("def spread(values, power):", *("    " + line for line in spread))
    lined = semantic.align({"stats.py": (kept + "\n\n" + dropped, kept)})["stats.py"]
    region = next(entry for entry in lined["rows"] if "region" in entry)
    assert region["names"] == ["spread"]
    assert region["deleted"] == len(spread) + 1


def test_a_file_that_does_not_parse_is_left_to_the_line_diff():
    files = semantic.align({"broken.py": ("def f(:\n", "def f():\n    pass\n"), "fine.py": ("x = 1\n", "x = 2\n")})
    assert set(files) == {"fine.py"}
