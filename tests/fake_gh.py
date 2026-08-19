"""Stand in for the gh command, answering exactly as the test in charge asked it to.

The reply is read at each call from the file named by FAKE_GH_SCRIPT, so one running desk can be made to succeed, to be
refused, or to be unreachable, without a network or a login. A script may carry rules matched against the arguments,
since resolving a repository, listing its pull requests and posting a review are all `gh` yet answered differently.
"""

import json
import os
import pathlib
import sys

asked = json.loads(pathlib.Path(os.environ["FAKE_GH_SCRIPT"]).read_text())
asking = " ".join(sys.argv[1:])
if os.environ.get("FAKE_GH_LOG"):
    # Whatever is piped in is recorded only when it was asked for, since a call given nothing inherits a standard input
    # that never ends and reading it would wait for good.
    given = sys.stdin.read() if "--input" in sys.argv else ""
    with pathlib.Path(os.environ["FAKE_GH_LOG"]).open("a") as told:
        # One call, one line, whatever it was given: a GraphQL document runs over several lines and a test counting the
        # calls would read one invocation as a dozen. What was piped in is left as it came, since a test reads it back.
        told.write(" ".join(asking.split()) + (f" <<< {given}" if given else "") + "\n")
wanted = next((rule for rule in asked.get("rules", []) if rule["match"] in asking), asked)
# Whatever is being piped in is left unread: only a posted review is given anything, and the calls that resolve a
# repository inherit a standard input that never ends, which reading would wait on for good.
sys.stdout.write(wanted.get("out", ""))
sys.stderr.write(wanted.get("err", ""))
raise SystemExit(wanted.get("code", 0))
