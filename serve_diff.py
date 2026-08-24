"""Serve the diff desk locally: rescan any repository, record every comment, and hand them to a session in order.

Endpoints, all on 127.0.0.1 so nothing is exposed off the machine:
  GET  /                      the page
  GET  /data                  the payload the page renders
  GET  /refs?dir=&base=       branches ahead of a base, for the source picker
  GET  /state                 what the served diffs are built from, so a page can tell the branch has moved on
  POST /scan                  {dir, base, refs} - regenerate the payload and return it
  GET  /reviewed              which files have been read, so a tick outlives the browser it was made in
  POST /reviewed              {marks, drop} - tick files at the digest they were read at, or untick them
  GET  /comments?since=N      every recorded comment past the cursor, each with its seq and batch
  GET  /serving             which run of the desk is answering, so a watch armed against an older one stops
  GET  /comments?event=N      every comment touched past that event, which is how a session hears about replies
  POST /comments              {comments: [...], github: bool} - a batch as submitted, or a bare list of comments
  POST /bind                  {seq: [...], github} - mark comments as bound for the pull request, or keep them local
  POST /edit                  {seq, text, reply} - rewrite a comment, or the reply at that place in its thread,
                              keeping what it said before
  POST /reply                 {seq, text, who} - add a reply to a comment, from the session or from the reviewer
  POST /resolve               {seq: [...], answer, resolved, who} - close comments, or reopen them
  POST /drop                  {seq, reply, repo, pr} - delete a comment, or only its last reply, here and there
  POST /publish               {repo, pr, summary, seq, resolved} - post those comments as one review; everything owed
                              when seq is omitted, which is how a post that did not land is retried
  POST /send                  {seq, repo, pr} - send one thread to the pull request as it now reads: the remark if it
                              is not there yet, the replies it does not hold, and its resolution when closed here
  POST /sync                  {repo, pr} - bring back what the pull request holds: replies, what it says is resolved,
                              and the comments written there that this desk has none of

Deleting is the one thing that does discard: a dropped comment leaves the page and every exchange with the pull
request, and a dropped reply is gone from the thread. What was posted is deleted on the pull request first, so a
deletion that could not be made there leaves both copies as they were rather than hiding a remark that is still on it.

Nothing leaves this desk on its own. A sync listens: it brings back the replies a thread holds, takes the pull
request's word on what is resolved, and records the comments written there. What this desk holds goes out one thread at
a time, when the reader sends it, and carries the thread as it reads at that moment - a reply written afterwards waits
for them to send it again.

A comment is a thread: a remark plus replies from either side, each stamped with who wrote it and whether the pull
request holds it - `none` while it is only here, `posted` once it is there, `failed` when a send did not land, with its
reason. The remark is the reviewer's own unless a sync brought it in from the pull request, in which case it carries the
author who wrote it there and stays their word: this desk answers and resolves it, and leaves its wording and its
existence to them. A reply
leaves the thread open; only resolving closes it, either side may do so, and a resolved thread keeps its text and every
reply - closing it hides nothing and deletes nothing. Rewriting a comment, or any reply written here, keeps every
earlier wording under `edits`, and one already posted is flagged as having moved on from what the pull request holds
rather than silently disagreeing with it. A reply brought back from the pull request is left as its author wrote it.

Closing a comment here closes nothing there. Sending the thread does, and where that stands is tracked apart under
`prResolve` - `done` once GitHub confirms it, `failed` when the attempt did not happen, with its reason. A thread closed
here whose thread is still open there says so rather than reading as resolved everywhere, and a sync that finds it open
there corrects a claim this desk could no longer stand behind.

A comment also carries where it stands with the pull request, apart from whether it is resolved: `none` when it was
never meant to go there, `pending` while it still owes a post, `failed` after an attempt worth trying again, `refused`
when GitHub rejected the comment itself and retrying cannot help, `posted` once it landed. A failure and a refusal both
keep their reason. The log on disk is written before GitHub is contacted, so a post that does not land loses nothing.
"""

import contextlib
import json
import os
import pathlib
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple
from urllib.parse import parse_qs, urlparse

import gen_diff_data


class Source(NamedTuple):
    """Where the served diffs come from: the repository, what they are taken against, and which refs are shown."""

    root: str
    base: str
    refs: list


class Serving:
    """What is being served, so a page load can collect it again and a page can be told it has moved on.

    Named at startup and renamed by every scan, since a scan is how the reader chooses what to review.
    """

    source = None


HERE = pathlib.Path(__file__).parent
HOME = gen_diff_data.home()
PAGE = HOME / "diff_desk.html"
TEMPLATE = HERE / "diff_desk_template.html"
DATA = HOME / "diff_data.json"
NOTES = HOME / "comments.jsonl"
TICKS = HOME / "reviewed.json"
PULLS = HOME / "pulls.json"
PORT = int(os.environ.get("DIFF_DESK_PORT", "8787"))


def read_notes():
    """Every recorded comment, numbered: a row written before the cursor existed is numbered by its position."""
    if not NOTES.exists():
        return []
    rows = [json.loads(line) for line in NOTES.read_text().splitlines() if line.strip()]
    for index, row in enumerate(rows, start=1):
        row.setdefault("seq", index)
        row.setdefault("batch", 0)
        row.setdefault("state", "open")
        row.setdefault("github", "none")
        row.setdefault("replies", [])
        row.setdefault("edits", [])
        row.setdefault("prResolve", "none")
        if row.get("endLine") and row.get("line") and row["endLine"] < row["line"]:
            row["line"], row["endLine"] = row["endLine"], row["line"]
        # A row written before the cursor existed is as old as its position says.
        row.setdefault("event", row["seq"])
        row.setdefault("eventBy", "you")
        for answer in row["replies"]:
            # A reply written before it had a standing of its own: one already on the pull request stands as posted, and
            # one written here stands local, which is what it would have been given had it been written now. One brought
            # back before it was stamped says where it is in its standing, so the words that stood for a time go.
            already = answer.pop("posted", False) or answer["at"] == "on the PR"
            answer.setdefault("github", "posted" if already else "none")
            if answer["at"] == "on the PR":
                answer["at"] = ""
    return rows


THREADS = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      headRefName
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          path
          line
          startLine
          originalLine
          originalStartLine
          diffSide
          comments(first: 50) { nodes { databaseId body path createdAt author { login } } }
        }
      }
    }
  }
}
"""

RESOLVE = "mutation($thread: ID!) { resolveReviewThread(input: {threadId: $thread}) { thread { isResolved } } }"

# A comment whose thread is nowhere on the pull request, which is the one failure decided without asking GitHub.
NOWHERE = "its thread could not be found on the pull request"


class Owing(NamedTuple):
    """One thing a pull request is owed: a reply to add to a review thread, or that thread resolved.

    A reply carries the text and the comment that opened the thread, which is what a reply sent on its own is posted
    against; a resolution carries neither.
    """

    thread: str
    comment: int
    body: str


def review_threads(repo, number):
    """Every review thread of a pull request and the ref it is opened on, or nothing and why it could not be read."""
    owner, _, name = repo.partition("/")
    variables = ["-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={number}"]
    done = gen_diff_data.gh("api", "graphql", "-f", f"query={THREADS}", *variables, repeatable=True)
    if done.returncode != 0:
        return None, " ".join((done.stderr or done.stdout).split())[:300]
    try:
        pull = json.loads(done.stdout)["data"]["repository"]["pullRequest"]
        return Reviewed(pull["reviewThreads"]["nodes"], pull.get("headRefName") or f"#{number}"), ""
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        return None, f"the pull request's threads could not be read: {type(error).__name__}"


def resolve_thread(thread):
    """Resolve one review thread, and believe it only when GitHub says it is resolved.

    Judged on the answer rather than on the exit status: a query can come back carrying errors and still be a successful
    request, and this desk must never report a resolution the pull request does not have.
    """
    # A thread already resolved resolves again to the same thing, so a lost answer costs nothing to ask for twice.
    done = gen_diff_data.gh("api", "graphql", "-f", f"query={RESOLVE}", "-F", f"thread={thread}", repeatable=True)
    if done.returncode != 0:
        return False, " ".join((done.stderr or done.stdout).split())[:300]
    try:
        answer = json.loads(done.stdout)
    except json.JSONDecodeError:
        return False, f"GitHub answered with something other than an answer: {done.stdout[:120]}"
    if answer.get("errors"):
        return False, " ".join(str(answer["errors"])[:300].split())
    if answer.get("data", {}).get("resolveReviewThread", {}).get("thread", {}).get("isResolved") is True:
        return True, ""
    return False, "GitHub did not report the thread as resolved"


def packed_mutations(wanted):
    """Ask for every mutation at once, under an alias apiece, and read each answer back against the item that asked.

    GraphQL carries as many mutations as a document is given aliases for, and answers them one by one - naming, when one
    of them fails, the alias it failed for. That is what lets a sync spend a single request on everything it owes while
    each comment still learns the fate of its own reply or resolution.
    """
    names, fields, arguments = [], [], []
    for index, item in enumerate(wanted):
        names.append(f"$t{index}: ID!")
        # Passed as variables rather than written into the document, so no comment text can be read as part of it.
        arguments += ["-f", f"t{index}={item.thread}"]
        if item.body:
            names.append(f"$b{index}: String!")
            arguments += ["-f", f"b{index}={item.body}"]
            fields.append(
                f"  m{index}: addPullRequestReviewThreadReply"
                f"(input: {{pullRequestReviewThreadId: $t{index}, body: $b{index}}}) {{ comment {{ id }} }}"
            )
        else:
            fields.append(
                f"  m{index}: resolveReviewThread(input: {{threadId: $t{index}}}) {{ thread {{ isResolved }} }}"
            )
    document = "mutation({}) {{\n{}\n}}".format(", ".join(names), "\n".join(fields))
    # A document that adds a reply must not be sent twice on a lost answer, since GitHub may have added it already.
    # One of nothing but resolutions can be, each of them landing on the same resolved thread it would have anyway.
    carries_reply = any(item.body for item in wanted)
    done = gen_diff_data.gh("api", "graphql", "-f", f"query={document}", *arguments, repeatable=not carries_reply)
    try:
        answer = json.loads(done.stdout)
    except json.JSONDecodeError:
        # No answer at all is the only failure the whole document shares: nothing in it can be told apart.
        told = " ".join((done.stderr or done.stdout).split())[:300] or "GitHub gave no answer"
        return [(False, told)] * len(wanted)
    blamed = {}
    for trouble in answer.get("errors") or []:
        named = (trouble.get("path") or [""])[0]
        blamed[named] = " ".join(str(trouble.get("message") or trouble).split())[:300]
    # An error naming no alias is the document's own, so it stands as the reason for whatever came back empty.
    whole = blamed.get("") or "GitHub did not answer for it"
    data = answer.get("data") or {}
    outcome = []
    for index, item in enumerate(wanted):
        held = data.get(f"m{index}")
        went = bool(held) if item.body else ((held or {}).get("thread") or {}).get("isResolved") is True
        outcome.append((True, "") if went else (False, blamed.get(f"m{index}") or whole))
    return outcome


def carry_out(repo, number, wanted):
    """Everything owed to a pull request, asked in one request, and what became of each item in the order it was given.

    A lone item keeps the plain spelling it has always had, so the simple case is exactly the call it always was.
    """
    if not wanted:
        return []
    if len(wanted) == 1:
        one = wanted[0]
        outcome = [reply_on_pull(repo, number, one.comment, one.body) if one.body else resolve_thread(one.thread)]
    else:
        outcome = packed_mutations(wanted)
    for item, (went, trouble) in zip(wanted, outcome, strict=True):
        if not went:
            print(f"{'REPLY' if item.body else 'RESOLVE'} REFUSED {trouble}", flush=True)
    return outcome


class Sent(NamedTuple):
    """What sending one thread came to: the replies that went, whether it was resolved there, and what refused."""

    sent: list
    resolved: bool
    trouble: str


def thread_of(threads, text):
    """The review thread a remark opened, found by the text of it, which is what this desk posted."""
    return next(
        (node for node in threads if node["comments"]["nodes"] and node["comments"]["nodes"][0]["body"] == text), None
    )


def owed_by(row, thread, going):
    """What a send owes the pull request for one thread: those replies, and its resolution when it is closed here."""
    said = thread["comments"]["nodes"]
    wanted = [Owing(thread["id"], said[0]["databaseId"], text) for text in going]
    if row.get("state") == "resolved" and not thread["isResolved"]:
        wanted.append(Owing(thread["id"], 0, ""))
    return wanted


def sent_out(wanted, answers):
    """What a send got back, read against what it asked for, in the order it asked."""
    sent, resolved, trouble = [], False, ""
    for item, (went, why) in zip(wanted, answers, strict=True):
        if not item.body:
            resolved = went
        elif went:
            sent.append(item.body)
        if not went:
            trouble = trouble or why
    return Sent(sent, resolved, trouble)


def note_trouble(row, trouble):
    """Say on the comment why a send did not land: on the replies it would have carried, and on its resolution."""
    for answer in row["replies"]:
        if not answer.get("note") and answer["github"] != "posted":
            answer["github"] = "failed"
            answer["error"] = trouble
    if row.get("state") == "resolved":
        row["prResolve"] = "failed"
        row["prResolveError"] = trouble


def settle_sent(row, thread, going, step):
    """Write what a send came to onto the comment it was asked for: its replies, and where its resolution stands."""
    spoken = {node["body"] for node in thread["comments"]["nodes"]}
    for answer in row["replies"]:
        if answer.get("note"):
            continue
        if answer["text"] in step.sent or answer["text"] in spoken:
            answer["github"] = "posted"
            answer.pop("error", None)
        elif answer["text"] in going:
            answer["github"] = "failed"
            answer["error"] = step.trouble
    if step.resolved or thread["isResolved"]:
        row["prResolve"] = "done"
        row.pop("prResolveError", None)
    elif row.get("state") == "resolved":
        row["prResolve"] = "failed"
        row["prResolveError"] = step.trouble


def post_review(repo, number, summary, sending):
    """Post comments as one review, and say whether it landed, where it landed, and why it did not.

    A remark about a whole file is a review comment naming the file and no line within it; every other one names the
    line it was written on, and a range names where it starts as well.
    """
    review = {"event": "COMMENT", "body": summary or "Review from the diff desk.", "comments": []}
    for note in sending:
        if note.get("side") == "file":
            review["comments"].append({"path": note["path"], "body": note["text"], "subject_type": "file"})
            continue
        side = "LEFT" if note.get("side") == "old" else "RIGHT"
        comment = {
            "path": note["path"],
            "body": note["text"],
            "line": note.get("endLine") or note["line"],
            "side": side,
        }
        if note.get("endLine") and note["endLine"] != note["line"]:
            comment["start_line"] = note["line"]
            comment["start_side"] = side
        review["comments"].append(comment)
    target = f"repos/{repo}/pulls/{number}/reviews"
    print(f"PUBLISH {len(review['comments'])} comment(s) -> {target}", flush=True)
    done = gen_diff_data.gh(
        "api", "--method", "POST", target, "--input", "-", repeatable=False, given=json.dumps(review)
    )
    landed = done.returncode == 0
    url = json.loads(done.stdout or "{}").get("html_url", "") if landed else ""
    error = "" if landed else " ".join((done.stderr or done.stdout).split())[:400]
    return landed, url, error, not landed and is_refusal(error)


def serve_payload(payload):
    """Make this payload the served one: the numbers it names are remembered, then it and the page are written."""
    remembered_pulls(payload["branches"])
    DATA.write_text(json.dumps(payload, separators=(",", ":")))
    if TEMPLATE.exists():
        PAGE.write_text(gen_diff_data.render_page(TEMPLATE.read_text(), payload))


def rebuild():
    """Collect the served diffs again and rewrite the page around them, keeping the last good pair on any failure.

    One at a time, since two page loads at once would write the same two files: what a caller gets is either the diffs
    as they now stand, or exactly what was being served before.
    """
    source = Serving.source
    if source is None:
        return None
    with BUILDING:
        try:
            payload = gen_diff_data.collect(source.root, source.base, source.refs)
        except Exception as error:  # noqa: BLE001 - a page must still be served, so this is reported and set aside
            print(f"REBUILD FAILED {type(error).__name__}: {error}", flush=True)
            return None
        if not payload["branches"]:
            print(f"REBUILD FAILED nothing ahead of {source.base} in {source.root}", flush=True)
            return None
        serve_payload(payload)
        return payload


def delete_comment(repo, comment):
    """Delete one review comment from a pull request, and say why it did not go when it did not."""
    done = gen_diff_data.gh("api", "-X", "DELETE", f"/repos/{repo}/pulls/comments/{comment}", repeatable=True)
    if done.returncode != 0:
        return False, " ".join((done.stderr or done.stdout).split())[:300]
    return True, ""


class Reviewed(NamedTuple):
    """What a pull request answers about its review: every thread, and the ref they are opened on."""

    threads: list
    ref: str


class Owed(NamedTuple):
    """What one comment and its thread owe each other, worked out before any of it is asked of GitHub."""

    landed: list
    incoming: list
    settled: bool
    given: tuple | None
    wanted: list
    # When the pull request says each of its comments was written, for anything held here without a stamp of its own.
    stamps: dict = types.MappingProxyType({})


class Reconciled(NamedTuple):
    """What one comment's sync came to: replies each way, how many went out, and where resolution stands."""

    landed: list
    sent: int
    incoming: list
    settled: bool
    given: tuple | None


def heard_from(row, thread):
    """What one comment learns from its thread: which of its replies are on it, what it holds that this desk has not,
    and whether it is resolved there.

    A sync only listens. What this desk holds reaches the pull request when the reader sends that thread, so nothing
    read here is owed back and nothing recorded here is second-guessed: a resolution that has not been sent is a local
    one, not a claim about the pull request, and stands until the reader sends the thread.
    """
    if thread is None:
        return Owed([], [], False, None, [])
    said = thread["comments"]["nodes"]
    # When the pull request says each of these was written, which is the answer for anything this desk holds without
    # one: a reply brought back before it kept them has a date after all, and it is the pull request that knows it.
    stamps = {node["body"]: said_at(node) for node in said}
    return Owed(landed_replies(row, said), incoming(row, said), thread["isResolved"], None, [], stamps)


def reconciled(plan, answers):
    """One comment's sync once GitHub has answered: what it asked for, read back in the order it asked."""
    landed, sent, given = list(plan.landed), 0, plan.given
    for item, (went, trouble) in zip(plan.wanted, answers, strict=True):
        if not item.body:
            given = ("done", "") if went else ("failed", trouble)
        elif went:
            landed.append(item.body)
            sent += 1
    return Reconciled(landed, sent, plan.incoming, plan.settled, given)


def under_review(row, order):
    """Whether a comment belongs to the review this request is about.

    One log holds every review this desk has ever served, so a request naming a pull request must reach only the
    comments of that pull request. Judged on where a comment was sent, falling back to the branch it was written on for
    one not sent anywhere yet: without this, reviewing one pull request would post, resolve or fail the comments of
    another - and their threads, being nowhere to be found, would be marked as owing a resolution for ever.
    """
    if row.get("prRepo") or row.get("prNumber"):
        return row.get("prRepo") == order.get("repo") and row.get("prNumber") == order.get("pr")
    if row.get("review") and order.get("review"):
        return row["review"] == order["review"]
    # A comment written before a review was named this way is placed by the ref it was written on. A request naming no
    # ref at all is asking about everything it can reach, which is what a bare repository and number mean.
    named = {name for name in (order.get("branch"), order.get("review")) if name}
    if not named:
        return True
    if order.get("pr"):
        named |= {f"refs/diffdesk/pull/{order['pr']}", f"#{order['pr']}"}
    return row.get("branch") in named


def settle(row, landed, incoming, resolved, stamps=()):
    """Write what a sync found onto one comment: replies that are on the pull request, replies brought back from it,
    when the pull request says each was written, and its resolution - the pull request being the copy others read,
    what it says is resolved is resolved."""
    stamps = stamps or {}
    if not row.get("at") and row["text"] in stamps:
        row["at"] = stamps[row["text"]]
    for answer in row.get("replies", []):
        if answer.get("note"):
            continue
        if answer["text"] in landed:
            answer["github"] = "posted"
        if not answer.get("at") and answer["text"] in stamps:
            answer["at"] = stamps[answer["text"]]
    if incoming:
        row.setdefault("replies", []).extend(incoming)
    if resolved:
        row["state"] = "resolved"
        row["prResolve"] = "done"


def landed_replies(row, said):
    """The replies of this comment the thread holds already, whether this desk sent them or they were pasted there."""
    spoken = {answer["body"] for answer in said}
    return [
        answer["text"]
        for answer in row["replies"]
        if not answer.get("note") and (answer["github"] == "posted" or answer["text"] in spoken)
    ]


def going_out(row, said):
    """What this thread owes the pull request, in the state the thread is in: the replies of it that are not there yet.

    Read at the moment a send is asked for and no later: a reply written afterwards waits for the reader to send the
    thread again, since what leaves this desk is never decided by what happens to be written in it.
    """
    spoken = {answer["body"] for answer in said}
    return [
        answer["text"]
        for answer in row["replies"]
        if not answer.get("note") and answer["github"] != "posted" and answer["text"] not in spoken
    ]


def brought_in(thread, order, ref):
    """One review thread this desk has no record of, as a comment of its own.

    Recorded as posted, which it is: replies, resolution and deletion all find their thread by the text that opened it,
    so a comment written on the pull request travels the same way as one written here. It carries its author, which is
    what tells the reader whose remark it is and keeps this desk from rewriting somebody else's words.

    A thread whose lines the diff no longer holds is placed by the lines it was written against; one the pull request
    reports no line for at all is a remark on the file.
    """
    said = thread["comments"]["nodes"]
    if thread["line"]:
        start, line = thread["startLine"] or thread["line"], thread["line"]
    else:
        start, line = thread["originalStartLine"] or thread["originalLine"], thread["originalLine"]
    return {
        "branch": ref,
        "review": f"#{order['pr']}",
        "prRepo": order["repo"],
        "prNumber": order["pr"],
        "path": thread["path"],
        "line": start,
        "endLine": line,
        "side": ("new" if thread["diffSide"] == "RIGHT" else "old") if line else "file",
        "anchor": "",
        "text": said[0]["body"],
        "who": author_of(said[0]),
        "at": said_at(said[0]),
        "state": "resolved" if thread["isResolved"] else "open",
        "github": "posted",
        "replies": [
            {"who": author_of(answer), "text": answer["body"], "at": said_at(answer), "github": "posted"}
            for answer in said[1:]
        ],
        "edits": [],
        "prResolve": "done" if thread["isResolved"] else "none",
    }


def take_in(rows, arriving, order, ref):
    """Record every thread of the pull request this desk has no record of, and answer with the comments they became.

    Numbered as any comment written here is, and all of one sync under one batch, which is what groups them as the
    reading they arrived from.
    """
    seq = max((row.get("seq", 0) for row in rows), default=0)
    group = max((row.get("batch", 0) for row in rows), default=0) + 1
    taken = []
    for thread in arriving:
        seq += 1
        note = brought_in(thread, order, ref)
        note["seq"], note["batch"] = seq, group
        rows.append(note)
        # Somebody else's remark, so it is news for this side just as a reply brought back from there is.
        touched(rows, note, "you")
        taken.append(note)
    return taken


def said_at(said):
    """When one comment of the pull request was written, to the minute, which is as fine as a foreign thread needs."""
    return (said.get("createdAt") or "").replace("T", " ")[:16]


def reader():
    """Who GitHub takes the reader for, read off the payload the page is served from.

    Asked for when the diffs are collected and answered from there afterwards: one question to GitHub per collection,
    and the desk and the page agree on whose words are the reader's own without asking twice.
    """
    if not DATA.exists():
        return ""
    try:
        return json.loads(DATA.read_text()).get("viewer") or ""
    except json.JSONDecodeError:
        return ""


def author_of(said):
    """Who wrote one comment on the pull request, or that it came from there when GitHub names nobody."""
    return (said.get("author") or {}).get("login") or "github"


def incoming(row, said):
    """The replies the thread holds that this desk does not, as replies of its own."""
    ours = {answer["text"] for answer in row.get("replies", [])} | {row["text"]}
    return [
        {"who": author_of(answer), "text": answer["body"], "at": said_at(answer), "github": "posted"}
        for answer in said[1:]
        if answer["body"] not in ours
    ]


def reply_on_pull(repo, number, comment, text):
    """Answer a pull request's review comment in its own thread. Says whether it went out, and why it did not."""
    where = f"repos/{repo}/pulls/{number}/comments/{comment}/replies"
    done = gen_diff_data.gh("api", "--method", "POST", where, "-f", f"body={text}", repeatable=False)
    if done.returncode != 0:
        return False, " ".join((done.stderr or done.stdout).split())[:300]
    return True, ""


def is_refusal(error):
    """Whether GitHub rejected the comment itself - a line outside the diff, a request that is gone, no permission.

    Retrying cannot cure any of those, so such a comment is kept with its reason rather than attempted on every sweep.
    Anything else - unreachable, timed out, a server error - stays owed and is tried again.
    """
    return any(code in error for code in ("HTTP 401", "HTTP 403", "HTTP 404", "HTTP 422"))


def touched(rows, row, by):
    """Stamp a row as the latest news, and say which side made it.

    A session follows this rather than the comment numbers: a reply on a comment it has already read is news just as
    much as a new comment, and numbering alone cannot say so. Its own writes are stamped too, so that what it is waiting
    for can be told from what it just did.
    """
    row["event"] = max((held.get("event", 0) for held in rows), default=0) + 1
    row["eventBy"] = by
    # The last word said carries that stamp too, so a session woken by a thread can tell which line woke it from the
    # ones it has already read.
    said = row.get("replies") or []
    if said and not said[-1].get("event"):
        said[-1]["event"] = row["event"]


def read_ticks():
    """Which files have been read, as `<review> <path>` against the digest of the diff they were read at."""
    return json.loads(TICKS.read_text()) if TICKS.exists() else {}


def write_ticks(marks):
    """Replace the ticks in one step, so a reader never sees them half-written."""
    spare = TICKS.with_suffix(".writing")
    spare.write_text(json.dumps(marks, indent=1, sort_keys=True))
    os.replace(spare, TICKS)


def remembered_pulls(branches):
    """Fill in the pull request of every ref that has one, and remember what was learnt for the next collect.

    GitHub answers the listing or it does not, and a pull request also leaves it the moment it is merged. Either way the
    ref stops naming a pull request, and since that name is what the ticks and the comments of a review are filed under,
    a review read once as `#3237` would read as an untouched branch afterwards. So a number, once seen for a ref, is the
    ref's number until another one is seen for it.
    """
    known = json.loads(PULLS.read_text()) if PULLS.exists() else {}
    learnt = dict(known)
    for branch in branches:
        if branch["pr"]:
            learnt[branch["ref"]] = branch["pr"]
        elif branch["ref"] in known:
            branch["pr"] = known[branch["ref"]]
    if learnt != known:
        spare = PULLS.with_suffix(".writing")
        spare.write_text(json.dumps(learnt, indent=1, sort_keys=True))
        os.replace(spare, PULLS)


def write_notes(rows):
    """Replace the log in one step, so a reader never sees it half-written."""
    spare = NOTES.with_suffix(".writing")
    spare.write_text("".join(json.dumps(row) + "\n" for row in rows))
    os.replace(spare, NOTES)


# The desk answers requests on several threads, and every change to the log is a read of the whole of it followed by a
# write of the whole of it. Without holding this, two changes made at once each rewrite the log the other just wrote,
# and a comment recorded in between is simply gone.
CHANGING = threading.Lock()

# Collecting the diffs ends in a write of the payload and of the page, so it is done one at a time for the same reason.
BUILDING = threading.Lock()
# This run of the desk, as the moment it started answering: a restart carries whatever the tool has become with it.
SERVING = time.strftime("%Y-%m-%d %H:%M:%S")


@contextlib.contextmanager
def changing():
    """The log, held for the length of a change. Never held across a call to GitHub: those take as long as they take."""
    with CHANGING:
        rows = read_notes()
        yield rows
        write_notes(rows)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body=b"", kind="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        route = urlparse(self.path)
        query = parse_qs(route.query)
        path = route.path.rstrip("/")
        if path in ("", "/index.html"):
            print(f"PAGE served to {self.headers.get('User-Agent', '?')}", flush=True)
            # Loading the page is asking for the diffs as they stand, so they are collected again before it goes out.
            rebuild()
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/serving":
            # Which desk is answering: a watch armed against one that has since been restarted is reading a page and
            # a wording that no longer exist, which is what tells it to arm itself again.
            self._json({"desk": SERVING})
        elif path == "/data":
            self._send(200, DATA.read_bytes(), "application/json")
        elif path == "/refs":
            root = query.get("dir", ["."])[0]
            base = query.get("base", ["upstream/main"])[0]
            upstream = gen_diff_data.canonical_repo(root)
            pulls = gen_diff_data.pull_requests(root, upstream) if upstream else {}
            self._json(
                {
                    "root": gen_diff_data.run(root, "rev-parse", "--show-toplevel").strip(),
                    "current": gen_diff_data.run(root, "rev-parse", "--abbrev-ref", "HEAD").strip(),
                    "upstream": upstream,
                    "refs": gen_diff_data.ahead_refs(root, base),
                    "pulls": sorted(pulls.values(), key=lambda row: -row["number"]),
                }
            )
        elif path == "/state":
            source = Serving.source
            marked = gen_diff_data.stamp(source.root, source.base, source.refs) if source else None
            self._json({"stamp": marked})
        elif path == "/lines":
            self._lines(query)
        elif path == "/favicon.ico":
            self._send(204)
        elif path == "/reviewed":
            self._json({"marks": read_ticks()})
        elif path == "/comments":
            since = int(query.get("since", ["0"])[0])
            event = int(query.get("event", ["0"])[0])
            rows = read_notes()
            if event:
                self._json([row for row in rows if row.get("event", 0) > event])
            else:
                self._json([row for row in rows if row.get("seq", 0) > since])
        else:
            self._send(404)

    def _lines(self, query):
        """A slice of a file at a revision, which is how the page fills the gaps between hunks.

        Work on disk is the one revision that can move under a page, and what a gap is asked in is the numbering of the
        diff the page holds. Anchors are how that is answered per gap: the lines the page has on either side of it, at
        the numbers it has them. Each is looked for where the page says it is and then, failing that, wherever it now
        reads - so work written above the gap, which moves every number below it, still fills. What both anchors must
        agree on is the shift, since that is what says the gap still holds the lines it did; when they cannot, or when
        one of them no longer reads at all, the gap has moved under the reader and this says so rather than answering.
        """
        root = pathlib.Path(query.get("dir", ["."])[0])
        rev = query.get("rev", [""])[0]
        name = query.get("path", [""])[0]
        anchors = json.loads(query.get("anchors", ["[]"])[0])
        text = gen_diff_data.run(root, "show", f"{rev}:{name}") if rev else (root / name).read_text()
        rows = text.split("\n")
        if rows and rows[-1] == "":
            rows.pop()
        shifts = set()
        for number, held in anchors:
            if 0 < number <= len(rows) and rows[number - 1] == held:
                shifts.add(0)
                continue
            reads = [spot + 1 for spot, row in enumerate(rows) if row == held]
            if not reads:
                self._json({"stale": True, "lines": [], "total": 0})
                return
            shifts.add(min(reads, key=lambda spot: abs(spot - number)) - number)
        if len(shifts) > 1:
            self._json({"stale": True, "lines": [], "total": 0})
            return
        shift = shifts.pop() if shifts else 0
        low = max(1, int(query.get("from", ["1"])[0]) + shift)
        high = min(len(rows), int(query.get("to", [str(len(rows))])[0]) + shift)
        # Reported in the numbering the page asked in, which is what it lays the lines out at.
        self._json({"total": len(rows), "from": low - shift, "to": high - shift, "lines": rows[low - 1 : high]})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        routes = {
            "/comments": self._record,
            "/reviewed": self._reviewed,
            "/scan": self._scan,
            "/bind": self._bind,
            "/edit": self._edit,
            "/reply": self._reply,
            "/resolve": self._resolve,
            "/drop": self._drop,
            "/forget": self._forget,
            "/publish": self._publish,
            "/send": self._send_thread,
            "/sync": self._sync,
        }
        serving = routes.get(path)
        if serving is None:
            self._send(404)
            return
        serving()

    def _reviewed(self):
        """Record which files have been read, and which have been unticked.

        Marks arrive as `<review> <path>` against the digest of the diff they were read at, and dropped keys as a list.
        A page that has been reading offline sends whatever it kept, which is how a browser's own copy is carried up.

        Dropped first and set second, so a page settling which name a file's tick is filed under can name every key it
        is replacing and the one it wants kept in the same request.
        """
        order = self._body()
        with CHANGING:
            marks = read_ticks()
            for gone in order.get("drop") or []:
                marks.pop(gone, None)
            marks.update(order.get("marks") or {})
            write_ticks(marks)
        set_marks = order.get("marks") or {}
        print(f"REVIEWED {len(set_marks)} set, {len(order.get('drop') or [])} dropped, {len(marks)} held", flush=True)
        self._json({"ok": True, "marks": marks})

    def _scan(self):
        """Rebuild the payload for the requested repository, base and refs, and rebuild the page around it."""
        order = self._body()
        root = order.get("dir") or "."
        base = order.get("base") or "upstream/main"
        refs = [ref for ref in (order.get("refs") or []) if ref]
        print(f"SCAN {root} {base} {refs or '(every branch ahead)'}", flush=True)
        Serving.source = Source(root, base, refs)
        try:
            payload = gen_diff_data.collect(root, base, refs)
        except Exception as error:  # noqa: BLE001 - whatever went wrong belongs on the page, not in a traceback
            print(f"SCAN FAILED {error}", flush=True)
            self._json({"ok": False, "error": f"{type(error).__name__}: {error}"})
            return
        if not payload["branches"]:
            self._json({"ok": False, "error": f"nothing ahead of {base} in {root}"})
            return
        serve_payload(payload)
        files = sum(len(entry["files"]) for entry in payload["branches"])
        print(f"SCANNED {len(payload['branches'])} branch(es), {files} file diffs", flush=True)
        self._json({"ok": True, "data": payload})

    def _record(self):
        body = self._body()
        # A batch names whether it is bound for GitHub; a bare list, or a lone comment, is the batch of one.
        if isinstance(body, dict) and "comments" in body:
            batch, bound = body["comments"], bool(body.get("github"))
        else:
            batch, bound = (body if isinstance(body, list) else [body]), False
        with changing() as rows:
            seq = max((row.get("seq", 0) for row in rows), default=0)
            group = max((row.get("batch", 0) for row in rows), default=0) + 1
            for note in batch:
                seq += 1
                # When it was written, where the writer did not say: a comment recorded through the API carries no
                # stamp of its own, and a thread with no time on it can say when nothing it holds was said.
                note.setdefault("at", time.strftime("%Y-%m-%d %H:%M:%S"))
                note["seq"] = seq
                note["batch"] = group
                note["state"] = "open"
                note["github"] = "pending" if bound else "none"
                note["replies"] = []
                rows.append(note)
                touched(rows, note, "you")
        print(f"BATCH {group}: {len(batch)} comment(s) submitted{', bound for GitHub' if bound else ''}", flush=True)
        for note in batch:
            span = str(note.get("line", "?"))
            if note.get("endLine") and note["endLine"] != note.get("line"):
                span += f"-{note['endLine']}"
            text = " ".join(str(note.get("text", "")).split())
            print(f"  COMMENT [{note['seq']}] {note.get('path', '?')}:{span} :: {text}", flush=True)
        self._json({"ok": True, "batch": group, "seq": seq, "seqs": [note["seq"] for note in batch]})

    def _bind(self):
        """Turn recorded comments towards the pull request, or back to local only.

        A comment is often written before deciding whether it should go out, and a refusal or a change of mind should
        not need the comment to be written again, so the decision stays changeable for as long as it has not landed.
        """
        order = self._body()
        wanted = set(order.get("seq") or [])
        bound = bool(order.get("github", True))
        turned = 0
        with changing() as rows:
            for row in rows:
                if row.get("seq") not in wanted or row.get("github") == "posted":
                    continue
                row["github"] = "pending" if bound else "none"
                row.pop("error", None)
                touched(rows, row, "session")
                if bound and order.get("repo"):
                    # Which pull request it is now headed for, so no other review's sweep picks it up.
                    row["prRepo"], row["prNumber"] = order["repo"], order.get("pr")
                turned += 1
        print(f"BIND {turned} comment(s) {'towards the pull request' if bound else 'back to local only'}", flush=True)
        self._json({"ok": True, "bound": turned})

    def _edit(self):
        """Rewrite a comment, or one of its replies, keeping every earlier wording.

        A reply is named by its place in the thread, which is how the page and a drop already address one, so `reply` 0
        is the first answer and no `reply` at all is the remark that opened the thread. Only what was written here can
        be reworded: a reply carried back from the pull request, and a remark brought in from it, are somebody else's
        word, and rewriting either would put words in their mouth on the one copy everyone reads.
        """
        order = self._body()
        text = (order.get("text") or "").strip()
        if not text:
            self._json({"ok": False, "error": "an empty comment says nothing"})
            return
        index = order.get("reply")
        said, refused = None, None
        with changing() as rows:
            found = next((row for row in rows if row["seq"] == order.get("seq")), None)
            replies = found["replies"] if found is not None else []
            if found is None:
                refused = f"no comment numbered {order.get('seq')}"
            elif index is None and found.get("who") and found["who"] != reader():
                refused = f"comment {found['seq']} is {found['who']}'s word, written on the pull request"
            elif index is None:
                said = found
            elif not 0 <= index < len(replies):
                refused = f"comment {found['seq']} has no reply {index}"
            elif replies[index]["who"] not in ("you", "session", reader()):
                refused = f"reply {index} of comment {found['seq']} is {replies[index]['who']}'s word, not this desk's"
            else:
                said = replies[index]
            if said is not None:
                said.setdefault("edits", []).append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "text": said["text"]})
                said["text"] = text
                # Rewriting is never carried to the pull request, so its copy is marked as having been moved on from.
                landed = (said["github"] if index is not None else found.get("github")) == "posted"
                if landed:
                    said["editedAfterPost"] = True
                touched(rows, found, "you")
        if refused is not None:
            self._json({"ok": False, "error": refused})
            return
        named = "" if index is None else f" reply {index}"
        print(f"EDIT [{found['seq']}]{named} {' '.join(text.split())}", flush=True)
        self._json({"ok": True, "seq": found["seq"], "reply": index, "edits": len(said["edits"])})

    def _reply(self):
        """Add a reply to a comment, from whichever side wrote it, or a note that stays here. A reply leaves the thread
        open.

        Recorded here and nowhere else: what a thread says reaches the pull request when the reader sends that thread,
        never as a consequence of somebody answering in it. A note goes further and never leaves at all: it is written
        for this side of the desk - what to look at again, what a session is to do - so nothing that decides what a
        thread owes the pull request looks at it.
        """
        order = self._body()
        text = (order.get("text") or "").strip()
        if not text:
            self._json({"ok": False, "error": "an empty reply says nothing"})
            return
        who = "you" if order.get("who") == "you" else "session"
        # What it hangs under: the remark it answers, or one of the words already said in the thread, by its place in
        # it. Nothing that hangs under a note can leave either, the pull request holding no such thing to answer.
        on = order.get("on")
        said = {"who": who, "text": text, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "github": "none"}
        if on is not None:
            said["on"] = on
        with changing() as rows:
            found = next((row for row in rows if row["seq"] == order.get("seq")), None)
            if found is None:
                is_note = bool(order.get("note"))
            else:
                said_before = found["replies"]
                if on is not None and not 0 <= on < len(said_before):
                    self._json({"ok": False, "error": f"comment {found['seq']} has nothing said at [{on}]"})
                    return
                is_note = bool(order.get("note"))
                if on is not None and said_before[on].get("note"):
                    # A reply is never turned into a note, nor a note into a reply: what is bound for the pull request
                    # cannot hang on something the pull request has never seen.
                    if not is_note:
                        self._json({"ok": False, "error": f"[{on}] is a note, which the pull request holds none of"})
                        return
                    # One more note on a note carries on where that note stands, so notes never stand inside notes.
                    while on is not None and said_before[on].get("note"):
                        on = said_before[on].get("on")
                    said.pop("on", None)
                    if on is not None:
                        said["on"] = on
                if is_note:
                    said["note"] = True
                said_before.append(said)
                touched(rows, found, who)
        if found is None:
            self._json({"ok": False, "error": f"no comment numbered {order.get('seq')}"})
            return
        print(f"{'NOTE' if is_note else 'REPLY'} [{found['seq']}] {who}: {' '.join(text.split())}", flush=True)
        self._json({"ok": True, "seq": found["seq"], "replies": len(found["replies"])})

    def _resolve(self):
        """Close comments, or reopen them. Either side may do it, and an answer is kept as a reply of its own."""
        order = self._body()
        wanted = set(order.get("seq") or [])
        answer = (order.get("answer") or "").strip()
        closing = bool(order.get("resolved", True))
        who = "you" if order.get("who") == "you" else "session"
        closed = 0
        with changing() as rows:
            for row in rows:
                if row.get("seq") in wanted:
                    row["state"] = "resolved" if closing else "open"
                    if answer:
                        row["replies"].append(
                            {"who": who, "text": answer, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "github": "none"}
                        )
                    touched(rows, row, who)
                    closed += 1
        print(f"{'RESOLVED' if closing else 'REOPENED'} {closed} comment(s) by {who}", flush=True)
        self._json({"ok": True, "resolved": closed, "state": "resolved" if closing else "open"})

    def _forget(self):
        """Forget the last note of a thread, named by its place in it.

        A note never left this desk, so nothing has to be asked of the pull request - which is what tells it apart
        from a reply, where a posted one has to be deleted there as well. What they share is that only the last of
        them can go: letting go of one further up leaves what stands under it standing against nothing.
        """
        order = self._body()
        seq, at = order.get("seq"), order.get("note")
        with changing() as rows:
            found = next((row for row in rows if row["seq"] == seq), None)
            said = (found or {}).get("replies") or []
            if found is None or at != len(said) - 1 or not said[at].get("note"):
                found = None
            else:
                said.pop(at)
                # What is said is addressed by its place in the thread, so the places move when one goes: an anchor
                # past it comes back one, and one that named it stands on the thread instead.
                for answer in said:
                    on = answer.get("on")
                    if on is None:
                        continue
                    if on == at:
                        answer.pop("on")
                    elif on > at:
                        answer["on"] = on - 1
                touched(rows, found, "you")
        if found is None:
            self._json({"ok": False, "error": f"[{at}] is not the last note of comment {seq}"})
            return
        print(f"FORGOT note [{at}] of [{seq}]", flush=True)
        self._json({"ok": True, "seq": seq, "note": at})

    def _drop(self):
        """Delete a comment, or only its last reply, here and on the pull request when it was posted.

        GitHub deletes a review comment whether or not anything answered it, and every reply is a comment of its own, so
        what this desk put there is deleted newest first. Replies written on the pull request itself are left alone, and
        a thread still holding one of them stays there with what remains. A remark brought in from the pull request goes
        no further than its own replies: the thread is its author's, and this desk answers in it rather than clears it.

        The pull request goes first: a deletion that could not be made there leaves the comment here untouched, so the
        two copies never disagree about what is still said.
        """
        order = self._body()
        seq = order.get("seq")
        last_only = bool(order.get("reply"))
        with CHANGING:
            rows = read_notes()
        found = next((row for row in rows if row["seq"] == seq), None)
        if found is None or found.get("state") == "deleted":
            self._json({"ok": False, "error": f"no comment numbered {seq}"})
            return
        replies = found.get("replies") or []
        if last_only and not replies:
            self._json({"ok": False, "error": "nothing has been said in answer to it"})
            return
        if not last_only and found.get("who") and found["who"] != reader():
            self._json({"ok": False, "error": f"comment {seq} is {found['who']}'s word, written on the pull request"})
            return
        # Newest first, so the comment that opened the thread is the last to go.
        going = (
            [replies[-1]["text"]] if last_only else [answer["text"] for answer in reversed(replies)] + [found["text"]]
        )
        gone = 0
        if found.get("github") == "posted" and order.get("repo") and order.get("pr"):
            reviewed, trouble = review_threads(order["repo"], order["pr"])
            if reviewed is None:
                print(f"DROP FAILED {trouble}", flush=True)
                self._json({"ok": False, "error": trouble})
                return
            thread = next(
                (
                    node
                    for node in reviewed.threads
                    if node["comments"]["nodes"] and node["comments"]["nodes"][0]["body"] == found["text"]
                ),
                None,
            )
            there = {
                answer["body"]: answer["databaseId"]
                for answer in (thread or {"comments": {"nodes": []}})["comments"]["nodes"]
            }
            for text in going:
                if text not in there:
                    continue
                went, trouble = delete_comment(order["repo"], there[text])
                if not went:
                    print(f"DROP FAILED {trouble}", flush=True)
                    self._json({"ok": False, "error": trouble, "deleted": gone})
                    return
                gone += 1
        with changing() as fresh:
            row = next((row for row in fresh if row["seq"] == seq), None)
            if row is not None:
                if last_only:
                    row["replies"] = (row.get("replies") or [])[:-1]
                else:
                    row["state"] = "deleted"
                    row["prResolve"] = "none"
                touched(fresh, row, "you")
        print(
            f"DROPPED {'the last reply of' if last_only else ''} [{seq}] ({gone} deleted on the pull request)",
            flush=True,
        )
        self._json({"ok": True, "seq": seq, "reply": last_only, "deleted": gone})

    def _publish(self):
        """Post comments to a pull request as one review, and record where each of them now stands.

        Called with `seq` for a batch just submitted, and without it to clear whatever is still owed, which is what
        makes a post that did not land recoverable rather than lost.
        """
        order = self._body()
        wanted = set(order.get("seq") or [])
        with CHANGING:
            rows = read_notes()
        owed = [
            row
            for row in rows
            if row.get("github") in ("pending", "failed")
            and under_review(row, order)
            and row.get("state") != "deleted"
            and (order.get("resolved") or row.get("state") != "resolved")
        ]
        sending = [row for row in owed if row["seq"] in wanted] if wanted else owed
        if not sending:
            self._json({"ok": True, "sent": 0, "owed": 0})
            return
        landed, url, error, refused = post_review(order["repo"], order["pr"], order.get("summary"), sending)
        marked = {note["seq"] for note in sending}
        # Read again now the call is over: comments recorded while it ran must not be written away.
        with changing() as rows:
            for row in rows:
                if row["seq"] in marked:
                    row["github"] = "posted" if landed else "refused" if refused else "failed"
                    # Where it was sent, so a later sweep for another pull request leaves it alone.
                    row["prRepo"], row["prNumber"] = order["repo"], order["pr"]
                    if landed:
                        row["reviewUrl"] = url
                        row.pop("error", None)
                    else:
                        row["error"] = error
                    touched(rows, row, "session")
            waiting = [row for row in rows if row.get("github") in ("pending", "failed")]
            still = len([row for row in waiting if under_review(row, order)])
        print(f"{'PUBLISHED ' + url if landed else 'PUBLISH FAILED ' + error} ({still} still owed)", flush=True)
        self._json({"ok": landed, "url": url, "error": error, "sent": len(sending) if landed else 0, "owed": still})

    def _send_thread(self):
        """Send one thread to the pull request, in the state it is in when the reader asks.

        What goes out is this thread as it now reads: the remark if it is this desk's and not there yet, every reply the
        thread does not hold, and its resolution when it is closed here. A reply written after this has answered waits
        for the reader to ask again - what leaves this desk is their decision, never a consequence of somebody writing
        in a thread.
        """
        order = self._body()
        with CHANGING:
            rows = read_notes()
        found = next((row for row in rows if row["seq"] == order.get("seq")), None)
        if found is None or found.get("state") == "deleted":
            self._json({"ok": False, "error": f"no comment numbered {order.get('seq')}"})
            return
        # A remark written on the pull request is its author's word and is already there, so what this desk sends of
        # that thread is what this desk wrote in it: the replies made here, and its resolution.
        if not found.get("who") and found.get("github") != "posted":
            trouble = self._post_remark(order, found)
            if trouble:
                print(f"SEND FAILED {trouble}", flush=True)
                self._json({"ok": False, "error": trouble, "sent": 0, "resolved": False})
                return
        reviewed, trouble = review_threads(order["repo"], order["pr"])
        thread = None if reviewed is None else thread_of(reviewed.threads, found["text"])
        if thread is None:
            trouble = trouble or NOWHERE
            # Written onto the comment, so the reader sees on the thread why the last send did not land: nothing sweeps
            # up after them, and a failure said only in a log they may not have open is a failure said nowhere.
            with changing() as fresh:
                for row in fresh:
                    if row["seq"] == found["seq"]:
                        note_trouble(row, trouble)
                        touched(fresh, row, "session")
            print(f"SEND FAILED {trouble}", flush=True)
            self._json({"ok": False, "error": trouble, "sent": 0, "resolved": False})
            return

        going = going_out(found, thread["comments"]["nodes"])
        wanted = owed_by(found, thread, going)
        step = sent_out(wanted, carry_out(order["repo"], order["pr"], wanted))
        with changing() as fresh:
            for row in fresh:
                if row["seq"] == found["seq"]:
                    settle_sent(row, thread, going, step)
                    touched(fresh, row, "session")
        told = f"[{found['seq']}] {len(step.sent)} reply(ies)" + (", resolved there" if step.resolved else "")
        print(f"SENT {told}", flush=True)
        self._json({"ok": not step.trouble, "error": step.trouble, "sent": len(step.sent), "resolved": step.resolved})

    def _post_remark(self, order, found):
        """Put the remark that opens a thread on the pull request, and say why it did not go when it did not."""
        landed, url, error, refused = post_review(order["repo"], order["pr"], order.get("summary"), [found])
        with changing() as fresh:
            for row in fresh:
                if row["seq"] != found["seq"]:
                    continue
                row["github"] = "posted" if landed else "refused" if refused else "failed"
                # Where it was sent, so a later sweep for another pull request leaves it alone.
                row["prRepo"], row["prNumber"] = order["repo"], order["pr"]
                if landed:
                    row["reviewUrl"] = url
                    row.pop("error", None)
                else:
                    row["error"] = error
                touched(fresh, row, "session")
        return "" if landed else error

    def _sync(self):
        """Bring this desk and the pull request to the same state.

        A sync only listens: replies come back, a thread resolved there is closed here since the pull request is the
        copy everyone else reads, and threads written there arrive as comments of their own. What this desk holds goes
        out when the reader sends that thread, never as a consequence of a sync. A thread is matched by the body of the
        comment that opened it, which is the text this desk posted.
        """
        order = self._body()
        reviewed, trouble = review_threads(order["repo"], order["pr"])
        if reviewed is None:
            print(f"SYNC FAILED {trouble}", flush=True)
            self._json({"ok": False, "error": trouble})
            return
        with CHANGING:
            rows = read_notes()
        posted = {
            row["seq"]: row
            for row in rows
            if row.get("github") == "posted" and row.get("state") != "deleted" and under_review(row, order)
        }
        theirs = {}
        for thread in reviewed.threads:
            said = thread["comments"]["nodes"]
            if said:
                theirs[said[0]["body"]] = thread
        # Threads opened on the pull request itself, which is where a comment this desk never wrote comes from. Matched
        # by the same body text a thread of its own is matched by, so a second sync finds them already here.
        known = {row["text"] for row in rows}
        arriving = [thread for body, thread in theirs.items() if body not in known]
        found = {seq: heard_from(row, theirs.get(row["text"])) for seq, row in posted.items()}
        brought = sum(len(step.incoming) for step in found.values())
        closed = len([seq for seq, step in found.items() if step.settled and posted[seq].get("state") != "resolved"])
        with changing() as fresh:
            for row in fresh:
                step = found.get(row["seq"])
                if step is None:
                    continue
                was_open = row.get("state") != "resolved"
                settle(row, step.landed, step.incoming, step.settled, step.stamps)
                # A reply brought back from the pull request is somebody else's word, so it is news for this side, and
                # so is a thread somebody closed there. A sync that found neither is not news, and saying it is would
                # float every synced thread above the answers written here since, on a page ordered by what is recent.
                if step.incoming:
                    touched(fresh, row, "you")
                elif step.settled and was_open:
                    touched(fresh, row, "session")
                if step.given:
                    row["prResolve"], trouble = step.given
                    if trouble:
                        row["prResolveError"] = trouble
                    else:
                        row.pop("prResolveError", None)
            taken = take_in(fresh, arriving, order, reviewed.ref)
        told = f"brought {brought} reply(ies) back, took {len(taken)} comment(s) in, closed {closed} here"
        print(f"SYNC {told}", flush=True)
        for note in taken:
            where = "the file" if note["side"] == "file" else f"L{note['endLine']}"
            print(f"  FROM THE PULL REQUEST [{note['seq']}] {note['who']} on {note['path']} {where}", flush=True)
        self._json({"ok": True, "brought": brought, "took": len(taken), "closed": closed})

    def log_message(self, *args):
        pass


def main(source=None):
    Serving.source = source
    print(f"diff desk on http://127.0.0.1:{PORT}/  (comments -> {NOTES})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
