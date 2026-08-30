---
name: diff-desk
description: Serve a local git branch as a browser diff review page, collect the line comments the user leaves on it, and work through them. Use when the user wants to review a branch, diff or pull request locally, when GitHub is unreachable, or when they refer to comments they left in the review tool.
---

# Diff desk

A local review page for any git range: side-by-side line numbers, per-commit or whole-branch scope, reviewed-file tracking, range comments dragged over any lines, and an optional batch post to the branch's pull request. Needs `git`, `gh` and python3 only. The page is served on `http://127.0.0.1:8787/`; its payload, page and comments live in `~/.claude/diff-desk/` (override with `DIFF_DESK_HOME`).

## Serving a review

Run in the background, then give the user the URL:

    python3 ~/.claude/skills/diff-desk/desk.py serve --owner "$CLAUDE_CODE_SESSION_ID" --dir <repo> --base <ref> [refs ...]

Serving fast-forwards the desk itself to what has been published and restarts into it, so a review always runs the current tool. It only ever fast-forwards a clean `main`: a checkout carrying work in progress, or sitting on another branch, is left alone and says so - which is what a session sees when it is serving from a working copy being changed.

`refs` are local branches, pull request numbers (`3243`, `#3243`, `pr/3243`), or a mix of both; omit them to offer every local branch ahead of the base. `--base` defaults to `upstream/main`. The checked-out branch is shown with its uncommitted work included. A pull request is fetched from the upstream repository by number into `refs/diffdesk/pull/<number>`, so the fork and branch it lives on never have to be named. A pull request is served as it was fetched, so the work saved on disk reaches the page under the checked-out branch's own name. Re-running while a desk is already up leaves the running one in charge of what is served: a desk already holding another review is left alone and says whose it is, since serving over it takes the page away from whoever is reading that, with nothing on either side to say so. Ask them rather than replacing it; `--take` serves it anyway where that is really what is wanted. Asking for the review a desk is already showing is not a clash and rebuilds its page as before. A page already open holds the payload it was built with, and its **Refresh** collects that payload's refs again, so a session that changes what is served asks for a reload in the browser: a refresh puts the old source back. The user can also switch repository, base, branches and pull requests from the page's own Source panel, which outlasts anything a session sets.

A desk is put down with `desk.py stop`, never by killing the process: one desk holds every review it was given, so a signal takes the page away from every session reading any of them, with nothing said on either side. Asked for with nobody named it says whose review it is holding rather than going quiet under them, and `--take` stops it anyway. Serving with `--owner <label>` names whoever opened the desk, and `stop --owner <label>` puts down only a desk opened under that label, which is what lets a session clean up the desk it opened on its way out while leaving another session's running. Serving under `$CLAUDE_CODE_SESSION_ID` is what makes a desk this session's own, so the desk it opened goes down when it ends and one opened by hand, or by another session, stays up.

A page load collects the diffs again, so a reload always shows the branch as it now stands, uncommitted work included. While the page is open it watches what it was built from and offers a **Refresh** in the bar once that has moved on - a commit, a fixup, work saved on disk - rather than rebuilding the diff under the reader. Taking it keeps the branch and commit being read. So an edit made while the user is reviewing needs no command from the session; tell them to refresh.

`desk.py refs --dir <repo> --base <ref>` lists what is available: branches ahead of the base, and open pull requests.

## Picking up the comments

The user writes comments on the page and presses "Submit review", which sends the whole batch at once. To wait for one, run in the background - it blocks until a batch lands, prints it, and exits:

    python3 ~/.claude/skills/diff-desk/desk.py watch

It reports to stdout and nowhere else, so whatever runs it has to be reading that stream: started with its output sent to a file, it watches faithfully and reports to nobody, and the review sits there unanswered with nothing to say it arrived. Either keep the stream where the session reads it, or arm `--once` and read what it printed when it exits - which means running it so that its finishing is something the session is told about. Detached and forgotten about, with `nohup` or a bare `&`, nothing announces it, and the reviewer's words wait in a file nobody opens.

A thread is printed whole and the line that woke the session is marked with a `*`, so what has already been answered reads as read. A watch armed against a desk that has since been restarted says so and stops, since a restart carries whatever the tool has become: arm it again rather than reading a page that no longer exists. It keeps running and prints whatever the reviewer says, as they say it - not only the first thing, and not only new comments: a reply on a comment already read reaches it just the same, since it follows the log's event cursor rather than the comment numbers. A session's own replies and resolutions bump that cursor too and are told apart, so what it is waiting for is never confused with what it just did.

`--once` stops it after the first report, and where it stopped is remembered, so the way to be woken rather than to keep looking is to arm `watch --once` in the background, answer what it reports, and arm it again: the same reply never wakes a session twice, and nothing said while it was answering is lost. `--since N` overrides where it resumes from.

One desk holds every review it was given, so a session working one of them arms `watch --branch <ref>`, repeatable, and hears only what is said about those. It is what keeps two sessions out of each other's way: where a watch stopped is remembered per review, so one is never carried past what another has yet to hear. Armed without it, a watch hears everything the desk holds - right for a session that is the only one reading it.

Each comment prints as `[seq] branch path:line-endLine (side) text`, followed by its state and any replies, each numbered `[0]`, `[1]` ... by its place in the thread. Answer in the thread, and close what is done - the page shows both without a reload:

    python3 ~/.claude/skills/diff-desk/desk.py reply 3 "it happens because ..."
    python3 ~/.claude/skills/diff-desk/desk.py reply 3 --whisper "check the sibling call site too"
    python3 ~/.claude/skills/diff-desk/desk.py forget 3
    python3 ~/.claude/skills/diff-desk/desk.py resolve 3 4 --answer "fixed in abc1234"
    python3 ~/.claude/skills/diff-desk/desk.py resolve 3 --reopen
    python3 ~/.claude/skills/diff-desk/desk.py edit 3 "what I actually meant ..."
    python3 ~/.claude/skills/diff-desk/desk.py edit 3 --reply 0 "worded better ..."

Reply when the answer needs discussing, resolve when it is settled - a resolved thread keeps its remark and every reply, and the reviewer can reopen it. A whisper is neither: printed as `[i] whisper <who> ...` in a thread, it is written for this side of the desk - what the reviewer wants looked at, what is left to do - and it never reaches the pull request, so it is guidance to act on rather than a remark to answer. It hangs on what it is about, said as `on [j]` when that is not the remark itself, and it is answered with a whisper of its own: a reviewer's "check whether this is true, and if it is not just resolve it" is answered with `reply 3 --whisper --on 0 "done."` and the thread closed with `resolve 3`, which leaves nothing of it on the pull request. A whisper stands on the remark or on a reply, and every comment carries one way to add another about it. `forget` lets go of the last whisper on a comment, which is the only one that can go: what stands under it would be left standing against nothing. Resolving a comment that was posted to a pull request also resolves its thread there, and says "not resolved there yet" until GitHub confirms it.

Nothing said here has left the desk until the reader sends the thread, so an answer that turns out to be wrong is rewritten rather than corrected underneath: `edit <seq> --reply <i>` keeps the earlier wording and leaves the thread reading as one answer. Append a correction only to what the pull request already holds.

`desk.py sync` brings back what the pull request holds: replies added there, its word on what is resolved, and the comments written there that this desk has no record of - a reviewer's remark, a bot's report - each numbered like any other and carrying its author. Run it when the reviewer mentions having answered on GitHub, or before working through comments, so this desk holds everything they have said. `desk.py comments [--all]` lists what is outstanding.

`desk.py comments --all` prints the resolved threads as well, which is what a long review has already decided: what it settles generalises past the line it was written on, and nothing carries that forward on its own. Read them on the ground about to be touched, before touching it - the answer that closed a thread says how it was settled, and who wrote that answer says whose decision it was. A thread the reviewer answered themselves is their edit to keep, not a line to revisit. Answering either question wrongly is answering the same remark twice.

A comment brought in that way is answered and resolved like any other, and is the one kind that cannot be reworded or deleted from here: the remark is its author's, on the copy everyone reads.

Nothing a session writes reaches the pull request. `desk.py sync` only brings back what the pull request holds; sending a thread out - the remark, the replies it does not hold, its resolution - is a press on that thread's **Sync**, on the thread itself or on its row in the Comments panel, and the reader's alone. A thread goes out whole and by that one door, so `desk.py publish <seq...>` sends exactly what a press sends. The panel also sends every thread already on the pull request at once. So working through a review leaves no trace there unless they put it there, and a reply written after they sent a thread waits for them to send it again - a resolution too: closing a thread here is local until sent.

## Behaviour to know

- A comment range may cover removed and added lines together. It is anchored to the added side when the range touches it, so `side`/`line`/`endLine` are always expressible as a GitHub line range.
- The page holds the files the reader is near: a file further off keeps its header and stands at the height its lines will take, and its rows are built when they come within reach. So a query about what is in the DOM is a question about where the reader is standing, and a test reaching into a distant file scrolls to it first.
- A batch is a submission, not a grouping of comments: it says which review a remark went out in, and a reply belongs to its thread and carries no batch at all. The Comments panel therefore reads either way - by batch, or by what moved last, which is where an answer written into an old thread is found.
- Reviewed-file ticks are remembered per branch and per file digest, so a file whose diff changes reopens by itself.
- Gap expanders on each hunk header read the file at the branch revision, so context beyond the diff needs the desk running (they are hidden otherwise).
- Comments are recorded whether or not GitHub is reachable; posting to a pull request is a separate opt-in tick. Binding one for the pull request says where it belongs, never that it may leave: what has only been bound waits to be asked for, and nothing sends it on a timer. `desk.py publish <seq...>` is the one way a session sends anything, and it names what it sends. A post that does not land leaves its comments marked as still owed, with the reason kept, and they are retried from the page - so a GitHub outage never costs a comment and never needs cleaning up by hand. A comment GitHub rejects outright is marked `refused` rather than retried forever.
- A comment can be sent on its own from the box, sent alone out of the review tray, or batched into a review; the Comments panel groups comments by batch and sends them one batch at a time. All of it is recorded identically.
- Whether a comment is bound for the pull request can be changed after it was recorded, from the page or with `desk.py bind <seq...> [--local]`, for as long as it has not landed.
- A comment whose line has left the diff is kept and marked, never resolved or dropped on the reviewer's behalf.

## Changing the page

`diff_desk_template.html` holds the page, with `__DIFF_DATA__` and `__BUILD__` substituted at build time. After editing it, verify with the suite rather than by inspection - it drives the real page in Chromium, WebKit and Firefox against a desk of its own, and covers the drag, mixed ranges, gap expansion, the single-click paths, the layout under a comment, and every exchange with a stand-in for `gh`:

    cd ~/.claude/skills/diff-desk && python3 -m pytest

Pointer behaviour differs between engines, so a change to selection or hit testing is not done until it passes in all three. The header carries a build stamp; if the user reports stale behaviour, have them compare it first.

Three traps the suite exists to catch, all of which shipped broken before it did:

- An `overflow` on the file card makes the card its own scrollport, so its head never pins and the hunk delimiter is what stands at the top of the view, reading as the file's name.
- A trailing click follows every drag, aimed at the pin or at an ancestor depending on the engine, and collapses the range to one line unless it is swallowed.
- A comment hangs inside the diff table, so one left without a width of its own fills the table and moves every column under it - which the page redrawing itself turns into a flicker.
