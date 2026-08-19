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
  POST /close                 {repo, pr} - resolve, on the pull request, the threads of comments closed here
  POST /sync                  {repo, pr} - carry replies both ways and take the pull request's word on what is resolved

Deleting is the one thing that does discard: a dropped comment leaves the page and every exchange with the pull
request, and a dropped reply is gone from the thread. What was posted is deleted on the pull request first, so a
deletion that could not be made there leaves both copies as they were rather than hiding a remark that is still on it.

A comment settled here stays here: posting and carrying replies leave it out unless the request asks for it, since a
remark already answered has no business arriving on the pull request. Resolving a thread that is already there is a
different matter, and always proceeds.

A comment is a thread: the reviewer's remark plus replies from either side, each stamped with who wrote it. A reply
leaves the thread open; only resolving closes it, either side may do so, and a resolved thread keeps its text and every
reply - closing it hides nothing and deletes nothing. Rewriting a comment, or any reply written here, keeps every
earlier wording under `edits`, and one already posted is flagged as having moved on from what the pull request holds
rather than silently disagreeing with it. A reply brought back from the pull request is left as its author wrote it.

Closing a comment here closes it there too, when it was posted: its thread on the pull request is resolved, tracked
apart under `prResolve` - `pending` until it is done, `done` once it is, `failed` when the attempt did not happen. A
comment closed here whose thread is still open there says so rather than reading as resolved everywhere.

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
        # A row written before the cursor existed is as old as its position says.
        row.setdefault("event", row["seq"])
        row.setdefault("eventBy", "you")
    return rows


THREADS = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 50) { nodes { databaseId body path author { login } } }
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
    """Every review thread of a pull request, or nothing and the reason it could not be read."""
    owner, _, name = repo.partition("/")
    variables = ["-F", f"owner={owner}", "-F", f"name={name}", "-F", f"number={number}"]
    done = gen_diff_data.gh("api", "graphql", "-f", f"query={THREADS}", *variables, repeatable=True)
    if done.returncode != 0:
        return None, " ".join((done.stderr or done.stdout).split())[:300]
    try:
        return json.loads(done.stdout)["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"], ""
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


def close_threads(repo, number, owed, threads):
    """Resolve at once the thread of every comment closed here, and say for each of them how that went.

    Split by what the pull request says: a thread it already holds as resolved is settled, one it holds as open is
    resolved now, and a comment matching neither is left owed. Assuming the last case settled would report a resolution
    the pull request does not have, which is the one thing this must never do.
    """
    opened, settled = {}, set()
    for thread in threads:
        said = thread["comments"]["nodes"]
        if not said:
            continue
        if thread["isResolved"]:
            settled.add(said[0]["body"])
        else:
            opened[said[0]["body"]] = thread["id"]
    outcome = {row["seq"]: ("done", "") for row in owed if row["text"] in settled}
    asking = [row for row in owed if row["seq"] not in outcome and row["text"] in opened]
    wanted = [Owing(opened[row["text"]], 0, "") for row in asking]
    for row, (went, trouble) in zip(asking, carry_out(repo, number, wanted), strict=True):
        outcome[row["seq"]] = ("done", "") if went else ("failed", trouble)
    for row in owed:
        outcome.setdefault(row["seq"], ("failed", NOWHERE))
    return outcome


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


class Owed(NamedTuple):
    """What one comment and its thread owe each other, worked out before any of it is asked of GitHub."""

    landed: list
    incoming: list
    settled: bool
    given: tuple | None
    wanted: list


class Reconciled(NamedTuple):
    """What one comment's sync came to: replies each way, how many went out, and where resolution stands."""

    landed: list
    sent: int
    incoming: list
    settled: bool
    given: tuple | None


def reconcile(row, thread, carry=True):
    """What one comment owes its thread and is owed by it, including every mutation that has to be asked for.

    Nothing is asked here: a sync collects the mutations of all its comments so it can ask for them in one request.
    `carry` withholds the replies written here; whatever the pull request holds is always brought back.
    """
    if thread is None:
        # Nothing on the pull request answers to this comment, so whatever was believed about it stands
        # uncorroborated: it is owed again rather than left claiming a resolution nobody can see.
        owed = ("failed", NOWHERE) if row.get("state") == "resolved" else None
        return Owed([], [], False, owed, [])
    said = thread["comments"]["nodes"]
    landed, sending = carry_replies(row, said) if carry else ([], [])
    settled, resolving = agree(thread, row)
    wanted = [Owing(thread["id"], said[0]["databaseId"], text) for text in sending]
    if resolving:
        wanted.append(Owing(resolving, 0, ""))
    return Owed(landed, incoming(row, said), settled, None, wanted)


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


def agree(thread, row):
    """What a sync should make of one thread: whether to close the comment here, and which thread to resolve there.

    Decided on what the pull request says rather than on what this desk recorded, which is what repairs a comment
    wrongly believed resolved there - trusting the record is how such a belief survives a sync.
    """
    if thread["isResolved"]:
        return True, ""
    if row.get("state") == "resolved":
        return False, thread["id"]
    return False, ""


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


def owes_resolution(row):
    """Whether the pull request still owes this comment a resolution.

    Read from the state rather than from the moment it was closed: a comment closed here before it was ever posted, or
    closed by an older version of this desk, owes one just the same. Only GitHub having confirmed it settles the matter.
    """
    if row.get("github") != "posted" or row.get("state") == "deleted":
        return False
    if row.get("state") != "resolved":
        return row.get("prResolve") in ("pending", "failed")
    return row.get("prResolve") != "done"


def settle(row, landed, incoming, resolved):
    """Write what a sync found onto one comment: replies that are on the pull request, replies brought back from it,
    and its resolution - the pull request being the copy others read, what it says is resolved is resolved."""
    for answer in row.get("replies", []):
        if answer["text"] in landed:
            answer["posted"] = True
    if incoming:
        row.setdefault("replies", []).extend(incoming)
    if resolved:
        row["state"] = "resolved"
        row["prResolve"] = "done"


def carry_replies(row, said):
    """The replies written here, split into those the thread already holds and those still to go out."""
    spoken = {answer["body"] for answer in said}
    landed, sending = [], []
    for answer in row.get("replies", []):
        if answer.get("posted") or answer["text"] in spoken:
            landed.append(answer["text"])
        else:
            sending.append(answer["text"])
    return landed, sending


def incoming(row, said):
    """The replies the thread holds that this desk does not, as replies of its own."""
    ours = {answer["text"] for answer in row.get("replies", [])} | {row["text"]}
    return [
        {"who": (answer.get("author") or {}).get("login") or "github", "text": answer["body"], "at": "on the PR"}
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
        if path == "/comments":
            self._record()
        elif path == "/reviewed":
            self._reviewed()
        elif path == "/scan":
            self._scan()
        elif path == "/bind":
            self._bind()
        elif path == "/edit":
            self._edit()
        elif path == "/reply":
            self._reply()
        elif path == "/resolve":
            self._resolve()
        elif path == "/drop":
            self._drop()
        elif path == "/publish":
            self._publish()
        elif path == "/close":
            self._close()
        elif path == "/sync":
            self._sync()
        else:
            self._send(404)

    def _reviewed(self):
        """Record which files have been read, and which have been unticked.

        Marks arrive as `<review> <path>` against the digest of the diff they were read at, and dropped keys as a list.
        A page that has been reading offline sends whatever it kept, which is how a browser's own copy is carried up.
        """
        order = self._body()
        with CHANGING:
            marks = read_ticks()
            marks.update(order.get("marks") or {})
            for gone in order.get("drop") or []:
                marks.pop(gone, None)
            write_ticks(marks)
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
        be reworded: a reply carried back from the pull request is somebody else's word, and rewriting it would put
        words in their mouth on the one copy everyone reads.
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
            elif index is None:
                said = found
            elif not 0 <= index < len(replies):
                refused = f"comment {found['seq']} has no reply {index}"
            elif replies[index]["who"] not in ("you", "session"):
                refused = f"reply {index} of comment {found['seq']} is {replies[index]['who']}'s word, not this desk's"
            else:
                said = replies[index]
            if said is not None:
                said.setdefault("edits", []).append({"at": time.strftime("%H:%M:%S"), "text": said["text"]})
                said["text"] = text
                # Rewriting is never carried to the pull request, so its copy is marked as having been moved on from.
                landed = said.get("posted") if index is not None else found.get("github") == "posted"
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
        """Add a reply to a comment, from whichever side wrote it. A reply leaves the thread open."""
        order = self._body()
        text = (order.get("text") or "").strip()
        if not text:
            self._json({"ok": False, "error": "an empty reply says nothing"})
            return
        who = "you" if order.get("who") == "you" else "session"
        with changing() as rows:
            found = next((row for row in rows if row["seq"] == order.get("seq")), None)
            if found is not None:
                found["replies"].append({"who": who, "text": text, "at": time.strftime("%H:%M:%S")})
                touched(rows, found, who)
        if found is None:
            self._json({"ok": False, "error": f"no comment numbered {order.get('seq')}"})
            return
        print(f"REPLY [{found['seq']}] {who}: {' '.join(text.split())}", flush=True)
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
                    # Closing a comment that reached the pull request owes a resolution there as well.
                    if row.get("github") == "posted":
                        row["prResolve"] = "pending" if closing else "none"
                    if answer:
                        row["replies"].append({"who": who, "text": answer, "at": time.strftime("%H:%M:%S")})
                    touched(rows, row, who)
                    closed += 1
        print(f"{'RESOLVED' if closing else 'REOPENED'} {closed} comment(s) by {who}", flush=True)
        self._json({"ok": True, "resolved": closed, "state": "resolved" if closing else "open"})

    def _drop(self):
        """Delete a comment, or only its last reply, here and on the pull request when it was posted.

        GitHub deletes a review comment whether or not anything answered it, and every reply is a comment of its own, so
        what this desk put there is deleted newest first. Replies written on the pull request itself are left alone, and
        a thread still holding one of them stays there with what remains.

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
        # Newest first, so the comment that opened the thread is the last to go.
        going = (
            [replies[-1]["text"]] if last_only else [answer["text"] for answer in reversed(replies)] + [found["text"]]
        )
        gone = 0
        if found.get("github") == "posted" and order.get("repo") and order.get("pr"):
            threads, trouble = review_threads(order["repo"], order["pr"])
            if threads is None:
                print(f"DROP FAILED {trouble}", flush=True)
                self._json({"ok": False, "error": trouble})
                return
            thread = next(
                (
                    node
                    for node in threads
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
        review = {"event": "COMMENT", "body": order.get("summary") or "Review from the diff desk.", "comments": []}
        for note in sending:
            if note.get("side") == "file":
                # A remark about a whole file is a review comment naming the file and no line within it.
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
        target = f"repos/{order['repo']}/pulls/{order['pr']}/reviews"
        print(f"PUBLISH {len(review['comments'])} comment(s) -> {target}", flush=True)
        done = gen_diff_data.gh(
            "api", "--method", "POST", target, "--input", "-", repeatable=False, given=json.dumps(review)
        )
        landed = done.returncode == 0
        url = json.loads(done.stdout or "{}").get("html_url", "") if landed else ""
        error = "" if landed else " ".join((done.stderr or done.stdout).split())[:400]
        refused = not landed and is_refusal(error)
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

    def _close(self):
        """Resolve on the pull request the threads of comments closed here, and record how that went.

        A thread is found by the body of the comment that opened it, which is the text this desk posted, so no
        identifier has to be kept in step with GitHub's own.
        """
        order = self._body()
        with CHANGING:
            rows = read_notes()
        owed = [row for row in rows if owes_resolution(row) and under_review(row, order)]
        if not owed:
            self._json({"ok": True, "closed": 0, "owed": 0})
            return
        wanted = {row["seq"] for row in owed}
        threads, trouble = review_threads(order["repo"], order["pr"])
        if threads is None:
            with changing() as fresh:
                for row in fresh:
                    if row["seq"] in wanted:
                        row["prResolve"] = "failed"
                        row["prResolveError"] = trouble
                        touched(fresh, row, "session")
            print(f"CLOSE FAILED {trouble}", flush=True)
            self._json({"ok": False, "error": trouble, "closed": 0, "owed": len(owed)})
            return
        outcome = close_threads(order["repo"], order["pr"], owed, threads)
        closed = len([seq for seq, (state, _) in outcome.items() if state == "done"])
        with changing() as fresh:
            for row in fresh:
                if row["seq"] in outcome:
                    row["prResolve"], trouble = outcome[row["seq"]]
                    if trouble:
                        row["prResolveError"] = trouble
                    else:
                        row.pop("prResolveError", None)
                    touched(fresh, row, "session")
            still = len([row for row in fresh if owes_resolution(row) and under_review(row, order)])
        print(f"CLOSED {closed} thread(s) on the pull request ({still} still owed)", flush=True)
        self._json({"ok": still == 0, "closed": closed, "owed": still})

    def _sync(self):
        """Bring this desk and the pull request to the same state.

        Replies go both ways, and so does resolution: a thread resolved there is closed here, since the pull request is
        the copy everyone else reads, and one closed here is resolved there. A thread is matched by the body of the
        comment that opened it, which is the text this desk posted.
        """
        order = self._body()
        threads, trouble = review_threads(order["repo"], order["pr"])
        if threads is None:
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
        for thread in threads:
            said = thread["comments"]["nodes"]
            if said:
                theirs[said[0]["body"]] = thread
        # A remark settled here withholds its replies unless asked for, but its resolution is still agreed with the pull
        # request either way: agreeing on what is resolved is the point of a sync.
        plans = {
            seq: reconcile(
                row, theirs.get(row["text"]), carry=bool(order.get("resolved")) or row.get("state") != "resolved"
            )
            for seq, row in posted.items()
        }
        # Every comment's replies and resolutions in one request, then handed back to the comment that asked for them.
        wanted, spans = [], {}
        for seq, plan in plans.items():
            spans[seq] = slice(len(wanted), len(wanted) + len(plan.wanted))
            wanted += plan.wanted
        answers = carry_out(order["repo"], order["pr"], wanted)
        found = {seq: reconciled(plan, answers[spans[seq]]) for seq, plan in plans.items()}
        sent = sum(step.sent for step in found.values())
        brought = sum(len(step.incoming) for step in found.values())
        closed = len([seq for seq, step in found.items() if step.settled and posted[seq].get("state") != "resolved"])
        away = len([step for step in found.values() if step.given and step.given[0] == "done"])
        with changing() as fresh:
            for row in fresh:
                step = found.get(row["seq"])
                if step is None:
                    continue
                settle(row, step.landed, step.incoming, step.settled)
                # A reply brought back from the pull request is somebody else's word, so it is news for this side.
                touched(fresh, row, "you" if step.incoming else "session")
                if step.given:
                    row["prResolve"], trouble = step.given
                    if trouble:
                        row["prResolveError"] = trouble
                    else:
                        row.pop("prResolveError", None)
        told = f"sent {sent} reply(ies), brought {brought} back, closed {closed} here, resolved {away} there"
        print(f"SYNC {told}", flush=True)
        self._json({"ok": True, "sent": sent, "brought": brought, "closed": closed, "resolved": away})

    def log_message(self, *args):
        pass


def main(source=None):
    Serving.source = source
    print(f"diff desk on http://127.0.0.1:{PORT}/  (comments -> {NOTES})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
