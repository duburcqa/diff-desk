"""What the collector makes of a range: the rows, the numbers they carry, and what a page is handed."""

import pytest

import gen_diff_data
from conftest import FILE_LINES, FIRST_EDIT, SECOND_EDIT

# All of these read one repository built once, and cost next to nothing to run, so they go to a single worker of a
# parallel run rather than having several build that repository over again.
pytestmark = pytest.mark.xdist_group("collect")


def hunks(entry):
    return [row for row in entry["lines"] if row[0] == "h"]


def test_rows_carry_the_numbers_of_both_sides(payload):
    sample = next(entry for entry in payload["branches"][0]["files"] if entry["path"] == "sample.py")
    assert (sample["added"], sample["removed"]) == (2, 2)
    # Two rewrites far apart give two hunks, each headed by the line it starts at on both sides.
    assert [(row[1], row[2]) for row in hunks(sample)] == [
        (FIRST_EDIT - 3, FIRST_EDIT - 3),
        (SECOND_EDIT - 3, SECOND_EDIT - 3),
    ]
    removed = [row for row in sample["lines"] if row[0] == "d"]
    added = [row for row in sample["lines"] if row[0] == "a"]
    assert [row[1] for row in removed] == [FIRST_EDIT, SECOND_EDIT]
    assert [row[2] for row in added] == [FIRST_EDIT, SECOND_EDIT]
    assert [row[3] for row in added] == [f"line {FIRST_EDIT} rewritten", f"line {SECOND_EDIT} rewritten"]


def test_a_new_file_is_marked_and_an_untouched_one_absent(payload):
    files = {entry["path"]: entry for entry in payload["branches"][0]["files"]}
    assert files["added.py"]["state"] == "added"
    assert files["added.py"]["removed"] == 0
    assert "kept.py" not in files


def test_the_digest_follows_the_hunks(repo, payload):
    before = {entry["path"]: entry["digest"] for entry in payload["branches"][0]["files"]}
    again = gen_diff_data.collect(str(repo), "main", ["feature"])
    assert {entry["path"]: entry["digest"] for entry in again["branches"][0]["files"]} == before
    # A digest is what tells a reviewed file from one that moved under the reviewer, so it must not survive an edit.
    lifted = gen_diff_data.parse(gen_diff_data.run(repo, "diff", "--unified=8", "main", "feature"))
    moved = {entry["path"]: entry["digest"] for entry in lifted}
    assert moved["sample.py"] != before["sample.py"]


def test_branches_ahead_of_the_base_are_offered(repo):
    assert gen_diff_data.ahead_refs(str(repo), "main") == [{"ref": "feature", "ahead": 1}]
    assert gen_diff_data.ahead_refs(str(repo), "feature") == []


def test_a_pull_request_is_named_by_number_in_any_of_its_spellings():
    assert [gen_diff_data.pull_number(text) for text in ("3243", "#3243", "pr/3243", "PR-3243", " 42 ")] == [
        3243,
        3243,
        3243,
        3243,
        42,
    ]
    # A branch whose name merely contains digits is a branch, not a pull request.
    assert [gen_diff_data.pull_number(text) for text in ("feature", "release-3.2", "pr/topic", "", "3243abc")] == [
        None,
        None,
        None,
        None,
        None,
    ]


def test_a_pull_request_needs_a_github_remote_to_resolve_against(repo):
    with pytest.raises(RuntimeError, match="#3243"):
        gen_diff_data.collect(str(repo), "main", ["#3243"])


def test_a_pull_request_fetched_earlier_is_reviewable_while_github_is_unreachable(repo):
    # Whatever GitHub cannot answer for, the head already on disk can: an outage must not cost the review.
    gen_diff_data.run(repo, "update-ref", "refs/diffdesk/pull/77", "feature")
    try:
        data = gen_diff_data.collect(str(repo), "main", ["#77"])
        branch = data["branches"][0]
        assert branch["ref"] == "refs/diffdesk/pull/77"
        assert branch["blurb"] == "#77"
        assert branch["pr"]["number"] == 77
        assert [entry["path"] for entry in branch["files"]] == [
            "added.py",
            "notes.txt",
            "pkg/sub/deep.py",
            "sample.py",
            "wide.py",
        ]
    finally:
        gen_diff_data.run(repo, "update-ref", "-d", "refs/diffdesk/pull/77")


def test_a_repository_without_a_remote_claims_no_upstream(repo, payload):
    assert gen_diff_data.canonical_repo(str(repo)) == ""
    assert payload["upstream"] == ""
    assert payload["branches"][0]["pr"] is None


def test_the_range_is_described_for_the_page(repo, payload):
    assert payload["baseRef"] == "main"
    assert payload["base"] == gen_diff_data.run(repo, "rev-parse", "--short", "main").strip()
    branch = payload["branches"][0]
    assert branch["ref"] == "feature"
    assert branch["rev"] == "feature"
    assert branch["dirty"] is False
    assert [commit["subject"] for commit in branch["commits"]] == ["rewrite two lines and add a file"]


def test_the_checked_out_branch_shows_its_uncommitted_work(repo):
    (repo / "sample.py").write_text("\n".join(f"line {number}" for number in range(1, FILE_LINES)) + "\n")
    try:
        scanned = gen_diff_data.collect(str(repo), "main", ["main"])
        branch = scanned["branches"][0]
        assert branch["dirty"] is True
        assert branch["rev"] == ""
        assert branch["files"][0]["removed"] == 1
    finally:
        gen_diff_data.run(repo, "checkout", "--", "sample.py")


def test_a_branch_the_base_moved_past_shows_only_its_own_work(repo):
    gen_diff_data.run(repo, "checkout", "-q", "main")
    (repo / "elsewhere.py").write_text("the base moved on\n")
    gen_diff_data.run(repo, "add", "-A")
    gen_diff_data.run(repo, "commit", "-m", "work the branch forked before")
    try:
        for standing in ("main", "feature"):
            gen_diff_data.run(repo, "checkout", "-q", standing)
            branch = gen_diff_data.collect(str(repo), "main", ["feature"])["branches"][0]
            touched = [entry["path"] for entry in branch["files"]]
            assert "elsewhere.py" not in touched
            assert sorted(touched) == ["added.py", "notes.txt", "pkg/sub/deep.py", "sample.py", "wide.py"]
    finally:
        gen_diff_data.run(repo, "checkout", "-q", "main")
        gen_diff_data.run(repo, "reset", "-q", "--hard", "HEAD~1")


def test_a_branch_the_base_has_taken_in_is_not_offered_again(repo):
    gen_diff_data.run(repo, "checkout", "-q", "main")
    # The base takes the work in as one commit of its own, which is what a squashed merge leaves behind: the same
    # content, under none of the commits the branch carries.
    gen_diff_data.run(repo, "merge", "--squash", "feature")
    gen_diff_data.run(repo, "commit", "-q", "-m", "the work, landed")
    try:
        assert gen_diff_data.collect(str(repo), "main", ["feature"])["branches"] == []
        # Work of its own the base cannot have keeps it reviewable.
        gen_diff_data.run(repo, "checkout", "-q", "feature")
        (repo / "sample.py").write_text("written after the merge\n")
        entry = gen_diff_data.collect(str(repo), "main", ["feature"])["branches"][0]
        assert [row["path"] for row in entry["files"]] == ["sample.py"]
    finally:
        gen_diff_data.run(repo, "checkout", "-q", "--", "sample.py")
        gen_diff_data.run(repo, "checkout", "-q", "main")
        gen_diff_data.run(repo, "reset", "-q", "--hard", "HEAD~1")


def test_the_page_is_built_around_the_payload(payload):
    page = gen_diff_data.render_page("<b>__BUILD__</b><script>__DIFF_DATA__</script>", payload)
    assert "__DIFF_DATA__" not in page and "__BUILD__" not in page
    assert "built " in page
    assert '"ref":"feature"' in page
    # A closing tag inside the payload would end the script element early.
    assert "</script" not in page.split("<script>", 1)[1].rsplit("</script>", 1)[0]
