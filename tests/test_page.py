"""The page, driven in every engine available: selecting lines, commenting on a range, and filling the gaps.

Pointer handling and sticky positioning differ between engines, so these run per engine rather than once. A missing
engine is skipped, never silently dropped from the run.
"""

import json
import time

import pytest

import gen_diff_data
from conftest import FIRST_EDIT, SECOND_EDIT

playwright = pytest.importorskip("playwright.sync_api")

ENGINES = ("chromium", "webkit", "firefox")
STEP = 20


@pytest.fixture(scope="module")
def play():
    with playwright.sync_playwright() as running:
        yield running


@pytest.fixture(scope="module", params=ENGINES)
def browser(play, request):
    kind = getattr(play, request.param)
    try:
        yield kind.launch()
    except playwright.Error:
        # A machine may carry the system browser rather than the bundled build.
        try:
            yield kind.launch(channel="chrome" if request.param == "chromium" else request.param)
        except playwright.Error as error:
            pytest.skip(f"{request.param} is not installed: {error}")


@pytest.fixture
def page(browser, desk):
    opened = browser.new_page(viewport={"width": 1500, "height": 900})
    problems = []
    opened.on("pageerror", lambda error: problems.append(str(error)))
    opened.goto(f"{desk.url}/", wait_until="load")
    # A file marked reviewed is folded, so readiness is a card being drawn rather than any given row being visible.
    opened.wait_for_selector("section.file")
    opened.wait_for_function("() => document.querySelectorAll('tr[data-line]').length > 0")
    yield opened
    assert problems == []
    opened.close()


def until(question, seconds=10.0):
    """Wait for a state the desk reports rather than for a guessed number of milliseconds."""
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        answer = question()
        if answer:
            return answer
        time.sleep(0.1)
    raise AssertionError("the desk never reported it")


def rows(page, kind=""):
    return page.locator(f"tr{kind}[data-line]")


def sample(page):
    """The card of the file both hunks live in, so a range never spans two files by accident."""
    return page.locator("section.file").filter(has=page.locator("text=sample.py")).first


def submit(page, text):
    """Write the comment, add it to the review, and send the batch: recording happens on the send, not the write."""
    page.locator("tr[data-composer='true'] textarea").fill(text)
    page.locator("tr[data-composer='true'] button.solid:not(.direct)").click()
    page.wait_for_selector("#tray[data-open='true']")
    page.locator("#traysend").click()
    page.wait_for_function("() => document.getElementById('tray').dataset.open === 'false'")


def submit_alone(page, text):
    """Send one comment straight from the box, without it waiting in the review tray."""
    page.locator("tr[data-composer='true'] textarea").fill(text)
    page.locator("tr[data-composer='true'] button.solid.direct").click()
    page.wait_for_function("() => document.querySelectorAll(\"tr[data-composer='true']\").length === 0")


def drag(page, first, last, column):
    """Press on one row and pull to another, the way a hand does it: in small steps, over the given column.

    A cursor cannot leave the window, so a target below the fold is reached by holding near the edge until the page has
    scrolled it into view, then releasing on it - which is also what exercises the drag's own edge scrolling.
    """
    where = {"pin": "button.pin", "rail": "td.ln", "code": "td.code"}[column]
    # Press on something in view, the way a hand has to: mid-viewport, clear of the header floating over the top.
    first.evaluate("node => node.scrollIntoView({block: 'center'})")
    page.wait_for_timeout(80)
    if column == "pin":
        first.locator("td.code").first.hover()
    start = first.locator(where).first.bounding_box()
    x0, y0 = start["x"] + start["width"] / 2, start["y"] + start["height"] / 2
    tall = page.viewport_size["height"]
    page.mouse.move(x0, y0)
    page.mouse.down()
    for _ in range(60):
        end = last.locator("td.code" if column == "pin" else where).first.bounding_box()
        y1 = end["y"] + end["height"] / 2
        x1 = end["x"] + end["width"] / 2 if column != "pin" else x0
        if 40 < y1 < tall - 40:
            page.mouse.move(x1, y1)
            break
        # Out of view in whichever direction: hold near that edge and let the page scroll the target in.
        page.mouse.move(x0, 20 if y1 <= 40 else tall - 20)
        page.wait_for_timeout(40)
    # What the range holds at the moment of release, which is what the release must not change.
    held = page.locator("tr.sel").count()
    page.mouse.up()
    page.wait_for_timeout(80)
    return held, page.locator("tr.sel").count()


@pytest.mark.parametrize("column", ["pin", "rail"])
@pytest.mark.parametrize("upward", [False, True])
def test_dragging_lines_selects_the_range_and_opens_the_box(page, column, upward):
    # Within one file: a range belongs to a single diff, so the rows are taken from one card rather than by position.
    lines = sample(page).locator("tr[data-line]")
    first, last = (lines.nth(9), lines.nth(2)) if upward else (lines.nth(2), lines.nth(9))
    held, kept = drag(page, first, last, column)
    assert held >= 4
    # The release must not shrink what was selected: a trailing click aimed at a line would collapse it.
    assert kept == held
    assert page.locator("tr[data-composer='true']").count() == 1


def test_one_range_covers_removed_and_added_lines_together(page, desk):
    card = sample(page)
    removed = card.locator("tr.d[data-line]").first
    added = card.locator("tr.a[data-line]").first
    drag(page, removed, added, "pin")
    picked = page.evaluate("""() => {
      const chosen = [...document.querySelectorAll('tr.sel')];
      return {removed: chosen.filter((row) => row.classList.contains('d')).length,
              added: chosen.filter((row) => row.classList.contains('a')).length};
    }""")
    assert picked["removed"] >= 1 and picked["added"] >= 1

    # A line range is only expressible on one side, so a range touching added lines is anchored there.
    submit(page, "both sides at once")
    note = desk.get("/comments")[-1]
    assert note["side"] == "new"
    assert note["line"] <= FIRST_EDIT <= (note["endLine"] or note["line"])
    assert note["text"] == "both sides at once"


def test_clicking_one_line_comments_on_that_line_alone(page, desk):
    line = sample(page).locator("tr.a[data-line]").last
    where = int(line.get_attribute("data-line"))
    assert where == SECOND_EDIT
    line.locator("td.code").first.hover()
    line.locator("button.pin").first.click()
    assert page.locator("tr.sel").count() == 1
    submit(page, "this line only")
    note = desk.get("/comments")[-1]
    assert note["text"] == "this line only"
    assert note["line"] == where
    assert not note.get("endLine") or note["endLine"] == note["line"]


def test_a_thread_can_be_answered_rewritten_closed_and_reopened_from_the_page(page, desk):
    line = sample(page).locator("tr.a[data-line]").first
    line.locator("td.code").first.hover()
    line.locator("button.pin").first.click()
    submit(page, "the remark as first written")
    seq = desk.get("/comments")[-1]["seq"]
    thread = page.locator(f"#note-{seq}")

    thread.locator("textarea").fill("a reply from the reviewer")
    thread.locator("button.ghost").filter(has_text="Reply").click()
    page.wait_for_function(f"() => document.querySelectorAll('#note-{seq} .reply').length === 1")
    said = {row["seq"]: row for row in desk.get("/comments")}[seq]
    assert [(reply["who"], reply["text"]) for reply in said["replies"]] == [("you", "a reply from the reviewer")]

    thread = page.locator(f"#note-{seq}")
    thread.locator("button.tiny").filter(has_text="Edit").click()
    thread.locator("textarea").first.fill("the remark, rewritten")
    thread.locator("button.solid").filter(has_text="Save").click()
    page.wait_for_timeout(400)
    rewritten = {row["seq"]: row for row in desk.get("/comments")}[seq]
    assert rewritten["text"] == "the remark, rewritten"
    # Rewriting keeps what it said before, so an edit never silently rewrites history.
    assert [earlier["text"] for earlier in rewritten["edits"]] == ["the remark as first written"]

    page.locator(f"#note-{seq} button.solid").filter(has_text="Resolve").click()
    page.wait_for_timeout(400)
    assert {row["seq"]: row for row in desk.get("/comments")}[seq]["state"] == "resolved"
    # A resolved thread folds to its remark alone: its replies and its actions are one click away, never discarded.
    folded = page.locator(f"#note-{seq} .thread.folded")
    assert folded.count() == 1
    assert page.locator(f"#note-{seq} .reply").count() == 0
    assert page.locator(f"#note-{seq} textarea").count() == 0
    page.locator(f"#note-{seq} button.tiny").filter(has_text="resolved").click()
    page.wait_for_timeout(200)
    assert page.locator(f"#note-{seq} .thread.folded").count() == 0
    assert page.locator(f"#note-{seq} .reply").count() == 1
    page.locator(f"#note-{seq} button.ghost").filter(has_text="Reopen").click()
    page.wait_for_timeout(400)
    reopened = {row["seq"]: row for row in desk.get("/comments")}[seq]
    assert reopened["state"] == "open"
    # Closing and reopening leave the thread exactly as it was: no reply invented, none dropped.
    assert reopened["text"] == "the remark, rewritten"
    assert [reply["text"] for reply in reopened["replies"]] == ["a reply from the reviewer"]


def test_one_comment_can_be_sent_without_a_batch(page, desk):
    line = sample(page).locator("tr.a[data-line]").first
    where = int(line.get_attribute("data-line"))
    line.locator("td.code").first.hover()
    line.locator("button.pin").first.click()
    before = len(desk.get("/comments"))
    submit_alone(page, "sent straight from the box")
    page.wait_for_timeout(300)
    rows = desk.get("/comments")
    # Recorded on its own, and the tray is never involved.
    assert len(rows) == before + 1
    assert rows[-1]["text"] == "sent straight from the box"
    assert rows[-1]["line"] == where
    assert page.locator("#tray[data-open='true']").count() == 0


def test_the_file_list_follows_the_folders(page):
    folders = page.locator("#filelist .folder")
    assert folders.count() >= 1
    names = [name.strip() for name in page.locator("#filelist .foldername .name").all_inner_texts()]
    # A chain of single-child directories is one row, so a deep path does not cost a level of nesting per segment.
    assert "pkg/sub" in names
    shelf = page.locator("#filelist .folder").filter(has=page.locator(".foldername", has_text="pkg/sub")).first
    assert shelf.get_attribute("data-open") == "true"
    inside = shelf.locator(".fileitem")
    assert inside.count() >= 1
    # Whatever depth a file sits at, its bar and its count stand in one column: the row spans the panel, and only its
    # indentation moves. A row sized to its own name would put every count wherever that name happened to end.
    columns = page.evaluate("""() => {
      const rows = [...document.querySelectorAll('#filelist .fileitem')];
      const edge = (row, part, side) => Math.round(row.querySelector(part).getBoundingClientRect()[side]);
      return {
        depths: new Set(rows.map((row) => parseInt(getComputedStyle(row).paddingLeft))).size,
        statRight: new Set(rows.map((row) => edge(row, '.stat', 'right'))).size,
        barLeft: new Set(rows.map((row) => edge(row, '.bar', 'left'))).size,
      };
    }""")
    assert columns["depths"] >= 2
    assert columns["statRight"] == 1
    assert columns["barLeft"] == 1

    # A folder folds away, and stays folded across reloads so a deep diff can be read a directory at a time.
    shelf.locator(".foldername").first.click()
    assert shelf.get_attribute("data-open") == "false"
    page.reload(wait_until="load")
    page.wait_for_selector("#filelist .folder")
    again = page.locator("#filelist .folder").filter(has=page.locator(".foldername", has_text="pkg/sub")).first
    assert again.get_attribute("data-open") == "false"
    # Walking onto a file inside a folded folder reveals it rather than marking something out of sight.
    page.keyboard.press("j")
    page.keyboard.press("j")
    page.wait_for_timeout(200)
    current = page.locator("#filelist .fileitem[data-current='true']")
    assert current.count() == 1
    assert current.first.is_visible()
    again.locator(".foldername").first.click()
    page.evaluate("() => localStorage.removeItem('diffdesk.folded')")


def test_a_comment_keeps_its_code_and_its_line_breaks(page, desk):
    branch = page.evaluate("() => data.branches[0].ref")
    pasted = "Try this instead:\n\n```python\nif x:\n    return math.pi * r**2\n```\n\nand call `area()` after."
    desk.post("/comments", [{"branch": branch, "path": "sample.py", "line": FIRST_EDIT, "side": "new", "text": pasted}])
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    thread = page.locator(".thread").filter(has=page.locator("pre code")).first
    block = thread.locator("pre code").first
    # The fence becomes a code block keeping its indentation, and its language line is a label rather than code.
    assert block.inner_text() == "if x:\n    return math.pi * r**2"
    assert "python" not in block.inner_text()
    assert thread.locator("code", has_text="area()").count() == 1
    # Text a reviewer pastes is text: it never becomes part of the page.
    hostile = "<script>window.broken = 1</script> and <b>bold</b>"
    desk.post(
        "/comments", [{"branch": branch, "path": "sample.py", "line": FIRST_EDIT, "side": "new", "text": hostile}]
    )
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    assert page.evaluate("() => window.broken") is None
    assert page.locator(".thread b").count() == 0
    assert page.locator(".thread .said", has_text="<b>bold</b>").count() >= 1


def test_a_local_comment_can_be_turned_towards_the_pull_request(page, desk):
    branch = page.evaluate("() => data.branches[0].ref")
    made = desk.post(
        "/comments", [{"branch": branch, "path": "sample.py", "line": FIRST_EDIT, "side": "new", "text": "local first"}]
    )
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    thread = page.locator(f"#note-{made['seq']}")
    standing = thread.locator("button.mark").first
    assert standing.inner_text() == "local only"
    # The decision is changeable after the fact, so a comment written before deciding does not have to be rewritten.
    standing.click()
    page.wait_for_timeout(400)
    assert {row["seq"]: row for row in desk.get("/comments")}[made["seq"]]["github"] in ("pending", "failed", "refused")
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    back = page.locator(f"#note-{made['seq']} button.mark").first
    assert back.inner_text() != "local only"
    back.click()
    page.wait_for_timeout(400)
    assert {row["seq"]: row for row in desk.get("/comments")}[made["seq"]]["github"] == "none"


def test_a_folded_thread_says_what_it_is_about_even_when_it_is_only_code(page, desk):
    branch = page.evaluate("() => data.branches[0].ref")
    made = desk.post(
        "/comments",
        [{"branch": branch, "path": "sample.py", "line": FIRST_EDIT, "side": "new", "text": "```py\nreturn 1\n```"}],
    )
    desk.post("/resolve", {"seq": [made["seq"]], "who": "session"})
    page.reload(wait_until="load")
    page.wait_for_selector(f"#note-{made['seq']} .thread.folded")
    said = page.locator(f"#note-{made['seq']} .said").first
    # Folded to one line, and a comment made only of code still says what it is about rather than showing nothing.
    assert said.inner_text().strip() == "return 1"
    # Everything hanging under a diff is sized to what is on screen, so nothing sits beyond the right edge.
    fits = page.evaluate(f"""() => {{
      const thread = document.querySelector('#note-{made["seq"]} .thread');
      const body = thread.closest('.body');
      return thread.getBoundingClientRect().width <= body.clientWidth + 1;
    }}""")
    assert fits


def test_a_comment_stays_inside_the_view_when_the_diff_is_scrolled(page, desk):
    branch = page.evaluate("() => data.branches[0].ref")
    long = "A remark long enough to need clipping: " + "the quick brown fox jumps over the lazy dog. " * 12
    desk.post("/comments", [{"branch": branch, "path": "sample.py", "line": FIRST_EDIT, "side": "new", "text": long}])
    # Narrow enough that the diff's own lines overflow, which is when a comment used to be dragged off with them.
    page.set_viewport_size({"width": 820, "height": 900})
    page.reload(wait_until="load")
    page.wait_for_selector(".thread")
    for shift in (0, 400):
        held = page.evaluate(
            """(shift) => {
              // A comment on a line, which is the one that hangs inside the scrolling diff.
              const thread = document.querySelector('.body .thread');
              const body = thread.closest('.body');
              body.scrollTo({left: shift});
              return new Promise((done) => setTimeout(() => {
                const seen = body.getBoundingClientRect();
                const box = thread.getBoundingClientRect();
                const line = thread.querySelector('.line');
                done({
                  leftGap: Math.round(box.left - seen.left),
                  rightGap: Math.round(seen.right - box.right),
                  lastInside: Math.round(line.lastElementChild.getBoundingClientRect().right) <= Math.round(seen.right),
                });
              }, 250));
            }""",
            shift,
        )
        # Flush with what is on screen at any scroll position, so nothing it carries is ever out of reach.
        assert held["leftGap"] == 0
        assert held["rightGap"] == 0
        assert held["lastInside"]
    page.set_viewport_size({"width": 1500, "height": 900})


def test_a_pending_comment_can_be_sent_on_its_own_from_the_tray(page, desk):
    lines = sample(page).locator("tr.a[data-line]")
    for index, text in ((0, "the first remark"), (1, "the second remark")):
        line = lines.nth(index)
        line.locator("td.code").first.hover()
        line.locator("button.pin").first.click()
        page.locator("tr[data-composer='true'] textarea").fill(text)
        page.locator("tr[data-composer='true'] button.solid:not(.direct)").click()
    assert page.locator("#traylist li").count() == 2
    before = len(desk.get("/comments"))

    page.locator("#traylist li button.tiny").first.click()
    # One leaves, the rest stay pending: a review is not held up by the comment still being thought about.
    page.wait_for_function("() => document.querySelectorAll('#traylist li').length === 1")
    until(lambda: len(desk.get("/comments")) == before + 1)
    assert page.locator("#tray").get_attribute("data-open") == "true"
    rows = desk.get("/comments")
    assert len(rows) == before + 1
    assert rows[-1]["text"] == "the first remark"
    page.locator("#traydrop").click()


def test_each_recorded_batch_can_be_sent_by_itself(page, desk):
    branch = page.evaluate("() => data.branches[0].ref")
    for text in ("first batch", "second batch"):
        desk.post(
            "/comments", [{"branch": branch, "path": "sample.py", "line": FIRST_EDIT, "side": "new", "text": text}]
        )
    listed = desk.get("/comments")
    batches = [row["batch"] for row in listed[-2:]]
    assert batches[0] != batches[1]

    # A pull request is what makes sending possible, so the repository is given one for the length of this test.
    knows = [
        {"match": "repos/someone/somewhere --jq", "out": "someone/somewhere"},
        {"match": "pr list", "out": json.dumps([{"number": 7, "url": "u", "title": "t", "headRefName": branch}])},
    ]
    desk.github_answers(rules=knows)
    gen_diff_data.run(desk.repo, "remote", "add", "origin", "https://github.com/someone/somewhere.git")
    try:
        rescan = desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": [branch]})
        assert rescan["ok"], rescan.get("error")
        page.reload(wait_until="load")
        page.wait_for_selector("section.file")
        page.locator("#logopen").click()
        page.wait_for_selector("#log[data-open='true']")
        groups = page.locator("#logrows .batch")
        assert groups.count() >= 2
        sends = page.locator("#logrows .batchhead button.tiny")
        # Every batch still holding something unposted can be sent by itself; one already on the PR offers nothing.
        # What the panel lists by default: batches still holding something unposted, unresolved and not deleted.
        pending = {
            row["batch"]
            for row in desk.get("/comments")
            if row["branch"] == branch and row["github"] != "posted" and row["state"] not in ("resolved", "deleted")
        }
        assert sends.count() == len(pending)
        assert "PR #7" in sends.last.inner_text()

        # Sending one batch leaves the others exactly as they were.
        landed = {"match": "/reviews", "out": json.dumps({"html_url": "https://github.com/x/y/pull/7#review-9"})}
        desk.github_answers(rules=[landed, *knows])
        sends.last.click()
        after = until(
            lambda: (
                {row["seq"]: row for row in desk.get("/comments")}
                if {row["seq"]: row for row in desk.get("/comments")}[listed[-1]["seq"]]["github"] == "posted"
                else None
            )
        )
        # Sending one batch leaves every other comment exactly as it was.
        assert after[listed[-1]["seq"]]["github"] == "posted"
        assert after[listed[-2]["seq"]]["github"] == "none"
    finally:
        gen_diff_data.run(desk.repo, "remote", "remove", "origin")
        desk.github_answers(code=1, err="gh: Not Found (HTTP 404)")
        desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": [branch]})


def test_a_comment_resolved_here_does_not_claim_the_pull_request_agrees(page, desk):
    branch = page.evaluate("() => data.branches[0].ref")
    made = desk.post(
        "/comments",
        {
            "comments": [
                {
                    "branch": branch,
                    "path": "sample.py",
                    "line": FIRST_EDIT,
                    "side": "new",
                    "text": "a remark of its own",
                }
            ],
            "github": True,
        },
    )
    seq = made["seqs"][0]
    desk.github_answers(out=json.dumps({"html_url": "https://github.com/x/y/pull/3#review-2"}))
    desk.post("/publish", {"repo": "someone/somewhere", "pr": 3, "seq": [seq]})
    # Whatever the page sweeps in the background cannot reach GitHub, so the pull request is left unasked.
    desk.github_answers(code=1, err="dial tcp: lookup api.github.com: no such host")
    desk.post("/resolve", {"seq": [seq], "who": "session"})
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")

    page.locator("#logopen").click()
    page.wait_for_selector("#log[data-open='true']")
    # Settled comments are out of the listing by default, so this one is asked for.
    page.locator("#logresolved").check()
    page.wait_for_timeout(150)
    row = page.locator("#logrows .logrow").filter(has_text="a remark of its own").first
    marks = row.locator(".mark").all_inner_texts()
    # Closed here and posted there, but the thread on the pull request is not resolved - and the page must say so.
    assert "resolved here" in marks
    assert "on the PR" in marks
    assert [mark for mark in marks if mark.startswith("not resolved there")]
    assert "resolved there" not in marks
    page.locator("#logresolved").uncheck()
    page.locator("#logclose").click()


def test_marking_a_file_reviewed_from_inside_it_brings_reading_back_to_the_next_one(page):
    # Short enough that one file fills more than the view, which is when folding it can carry the reader forwards.
    page.set_viewport_size({"width": 1200, "height": 420})
    card = sample(page)
    path = card.get_attribute("data-path")
    page.evaluate(
        """(path) => {
          const node = document.querySelector(`section.file[data-path="${CSS.escape(path)}"]`);
          window.scrollTo(0, node.offsetTop + node.offsetHeight * 0.6);
        }""",
        path,
    )
    page.wait_for_timeout(150)
    started = page.evaluate("() => Math.round(window.scrollY)")
    card.locator("input[type=checkbox]").check()
    page.wait_for_timeout(400)
    placed = page.evaluate(
        """(path) => {
          const node = document.querySelector(`section.file[data-path="${CSS.escape(path)}"]`);
          const after = node.nextElementSibling;
          const page = document.scrollingElement;
          return {
            header: Math.round(document.querySelector('header').getBoundingClientRect().bottom),
            head: Math.round(node.getBoundingClientRect().top),
            next: after ? Math.round(after.getBoundingClientRect().top) : null,
            folded: node.dataset.open,
            scroll: Math.round(window.scrollY),
            atEnd: Math.round(page.scrollTop + page.clientHeight) >= Math.round(page.scrollHeight) - 2,
          };
        }""",
        path,
    )
    assert placed["folded"] == "false"
    # Reading resumes at the file just folded away, with the next one under it: nothing between is scrolled past. Where
    # the document has run out of room below - the last file of a diff - it comes only as near the top as that allows.
    assert placed["head"] >= placed["header"] - 1
    if not placed["atEnd"]:
        assert placed["head"] <= placed["header"] + 40
    if placed["next"] is not None:
        assert placed["next"] > placed["head"]
    assert placed["scroll"] < started
    card.locator("input[type=checkbox]").uncheck()
    page.evaluate("() => localStorage.clear()")
    page.set_viewport_size({"width": 1500, "height": 900})


def test_a_file_opened_by_hand_stays_open_when_the_page_redraws(page):
    card = page.locator("section.file").first
    card.locator("input[type=checkbox]").check()
    page.wait_for_selector("section.file[data-done='true'][data-open='false']")

    # Opened to look at what is inside it, a reviewed file must stay open: the page redraws itself while it is read.
    card.locator(".filehead").first.click()
    assert card.get_attribute("data-open") == "true"
    page.evaluate("() => render()")
    assert card.get_attribute("data-open") == "true"

    # Folding one that is not reviewed lasts just as long.
    card.locator("input[type=checkbox]").uncheck()
    card.locator(".filehead").first.click()
    assert card.get_attribute("data-open") == "false"
    page.evaluate("() => render()")
    assert card.get_attribute("data-open") == "false"
    card.locator(".filehead").first.click()
    page.evaluate("() => localStorage.clear()")


def test_a_file_changed_since_it_was_reviewed_opens_itself(page, desk):
    branch = page.evaluate("() => data.branches[0].ref")
    card = page.locator("section.file[data-path='added.py']")
    card.locator("input[type=checkbox]").check()
    page.wait_for_selector("section.file[data-path='added.py'][data-open='false']")

    gen_diff_data.run(desk.repo, "checkout", "-q", branch)
    written = desk.repo / "added.py"
    kept = written.read_text()
    try:
        written.write_text(kept + "SAID_ON_DISK = 1\n")
        # Refreshed rather than reloaded, so the fold the reader just made is still remembered when the diff arrives.
        page.wait_for_function("() => !document.getElementById('moved').hidden", timeout=30000)
        page.locator("#moved").click()
        page.wait_for_function("() => document.getElementById('moved').hidden", timeout=30000)
        # Reviewed and folded, then changed: the reader has not seen this diff, so it is not folded away for them.
        again = page.locator("section.file[data-path='added.py']")
        assert again.get_attribute("data-open") == "true"
        assert "changed since review" in again.locator(".filehead").inner_text().lower()

        # Folded by hand on this diff, it stays folded through the redraws.
        again.locator(".filehead").first.click()
        page.evaluate("() => render()")
        assert again.get_attribute("data-open") == "false"
    finally:
        written.write_text(kept)
        gen_diff_data.run(desk.repo, "checkout", "-q", "main")
        desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": [branch]})
        page.reload(wait_until="load")
        page.wait_for_selector("section.file")
        page.locator("section.file[data-path='added.py'] input[type=checkbox]").uncheck()
        page.evaluate("() => localStorage.clear()")


def test_a_resolved_thread_answered_since_opens_itself(page, desk):
    card = sample(page)
    line = card.locator("tr.a[data-line]").first
    line.locator("td.code").first.hover()
    line.locator("button.pin").first.click()
    saying = f"answered after settling, number {len(desk.get('/comments')) + 1}"
    submit(page, saying)
    page.wait_for_timeout(200)
    made = desk.get("/comments")[-1]["seq"]
    desk.post("/resolve", {"seq": [made], "who": "session"})
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    # Settled and seen, so it is folded to its remark.
    assert page.locator(f"#note-{made} .thread.folded").count() == 1

    desk.post("/reply", {"seq": made, "text": "one more thing about it", "who": "session"})
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    # Answered since, which the reader has not read: the thread shows what was said rather than hiding it.
    assert page.locator(f"#note-{made} .thread.folded").count() == 0
    assert "one more thing about it" in page.locator(f"#note-{made}").inner_text()

    # Read now, so it folds again on its own.
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    assert page.locator(f"#note-{made} .thread.folded").count() == 1
    page.evaluate("() => localStorage.clear()")


def test_marking_a_file_reviewed_from_above_it_leaves_the_view_alone(page):
    page.evaluate("() => window.scrollTo(0, 0)")
    page.wait_for_timeout(120)
    before = page.evaluate("() => Math.round(window.scrollY)")
    card = page.locator("section.file").first
    card.locator("input[type=checkbox]").check()
    page.wait_for_timeout(300)
    # The reader had not reached inside it, so folding it must not move them anywhere.
    assert page.evaluate("() => Math.round(window.scrollY)") == before
    card.locator("input[type=checkbox]").uncheck()
    page.evaluate("() => localStorage.clear()")


def test_copying_a_selection_of_lines_yields_the_code_alone(page):
    page.evaluate("""() => {
      window.__copied = null;
      document.addEventListener('copy', (event) => {
        window.__copied = event.clipboardData.getData('text/plain');
      });
    }""")
    wanted = page.evaluate("""() => {
      const card = [...document.querySelectorAll('section.file')].find((node) => node.textContent.includes('deep.py'));
      const rows = [...card.querySelectorAll('.body tr[data-line]')];
      const range = document.createRange();
      range.setStart(rows[0].querySelector('td.code'), 0);
      range.setEnd(rows[rows.length - 1].querySelector('td.code'), 1);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      document.execCommand('copy');
      return rows.map((row) => row.querySelector('td.code').textContent);
    }""")
    copied = page.evaluate("() => window.__copied")
    # The code of the selected lines, and nothing the diff put beside it: no line numbers, no markers.
    assert copied == "\n".join(wanted)
    assert not any(line.strip().startswith(("+", "-")) and line.strip() in "+-" for line in copied.split("\n"))
    # Indentation is what makes pasted code usable, so it survives.
    assert any(line.startswith("    ") for line in copied.split("\n"))
    page.evaluate("() => window.getSelection().removeAllRanges()")


def test_copying_a_single_line_yields_the_code_alone(page):
    page.evaluate("""() => {
      window.__copied = null;
      document.addEventListener('copy', (event) => {
        window.__copied = event.clipboardData.getData('text/plain');
      });
    }""")
    wanted = page.evaluate("""() => {
      const row = document.querySelectorAll('section.file .body tr[data-line]')[2];
      const range = document.createRange();
      range.selectNode(row);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      document.execCommand('copy');
      return row.querySelector('td.code').textContent;
    }""")
    # One line is the common case, and it carries the numbers just as a span of rows does.
    assert page.evaluate("() => window.__copied") == wanted
    page.evaluate("() => window.getSelection().removeAllRanges()")


def test_a_letter_held_with_a_modifier_is_left_to_the_browser(page):
    page.evaluate("""() => {
      window.__taken = {};
      document.addEventListener('keydown', (event) => {
        window.__taken[event.key] = event.defaultPrevented;
      });
      const cell = document.querySelector('section.file .body tr[data-line] td.code');
      const range = document.createRange();
      range.setStart(cell.firstChild, 0);
      range.setEnd(cell.firstChild, Math.min(10, cell.textContent.length));
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }""")
    before = page.evaluate("() => String(window.getSelection())")
    for combination in ("Meta+c", "Control+c", "Meta+r", "Meta+j", "Meta+k"):
        page.keyboard.press(combination)
    page.wait_for_timeout(150)
    taken = page.evaluate("() => window.__taken")
    # Copy, reload and the rest belong to the browser: taking them left the shortcut doing nothing in their place.
    assert not any(taken.values()), taken
    assert page.locator("tr[data-composer='true']").count() == 0
    assert page.evaluate("() => String(window.getSelection())") == before

    # The same letters alone are still this page's own.
    page.keyboard.press("c")
    page.wait_for_timeout(150)
    assert page.evaluate("() => window.__taken.c") is True
    page.keyboard.press("Escape")
    for _ in range(page.locator("tr[data-composer='true']").count()):
        page.locator("tr[data-composer='true'] button.ghost").first.click()


def test_a_file_can_be_commented_on_as_a_whole(page, desk):
    card = sample(page)
    path = card.locator(".path").first.inner_text()
    before = len(desk.get("/comments"))
    card.locator("button.tiny").filter(has_text="Comment on the file").click()
    page.locator(".filenote.writing textarea").fill("this file wants splitting in two")
    page.locator(".filenote.writing button.solid.direct").click()
    page.wait_for_function("() => document.querySelectorAll('.filenote.writing').length === 0")
    until(lambda: len(desk.get("/comments")) == before + 1)
    note = desk.get("/comments")[-1]
    # Tied to the file and to no line of it, which is what makes it a remark about the file.
    assert note["path"] == path
    assert note["side"] == "file"
    assert note["line"] == 0
    assert note["text"] == "this file wants splitting in two"

    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    again = sample(page)
    # Shown under the file's own header, where it was written, rather than against a line it does not have.
    thread = again.locator(".filenote .thread").first
    assert "this file wants splitting in two" in thread.inner_text()
    assert "the file" in thread.locator(".who").first.inner_text()
    page.locator("#logopen").click()
    page.wait_for_selector("#log[data-open='true']")
    assert "the file" in page.locator("#logrows .logrow").filter(has_text="wants splitting").first.inner_text()
    page.locator("#logclose").click()


def test_a_review_keeps_its_comments_and_ticks_when_opened_from_another_ref(page, desk):
    branch = page.evaluate("() => data.branches[0].ref")
    knows = [
        {"match": "repos/someone/somewhere --jq", "out": "someone/somewhere"},
        {
            "match": "pr list",
            "out": json.dumps([{"number": 21, "url": "u", "title": "the same work", "headRefName": branch}]),
        },
        {
            "match": "pr view",
            "out": json.dumps(
                {"number": 21, "url": "u", "title": "the same work", "headRefName": branch, "baseRefName": "main"}
            ),
        },
    ]
    desk.github_answers(rules=knows)
    gen_diff_data.run(desk.repo, "remote", "add", "origin", "https://github.com/someone/somewhere.git")
    try:
        assert desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": [branch]})["ok"]
        page.reload(wait_until="load")
        page.wait_for_selector("section.file")
        # A comment and a tick, made while the review is open on its branch. The remark comes first: ticking folds the
        # file away, and a folded file has no lines to reach.
        card = sample(page)
        name = card.locator(".path").first.inner_text()
        line = card.locator("tr.a[data-line]").first
        line.locator("td.code").first.hover()
        line.locator("button.pin").first.click()
        submit(page, "said on the branch")
        card.locator("input[type=checkbox]").check()
        page.wait_for_timeout(200)

        # The same review, opened from the head fetched by number instead of from the branch.
        gen_diff_data.run(desk.repo, "update-ref", "refs/diffdesk/pull/21", branch)
        assert desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": ["#21"]})["ok"]
        page.reload(wait_until="load")
        page.wait_for_selector("section.file")
        again = page.locator("section.file").filter(has=page.locator(f"text={name}")).first
        # Same work, so the same history: the file is still ticked, and the remark is still there under it.
        assert again.get_attribute("data-done") == "true"
        assert again.get_attribute("data-open") == "false"
        again.locator("button.grow").click()
        page.wait_for_timeout(200)
        assert "said on the branch" in again.inner_text()
        # The log is what this review holds, so finding the remark there is the history being attributed to it.
        page.locator("#logopen").click()
        page.wait_for_selector("#log[data-open='true']")
        assert page.locator("#logrows .logrow").filter(has_text="said on the branch").count() >= 1
        page.locator("#logclose").click()
    finally:
        gen_diff_data.run(desk.repo, "update-ref", "-d", "refs/diffdesk/pull/21")
        gen_diff_data.run(desk.repo, "remote", "remove", "origin")
        desk.github_answers(code=1, err="gh: Not Found (HTTP 404)")
        desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": [branch]})
        page.evaluate("() => localStorage.clear()")


def test_the_log_reaches_a_comment_and_leaves_what_is_settled_out(page, desk):
    card = sample(page)
    line = card.locator("tr.a[data-line]").first
    line.locator("td.code").first.hover()
    line.locator("button.pin").first.click()
    # Worded uniquely: each engine runs against the same desk, and a shared wording would find the other's row.
    saying = f"worth reaching, number {len(desk.get('/comments')) + 1}"
    submit(page, saying)
    page.wait_for_timeout(200)
    made = desk.get("/comments")[-1]["seq"]

    # A file folded away still holds its comment, and the panel goes to it.
    card.locator("input[type=checkbox]").check()
    page.wait_for_timeout(150)
    page.locator("#logopen").click()
    page.wait_for_selector("#log[data-open='true']")
    page.locator("#logrows .logrow").filter(has_text=saying).first.click()
    # The scroll is animated, so the arrival is waited for rather than guessed at.
    page.wait_for_function(
        """(seq) => {
          const thread = document.getElementById(`note-${seq}`);
          if (!thread) return false;
          const box = thread.getBoundingClientRect();
          const covered = document.querySelector("header").getBoundingClientRect().bottom;
          return box.top >= covered && box.bottom < window.innerHeight;
        }""",
        arg=made,
        timeout=8000,
    )
    reached = page.evaluate(
        """(seq) => {
          const thread = document.getElementById(`note-${seq}`);
          if (!thread) return null;
          const box = thread.getBoundingClientRect();
          const covered = document.querySelector("header").getBoundingClientRect().bottom;
          return {
            open: thread.closest('section.file').dataset.open,
            panelShut: document.getElementById('log').dataset.open,
            inView: box.top >= covered && box.bottom < window.innerHeight,
          };
        }""",
        made,
    )
    assert reached is not None
    assert reached["open"] == "true"
    assert reached["panelShut"] == "false"
    assert reached["inView"]
    card.locator("input[type=checkbox]").uncheck()

    # Settled here, it drops out of the listing until the listing is asked to show it.
    desk.post("/resolve", {"seq": [made], "who": "session"})
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    page.locator("#logopen").click()
    page.wait_for_selector("#log[data-open='true']")
    listing = page.locator("#logrows .logrow").filter(has_text=saying)
    assert listing.count() == 0
    page.locator("#logresolved").check()
    page.wait_for_timeout(200)
    assert page.locator("#logrows .logrow").filter(has_text=saying).count() >= 1
    page.locator("#logresolved").uncheck()
    page.locator("#logclose").click()
    page.evaluate("() => localStorage.clear()")


def test_the_log_says_where_every_comment_stands(page, desk):
    branch = page.evaluate("() => data.branches[0].ref")
    desk.post(
        "/comments",
        {
            "comments": [
                {"branch": branch, "path": "sample.py", "line": FIRST_EDIT, "side": "new", "text": "for a PR"}
            ],
            "github": True,
        },
    )
    desk.post(
        "/comments", [{"branch": branch, "path": "sample.py", "line": FIRST_EDIT, "side": "new", "text": "local"}]
    )
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    page.locator("#logopen").click()
    page.wait_for_selector("#log[data-open='true']")
    marks = set(page.locator("#logrows .mark").all_inner_texts())
    assert "waiting for GitHub" in marks
    assert "local only" in marks
    # What is owed is offered for sending, rather than being discoverable only in a log file.
    assert page.locator("#logretry").is_enabled()
    assert "waiting" in page.locator("#logopen").inner_text()
    page.locator("#logclose").click()


def test_a_comment_follows_the_line_it_was_written_against(page, desk):
    card = sample(page)
    line = card.locator("tr.a[data-line]").first
    was = int(line.get_attribute("data-line"))
    code = line.locator("td.code").first.inner_text()
    line.locator("td.code").first.hover()
    line.locator("button.pin").first.click()
    submit(page, "about this very line")
    page.wait_for_timeout(200)
    note = desk.get("/comments")[-1]
    # The line it was written against is remembered, which is what lets it be found again.
    assert note["anchor"] == code
    assert note["line"] == was

    # The diff moves under it: the same code now sits further down, and something else holds the old line number.
    page.evaluate(
        """([path, code, was]) => {
          const file = view().files.find((entry) => entry.path === path);
          const at = file.lines.findIndex((row) => row[3] === code);
          file.lines.splice(at, 0, ['c', 0, was, 'something inserted above'], ['c', 0, was + 1, 'and another']);
          for (let i = at + 2; i < file.lines.length; i += 1) {
            if (file.lines[i][0] !== 'h' && file.lines[i][2]) file.lines[i][2] += 2;
          }
          render();
        }""",
        [card.locator(".path").first.inner_text(), code, was],
    )
    page.wait_for_timeout(200)
    shown = page.evaluate(
        """() => {
          const threads = [...document.querySelectorAll('.thread')];
          const thread = threads.find((node) => node.textContent.includes('about this very line'));
          if (!thread) return null;
          // Other comments may sit between: the line this one hangs under is the nearest row above that holds code.
          let row = thread.closest('tr').previousElementSibling;
          while (row && !row.querySelector('td.code')) row = row.previousElementSibling;
          if (!row) return null;
          const marks = [...thread.querySelectorAll('.mark')].map((mark) => mark.textContent);
          return {above: row.querySelector('td.code').textContent, marks};
        }"""
    )
    # It hangs under the code it was written about, wherever that ended up, and is not taken for one left behind.
    assert shown is not None
    assert shown["above"] == code
    assert not any(mark == "code moved on" for mark in shown["marks"])


def test_a_comment_whose_line_left_the_diff_is_kept_and_marked(page, desk):
    branch = page.evaluate("() => data.branches[0].ref")
    desk.post(
        "/comments",
        [{"branch": branch, "path": "sample.py", "line": 9999, "side": "new", "text": "anchored to a vanished line"}],
    )
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    page.wait_for_function("() => document.querySelectorAll('.thread.stale').length > 0")
    stale = page.locator(".thread.stale").first
    # Kept with its file and marked, never dropped from the page and never resolved on its behalf.
    assert "anchored to a vanished line" in stale.inner_text()
    assert stale.locator(".mark.outdated").first.inner_text() == "code moved on"
    kept = next(row for row in desk.get("/comments") if row["line"] == 9999)
    assert kept["state"] == "open"


def test_a_release_the_page_never_sees_does_not_leave_it_dragging(page):
    line = rows(page).nth(2)
    line.locator("td.code").first.hover()
    box = line.locator("button.pin").first.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 60)
    assert page.locator("body.dragging").count() == 1
    # A release outside the window is never delivered; the next motion with no button held has to end the drag, or
    # every later hover keeps extending a range nobody is holding.
    page.evaluate("""() => document.dispatchEvent(
      new PointerEvent('pointermove', {clientX: 40, clientY: 40, buttons: 0, bubbles: true})
    )""")
    assert page.locator("body.dragging").count() == 0
    page.mouse.up()


def test_dragging_across_the_code_selects_the_code(page):
    lines = sample(page).locator("tr[data-line]")
    lines.nth(2).evaluate("node => node.scrollIntoView({block: 'center'})")
    page.wait_for_timeout(80)
    one = lines.nth(2).locator("td.code").first.bounding_box()
    four = lines.nth(5).locator("td.code").first.bounding_box()
    page.mouse.move(one["x"] + 20, one["y"] + one["height"] / 2)
    page.mouse.down()
    for step in range(1, 11):
        page.mouse.move(one["x"] + 20 + step * 8, one["y"] + (four["y"] - one["y"]) * step / 10)
        page.wait_for_timeout(10)
    page.mouse.up()
    # Dragging over code is how code is copied, so it selects text and starts no range and no comment box.
    assert page.evaluate("() => String(window.getSelection()).length") > 10
    assert page.locator("tr.sel").count() == 0
    assert page.locator("tr[data-composer='true']").count() == 0


def test_a_gap_can_be_filled_and_leaves_no_bare_delimiter(page):
    card = page.locator("section.file").filter(has=page.locator("text=sample.py")).first
    before = card.locator("tr.c").count()
    gap = card.locator("button.expand").first
    assert "+" in gap.inner_text() or "all" in gap.inner_text()
    gap.click()
    page.wait_for_timeout(600)
    assert card.locator("tr.c").count() > before
    # A line brought in is the file's own, numbered identically on both sides, which is what makes it commentable.
    brought = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('section.file')];
      const card = cards.find((node) => node.textContent.includes('sample.py'));
      const row = card.querySelector('tr.c[data-line]');
      return {
        side: row.dataset.side,
        numbers: [...row.querySelectorAll('td.ln')].map((cell) => cell.textContent),
        text: row.querySelector('td.code').textContent,
      };
    }""")
    assert brought["side"] == "new"
    assert brought["numbers"] == [brought["numbers"][0]] * 2
    assert brought["text"] == f"line {brought['numbers'][0]}"
    bare = page.evaluate(
        """() => [...document.querySelectorAll('tr.h')].filter((row) => !row.querySelector('button')).length"""
    )
    assert bare == 0


def test_expanding_every_gap_reaches_the_whole_file(page):
    card = page.locator("section.file").filter(has=page.locator("text=sample.py")).first
    for _ in range(12):
        buttons = card.locator("button.expand")
        if not buttons.count():
            break
        buttons.first.click()
        page.wait_for_timeout(400)
    shown = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('section.file')];
      const card = cards.find((node) => node.textContent.includes('sample.py'));
      const seen = new Set();
      for (const row of card.querySelectorAll('tr[data-side="new"]')) seen.add(Number(row.dataset.line));
      return {lines: seen.size, first: Math.min(...seen), last: Math.max(...seen)};
    }""")
    assert shown["first"] == 1
    assert shown["lines"] >= SECOND_EDIT


def test_a_branch_with_a_pull_request_says_which_and_opens_it(page, desk):
    branch = page.evaluate("() => data.branches[0].ref")
    knows = [
        {"match": "repos/someone/somewhere --jq", "out": "someone/somewhere"},
        {
            "match": "pr list",
            "out": json.dumps(
                [
                    {
                        "number": 7,
                        "url": "https://github.com/someone/somewhere/pull/7",
                        "title": "A tidy change",
                        "headRefName": branch,
                    }
                ]
            ),
        },
    ]
    desk.github_answers(rules=knows)
    gen_diff_data.run(desk.repo, "remote", "add", "origin", "https://github.com/someone/somewhere.git")
    try:
        assert desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": [branch]})["ok"]
        page.reload(wait_until="load")
        page.wait_for_selector(".tab")
        tab = page.locator(".tab").first
        # Hovering says what the pull request is; the mark shows that the chosen tab now leads to it.
        assert "A tidy change" in tab.get_attribute("title")
        assert "https://github.com/someone/somewhere/pull/7" in tab.get_attribute("title")
        assert tab.get_attribute("aria-selected") == "true"
        assert tab.locator(".away").count() == 1

        # What the page asks the browser to open, rather than following it: a test must reach no further than itself.
        page.evaluate("""() => {
          window.__opened = [];
          window.open = (url) => {
            window.__opened.push(url);
            return null;
          };
        }""")
        tab.click()
        assert page.evaluate("() => window.__opened") == ["https://github.com/someone/somewhere/pull/7"]
    finally:
        gen_diff_data.run(desk.repo, "remote", "remove", "origin")
        desk.github_answers(code=1, err="gh: Not Found (HTTP 404)")
        desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": [branch]})


def test_the_source_panel_stands_clear_of_the_diff(page):
    page.locator("#sourceopen").click()
    page.wait_for_selector("#srcrefs label")
    standing = page.evaluate("""() => {
      const panel = document.getElementById('source');
      const box = panel.getBoundingClientRect();
      const middle = document.elementFromPoint(Math.round(box.left + box.width / 2), Math.round(box.top + 40));
      return {
        visible: panel.offsetParent !== null || getComputedStyle(panel).position === 'fixed',
        clipped: box.height < 40 || box.width < 40,
        onTop: Boolean(middle && middle.closest('#source')),
        belowBar: Math.round(box.top) >= Math.round(document.querySelector('header').getBoundingClientRect().bottom),
      };
    }""")
    # The bar scrolls sideways, so a panel hanging out of it would be clipped by that: it stands outside the bar, over
    # the diff rather than behind it.
    assert standing["visible"]
    assert not standing["clipped"]
    assert standing["onTop"]
    assert standing["belowBar"]
    page.locator("#sourceopen").click()


def test_the_source_panel_lists_what_can_be_reviewed(page):
    page.locator("#sourceopen").click()
    page.wait_for_selector("#srcrefs label")
    listed = page.locator("#srcrefs label")
    assert listed.count() >= 1
    assert "feature" in listed.first.inner_text()
    # The branch under review is already ticked, so a rescan does not silently drop it.
    assert listed.first.locator("input").is_checked()
    # Pull requests are listed only where there are any, so a plain local repository shows branches alone.
    assert page.locator("#srcpulls label").count() == 0
    page.locator("#srcfilter").fill("nothing matches this")
    assert page.locator("#srcrefs label").count() == 1
    page.locator("#srcfilter").fill("")


def test_a_file_changed_since_it_was_reviewed_says_so_where_the_count_is(page, desk):
    card = sample(page)
    card.locator("input[type=checkbox]").check()
    page.wait_for_timeout(150)
    assert "reopened" not in page.locator("#ptext").inner_text().lower()

    # The same file, its diff moved on: the tick is dropped, and the count says how many were dropped rather than
    # quietly falling back to zero reviewed.
    page.evaluate("""() => {
      const file = view().files.find((entry) => entry.path.endsWith('sample.py'));
      file.digest = 'moved-on';
      render();
    }""")
    page.wait_for_timeout(150)
    # Read case-insensitively: the label is set in capitals by the style, not by what is written into it.
    reads = page.locator("#ptext").inner_text().lower()
    assert "1 reopened" in reads
    assert page.locator("#ptext").get_attribute("data-stale") == "true"
    assert "changed since they were reviewed" in page.locator("#ptext").get_attribute("title")
    # The tick is kept, so the file still shows it was read once and says the diff has moved since - and it survives the
    # next render rather than being cleared by the very act of noticing it.
    page.evaluate("() => render()")
    page.wait_for_timeout(120)
    assert "1 reopened" in page.locator("#ptext").inner_text().lower()
    assert card.get_attribute("data-done") == "false"
    assert "changed since review" in card.inner_text().lower()
    listed = page.locator("#filelist .fileitem[data-stale='true']")
    assert listed.count() == 1
    card.locator("input[type=checkbox]").uncheck()
    page.evaluate("() => localStorage.clear()")


@pytest.mark.parametrize("width", [1900, 1400, 1100, 900])
def test_the_top_bar_holds_one_line(page, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.wait_for_timeout(120)
    bar = page.evaluate("""() => {
      const head = document.querySelector('header');
      const kids = [...head.children].filter((node) => node.offsetParent !== null);
      return {
        height: Math.round(head.getBoundingClientRect().height),
        tallest: Math.max(...kids.map((node) => Math.round(node.getBoundingClientRect().height))),
        items: kids.length,
        overflowing: head.scrollWidth > head.clientWidth,
        scrolls: getComputedStyle(head).overflowX,
        cut: kids.filter((node) => node.getBoundingClientRect().right > head.scrollWidth + 1).length,
      };
    }""")
    # A bar as tall as its tallest control is a bar on one line; anything folded onto a second row makes it taller.
    assert bar["height"] <= bar["tallest"] + 22
    assert bar["items"] >= 6
    # Whatever a window cannot fit is reached by scrolling the bar, never cut off: font metrics differ between machines,
    # so which widths need that scrolling is not something to pin down - that none of it is unreachable is.
    assert bar["cut"] == 0
    if bar["overflowing"]:
        assert bar["scrolls"] in ("auto", "scroll")
    page.set_viewport_size({"width": 1500, "height": 900})


@pytest.mark.parametrize("width", [1500, 1000, 760])
def test_the_pinned_file_head_clears_the_page_header(page, width):
    page.set_viewport_size({"width": width, "height": 900})
    page.evaluate("""() => {
      const cards = [...document.querySelectorAll('section.file')];
      const tall = cards.reduce((one, other) => (other.offsetHeight > one.offsetHeight ? other : one));
      window.scrollTo(0, tall.offsetTop + tall.offsetHeight / 2);
    }""")
    page.wait_for_timeout(250)
    look = page.evaluate("""() => {
      const header = document.querySelector('header').getBoundingClientRect();
      const pinned = [...document.querySelectorAll('.filehead')]
        .map((node) => node.getBoundingClientRect())
        .filter((box) => box.top >= -1 && box.top < 260)
        .sort((one, other) => one.top - other.top)[0];
      const under = document.elementFromPoint(Math.round(header.width / 2), Math.round(header.bottom + 4));
      return {
        bottom: header.bottom,
        top: pinned ? pinned.top : null,
        head: Boolean(under && under.closest('.filehead')),
      };
    }""")
    assert look["top"] is not None
    # A head hidden behind the page header leaves a hunk delimiter standing where the file name should be.
    assert look["top"] >= look["bottom"] - 1
    assert look["head"]


def test_marking_a_file_reviewed_is_remembered_across_reloads(page):
    card = sample(page)
    card.locator("input[type=checkbox]").check()
    page.wait_for_timeout(150)
    assert card.get_attribute("data-done") == "true"
    # Reviewed folds the diff away, which is the point of the tick.
    assert card.get_attribute("data-open") == "false"
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    again = sample(page)
    assert again.get_attribute("data-done") == "true"
    again.locator("input[type=checkbox]").uncheck()
    page.wait_for_timeout(150)
    assert again.get_attribute("data-done") == "false"
    page.evaluate("() => localStorage.clear()")


def test_a_comment_and_its_last_reply_can_be_deleted_from_the_page(page, desk):
    card = sample(page)
    line = card.locator("tr.a[data-line]").first
    line.locator("td.code").first.hover()
    line.locator("button.pin").first.click()
    saying = f"written to be deleted, number {len(desk.get('/comments')) + 1}"
    submit(page, saying)
    page.wait_for_timeout(200)
    made = desk.get("/comments")[-1]["seq"]
    desk.post("/reply", {"seq": made, "text": "first answer", "who": "session"})
    desk.post("/reply", {"seq": made, "text": "second answer", "who": "session"})
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    thread = page.locator(f"#note-{made}")

    # Only the last reply carries a way out, and taking it leaves the comment and what was said before it.
    page.on("dialog", lambda dialog: dialog.accept())
    assert thread.locator(".line.reply button.drop").count() == 1
    thread.locator(".line.reply button.drop").click()
    page.wait_for_function("(seq) => document.querySelectorAll(`#note-${seq} .line.reply`).length === 1", arg=made)
    kept = {row["seq"]: row for row in desk.get("/comments")}[made]
    assert [answer["text"] for answer in kept["replies"]] == ["first answer"]

    # Dropping the comment itself takes it off the page and out of the panel, and the log says it is gone.
    thread.locator(".thread > .line button.drop").first.click()
    page.wait_for_function("(seq) => !document.getElementById(`note-${seq}`)", arg=made)
    assert {row["seq"]: row for row in desk.get("/comments")}[made]["state"] == "deleted"
    page.locator("#logopen").click()
    page.wait_for_selector("#log[data-open='true']")
    assert page.locator("#logrows .logrow").filter(has_text=saying).count() == 0
    page.locator("#logclose").click()
    page.evaluate("() => localStorage.clear()")


def test_a_comment_does_not_shift_the_columns_of_the_diff_it_hangs_under(page, desk):
    card = sample(page)
    line = card.locator("tr.a[data-line]").first
    line.locator("td.code").first.hover()
    line.locator("button.pin").first.click()
    submit(page, f"hanging under the diff, number {len(desk.get('/comments')) + 1}")
    page.wait_for_timeout(200)

    # The page redraws itself while it is read, and a comment sized to nothing would fill the table and move every
    # column under it.
    widths = page.evaluate(
        """async () => {
          const gutter = () => Math.round(document.querySelector('section.file[data-open="true"] td.ln')
            .getBoundingClientRect().width);
          const seen = [gutter()];
          render();
          seen.push(gutter());
          await new Promise(requestAnimationFrame);
          await new Promise(requestAnimationFrame);
          seen.push(gutter());
          return seen;
        }"""
    )
    assert widths[0] > 0
    assert len(set(widths)) == 1

    # Folding the file leaves its body with no room to report, which must not be taken for a width its comment should
    # have: sized to nothing the comment fills the table, the code column collapses and the page stretches. Sampled
    # frame by frame, since what this costs the reader is one flash of a diff laid out wrongly.
    folded = page.evaluate(
        """async () => {
          const card = document.querySelector('section.file[data-open="true"]');
          const shape = () => {
            const cell = card.querySelector('td.ln').getBoundingClientRect();
            return `${Math.round(cell.width)}|${Math.round(document.documentElement.scrollHeight)}`;
          };
          const was = shape();
          card.dataset.open = "false";
          await new Promise((done) => setTimeout(done, 150));
          card.dataset.open = "true";
          const seen = [];
          for (let i = 0; i < 12; i++) {
            await new Promise(requestAnimationFrame);
            seen.push(shape());
          }
          return { was, seen: [...new Set(seen)] };
        }"""
    )
    assert folded["seen"] == [folded["was"]]
    page.evaluate("() => localStorage.clear()")


def test_a_branch_that_has_moved_on_offers_a_refresh_that_keeps_the_place(page, desk):
    branch = page.evaluate("() => data.branches[0].ref")
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    assert page.locator("#moved").is_hidden()

    gen_diff_data.run(desk.repo, "checkout", "-q", branch)
    written = desk.repo / "added.py"
    kept = written.read_text()
    try:
        written.write_text(kept + "SAID_ON_DISK = 1\n")
        # Offered rather than taken: the diff under the reader is only rebuilt when they ask for it.
        page.wait_for_function("() => !document.getElementById('moved').hidden", timeout=30000)
        assert "SAID_ON_DISK" not in page.locator("#main").inner_text()

        # Coloured like a file whose diff has moved on: the same news, so the same colour draws the eye to it.
        painted = page.evaluate(
            """() => {
              const style = getComputedStyle(document.getElementById('moved'));
              const root = getComputedStyle(document.documentElement);
              return [style.color, root.getPropertyValue('--stale').trim()];
            }"""
        )
        assert painted[0] == page.evaluate(
            "(hex) => { const probe = document.createElement('span');"
            " probe.style.color = hex; document.body.append(probe);"
            " const said = getComputedStyle(probe).color; probe.remove(); return said; }",
            painted[1],
        )

        # Pressing it is acknowledged while the diffs are collected, and the label goes back if nothing landed.
        working = page.evaluate(
            """async () => {
              const going = rescan(true);
              const button = document.getElementById('moved');
              const said = { busy: button.dataset.busy, label: button.textContent, off: button.disabled };
              await going;
              return { ...said, after: button.textContent, hidden: button.hidden };
            }"""
        )
        assert working["busy"] == "true"
        assert working["label"] == "Refreshing..."
        assert working["off"] is True
        assert working["after"] == "Refresh"
        assert working["hidden"] is True
        assert "SAID_ON_DISK" in page.locator("#main").inner_text()
        # The branch being read is still the one being read.
        assert page.evaluate("() => data.branches[state.branch].ref") == branch
    finally:
        written.write_text(kept)
        gen_diff_data.run(desk.repo, "checkout", "-q", "main")
        desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": [branch]})
        page.reload(wait_until="load")
        page.wait_for_selector("section.file")


def test_a_comment_waiting_to_be_sent_survives_a_refresh_and_a_reload(page, desk):
    card = sample(page)
    line = card.locator("tr.a[data-line]").first
    line.locator("td.code").first.hover()
    line.locator("button.pin").first.click()
    saying = f"waiting to be sent, number {len(desk.get('/comments')) + 1}"
    page.locator("tr[data-composer='true'] textarea").fill(saying)
    page.locator("tr[data-composer='true'] button.solid:not(.direct)").click()
    page.wait_for_selector("#tray[data-open='true']")
    try:
        # Rebuilding the diff must not throw away words that have been written and not yet sent.
        page.evaluate("() => rescan(true)")
        page.wait_for_function("() => document.getElementById('tray').dataset.open === 'true'")
        assert saying in page.locator("#traylist").inner_text()

        page.reload(wait_until="load")
        page.wait_for_selector("section.file")
        assert page.locator("#tray").get_attribute("data-open") == "true"
        assert saying in page.locator("#traylist").inner_text()
        # Still a draft, so nothing of it has reached the log.
        assert all(saying != row["text"] for row in desk.get("/comments"))
    finally:
        # Tolerant of an empty tray, so a draft that did not survive is reported by the assertion that looked for it.
        if page.locator("#tray").get_attribute("data-open") == "true":
            page.locator("#traydrop").click()
        page.evaluate("() => localStorage.clear()")


def test_a_reply_being_written_survives_the_page_redrawing_itself(page, desk):
    card = sample(page)
    line = card.locator("tr.a[data-line]").first
    line.locator("td.code").first.hover()
    line.locator("button.pin").first.click()
    saying = f"answered while writing, number {len(desk.get('/comments')) + 1}"
    submit(page, saying)
    page.wait_for_timeout(200)
    made = desk.get("/comments")[-1]["seq"]
    desk.post("/resolve", {"seq": [made], "who": "session"})
    page.wait_for_function("(seq) => document.querySelector(`#note-${seq} .thread.done`)", arg=made, timeout=30000)

    # A resolved thread is folded to its remark, and a reply half written into it is words not yet finished.
    thread = page.locator(f"#note-{made}")
    thread.locator("button.tiny").first.click()
    box = thread.locator("textarea").first
    box.fill("half of what I mean")
    page.evaluate("() => render()")
    assert page.locator(f"#note-{made} textarea").first.input_value() == "half of what I mean"

    # Sent, it is said rather than waiting to be, so the box comes back empty.
    page.locator(f"#note-{made} textarea").first.fill("all of what I mean")
    page.locator(f"#note-{made} .actions button.ghost").first.click()
    page.wait_for_function(
        "(seq) => (document.querySelectorAll(`#note-${seq} .line.reply`).length || 0) >= 1", arg=made
    )
    assert page.locator(f"#note-{made} textarea").first.input_value() == ""
    assert "all of what I mean" in thread.inner_text()
    page.evaluate("() => localStorage.clear()")


def test_a_box_takes_the_height_of_what_is_written_into_it(page, desk):
    card = sample(page)
    line = card.locator("tr.a[data-line]").first
    line.locator("td.code").first.hover()
    line.locator("button.pin").first.click()
    box = page.locator("tr[data-composer='true'] textarea")
    page.wait_for_timeout(150)
    one = box.bounding_box()["height"]
    box.fill("\n".join(f"line {index} of a long remark" for index in range(8)))
    page.wait_for_timeout(150)
    many = box.bounding_box()["height"]
    # Grown to what it holds, and back down when it holds less.
    assert many > one
    box.fill("one line again")
    page.wait_for_timeout(150)
    assert abs(box.bounding_box()["height"] - one) < 2

    # Never past what leaves the diff readable, whatever is pasted into it.
    box.fill("\n".join(f"line {index} of something very long" for index in range(200)))
    page.wait_for_timeout(150)
    assert box.bounding_box()["height"] <= page.evaluate("() => window.innerHeight") * 0.45 + 2
    page.keyboard.press("Escape")
    page.evaluate("() => localStorage.clear()")


def test_a_reply_quoting_a_passage_shows_it_as_a_quote(page, desk):
    card = sample(page)
    line = card.locator("tr.a[data-line]").first
    line.locator("td.code").first.hover()
    line.locator("button.pin").first.click()
    saying = f"quoted back at me, number {len(desk.get('/comments')) + 1}"
    submit(page, saying)
    page.wait_for_timeout(200)
    made = desk.get("/comments")[-1]["seq"]
    desk.post(
        "/reply",
        {
            "seq": made,
            "text": "> the passage being answered\n> and the rest of it\n\nwhat I make of it, with `code` in it",
            "who": "session",
        },
    )
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")

    quoted = page.locator(f"#note-{made} .line.reply blockquote")
    assert quoted.count() == 1
    # One passage, not one quote per line, and the marks that made it are gone.
    assert quoted.inner_text() == "the passage being answered\nand the rest of it"
    answer = page.locator(f"#note-{made} .line.reply .said").first.inner_text()
    assert "what I make of it" in answer
    assert ">" not in answer
    assert page.locator(f"#note-{made} .line.reply code").first.inner_text() == "code"
    page.evaluate("() => localStorage.clear()")


def test_a_recorded_comment_shows_the_number_it_is_referred_to_by(page, desk):
    card = sample(page)
    line = card.locator("tr.a[data-line]").first
    line.locator("td.code").first.hover()
    line.locator("button.pin").first.click()
    saying = f"referred to by number, number {len(desk.get('/comments')) + 1}"
    submit(page, saying)
    page.wait_for_timeout(200)
    made = desk.get("/comments")[-1]["seq"]

    # A session answers by number, so the number is on the thread rather than only in the panel.
    head = page.locator(f"#note-{made} .thread > .line .who").first
    assert head.inner_text().startswith(f"[{made}]")
    page.evaluate("() => localStorage.clear()")


def test_a_file_comment_being_written_outlives_the_poll(page, desk):
    card = sample(page)
    card.locator(".filehead button.tiny", has_text="Comment on the file").first.click()
    page.wait_for_selector(".filenote.writing")
    page.locator(".filenote.writing textarea").fill("about the file as a whole")

    # The page redraws itself while it is read, and the box a comment is being written in must survive that.
    page.evaluate("() => { document.getElementById('main').blur(); document.activeElement.blur(); }")
    page.evaluate("() => tick()")
    page.wait_for_timeout(200)
    assert page.locator(".filenote.writing").count() == 1
    assert page.locator(".filenote.writing textarea").input_value() == "about the file as a whole"
    page.locator(".filenote.writing button.ghost", has_text="Cancel").first.click()
    page.evaluate("() => localStorage.clear()")


def test_a_comment_can_be_reached_by_its_number(page, desk):
    card = sample(page)
    line = card.locator("tr.a[data-line]").first
    line.locator("td.code").first.hover()
    line.locator("button.pin").first.click()
    saying = f"reached by number, number {len(desk.get('/comments')) + 1}"
    submit(page, saying)
    page.wait_for_timeout(200)
    made = desk.get("/comments")[-1]["seq"]
    # Settled and in a file marked reviewed, which is everything that would keep it out of sight.
    desk.post("/resolve", {"seq": [made], "who": "session"})
    card.locator("input[type=checkbox]").check()
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")

    # '#' from anywhere asks for a number, and the number is the one a session refers to the comment by.
    page.keyboard.press("#")
    assert page.evaluate("() => document.activeElement.id") == "goto"
    page.keyboard.type(str(made))
    page.keyboard.press("Enter")
    page.wait_for_function(
        """(seq) => {
          const thread = document.getElementById(`note-${seq}`);
          if (!thread) return false;
          const box = thread.getBoundingClientRect();
          const covered = document.querySelector("header").getBoundingClientRect().bottom;
          return box.top >= covered && box.bottom < window.innerHeight;
        }""",
        arg=made,
        timeout=8000,
    )
    assert saying in page.locator(f"#note-{made}").inner_text()
    # Unfolded, not merely on screen: a settled thread folds to an outline of its remark, which is not reading it.
    assert page.locator(f"#note-{made} .thread.folded").count() == 0
    assert page.locator(f"#note-{made} .actions").count() == 1

    # A number no comment answers to says so rather than going anywhere.
    page.keyboard.press("#")
    page.keyboard.type("999999")
    page.keyboard.press("Enter")
    assert page.locator("#goto.lost").count() == 1
    page.locator("section.file[data-path='sample.py'] input[type=checkbox]").uncheck()
    page.evaluate("() => localStorage.clear()")


def test_a_file_holding_an_unread_comment_opens_itself(page, desk):
    branch = page.evaluate("() => data.branches[0].ref")
    card = page.locator("section.file[data-path='added.py']")
    card.locator("input[type=checkbox]").check()
    page.wait_for_selector("section.file[data-path='added.py'][data-open='false']")
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    # Reviewed and read: nothing asks for it to be open.
    assert page.locator("section.file[data-path='added.py']").get_attribute("data-open") == "false"

    desk.post(
        "/comments",
        [{"branch": branch, "path": "added.py", "line": 1, "side": "new", "text": "arrived after you read it"}],
    )
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    # A remark the reader has never been shown is not hidden behind a file they had finished with.
    again = page.locator("section.file[data-path='added.py']")
    assert again.get_attribute("data-open") == "true"
    assert "arrived after you read it" in again.inner_text()

    # Shown once, it stops asking: the file goes back to being folded away.
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    assert page.locator("section.file[data-path='added.py']").get_attribute("data-open") == "false"

    # One that arrives already settled is unread too, so it opens its file and shows the thread rather than an outline.
    made = desk.post(
        "/comments",
        [{"branch": branch, "path": "added.py", "line": 1, "side": "new", "text": "settled before you saw it"}],
    )["seqs"][0]
    desk.post("/resolve", {"seq": [made], "who": "session"})
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    assert page.locator("section.file[data-path='added.py']").get_attribute("data-open") == "true"
    assert page.locator(f"#note-{made} .thread.folded").count() == 0
    assert "settled before you saw it" in page.locator(f"#note-{made}").inner_text()

    # Read now, so it folds to its remark and lets the file close again.
    page.reload(wait_until="load")
    page.wait_for_selector("section.file")
    assert page.locator(f"#note-{made} .thread.folded").count() == 1
    page.locator("section.file[data-path='added.py'] input[type=checkbox]").uncheck()
    page.evaluate("() => localStorage.clear()")


def test_a_sync_that_could_not_happen_says_so_where_the_reader_is(page, desk):
    branch = page.evaluate("() => data.branches[0].ref")
    knows = [
        {"match": "repos/someone/somewhere --jq", "out": "someone/somewhere"},
        {"match": "pr list", "out": json.dumps([{"number": 7, "url": "u", "title": "t", "headRefName": branch}])},
    ]
    desk.github_answers(rules=knows)
    gen_diff_data.run(desk.repo, "remote", "add", "origin", "https://github.com/someone/somewhere.git")
    try:
        assert desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": [branch]})["ok"]
        page.reload(wait_until="load")
        page.wait_for_selector("section.file")
        assert page.locator("#toast").get_attribute("data-open") == "false"

        # GitHub refuses the sync, and the panel it happened in is not where the reader is looking.
        desk.github_answers(rules=[{"match": "graphql", "code": 1, "err": "gh: Bad credentials (HTTP 401)"}, *knows])
        page.locator("#logopen").click()
        page.wait_for_selector("#log[data-open='true']")
        page.locator("#logsync").click()
        page.wait_for_selector("#toast[data-open='true']", timeout=30000)
        said = page.locator("#toastsaid").inner_text()
        assert "Could not sync" in said
        assert "401" in said

        # It stays until dismissed, since a reason worth reading is worth reading at leisure.
        page.locator("#logclose").click()
        assert page.locator("#toast").get_attribute("data-open") == "true"
        page.locator("#toastclose").click()
        assert page.locator("#toast").get_attribute("data-open") == "false"
    finally:
        gen_diff_data.run(desk.repo, "remote", "remove", "origin")
        desk.github_answers(code=1, err="gh: Not Found (HTTP 404)")
        desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": [branch]})
        page.reload(wait_until="load")
        page.wait_for_selector("section.file")
        page.evaluate("() => localStorage.clear()")
