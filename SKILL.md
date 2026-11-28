---
name: diff-desk
description: Serve a local git branch as a browser diff review page, collect the line comments the user leaves on it, and work through them. Use when the user wants to review a branch, diff or pull request locally, when GitHub is unreachable, or when they refer to comments they left in the review tool.
---

# Diff desk

A local review page for any git range: side-by-side line numbers, per-commit or whole-branch scope, reviewed-file tracking, range comments dragged over any lines, and an optional batch post to the branch's pull request. Needs `git`, `gh` and python3 only. The page is served on `http://127.0.0.1:8787/`; its payload, page and comments live in `~/.claude/diff-desk/` (override with `DIFF_DESK_HOME`). Every command below is `desk.py` in the directory holding this file.

## Who you are

The desk is shared: the reader at the page, and any number of agents, each reviewing, answering and resolving with the same standing as every other. What an agent writes is recorded under the name it gives, so name yourself on every command, with `--as <name>` before the command or `DIFF_DESK_AGENT=<name>` in its environment:

    python3 desk.py --as <name> reply 3 "it happens because ..."

Pick a name that stays the same for as long as you work, and that no other agent on the desk uses: what you write reads as yours on the page, and your watch wakes on what everyone else says, the reader and the other agents alike. `you`, `github` and `desk` stand for someone else, and the desk refuses a write that names nobody, rather than recording it as the reader's. Reactions are the reader's alone, since they go out under the reader's GitHub account.

The desk answers on `127.0.0.1` only. An agent running in a sandbox needs that address reachable, which a sandbox cutting the network off also cuts.

## Serving a review

Run in the background, then give the user the URL:

    python3 desk.py serve --owner <your session> --dir <repo> --base <ref> [refs ...]

`--owner` takes whatever identifies your session, the session id where your harness gives one. Run it as a background job of the session, the way any other long-running command is run, and never detached with `nohup`, `disown` or `setsid`. A desk inside the session's process tree goes down when the session ends, as every command it ran does; one put outside that tree outlives it, and the next agent finds a page serving a review nobody is reading, on code that has since been superseded.

Serving fast-forwards the desk itself to what has been published and restarts into it, so a review always runs the current tool. It only ever fast-forwards a clean `main`: a checkout carrying work in progress, or sitting on another branch, is left alone and says so - which is what an agent sees when it is serving from a working copy being changed.

`refs` are local branches, pull request numbers (`3243`, `#3243`, `pr/3243`), or a mix of both; omit them to offer every local branch ahead of the base. `--base` defaults to `upstream/main`. A ref written `base..ref` is read against a base of its own rather than `--base`, which is how a stack is reviewed one increment at a time, each branch against the one below it: `first first..second second..third`. The Source panel sets the same base per branch, and a tab names its base when hovered. `--stack <tip>` serves a whole stack written that way, read from the pull requests below the one opened from `<tip>` on `origin` or `upstream`, each branch as its pushed head (`--local` reads the local branches instead); with no pull request, the stack is the local branches the tip builds on. The bar draws a stack as one chain of tabs, and marks a branch that does not build on the one below it, since it is read from where they forked and needs restacking. The checked-out branch is shown with its uncommitted work included. A pull request is fetched from the upstream repository by number into `refs/diffdesk/pull/<number>`, so the fork and branch it lives on never have to be named. A pull request is served as it was fetched, so the work saved on disk reaches the page under the checked-out branch's own name. A local branch carries the pull request opened from where it is pushed (its upstream), whatever it is called locally, so a branch pushed with `git push -u origin HEAD:<name>` under another name keeps both its work on disk and its pull request; without an upstream, it carries the one opened from a branch of its own name. Re-running while a desk is already up leaves the running one in charge of what is served: a desk already holding another review is left alone and says whose it is, since serving over it takes the page away from whoever is reading that, with nothing on either side to say so. Ask them rather than replacing it; `--take` serves it anyway where that is really what is wanted. Asking for the review a desk is already showing is not a clash and rebuilds its page as before. A page already open holds the payload it was built with, and its **Refresh** collects that payload's refs again, so an agent that changes what is served asks for a reload in the browser: a refresh puts the old source back. The user can also switch repository, base, branches and pull requests from the page's own Source panel, which outlasts anything an agent sets.

A desk is put down with `desk.py stop`, never by killing the process: one desk holds every review it was given, so a signal takes the page away from every agent reading any of them, with nothing said on either side. Asked for with nobody named it says whose review it is holding rather than going quiet under them, and `--take` stops it anyway. Serving with `--owner <label>` names whoever opened the desk, and `stop --owner <label>` puts down only a desk opened under that label, which is what lets an agent clean up the desk it opened on its way out while leaving another agent's running.

A page load shows the page as it was last built at once, then collects the diffs again behind a progress bar under the top bar, so a reload always lands on the branch as it now stands, uncommitted work included. Commits are read once per desk, so collecting again costs about what the uncommitted work does. The diffs are read from git alone; what GitHub adds to them (pull request links, the reader's login) is read from what it last answered and asked again in the background, so neither a page load nor a refresh ever waits on GitHub, with the network gone or not. An answer landing later reaches the open page on its own, with the diffs left as they are. While the page is open it watches what it was built from and offers a **Refresh** in the bar once that has moved on - a commit, a fixup, work saved on disk - rather than rebuilding the diff under the reader. Taking it keeps the branch and commit being read. So an edit made while the user is reviewing needs no command from the agent; tell them to refresh.

`desk.py refs --dir <repo> --base <ref>` lists what is available: branches ahead of the base, and open pull requests.

## Picking up the comments

The user writes comments on the page and presses "Submit review", which sends the whole batch at once, and other agents write on the same desk. To wait for any of it, run in the background - it blocks until someone else says something, prints it, and keeps watching:

    python3 desk.py --as <name> watch

It reports to stdout and nowhere else, so whatever runs it has to be reading that stream: started with its output sent to a file, it watches faithfully and reports to nobody, and the review sits there unanswered with nothing to say it arrived. Either keep the stream where you read it, or arm `--once` and read what it printed when it exits - which means running it so that its finishing is something you are told about. An agent with no way to be told about a background command runs `watch --once --timeout <seconds>` in the foreground, as a blocking call that returns the moment something is said, and arms it again after answering. Detached and forgotten about, with `nohup` or a bare `&`, nothing announces it, and the reviewer's words wait in a file nobody opens.

The desk holds the watch's request open until there is news, so a word wakes it as it is written, and the page says who is watching while it waits. A thread is printed whole and the line that woke the watch is marked with a `*`, so what has already been answered reads as read. A watch armed after the desk restarted says so once and then follows the desk now serving, so a restart costs no arming cycle and nothing said meanwhile is missed. It keeps running and prints whatever anyone else says, as they say it - not only the first thing, and not only new comments: a reply on a comment already read reaches it just the same, since it follows the log's event cursor rather than the comment numbers. Your own replies and resolutions bump that cursor too and are told apart, so what you are waiting for is never confused with what you just did.

Armed without `--once` it never stops of its own accord: every batch reaches you as it lands, with nothing to arm again and no moment at which forgetting to is silent. `--once` stops it after the first report, for an agent that would rather be woken once and decide what to do next. The desk remembers where each agent stopped, so the same reply never wakes you twice, and nothing said while you were answering is lost. `--since N` overrides where it resumes from.

One desk holds every review it was given, so an agent working one of them arms `watch --branch <ref>`, repeatable, and hears only what is said about those. Where a watch stopped is remembered per agent and per review, so no agent is carried past what another has yet to hear. Armed without it, a watch hears everything the desk holds - right for an agent that is the only one reading it.

Each comment prints as `[seq] branch path:line-endLine (side) text`, followed by its state, where to open every file it carries, and any replies, each numbered `[0]`, `[1]` ... by its place in the thread. A file is worth opening: the reviewer attached it instead of describing what they saw. Answer in the thread, and close what is done - the page shows both without a reload. The reviewer reads the page, not your terminal: an answer written as a message of yours reaches nobody, so a remark that came from the desk is answered with `reply`, every time, and you say only in a line that you did:

    python3 desk.py --as <name> reply 3 "it happens because ..."
    python3 desk.py --as <name> reply 3 --whisper "check the sibling call site too"
    python3 desk.py --as <name> forget 3
    python3 desk.py --as <name> resolve 3 4 --answer "fixed in abc1234"
    python3 desk.py --as <name> resolve 3 --reopen
    python3 desk.py --as <name> edit 3 "what I actually meant ..."
    python3 desk.py --as <name> edit 3 --reply 0 "worded better ..."

An agent that reviews opens its own threads with `remark`, naming the review as its ref or its pull request and the lines as the diff numbers them, `--old` for removed lines. The thread reads under your name on the page, wakes the watches of everyone else, and reaches the pull request only through a **Sync**, under the reader's login, as any remark written here does.

    python3 desk.py --as <name> remark '#3243' genesis/utils/misc.py:12-14 "this copies the array twice"

Write every remark and reply in markdown: it is strongly recommended, since the page lays it out the way the pull request does. Put identifiers, paths, values and commands in backticks, code in fenced blocks, several points in a list, and keep the decision a reader is asked for in its own paragraph rather than buried in prose. Refer to another thread as `[N]`, its number in brackets: the page makes it a link to that thread and lists the mention on the thread named, so related threads read as one discussion. Sent to the pull request, it goes out as a link reading "this thread", since the number means nothing there, so write it where those words read as well ("as said in [N]"). It is linked once the pull request holds that thread; a thread this desk alone holds, or one going out in the same send, keeps its number as written. Outside code, a number in brackets always names a thread, so write anything else another way.

Reply when the answer needs discussing, resolve when it is settled - a resolved thread keeps its remark and every reply, and the reviewer can reopen it. A whisper is neither: printed as `[i] whisper <who> ...` in a thread, it is written for this side of the desk - what the reviewer or another agent wants looked at, what is left to do - and it never reaches the pull request, so it is guidance to weigh rather than a remark to answer. It hangs on what it is about, said as `on [j]` when that is not the remark itself, and it is answered with a whisper of its own: a reviewer's "check whether this is true, and if it is not just resolve it" is answered with `reply 3 --whisper --on 0 "done."` and the thread closed with `resolve 3`, which leaves nothing of it on the pull request. A whisper stands on the remark or on a reply, and every comment carries one way to add another about it. `forget` lets go of the last whisper standing on one thing, the remark or a reply, whatever was said to the thread after it; one with a later whisper beside it stays, since what hangs under it would be left hanging from nothing. Asked with only the comment named, it lets go of the thread's last whisper. Resolving a comment that was posted to a pull request also resolves its thread there, and says "not resolved there yet" until GitHub confirms it.

Nothing said here has left the desk until the reader sends the thread, so an answer that turns out to be wrong is rewritten rather than corrected underneath: `edit <seq> --reply <i>` keeps the earlier wording and leaves the thread reading as one answer. Append a correction only to what the pull request already holds.

`desk.py sync` brings back what the pull request holds: replies added there, its word on what is resolved, and the comments written there that this desk has no record of - a reviewer's remark, a bot's report - each numbered like any other and carrying its author. Run it when the reviewer mentions having answered on GitHub, or before working through comments, so this desk holds everything they have said. `desk.py comments [--all]` lists what is outstanding.

`desk.py comments` prints the emoji reactions a remark or a reply carries, as `reactions thumbs_up 1 (you)`. A reaction does not wake a watch, so read them when listing comments. A reviewer's thumbs-up on an answer accepts it and asks for nothing more; their `confused` or `eyes` is worth a look at that answer.

`desk.py comments --all` prints the resolved threads as well, which is what a long review has already decided: what it settles generalises past the line it was written on, and nothing carries that forward on its own. Read them on the ground about to be touched, before touching it - the answer that closed a thread says how it was settled, and who wrote that answer says whose decision it was. A thread the reviewer answered themselves is their edit to keep, not a line to revisit. Answering either question wrongly is answering the same remark twice.

A comment brought in that way is answered and resolved like any other, and is the one kind that cannot be reworded or deleted from here: the remark is its author's, on the copy everyone reads.

Nothing an agent writes reaches the pull request on its own. `desk.py sync` only brings back what the pull request holds; sending a thread out - the remark, the replies it does not hold, its resolution - is a press on that thread's **Sync**, on the thread itself or on its row in the Comments panel, and the reader's alone. A thread goes out whole and by that one door, so `desk.py publish <seq...>` sends exactly what a press sends. The panel also sends every thread already on the pull request at once. So working through a review leaves no trace there unless they put it there, and a reply written after they sent a thread waits for them to send it again - a resolution too: closing a thread here is local until sent.

## Talking outside the diff

The chat is where the reader and the agents talk about what no single line holds: an investigation, a measurement, a decision. It is a column beside the diff on the page, and a pair of commands here. It is organised in threads by topic, each opened under a title:

    python3 desk.py --as <name> chat                               # the threads, newest last
    python3 desk.py --as <name> chat 3                             # one thread whole, as plain markdown
    python3 desk.py --as <name> say 3 "the jump comes from ..."    # say something in thread 3
    python3 desk.py --as <name> say --new "Contact jitter" "..."   # open a thread
    python3 desk.py --as <name> say 3 "..." --on 12                # answering message #12, read under it
    python3 desk.py --as <name> say 3 "Benchmark" --report out/report.html   # pin a report: an HTML page, a PDF, a plot, a web address
    python3 desk.py --as <name> say --pin 12                       # pin message #12 over its thread (--unpin takes it off)

A report is one artifact kept up to date: write the file again where it is, and the page marks it as changed, named by its own title (an HTML page's `<title>`, a markdown file's first heading). Pin it once. Pinning the same file again in the thread moves its one pin to the new message rather than adding another.
    python3 desk.py --as <name> say 3 "..." --attach plot.png      # with an image or a video; any other file by its absolute path

Write it in markdown, as comments are: tables for measurements, `[N]` to name a thread of the review, `` `path` `` or `` `path:line` `` in backticks to name a file or a line of the diff (the page makes it a link there), `#N` to name message N of the chat (every message carries its number, printed by `watch` and `chat N`), and `@name` to call on an agent. Links are shown under the message on the page: an image, a video or a PDF where it stands, whether on the web or on disk by its absolute path, a markdown, text or HTML file on disk by its title and first words, a GitHub pull request, issue or commit by its title, and any other page by what it says of itself, so a link to a plot or a report is worth writing in full. Name every file by its absolute path, each time, a table of results included: a name alone, read against a folder given earlier in the message, is neither a link nor shown. A watch hears the chat as it hears the comments, printed as `chat [N] <title>` with the message under it. A message wakes you when it opens a thread, when you have already spoken in its thread, or when it names you as `@<name>`, so two agents talking leave the others asleep. Answer in the chat what was asked in the chat. `chat <N>` prints a thread as plain markdown, which is what to keep of a conversation once it settles something, or to read back when picking one up again.

## Behaviour to know

- A comment range may cover removed and added lines together. It is anchored to the added side when the range touches it, so `side`/`line`/`endLine` are always expressible as a GitHub line range.
- The page holds the files the reader is near: a file further off keeps its header and stands at the height its lines will take, and its rows are built when they come within reach. So a query about what is in the DOM is a question about where the reader is standing, and a test reaching into a distant file scrolls to it first.
- A batch is a submission, not a grouping of comments: it says which review a remark went out in, and a reply belongs to its thread and carries no batch at all. The Comments panel therefore reads either way - by batch, or by what moved last, which is where an answer written into an old thread is found.
- A ref's diff starts at its merge base with `--base`, not at `--base` itself, so serving a ref that does not descend from the base shows everything since their fork. To review a reworked branch as an increment over another version of it, such as the pushed head of a pull request replayed onto a newer base, serve a commit whose parent is that version, and check the header totals against `git diff --shortstat <base> <ref>` before handing the page over.
- Reviewed-file ticks are remembered per branch and per file digest, so a file whose diff changes reopens by itself.
- Gap expanders on each hunk header read the file at the branch revision, so context beyond the diff needs the desk running (they are hidden otherwise).
- Comments are recorded whether or not GitHub is reachable; posting to a pull request is a separate opt-in tick. Binding one for the pull request says where it belongs, never that it may leave: what has only been bound waits to be asked for, and nothing sends it on a timer. `desk.py publish <seq...>` is the one way an agent sends anything, and it names what it sends. A post that does not land leaves its comments marked as still owed, with the reason kept, and they are retried from the page - so a GitHub outage never costs a comment and never needs cleaning up by hand. A comment GitHub rejects outright is marked `refused` rather than retried forever.
- A comment can be sent on its own from the box, sent alone out of the review tray, or batched into a review; the Comments panel groups comments by batch and sends them one batch at a time. All of it is recorded identically.
- Whether a comment is bound for the pull request can be changed after it was recorded, from the page or with `desk.py bind <seq...> [--local]`, for as long as it has not landed.
- A comment carries files as well as words - an image or a video pasted, dropped or picked into any box the reviewer writes in: `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.svg`, `.mp4`, `.mov`, `.webm`, which is what a token can upload and therefore all a box takes. A reviewer reaching for a PDF, a spreadsheet, a markdown file or a dump is told so as they offer it; those reach a pull request only through a browser, so nothing here holds one. They are kept beside the log, printed as paths by `desk.py comments`, and reach the pull request only when the reader sends that thread, uploaded then and linked under the words. A comment kept local keeps its files on the desk, as its text stays there.
- A comment whose line has left the diff is kept and marked, never resolved or dropped on the reviewer's behalf.

## Changing the page

`diff_desk_template.html` holds the page, with `__DIFF_DATA__` and `__BUILD__` substituted at build time. After editing it, verify with the suite rather than by inspection - it drives the real page in Chromium, WebKit and Firefox against a desk of its own, and covers the drag, mixed ranges, gap expansion, the single-click paths, the layout under a comment, and every exchange with a stand-in for `gh`:

    python3 -m pytest

Pointer behaviour differs between engines, so a change to selection or hit testing is not done until it passes in all three. The header carries a build stamp; if the user reports stale behaviour, have them compare it first.

Three traps the suite exists to catch, all of which shipped broken before it did:

- An `overflow` on the file card makes the card its own scrollport, so its head never pins and the hunk delimiter is what stands at the top of the view, reading as the file's name.
- A trailing click follows every drag, aimed at the pin or at an ancestor depending on the engine, and collapses the range to one line unless it is swallowed.
- A comment hangs inside the diff table, so one left without a width of its own fills the table and moves every column under it - which the page redrawing itself turns into a flicker.
