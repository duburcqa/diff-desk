"""The desk's endpoints: what it serves, what it records, and how a session reads the comments back out."""

import concurrent.futures
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

import gen_diff_data
from conftest import FILE_LINES, SECOND_EDIT
from serve_diff import is_refusal


def read(desk, route):
    with urllib.request.urlopen(f"{desk.url}{route}", timeout=30) as answer:
        return answer.status, answer.read()


def test_the_page_and_its_payload_are_served(desk):
    status, body = read(desk, "/")
    assert status == 200
    assert b"<title>" in body and b"__DIFF_DATA__" not in body
    assert desk.get("/data")["branches"][0]["ref"] == "feature"


def test_an_unknown_route_is_refused(desk):
    with pytest.raises(urllib.error.HTTPError) as raised:
        read(desk, "/nowhere")
    assert raised.value.code == 404


def test_branches_are_offered_for_the_picker(desk):
    info = desk.get(f"/refs?dir={urllib.parse.quote(str(desk.repo))}&base=main")
    assert info["current"] == "main"
    assert info["refs"] == [{"ref": "feature", "ahead": 1}]


def test_pull_requests_are_offered_beside_the_branches(desk):
    info = desk.get(f"/refs?dir={urllib.parse.quote(str(desk.repo))}&base=main")
    # A repository with no GitHub remote has no pull requests to offer, and says so rather than omitting the key.
    assert info["pulls"] == []
    assert info["upstream"] == ""


def test_a_slice_of_the_file_fills_a_gap(desk):
    where = f"/lines?dir={urllib.parse.quote(str(desk.repo))}&rev=feature&path=sample.py&from=20&to=24"
    answer = desk.get(where)
    assert answer["total"] == FILE_LINES
    assert answer["lines"] == [f"line {number}" for number in range(20, 25)]
    # A revision the page did not ask for must not leak in: the branch's own content is what a gap is filled with.
    edited = desk.get(
        f"/lines?dir={urllib.parse.quote(str(desk.repo))}&rev=feature&path=sample.py&from={SECOND_EDIT}&to={SECOND_EDIT}"
    )
    assert edited["lines"] == [f"line {SECOND_EDIT} rewritten"]
    ranged = desk.get(
        f"/lines?dir={urllib.parse.quote(str(desk.repo))}&rev=main&path=sample.py&from={SECOND_EDIT}&to={SECOND_EDIT}"
    )
    assert ranged["lines"] == [f"line {SECOND_EDIT}"]


def test_a_slice_beyond_the_file_is_clamped(desk):
    beyond = FILE_LINES + 40
    where = f"/lines?dir={urllib.parse.quote(str(desk.repo))}&rev=feature&path=sample.py&from={FILE_LINES}&to={beyond}"
    answer = desk.get(where)
    assert answer["lines"] == [f"line {FILE_LINES}"]
    assert answer["to"] == FILE_LINES


def test_a_batch_is_recorded_numbered_and_read_back_past_a_cursor(desk):
    first = desk.post(
        "/comments",
        [
            {"branch": "feature", "path": "sample.py", "line": 10, "endLine": 12, "side": "new", "text": "the range"},
            {"branch": "feature", "path": "sample.py", "line": 150, "side": "old", "text": "the removal"},
        ],
    )
    assert first["ok"] and first["batch"] >= 1
    rows = desk.get("/comments")
    assert [row["seq"] for row in rows][-2:] == [first["seq"] - 1, first["seq"]]
    assert all(row["batch"] == first["batch"] for row in rows[-2:])
    assert all(row["state"] == "open" for row in rows[-2:])

    # A cursor is what lets a session pick up only what it has not seen.
    assert desk.get(f"/comments?since={first['seq']}") == []
    assert [row["text"] for row in desk.get(f"/comments?since={first['seq'] - 2}")] == ["the range", "the removal"]

    second = desk.post(
        "/comments", {"branch": "feature", "path": "added.py", "line": 1, "side": "new", "text": "alone"}
    )
    assert second["batch"] == first["batch"] + 1
    assert [row["text"] for row in desk.get(f"/comments?since={first['seq']}")] == ["alone"]


def test_resolving_closes_only_what_was_named_and_can_be_undone(desk):
    marked = desk.post("/comments", {"branch": "feature", "path": "sample.py", "line": 8, "side": "new", "text": "one"})
    other = desk.post("/comments", {"branch": "feature", "path": "sample.py", "line": 9, "side": "new", "text": "two"})
    outcome = desk.post("/resolve", {"seq": [marked["seq"]], "answer": "done in abc1234"})
    assert outcome["resolved"] == 1
    rows = {row["seq"]: row for row in desk.get("/comments")}
    closed = rows[marked["seq"]]
    assert closed["state"] == "resolved"
    # Closing keeps the remark and files the answer as a reply of its own, so nothing about it is lost.
    assert closed["text"] == "one"
    assert [(reply["who"], reply["text"]) for reply in closed["replies"]] == [("session", "done in abc1234")]
    assert rows[other["seq"]]["state"] == "open"

    reopened = desk.post("/resolve", {"seq": [marked["seq"]], "resolved": False, "who": "you"})
    assert reopened["state"] == "open"
    again = {row["seq"]: row for row in desk.get("/comments")}[marked["seq"]]
    assert again["state"] == "open"
    assert len(again["replies"]) == 1


def test_either_side_can_reply_without_closing_the_thread(desk):
    made = desk.post("/comments", {"branch": "feature", "path": "sample.py", "line": 11, "side": "new", "text": "why?"})
    assert desk.post("/reply", {"seq": made["seq"], "text": "because of X", "who": "session"})["replies"] == 1
    assert desk.post("/reply", {"seq": made["seq"], "text": "then what about Y", "who": "you"})["replies"] == 2
    row = {row["seq"]: row for row in desk.get("/comments")}[made["seq"]]
    assert [(reply["who"], reply["text"]) for reply in row["replies"]] == [
        ("session", "because of X"),
        ("you", "then what about Y"),
    ]
    assert row["state"] == "open"
    assert desk.post("/reply", {"seq": made["seq"], "text": "   "})["ok"] is False
    assert desk.post("/reply", {"seq": 99999, "text": "nowhere"})["ok"] is False


def test_rewriting_a_comment_keeps_what_it_said_before(desk):
    made = desk.post(
        "/comments", {"branch": "feature", "path": "sample.py", "line": 12, "side": "new", "text": "first"}
    )
    assert desk.post("/edit", {"seq": made["seq"], "text": "second"})["edits"] == 1
    assert desk.post("/edit", {"seq": made["seq"], "text": "third"})["edits"] == 2
    row = {row["seq"]: row for row in desk.get("/comments")}[made["seq"]]
    assert row["text"] == "third"
    assert [earlier["text"] for earlier in row["edits"]] == ["first", "second"]
    assert desk.post("/edit", {"seq": made["seq"], "text": " "})["ok"] is False


def test_a_refusal_is_told_apart_from_a_failure_worth_retrying():
    # What GitHub says when it rejects the comment itself, against what it says when the attempt simply did not happen.
    assert is_refusal("gh: Unprocessable Entity (HTTP 422)")
    assert is_refusal("gh: Not Found (HTTP 404)")
    assert is_refusal("gh: Forbidden (HTTP 403)")
    assert not is_refusal("dial tcp: lookup api.github.com: no such host")
    assert not is_refusal("gh: Internal Server Error (HTTP 500)")
    assert not is_refusal("context deadline exceeded")


def test_a_comment_bound_for_github_waits_rather_than_being_lost(desk):
    made = desk.post(
        "/comments",
        {
            "comments": [{"branch": "feature", "path": "sample.py", "line": 13, "side": "new", "text": "for the PR"}],
            "github": True,
        },
    )
    rows = {row["seq"]: row for row in desk.get("/comments")}
    assert rows[made["seqs"][0]]["github"] == "pending"

    # Unreachable: the attempt did not happen, so the comment stays owed with its reason and a retry sweeps it up.
    desk.github_answers(code=1, err="dial tcp: lookup api.github.com: no such host")
    outcome = desk.post("/publish", {"repo": "someone/somewhere", "pr": 1, "seq": made["seqs"]})
    assert outcome["ok"] is False
    assert outcome["sent"] == 0
    kept = {row["seq"]: row for row in desk.get("/comments")}[made["seqs"][0]]
    assert kept["github"] == "failed"
    assert kept["text"] == "for the PR"
    assert "no such host" in kept["error"]
    owed = {row["seq"] for row in desk.get("/comments") if row["github"] in ("pending", "failed")}
    assert made["seqs"][0] in owed

    # It lands on a later try, and what it landed in is recorded against it.
    desk.github_answers(out=json.dumps({"html_url": "https://github.com/x/y/pull/1#pullrequestreview-42"}))
    landed = desk.post("/publish", {"repo": "someone/somewhere", "pr": 1})
    assert landed["ok"] is True
    assert landed["sent"] >= 1
    assert landed["owed"] == 0
    posted = {row["seq"]: row for row in desk.get("/comments")}[made["seqs"][0]]
    assert posted["github"] == "posted"
    assert posted["reviewUrl"].endswith("pullrequestreview-42")
    assert "error" not in posted

    # A sequence nobody owes anything for is not a post at all.
    assert desk.post("/publish", {"repo": "someone/somewhere", "pr": 1, "seq": [99999]}) == {
        "ok": True,
        "sent": 0,
        "owed": 0,
    }


def test_closing_a_posted_comment_resolves_its_thread_on_the_pull_request(desk):
    made = desk.post(
        "/comments",
        {
            "comments": [{"branch": "feature", "path": "sample.py", "line": 16, "side": "new", "text": "close me"}],
            "github": True,
        },
    )
    seq = made["seqs"][0]
    desk.github_answers(out=json.dumps({"html_url": "https://github.com/x/y/pull/3#review-1"}))
    assert desk.post("/publish", {"repo": "someone/somewhere", "pr": 3, "seq": [seq]})["ok"]

    # Closing it here owes a resolution there, which is tracked apart from whether it was posted.
    desk.post("/resolve", {"seq": [seq], "who": "you"})
    rows = {row["seq"]: row for row in desk.get("/comments")}
    assert rows[seq]["state"] == "resolved"
    assert rows[seq]["prResolve"] == "pending"

    # Unreachable: it stays owed with its reason, and nothing pretends the pull request agrees.
    desk.github_answers(code=1, err="dial tcp: lookup api.github.com: no such host")
    outcome = desk.post("/close", {"repo": "someone/somewhere", "pr": 3})
    assert outcome["ok"] is False
    assert {row["seq"]: row for row in desk.get("/comments")}[seq]["prResolve"] == "failed"

    # A pull request that holds no thread of ours settles nothing: claiming resolved here would be claiming GitHub's
    # agreement without it, which is worse than saying it is still owed.
    empty = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}
    desk.github_answers(rules=[{"match": "reviewThreads", "out": json.dumps(empty)}])
    desk.post("/close", {"repo": "someone/somewhere", "pr": 3})
    unfound = {row["seq"]: row for row in desk.get("/comments")}[seq]
    assert unfound["prResolve"] == "failed"
    assert "could not be found" in unfound["prResolveError"]

    # A mutation that answers without saying the thread is resolved is not a resolution either.
    quiet = {"data": {"resolveReviewThread": {"thread": {"isResolved": False}}}}
    desk.github_answers(
        rules=[
            {
                "match": "reviewThreads",
                "out": json.dumps(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": [
                                            {
                                                "id": "T_ours",
                                                "isResolved": False,
                                                "comments": {
                                                    "nodes": [
                                                        {
                                                            "databaseId": 9,
                                                            "body": "close me",
                                                            "path": "sample.py",
                                                            "author": {"login": "duburcqa"},
                                                        }
                                                    ]
                                                },
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    }
                ),
            },
            {"match": "resolveReviewThread", "out": json.dumps(quiet)},
        ]
    )
    desk.post("/close", {"repo": "someone/somewhere", "pr": 3})
    unsure = {row["seq"]: row for row in desk.get("/comments")}[seq]
    assert unsure["prResolve"] == "failed"
    assert "did not report" in unsure["prResolveError"]

    # The thread that holds our comment is the one resolved, found by the body this desk posted.
    threads = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "T_other",
                                "isResolved": False,
                                "comments": {"nodes": [{"body": "someone else's remark", "path": "sample.py"}]},
                            },
                            {
                                "id": "T_ours",
                                "isResolved": False,
                                "comments": {"nodes": [{"body": "close me", "path": "sample.py"}]},
                            },
                        ]
                    }
                }
            }
        }
    }
    desk.github_answers(
        rules=[
            {"match": "reviewThreads", "out": json.dumps(threads)},
            {
                "match": "resolveReviewThread",
                "out": json.dumps({"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}),
            },
        ]
    )
    landed = desk.post("/close", {"repo": "someone/somewhere", "pr": 3})
    assert landed["closed"] >= 1
    assert {row["seq"]: row for row in desk.get("/comments")}[seq]["prResolve"] == "done"

    # Reopening it here owes nothing there again.
    desk.post("/resolve", {"seq": [seq], "resolved": False, "who": "you"})
    assert {row["seq"]: row for row in desk.get("/comments")}[seq]["prResolve"] == "none"


def test_a_comment_closed_before_the_pull_request_knew_of_it_is_still_owed_a_resolution(desk):
    made = desk.post(
        "/comments",
        {
            "comments": [{"branch": "feature", "path": "sample.py", "line": 21, "side": "new", "text": "closed early"}],
            "github": True,
        },
    )
    seq = made["seqs"][0]
    desk.github_answers(out=json.dumps({"html_url": "https://github.com/x/y/pull/5#review-1"}))
    desk.post("/publish", {"repo": "someone/somewhere", "pr": 5, "seq": [seq]})

    # As a comment closed by an older desk looks: resolved, posted, and owing the pull request nothing on its face.
    with (desk.home / "comments.jsonl").open() as held:
        rows = [json.loads(line) for line in held if line.strip()]
    for row in rows:
        if row["seq"] == seq:
            row["state"] = "resolved"
            row["prResolve"] = "none"
    (desk.home / "comments.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))

    threads = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "T_early",
                                "isResolved": False,
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 601,
                                            "body": "closed early",
                                            "path": "sample.py",
                                            "author": {"login": "duburcqa"},
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                }
            }
        }
    }
    desk.github_answers(
        rules=[
            {"match": "reviewThreads", "out": json.dumps(threads)},
            {
                "match": "resolveReviewThread",
                "out": json.dumps({"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}),
            },
        ]
    )
    # A sweep must notice it from its state alone, rather than only from the moment it was closed.
    outcome = desk.post("/close", {"repo": "someone/somewhere", "pr": 5})
    assert outcome["closed"] >= 1
    assert {row["seq"]: row for row in desk.get("/comments")}[seq]["prResolve"] == "done"


def test_one_review_never_reaches_another_review_comments(desk):
    # One log holds every review this desk has served, so a sweep for one pull request must leave the others alone.
    elsewhere = desk.post(
        "/comments",
        {
            "comments": [
                {"branch": "other/branch", "path": "sample.py", "line": 25, "side": "new", "text": "elsewhere"}
            ],
            "github": True,
        },
    )
    far = elsewhere["seqs"][0]
    desk.github_answers(out=json.dumps({"html_url": "https://github.com/x/other/pull/11#review-1"}))
    desk.post("/publish", {"repo": "x/other", "pr": 11, "branch": "other/branch", "seq": [far]})
    desk.post("/resolve", {"seq": [far], "who": "you"})

    here = desk.post(
        "/comments",
        {
            "comments": [{"branch": "feature", "path": "sample.py", "line": 26, "side": "new", "text": "right here"}],
            "github": True,
        },
    )
    near = here["seqs"][0]

    # A post for this review must carry only this review's comments, not the one bound for another pull request.
    desk.github_answers(out=json.dumps({"html_url": "https://github.com/someone/somewhere/pull/12#review-1"}))
    desk.post("/publish", {"repo": "someone/somewhere", "pr": 12, "branch": "feature"})
    rows = {row["seq"]: row for row in desk.get("/comments")}
    assert rows[near]["prRepo"] == "someone/somewhere"
    assert rows[far]["prRepo"] == "x/other"
    assert rows[far]["reviewUrl"].endswith("other/pull/11#review-1")

    # A sync for this review must not judge the other's comment against threads that were never its own.
    empty = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}
    desk.github_answers(rules=[{"match": "reviewThreads", "out": json.dumps(empty)}])
    desk.post("/sync", {"repo": "someone/somewhere", "pr": 12, "branch": "feature"})
    after = {row["seq"]: row for row in desk.get("/comments")}
    assert after[far]["prResolve"] == "pending"
    assert "prResolveError" not in after[far]


def test_syncing_repairs_a_comment_wrongly_believed_resolved_on_the_pull_request(desk):
    made = desk.post(
        "/comments",
        {
            "comments": [
                {"branch": "feature", "path": "sample.py", "line": 23, "side": "new", "text": "believed resolved"}
            ],
            "github": True,
        },
    )
    seq = made["seqs"][0]
    desk.github_answers(out=json.dumps({"html_url": "https://github.com/x/y/pull/8#review-1"}))
    desk.post("/publish", {"repo": "someone/somewhere", "pr": 8, "seq": [seq]})

    # As an older desk left it: closed here and recorded as resolved there, though the pull request never was asked.
    held = desk.home / "comments.jsonl"
    rows = [json.loads(line) for line in held.read_text().splitlines() if line.strip()]
    for row in rows:
        if row["seq"] == seq:
            row["state"] = "resolved"
            row["prResolve"] = "done"
    held.write_text("".join(json.dumps(row) + "\n" for row in rows))

    thread = {
        "id": "T_believed",
        "isResolved": False,
        "comments": {
            "nodes": [
                {"databaseId": 801, "body": "believed resolved", "path": "sample.py", "author": {"login": "duburcqa"}}
            ]
        },
    }
    desk.github_answers(
        rules=[
            {
                "match": "reviewThreads",
                "out": json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": [thread]}}}}}),
            },
            {
                "match": "resolveReviewThread",
                "out": json.dumps({"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}),
            },
        ]
    )
    # What the pull request says decides, so a resolution only this desk believed in is made real rather than trusted.
    outcome = desk.post("/sync", {"repo": "someone/somewhere", "pr": 8})
    assert outcome["resolved"] >= 1
    assert any("resolveReviewThread" in call and "T_believed" in call for call in desk.github_calls())
    assert {row["seq"]: row for row in desk.get("/comments")}[seq]["prResolve"] == "done"


def test_syncing_reopens_the_question_when_the_thread_is_nowhere_to_be_found(desk):
    made = desk.post(
        "/comments",
        {
            "comments": [
                {"branch": "feature", "path": "sample.py", "line": 24, "side": "new", "text": "vanished thread"}
            ],
            "github": True,
        },
    )
    seq = made["seqs"][0]
    desk.github_answers(out=json.dumps({"html_url": "https://github.com/x/y/pull/9#review-1"}))
    desk.post("/publish", {"repo": "someone/somewhere", "pr": 9, "seq": [seq]})
    desk.post("/resolve", {"seq": [seq], "who": "you"})

    empty = {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}}
    desk.github_answers(rules=[{"match": "reviewThreads", "out": json.dumps(empty)}])
    desk.post("/sync", {"repo": "someone/somewhere", "pr": 9})
    row = {row["seq"]: row for row in desk.get("/comments")}[seq]
    # Believed of nothing: with no thread answering to it, the comment is owed a resolution rather than granted one.
    assert row["prResolve"] == "failed"
    assert "could not be found" in row["prResolveError"]


def test_syncing_resolves_there_what_was_closed_here(desk):
    made = desk.post(
        "/comments",
        {
            "comments": [
                {"branch": "feature", "path": "sample.py", "line": 22, "side": "new", "text": "closed here only"}
            ],
            "github": True,
        },
    )
    seq = made["seqs"][0]
    desk.github_answers(out=json.dumps({"html_url": "https://github.com/x/y/pull/6#review-1"}))
    desk.post("/publish", {"repo": "someone/somewhere", "pr": 6, "seq": [seq]})
    desk.post("/resolve", {"seq": [seq], "who": "you"})

    threads = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "T_here",
                                "isResolved": False,
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 701,
                                            "body": "closed here only",
                                            "path": "sample.py",
                                            "author": {"login": "duburcqa"},
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                }
            }
        }
    }
    desk.github_answers(
        rules=[
            {"match": "reviewThreads", "out": json.dumps(threads)},
            {
                "match": "resolveReviewThread",
                "out": json.dumps({"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}),
            },
        ]
    )
    outcome = desk.post("/sync", {"repo": "someone/somewhere", "pr": 6})
    # Resolution travels both ways: what was closed here is closed there by the same sync that brings their word back.
    assert outcome["resolved"] >= 1
    assert any("resolveReviewThread" in call and "T_here" in call for call in desk.github_calls())
    assert {row["seq"]: row for row in desk.get("/comments")}[seq]["prResolve"] == "done"


def test_closing_a_comment_that_never_reached_the_pull_request_owes_it_nothing(desk):
    made = desk.post(
        "/comments", [{"branch": "feature", "path": "sample.py", "line": 17, "side": "new", "text": "here only"}]
    )
    desk.post("/resolve", {"seq": [made["seq"]], "who": "session"})
    row = {row["seq"]: row for row in desk.get("/comments")}[made["seq"]]
    assert row["state"] == "resolved"
    assert row["prResolve"] == "none"


def test_syncing_carries_replies_both_ways_and_takes_the_pull_request_word(desk):
    made = desk.post(
        "/comments",
        {
            "comments": [
                {"branch": "feature", "path": "sample.py", "line": 18, "side": "new", "text": "worth syncing"}
            ],
            "github": True,
        },
    )
    seq = made["seqs"][0]
    desk.github_answers(out=json.dumps({"html_url": "https://github.com/x/y/pull/4#review-1"}))
    assert desk.post("/publish", {"repo": "someone/somewhere", "pr": 4, "seq": [seq]})["ok"]
    desk.post("/reply", {"seq": seq, "text": "written here, not there yet", "who": "session"})

    # The pull request holds the remark, plus a reply from someone else, and someone resolved the thread there.
    threads = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "T_ours",
                                "isResolved": True,
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 501,
                                            "body": "worth syncing",
                                            "path": "sample.py",
                                            "author": {"login": "duburcqa"},
                                        },
                                        {
                                            "databaseId": 502,
                                            "body": "said on the PR",
                                            "path": "sample.py",
                                            "author": {"login": "someone"},
                                        },
                                    ]
                                },
                            }
                        ]
                    }
                }
            }
        }
    }
    desk.github_answers(
        rules=[
            {"match": "reviewThreads", "out": json.dumps(threads)},
            {"match": "/comments/501/replies", "out": json.dumps({"id": 777})},
        ]
    )
    outcome = desk.post("/sync", {"repo": "someone/somewhere", "pr": 4})
    assert outcome["ok"] is True
    assert outcome["sent"] == 1
    assert outcome["brought"] == 1
    assert outcome["closed"] >= 1

    # The reply written here went out, against the comment that opened the thread.
    assert any("/comments/501/replies" in call and "written here" in call for call in desk.github_calls())
    row = {row["seq"]: row for row in desk.get("/comments")}[seq]
    said = [(reply["who"], reply["text"]) for reply in row["replies"]]
    assert ("session", "written here, not there yet") in said
    assert ("someone", "said on the PR") in said
    # Resolved there, so resolved here: the pull request is the copy everyone else reads.
    assert row["state"] == "resolved"
    assert row["prResolve"] == "done"

    # Syncing again sends nothing twice and brings nothing back twice.
    again = desk.post("/sync", {"repo": "someone/somewhere", "pr": 4})
    assert (again["sent"], again["brought"]) == (0, 0)
    assert len({row["seq"]: row for row in desk.get("/comments")}[seq]["replies"]) == 2


def test_a_comment_github_rejects_is_kept_and_not_retried(desk):
    made = desk.post(
        "/comments",
        {
            "comments": [{"branch": "feature", "path": "sample.py", "line": 15, "side": "new", "text": "out of diff"}],
            "github": True,
        },
    )
    desk.github_answers(code=1, err="gh: Unprocessable Entity (HTTP 422)")
    assert desk.post("/publish", {"repo": "someone/somewhere", "pr": 1, "seq": made["seqs"]})["ok"] is False
    kept = {row["seq"]: row for row in desk.get("/comments")}[made["seqs"][0]]
    assert kept["github"] == "refused"
    assert "422" in kept["error"]

    # Left out of every later sweep, so a rejection GitHub owns is not attempted forever.
    desk.github_answers(out=json.dumps({"html_url": "https://github.com/x/y/pull/1#pullrequestreview-43"}))
    assert desk.post("/publish", {"repo": "someone/somewhere", "pr": 1})["sent"] == 0
    assert {row["seq"]: row for row in desk.get("/comments")}[made["seqs"][0]]["github"] == "refused"


def test_a_file_comment_is_posted_against_the_file_and_no_line(desk):
    made = desk.post(
        "/comments",
        {
            "comments": [
                {"branch": "feature", "path": "sample.py", "line": 0, "side": "file", "text": "about the file"}
            ],
            "github": True,
        },
    )
    desk.github_answers(out=json.dumps({"html_url": "https://github.com/x/y/pull/13#review-1"}))
    assert desk.post("/publish", {"repo": "someone/somewhere", "pr": 13, "seq": made["seqs"]})["ok"]
    # What went out, read from what the stand-in was given: the file named, and no line beside it.
    sent = json.loads([call for call in desk.github_calls() if "/reviews" in call][-1].split(" <<< ", 1)[1])
    assert sent["comments"][-1] == {"path": "sample.py", "body": "about the file", "subject_type": "file"}
    posted = {row["seq"]: row for row in desk.get("/comments")}[made["seqs"][0]]
    assert posted["github"] == "posted"
    assert posted["side"] == "file"
    assert posted["line"] == 0


def test_a_comment_settled_here_is_not_sent_unless_it_is_asked_for(desk):
    made = desk.post(
        "/comments",
        {
            "comments": [
                {"branch": "feature", "path": "sample.py", "line": 27, "side": "new", "text": "answered already"}
            ],
            "github": True,
        },
    )
    seq = made["seqs"][0]
    desk.post("/resolve", {"seq": [seq], "answer": "dealt with", "who": "session"})

    # Bound for the pull request, but settled here before it went: sending leaves it alone.
    desk.github_answers(out=json.dumps({"html_url": "https://github.com/x/y/pull/14#review-1"}))
    desk.post("/publish", {"repo": "someone/somewhere", "pr": 14, "branch": "feature"})
    assert seq not in {row["seq"] for row in desk.get("/comments") if row["github"] == "posted"}
    row = {row["seq"]: row for row in desk.get("/comments")}[seq]
    assert row["github"] == "pending"

    # Asked for, it goes.
    landed = desk.post("/publish", {"repo": "someone/somewhere", "pr": 14, "branch": "feature", "resolved": True})
    assert landed["ok"]
    assert {row["seq"]: row for row in desk.get("/comments")}[seq]["github"] == "posted"


def test_a_comment_not_bound_for_github_is_never_offered_to_it(desk):
    made = desk.post(
        "/comments", [{"branch": "feature", "path": "sample.py", "line": 14, "side": "new", "text": "local"}]
    )
    row = {row["seq"]: row for row in desk.get("/comments")}[made["seq"]]
    assert row["github"] == "none"
    # Publishing everything owed must leave a comment that was never meant for the pull request alone.
    desk.github_answers(out=json.dumps({"html_url": "https://github.com/x/y/pull/1#pullrequestreview-44"}))
    desk.post("/publish", {"repo": "someone/somewhere", "pr": 1})
    assert {row["seq"]: row for row in desk.get("/comments")}[made["seq"]]["github"] == "none"


def test_comments_recorded_at_the_same_time_all_survive(desk):
    # Every change rewrites the whole log, so two at once used to write each other away and lose what was in between.
    before = {row["seq"] for row in desk.get("/comments")}
    made = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as crowd:
        for outcome in crowd.map(
            lambda n: desk.post(
                "/comments",
                [{"branch": "feature", "path": "sample.py", "line": 20 + n, "side": "new", "text": f"at once {n}"}],
            ),
            range(16),
        ):
            made.append(outcome["seq"])
    after = {row["seq"]: row for row in desk.get("/comments")}
    assert len(set(made)) == 16
    assert set(made) <= set(after)
    assert before <= set(after)
    # Numbering under concurrency is not ordered by who asked first; what matters is that every one is there, once.
    assert sorted(after[seq]["text"] for seq in made) == sorted(f"at once {n}" for n in range(16))


def test_replies_added_at_the_same_time_all_survive(desk):
    made = desk.post(
        "/comments", [{"branch": "feature", "path": "sample.py", "line": 19, "side": "new", "text": "busy"}]
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as crowd:
        list(crowd.map(lambda n: desk.post("/reply", {"seq": made["seq"], "text": f"reply {n}"}), range(12)))
    row = {row["seq"]: row for row in desk.get("/comments")}[made["seq"]]
    assert sorted(reply["text"] for reply in row["replies"]) == sorted(f"reply {n}" for n in range(12))


def test_the_comments_survive_as_a_readable_log(desk):
    rows = [json.loads(line) for line in (desk.home / "comments.jsonl").read_text().splitlines() if line.strip()]
    assert [row["seq"] for row in rows] == list(range(1, len(rows) + 1))
    assert {row["path"] for row in rows} >= {"sample.py", "added.py"}


def test_rescanning_switches_what_is_reviewed(desk):
    outcome = desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": ["feature"]})
    assert outcome["ok"]
    assert [branch["ref"] for branch in outcome["data"]["branches"]] == ["feature"]
    assert desk.get("/data")["branches"][0]["ref"] == "feature"
    # The page is rebuilt around the new payload, so a reload shows it without another command.
    assert "feature" in (desk.home / "diff_desk.html").read_text()


def test_a_ref_with_nothing_to_review_is_reported_not_served(desk):
    outcome = desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": ["parked"]})
    assert outcome["ok"] is False
    assert "nothing ahead" in outcome["error"]
    # What was being reviewed stays on screen, so a mistyped scan cannot empty the page.
    assert desk.get("/data")["branches"][0]["ref"] == "feature"


def test_a_branch_behind_the_base_shows_the_difference_it_does_have(desk):
    outcome = desk.post("/scan", {"dir": str(desk.repo), "base": "feature", "refs": ["main"]})
    assert outcome["ok"]
    branch = outcome["data"]["branches"][0]
    assert branch["commits"] == []
    assert branch["files"][0]["path"] == "added.py"
    desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": ["feature"]})


def test_a_dropped_comment_is_deleted_on_the_pull_request_and_leaves_the_log_behind(desk):
    made = desk.post(
        "/comments",
        {
            "comments": [
                {"branch": "feature", "path": "sample.py", "line": 42, "side": "new", "text": "written to be dropped"}
            ],
            "github": True,
        },
    )
    seq = made["seqs"][0]
    desk.github_answers(out=json.dumps({"html_url": "https://github.com/someone/somewhere/pull/7#review-1"}))
    desk.post("/publish", {"repo": "someone/somewhere", "pr": 7, "seq": [seq]})
    desk.post("/reply", {"seq": seq, "text": "answered before it went", "who": "session"})

    threads = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "T_drop",
                                "isResolved": False,
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 900,
                                            "body": "written to be dropped",
                                            "path": "sample.py",
                                            "author": {"login": "duburcqa"},
                                        },
                                        {
                                            "databaseId": 901,
                                            "body": "answered before it went",
                                            "path": "sample.py",
                                            "author": {"login": "duburcqa"},
                                        },
                                    ]
                                },
                            }
                        ]
                    }
                }
            }
        }
    }
    desk.github_answers(rules=[{"match": "reviewThreads", "out": json.dumps(threads)}])

    outcome = desk.post("/drop", {"seq": seq, "repo": "someone/somewhere", "pr": 7})
    assert outcome["ok"]
    # The reply goes first and the comment that opened the thread last, so nothing is left answering a deleted remark.
    deletions = [call for call in desk.github_calls() if "DELETE" in call]
    assert [call.split("/")[-1] for call in deletions[-2:]] == ["901", "900"]
    dropped = {row["seq"]: row for row in desk.get("/comments")}[seq]
    assert dropped["state"] == "deleted"
    assert dropped["text"] == "written to be dropped"

    # Dropped, it owes the pull request nothing and is offered to nothing: a sweep must not raise it from the log.
    assert desk.post("/publish", {"repo": "someone/somewhere", "pr": 7})["sent"] == 0
    assert desk.post("/close", {"repo": "someone/somewhere", "pr": 7})["closed"] == 0
    assert desk.post("/drop", {"seq": seq, "repo": "someone/somewhere", "pr": 7})["ok"] is False


def test_dropping_the_last_reply_leaves_the_comment_and_what_was_said_before_it(desk):
    made = desk.post(
        "/comments",
        {
            "comments": [{"branch": "feature", "path": "sample.py", "line": 43, "side": "new", "text": "kept"}],
            "github": False,
        },
    )
    seq = made["seqs"][0]
    assert desk.post("/drop", {"seq": seq, "reply": True})["ok"] is False

    desk.post("/reply", {"seq": seq, "text": "first answer", "who": "session"})
    desk.post("/reply", {"seq": seq, "text": "second answer", "who": "you"})
    outcome = desk.post("/drop", {"seq": seq, "reply": True})
    assert outcome["ok"]
    kept = {row["seq"]: row for row in desk.get("/comments")}[seq]
    assert kept["state"] == "open"
    assert [answer["text"] for answer in kept["replies"]] == ["first answer"]


def test_a_deletion_refused_by_github_leaves_the_comment_exactly_as_it_was(desk):
    made = desk.post(
        "/comments",
        {
            "comments": [{"branch": "feature", "path": "sample.py", "line": 44, "side": "new", "text": "refused drop"}],
            "github": True,
        },
    )
    seq = made["seqs"][0]
    desk.github_answers(out=json.dumps({"html_url": "https://github.com/someone/somewhere/pull/7#review-1"}))
    desk.post("/publish", {"repo": "someone/somewhere", "pr": 7, "seq": [seq]})

    threads = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "T_refused",
                                "isResolved": False,
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 950,
                                            "body": "refused drop",
                                            "path": "sample.py",
                                            "author": {"login": "duburcqa"},
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                }
            }
        }
    }
    desk.github_answers(
        rules=[
            {"match": "reviewThreads", "out": json.dumps(threads)},
            {"match": "DELETE", "code": 1, "err": "gh: Forbidden (HTTP 403)"},
        ]
    )
    outcome = desk.post("/drop", {"seq": seq, "repo": "someone/somewhere", "pr": 7})
    assert outcome["ok"] is False
    assert "403" in outcome["error"]
    # Still on the pull request, so it is still here: the two copies never disagree about what is said.
    still = {row["seq"]: row for row in desk.get("/comments")}[seq]
    assert still["state"] == "open"
    assert still["github"] == "posted"


def test_loading_the_page_collects_the_diffs_as_they_now_stand(desk):
    gen_diff_data.run(desk.repo, "checkout", "-q", "feature")
    written = desk.repo / "added.py"
    kept = written.read_text()
    try:
        desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": ["feature"]})
        was = desk.get("/state")["stamp"]
        assert was
        assert desk.get("/data")["stamp"] == was

        # Work saved on disk is part of what the checked-out branch shows, so the desk reports it has moved on - and
        # goes on serving what it collected until it is asked for more.
        written.write_text(kept + "SAID_ON_DISK = 1\n")
        moved = desk.get("/state")["stamp"]
        assert moved != was
        assert desk.get("/data")["stamp"] == was
        assert "SAID_ON_DISK" not in json.dumps(desk.get("/data"))

        # Loading the page is that request, so what it is built from holds the new line and stands at the new stamp.
        assert "SAID_ON_DISK" in desk.page()
        fresh = desk.get("/data")
        assert fresh["stamp"] == moved
        assert "SAID_ON_DISK" in json.dumps(fresh)
    finally:
        written.write_text(kept)
        gen_diff_data.run(desk.repo, "checkout", "-q", "main")
        desk.post("/scan", {"dir": str(desk.repo), "base": "main", "refs": ["feature"]})
    assert "SAID_ON_DISK" not in json.dumps(desk.get("/data"))
