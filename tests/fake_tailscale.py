"""A stand-in for Tailscale's command line: it records what it is asked, and serves one path as told."""

import json
import pathlib
import sys

held = pathlib.Path(sys.argv[1])
asked = sys.argv[2:]
with (held / "asked.log").open("a") as log:
    log.write(" ".join(asked) + "\n")
serving = held / "serving"
if asked[:2] == ["status", "--json"]:
    print(json.dumps({"BackendState": "Running", "Self": {"DNSName": "desk.tail0.ts.net."}}))
elif asked[:2] == ["serve", "--bg"]:
    serving.write_text(asked[3].removeprefix("--set-path="))
elif asked[0] == "serve" and asked[-1] == "off":
    serving.unlink(missing_ok=True)
