"""A repository to review and a desk serving it, both built from scratch so no checkout or network is involved."""

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gen_diff_data  # noqa: E402  (the package under test lives beside its tests)

# The two lines the fixture branch rewrites, far enough apart to leave a gap between the hunks they produce.
FIRST_EDIT = 10
SECOND_EDIT = 150
FILE_LINES = 200


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def repo(tmp_path_factory):
    """A branch that rewrites two distant lines of a long file, so hunks, a gap between them, and both sides exist."""
    root = tmp_path_factory.mktemp("repo")
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "desk@example.com")
    git(root, "config", "user.name", "Desk")
    body = [f"line {number}" for number in range(1, FILE_LINES + 1)]
    (root / "sample.py").write_text("\n".join(body) + "\n")
    (root / "kept.py").write_text("untouched\n")
    # A second long file, which no test comments on: a drag needs a stretch of rows with nothing hanging between them,
    # and comments accumulate in this desk by design - the file they are written on runs out of clean stretches.
    (root / "wide.py").write_text("\n".join(f"wide {number}" for number in range(1, 41)) + "\n")
    # A file a couple of directories down, so the file list has a tree to draw and a chain to fold.
    (root / "pkg" / "sub").mkdir(parents=True)
    # Indented, so a test can tell whether copying a selection keeps the indentation that makes code usable.
    (root / "pkg" / "sub" / "deep.py").write_text("def area(r):\n    return 1\nthree\n")
    git(root, "add", "-A")
    git(root, "commit", "-m", "the file under review")
    git(root, "checkout", "-q", "-b", "feature")
    body[FIRST_EDIT - 1] = f"line {FIRST_EDIT} rewritten"
    body[SECOND_EDIT - 1] = f"line {SECOND_EDIT} rewritten"
    (root / "sample.py").write_text("\n".join(body) + "\n")
    (root / "added.py").write_text("brand new\n")
    wide = [f"wide {number}" for number in range(1, 41)]
    # A block of lines rather than one, so its single hunk holds rows enough for a drag to cross several of them.
    for number in range(18, 25):
        wide[number - 1] = f"wide {number} rewritten"
    (root / "wide.py").write_text("\n".join(wide) + "\n")
    (root / "pkg" / "sub" / "deep.py").write_text("def area(r):\n    return 2\nthree\n")
    git(root, "add", "-A")
    git(root, "commit", "-m", "rewrite two lines and add a file")
    git(root, "checkout", "-q", "main")
    # A branch sitting exactly at the base, to pin what happens when a ref has nothing to review.
    git(root, "branch", "parked", "main")
    return root


class Desk:
    """A running desk, addressed by URL and answering about the repository it was pointed at."""

    def __init__(self, url, repo, home):
        self.url = url
        self.repo = repo
        self.home = home

    def github_answers(self, code=0, out="", err="", rules=()):
        """What the stand-in for gh replies to the next call, so every arm of a post can be exercised in place.

        A rule is `{"match": <text found in the arguments>, "out"/"err"/"code": ...}`, for a run where resolving a
        repository, listing its pull requests and posting a review must be answered differently.
        """
        reply = {"code": code, "out": out, "err": err, "rules": list(rules)}
        (self.home / "fake_gh.json").write_text(json.dumps(reply))

    def github_calls(self):
        """Everything the stand-in for gh was asked, in order, so a test can prove a call was made."""
        told = self.home / "fake_gh.log"
        return told.read_text().splitlines() if told.exists() else []

    def get(self, route):
        with urllib.request.urlopen(f"{self.url}{route}", timeout=30) as answer:
            return json.loads(answer.read() or b"null")

    def page(self):
        """The page itself, as a browser asks for it, which is what a reload does."""
        with urllib.request.urlopen(f"{self.url}/", timeout=60) as answer:
            return answer.read().decode()

    def post(self, route, payload):
        request = urllib.request.Request(
            f"{self.url}{route}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as answer:
            body = answer.read()
            return json.loads(body) if body else None


@pytest.fixture(scope="session")
def desk(repo, tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "desk.py"), "serve", "--dir", str(repo), "--base", "main", "feature"],
        cwd=ROOT,
        env={
            **os.environ,
            "DIFF_DESK_HOME": str(home),
            "DIFF_DESK_PORT": str(port),
            # GitHub is answered for by a stand-in, so posting is exercised without a network or a login.
            "DIFF_DESK_GH": f"{sys.executable} {ROOT / 'tests' / 'fake_gh.py'}",
            "FAKE_GH_SCRIPT": str(home / "fake_gh.json"),
            "FAKE_GH_LOG": str(home / "fake_gh.log"),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    running = Desk(f"http://127.0.0.1:{port}", repo, home)
    running.github_answers(code=1, err="gh: Not Found (HTTP 404)")
    for _ in range(200):
        if process.poll() is not None:
            pytest.fail(f"the desk exited: {process.stdout.read()}")
        try:
            running.get("/data")
            break
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    else:
        process.kill()
        pytest.fail("the desk never came up")
    yield running
    process.terminate()
    process.wait(timeout=10)


@pytest.fixture(scope="session")
def payload(repo):
    return gen_diff_data.collect(str(repo), "main", ["feature"])
