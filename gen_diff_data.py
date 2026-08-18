"""Collect a git range as reviewable data: whole branch and per commit, for any repository and any base."""

import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time


def github():
    """The command that talks to GitHub. Overridable so a test can answer for it without a network or a login."""
    return shlex.split(os.environ.get("DIFF_DESK_GH", "gh"))


def home():
    """Where the desk keeps its payload, its page and the comments it has collected."""
    where = pathlib.Path(os.environ.get("DIFF_DESK_HOME", pathlib.Path.home() / ".claude" / "diff-desk"))
    where.mkdir(parents=True, exist_ok=True)
    return where


def run(root, *args):
    """Ask git something. A command that fails answers with nothing, which every caller reads as absence."""
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=root, check=False).stdout


def canonical_repo(root):
    """The slug pull requests live under, followed through renames so the API does not answer with a redirect."""
    for remote in ("upstream", "origin"):
        url = run(root, "remote", "get-url", remote).strip()
        if not url:
            continue
        match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
        if not match:
            continue
        slug = subprocess.run(
            [*github(), "api", f"repos/{match.group(1)}", "--jq", ".full_name"],
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        ).stdout.strip()
        return slug or match.group(1)
    return ""


PULL = re.compile(r"^(?:#|pr[/-])?(\d+)$", re.IGNORECASE)


def pull_number(ref):
    """The pull request a ref names, when it names one rather than a branch."""
    match = PULL.match(str(ref).strip())
    return int(match.group(1)) if match else None


def fetch_pull(root, upstream, number):
    """Bring a pull request's head into a local ref, and describe it.

    Fetched by number through the upstream repository, so neither the fork it lives on nor the branch name it uses has
    to be known, and a head force-pushed since the last look is picked up.

    A head fetched earlier stays reviewable while GitHub is unreachable: whatever cannot be read or fetched falls back
    to what is already on disk, and only a pull request with nothing local behind it is refused.
    """
    local = f"refs/diffdesk/pull/{number}"
    held = run(root, "rev-parse", "--verify", "--quiet", local).strip()
    fetched = {"number": number, "title": f"#{number}", "url": "", "headRefName": local}
    if not upstream:
        if held:
            return local, fetched
        raise RuntimeError(f"#{number} needs a GitHub remote to resolve against, and this repository has none")
    wanted = "number,title,url,headRefName,baseRefName"
    seen = subprocess.run(
        [*github(), "pr", "view", str(number), "--repo", upstream, "--json", wanted],
        capture_output=True,
        text=True,
        cwd=root,
        timeout=60,
        check=False,
    )
    read = None
    if seen.returncode == 0:
        try:
            read = json.loads(seen.stdout)
        except json.JSONDecodeError:
            # An answer that is not one is no answer: treated as a failed read rather than crashing the collection.
            read = None
    if read is not None:
        request = read
    elif held:
        print(f"#{number} could not be read from {upstream}; showing the head fetched earlier", flush=True)
        request = fetched
    else:
        told = " ".join((seen.stderr or seen.stdout or "no answer").split())[:200]
        raise RuntimeError(f"#{number} could not be read from {upstream}: {told}")
    brought = subprocess.run(
        ["git", "fetch", "--quiet", f"https://github.com/{upstream}.git", f"+refs/pull/{number}/head:{local}"],
        capture_output=True,
        text=True,
        cwd=root,
        timeout=300,
        check=False,
    )
    if brought.returncode != 0:
        if not held:
            raise RuntimeError(f"#{number} could not be fetched: {' '.join(brought.stderr.split())[:200]}")
        print(f"#{number} could not be fetched; showing the head fetched earlier", flush=True)
    return local, request


def render_page(template, payload):
    """The page as served: the payload inlined, stamped with the moment it was built so a stale tab is obvious."""
    body = json.dumps(payload, separators=(",", ":")).replace("</script", "<\\/script")
    stamp = time.strftime("built %H:%M:%S")
    return template.replace("__DIFF_DATA__", body).replace("__BUILD__", stamp)


def parse(diff):
    files = []
    current = None
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.*) b/(.*)", line)
            path = match.group(2) if match else line
            current = {"path": path, "added": 0, "removed": 0, "lines": [], "binary": False}
            files.append(current)
            continue
        if current is None:
            continue
        if line.startswith("Binary files"):
            current["binary"] = True
            continue
        if line.startswith(("index ", "--- ", "+++ ", "old mode", "new mode", "similarity index", "rename ")):
            continue
        if line.startswith("new file mode"):
            current["state"] = "added"
            continue
        if line.startswith("deleted file mode"):
            current["state"] = "deleted"
            continue
        if line.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)", line)
            old_no = int(match.group(1)) if match else 0
            new_no = int(match.group(2)) if match else 0
            current["lines"].append(["h", old_no, new_no, match.group(3).strip() if match else line])
            current["_old"], current["_new"] = old_no, new_no
            continue
        kind = {"+": "a", "-": "d"}.get(line[:1], "c")
        if kind == "a":
            current["lines"].append(["a", 0, current.get("_new", 0), line[1:]])
            current["_new"] = current.get("_new", 0) + 1
            current["added"] += 1
        elif kind == "d":
            current["lines"].append(["d", current.get("_old", 0), 0, line[1:]])
            current["_old"] = current.get("_old", 0) + 1
            current["removed"] += 1
        else:
            current["lines"].append(["c", current.get("_old", 0), current.get("_new", 0), line[1:]])
            current["_old"] = current.get("_old", 0) + 1
            current["_new"] = current.get("_new", 0) + 1
    for entry in files:
        entry.pop("_old", None)
        entry.pop("_new", None)
        # A digest of the hunks, so a page can tell a file it already reviewed from one that moved under it.
        body = "\n".join("".join(str(part) for part in line) for line in entry["lines"])
        entry["digest"] = hashlib.sha1(body.encode()).hexdigest()[:12]
    return files


def pull_requests(root, upstream):
    """Every open pull request, keyed by the ref it is opened from. One listing, matched locally: asking per ref costs
    a request each and a single hiccup silently drops that branch's link."""
    out = subprocess.run(
        [
            *github(),
            "pr",
            "list",
            "--repo",
            upstream,
            "--state",
            "open",
            "--limit",
            "200",
            "--json",
            "number,url,title,headRefName",
        ],
        capture_output=True,
        text=True,
        cwd=root,
        timeout=60,
        check=False,
    ).stdout
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError:
        return {}
    return {row["headRefName"]: row for row in rows}


def ahead_refs(root, base):
    """Every local branch holding at least one commit the base does not, newest tip first."""
    rows = []
    for name in run(root, "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)", "refs/heads").split():
        count = run(root, "rev-list", "--count", f"{base}..{name}").strip()
        if count not in ("", "0"):
            rows.append({"ref": name, "ahead": int(count)})
    return rows


def stamp(root, base, refs):
    """What the diffs of these refs are built from, in one string: their tips, the base, and the work on disk.

    The work on disk is taken as the content of the change, not as the list of files carrying it: an edit inside a file
    that was already modified - a rename swept through it, a comment rewritten - leaves `git status` saying exactly what
    it said before, and a page comparing that would believe it was up to date while showing the previous diff.

    Cheap enough to ask for every few seconds, which is what lets a page notice the branch has moved on without
    collecting the diffs and rebuilding itself around them.
    """
    root = str(pathlib.Path(root).expanduser())
    marks = [run(root, "rev-parse", ref).strip() for ref in (base, *refs)]
    marks.append(run(root, "status", "--porcelain").strip())
    marks.append(run(root, "diff", "HEAD").strip())
    return hashlib.sha1("\x1f".join(marks).encode()).hexdigest()[:16]


def collect(root, base, refs, upstream=None):
    """The whole reviewable payload: one entry per ref, each with its per-commit breakdown."""
    root = str(pathlib.Path(root).expanduser())
    upstream = upstream if upstream is not None else canonical_repo(root)
    current = run(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    refs = list(refs) or [row["ref"] for row in ahead_refs(root, base)]
    requests = pull_requests(root, upstream) if upstream else {}
    data = {
        "root": root,
        "name": pathlib.Path(run(root, "rev-parse", "--show-toplevel").strip() or root).name,
        "baseRef": base,
        "base": run(root, "rev-parse", "--short", base).strip(),
        "upstream": upstream,
        "branches": [],
    }
    data["stamp"] = stamp(root, base, refs)
    for wanted in refs:
        number = pull_number(wanted)
        request = None
        ref = wanted
        if number is not None:
            ref, request = fetch_pull(root, upstream, number)
        commits = []
        log = run(root, "log", "--format=%h%x1f%s", f"{base}..{ref}").strip().split("\n")
        for row in reversed([line for line in log if line]):
            sha, subject = row.split("\x1f")
            commits.append(
                {"sha": sha, "subject": subject, "files": parse(run(root, "show", "--format=", "--unified=3", sha))}
            )
        # The checked-out branch is shown as it stands on disk, uncommitted work included.
        whole = (
            run(root, "diff", "--unified=3", base)
            if ref == current
            else run(root, "diff", "--unified=3", f"{base}...{ref}")
        )
        files = parse(whole)
        # A ref holding neither a commit nor an uncommitted change has nothing to review, so it is not offered.
        if not commits and not files:
            continue
        data["branches"].append(
            {
                "ref": ref,
                "blurb": f"#{number}" if number else ref.split("/")[-1].replace("_", " "),
                "pr": request or requests.get(ref),
                "tip": run(root, "rev-parse", "--short", ref).strip(),
                # Empty means the working tree, which is what the checked-out branch is shown as.
                "rev": "" if ref == current else ref,
                "commits": commits,
                "files": files,
                "dirty": ref == current,
            }
        )
    return data


if __name__ == "__main__":
    payload = collect(
        os.environ.get("DIFF_ROOT", "."),
        os.environ.get("DIFF_BASE", "upstream/main"),
        sys.argv[1:],
        os.environ.get("DIFF_UPSTREAM"),
    )
    out = home() / "diff_data.json"
    out.write_text(json.dumps(payload, separators=(",", ":")))
    template = pathlib.Path(__file__).parent / "diff_desk_template.html"
    (home() / "diff_desk.html").write_text(render_page(template.read_text(), payload))
    print(f"{sum(len(b['files']) for b in payload['branches'])} file diffs, ", end="")
    print(f"{sum(len(b['commits']) for b in payload['branches'])} commits, upstream {payload['upstream'] or '(none)'}")
    print("bytes:", out.stat().st_size)
