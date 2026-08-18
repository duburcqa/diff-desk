"""One entry point for the diff desk: serve a branch for review, then pick up the comments it collects.

desk.py serve --dir <repo> --base <ref> [refs ...]   collect the diffs and serve them (blocks)
desk.py watch [--since N] [--once]                   print whatever the reviewer says, as they say it
desk.py comments [--all]                             what has been submitted, unresolved unless --all
desk.py reply 3 "why it happens ..."                 answer a comment without closing it
desk.py edit 3 "what I actually meant ..."            rewrite a comment, keeping what it said before
desk.py bind 3 4 [--local]                           aim comments at the pull request, or keep them local
desk.py sync                                         carry replies both ways with the pull request
desk.py resolve 3 4 --answer "fixed in abc1234"      answer and close; --reopen puts them back
desk.py refs --dir <repo> --base <ref>               the branches ahead of a base, and the open pull requests
"""

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import gen_diff_data
import serve_diff

HERE = pathlib.Path(__file__).parent
URL = f"http://127.0.0.1:{serve_diff.PORT}"


def refresh():
    """Fast-forward this desk to what has been published, and run that instead of what is already loaded.

    Only ever a fast-forward of a clean `main`: the checkout is the user's, so work in progress and any other branch are
    left exactly as they are, with a word about why nothing was taken. Whatever came in is running code, so the process
    hands over to it - and never asks a second time, since a desk relaunched by its own update would fetch for ever.
    """
    if os.environ.get("DIFF_DESK_UPDATED"):
        return
    os.environ["DIFF_DESK_UPDATED"] = "1"
    here = pathlib.Path(__file__).resolve().parent

    def git(*words, quiet=False):
        done = subprocess.run(
            ["git", "-C", str(here), *words], capture_output=True, text=True, timeout=120, check=False
        )
        if done.returncode != 0 and not quiet:
            print(f"not updated: git {words[0]} said {' '.join((done.stderr or done.stdout).split())[:120]}")
        return done.returncode == 0, done.stdout.strip()

    inside, _ = git("rev-parse", "--git-dir", quiet=True)
    if not inside:
        return
    _, branch = git("rev-parse", "--abbrev-ref", "HEAD", quiet=True)
    _, dirty = git("status", "--porcelain", quiet=True)
    if branch != "main" or dirty:
        print(f"not updated: {here} is on {branch or 'a detached head'}{' with uncommitted work' if dirty else ''}")
        return
    fetched, _ = git("fetch", "--quiet", "origin", "main")
    if not fetched:
        return
    _, before = git("rev-parse", "HEAD", quiet=True)
    moved, _ = git("merge", "--ff-only", "--quiet", "origin/main")
    if not moved:
        return
    _, after = git("rev-parse", "HEAD", quiet=True)
    if after == before:
        return
    print(f"updated to {after[:9]}, restarting")
    os.execv(sys.executable, [sys.executable, *sys.argv])


def ask(route, payload=None):
    """One request to the running desk, or None when nothing is serving."""
    request = urllib.request.Request(
        f"{URL}{route}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as answer:
            return json.loads(answer.read() or b"null")
    except (urllib.error.URLError, OSError):
        return None


def span(note):
    if note.get("side") == "file":
        return "the file"
    end = note.get("endLine")
    return f"{note['line']}-{end}" if end and end != note["line"] else str(note["line"])


def show(note):
    text = " ".join(str(note.get("text", "")).split())
    marks = [note.get("state", "open")]
    if note.get("github", "none") != "none":
        marks.append(f"github {note['github']}")
    if note.get("error"):
        marks.append(f"error {note['error'][:60]}")
    # Flushed as it goes: a watch that keeps running holds its output in a buffer otherwise, and a session reading
    # along sees a heading with nothing under it.
    print(
        f"[{note['seq']}] {note.get('branch', '?')} {note['path']}:{span(note)} ({note.get('side')}) {text}", flush=True
    )
    print(f"      {' | '.join(marks)}", flush=True)
    for answer in note.get("replies") or []:
        print(f"      {answer['who']} {answer['at']}: {' '.join(answer['text'].split())}", flush=True)
    for earlier in note.get("edits") or []:
        print(f"      was {earlier['at']}: {' '.join(earlier['text'].split())[:80]}", flush=True)


def serve(args):
    refresh()
    payload = gen_diff_data.collect(args.dir, args.base, args.refs)
    if not payload["branches"]:
        sys.exit(f"nothing ahead of {args.base} in {args.dir}")
    home = gen_diff_data.home()
    (home / "diff_data.json").write_text(json.dumps(payload, separators=(",", ":")))
    template = (HERE / "diff_desk_template.html").read_text()
    (home / "diff_desk.html").write_text(gen_diff_data.render_page(template, payload))
    files = sum(len(entry["files"]) for entry in payload["branches"])
    print(f"{files} file diffs across {len(payload['branches'])} branch(es), base {payload['base']}")
    for entry in payload["branches"]:
        request = entry["pr"]
        named = f" -> PR #{request['number']} {request['title']}" if request else ""
        print(f"  {entry['ref']}: {len(entry['files'])} files{named}")
    if ask("/data") is not None:
        print(f"already serving, page rebuilt: {URL}")
        return
    serve_diff.main(serve_diff.Source(args.dir, args.base, args.refs))


def watch(args):
    """Block until the reviewer says something, then print it. This is how a session picks up a review.

    What it follows is the log's event cursor rather than the comment numbers, so a reply on a comment the session has
    already read wakes it just as a new comment does - and the reviewer's answer to a question is not lost because the
    comment it hangs on is old news.
    """
    if ask("/data") is None:
        sys.exit("nothing is serving; start with 'desk.py serve' first")
    sent = ask("/comments") or []
    # Anything still open is unaddressed, whenever it arrived, so watching starts before it rather than after: a cursor
    # set to the end swallows whatever was written while the last batch was being worked on.
    waiting = [row.get("event", row["seq"]) for row in sent if row.get("state") == "open"]
    if args.since is not None:
        since = args.since
    elif waiting:
        since = min(waiting) - 1
    else:
        since = max((row.get("event", row["seq"]) for row in sent), default=0)
    print(f"watching for anything said past event {since}", flush=True)
    deadline = time.monotonic() + args.timeout if args.timeout else None
    # Never returns of its own accord: a reviewer says one thing, then another, and a watch that stopped at the first
    # left every word after it unheard - which is what happened before it kept going.
    while deadline is None or time.monotonic() < deadline:
        # The session's own writes bump the cursor too, so what it is waiting for is told from what it just did.
        fresh = [row for row in (ask(f"/comments?event={since}") or []) if row.get("eventBy") == "you"]
        if fresh:
            print(f"{len(fresh)} comment(s) with news:", flush=True)
            for note in fresh:
                show(note)
            since = max(row.get("event", row["seq"]) for row in fresh)
            if args.once:
                return
        time.sleep(args.every)
    print("nothing said within the timeout")


def comments(args):
    rows = ask("/comments")
    if rows is None:
        sys.exit("nothing is serving")
    rows = rows if args.all else [row for row in rows if row.get("state") == "open"]
    print(f"{len(rows)} comment(s){'' if args.all else ' unresolved'}")
    for note in rows:
        show(note)


def sync(args):
    data = ask("/data")
    if data is None:
        sys.exit("nothing is serving")
    branch = next((entry for entry in data["branches"] if entry["pr"]), None)
    if branch is None:
        sys.exit("no branch under review has a pull request")
    outcome = ask("/sync", {"repo": data["upstream"], "pr": branch["pr"]["number"]})
    if not outcome.get("ok"):
        sys.exit(outcome.get("error", "the sync was refused"))
    print(
        f"sent {outcome['sent']} reply(ies), brought {outcome['brought']} back, "
        f"closed {outcome['closed']} here, resolved {outcome['resolved']} there"
    )


def bind(args):
    outcome = ask("/bind", {"seq": args.seq, "github": not args.local})
    if outcome is None:
        sys.exit("nothing is serving")
    print(f"{outcome['bound']} comment(s) now {'local only' if args.local else 'bound for the pull request'}")


def edit(args):
    outcome = ask("/edit", {"seq": args.seq, "text": " ".join(args.text)})
    if outcome is None:
        sys.exit("nothing is serving")
    if not outcome.get("ok"):
        sys.exit(outcome.get("error", "the edit was refused"))
    print(f"rewrote [{outcome['seq']}], {outcome['edits']} earlier wording(s) kept")


def reply(args):
    outcome = ask("/reply", {"seq": args.seq, "text": " ".join(args.text), "who": "session"})
    if outcome is None:
        sys.exit("nothing is serving")
    if not outcome.get("ok"):
        sys.exit(outcome.get("error", "the reply was refused"))
    print(f"replied to [{outcome['seq']}], now {outcome['replies']} reply(ies) on it")


def resolve(args):
    outcome = ask("/resolve", {"seq": args.seq, "answer": args.answer, "resolved": not args.reopen, "who": "session"})
    if outcome is None:
        sys.exit("nothing is serving")
    print(f"{'reopened' if args.reopen else 'closed'} {outcome['resolved']} comment(s)")


def refs(args):
    where = f"/refs?dir={urllib.parse.quote(args.dir)}&base={urllib.parse.quote(args.base)}"
    info = ask(where) or {
        "current": gen_diff_data.run(args.dir, "rev-parse", "--abbrev-ref", "HEAD").strip(),
        "upstream": gen_diff_data.canonical_repo(args.dir),
        "refs": gen_diff_data.ahead_refs(args.dir, args.base),
    }
    print(f"{args.dir} on {info['current']}, upstream {info['upstream'] or '(none)'}")
    for row in info["refs"]:
        print(f"  {row['ref']}  {row['ahead']} commit(s) ahead of {args.base}")
    for row in info.get("pulls") or []:
        print(f"  #{row['number']}  {row['title']}")


parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
jobs = parser.add_subparsers(dest="job", required=True)

job = jobs.add_parser("serve", help="collect the diffs and serve the review page")
job.add_argument(
    "refs",
    nargs="*",
    help="branches to review, or pull requests as numbers (3243, #3243, pr/3243); every branch ahead of the base "
    "when omitted",
)
job.add_argument("--dir", default=".", help="the repository to read")
job.add_argument("--base", default="upstream/main", help="the ref to diff against")
job.set_defaults(run=serve)

job = jobs.add_parser("watch", help="block until a review batch is submitted")
job.add_argument("--since", type=int, default=None, help="event to resume from; the oldest open comment by default")
job.add_argument("--once", action="store_true", help="stop after the first thing said, rather than keeping watch")
job.add_argument("--every", type=float, default=10.0, help="seconds between polls")
job.add_argument("--timeout", type=float, default=0.0, help="give up after this many seconds; 0 waits forever")
job.set_defaults(run=watch)

job = jobs.add_parser("comments", help="what has been submitted")
job.add_argument("--all", action="store_true", help="include the ones already addressed")
job.set_defaults(run=comments)

job = jobs.add_parser("sync", help="carry replies both ways with the pull request")
job.set_defaults(run=sync)

job = jobs.add_parser("bind", help="aim comments at the pull request, or keep them local")
job.add_argument("seq", nargs="+", type=int)
job.add_argument("--local", action="store_true", help="keep them out of the pull request instead")
job.set_defaults(run=bind)

job = jobs.add_parser("edit", help="rewrite a comment, keeping what it said before")
job.add_argument("seq", type=int)
job.add_argument("text", nargs="+")
job.set_defaults(run=edit)

job = jobs.add_parser("reply", help="answer a comment without closing it")
job.add_argument("seq", type=int)
job.add_argument("text", nargs="+", help="the reply, shown under the comment on the page")
job.set_defaults(run=reply)

job = jobs.add_parser("resolve", help="answer and close comments, or reopen them")
job.add_argument("seq", nargs="+", type=int)
job.add_argument("--answer", default="", help="the closing reply, shown under the comment on the page")
job.add_argument("--reopen", action="store_true", help="put the comments back to open instead")
job.set_defaults(run=resolve)

job = jobs.add_parser("refs", help="the branches ahead of a base, and the open pull requests")
job.add_argument("--dir", default=".")
job.add_argument("--base", default="upstream/main")
job.set_defaults(run=refs)

if __name__ == "__main__":
    known = parser.parse_args()
    known.run(known)
