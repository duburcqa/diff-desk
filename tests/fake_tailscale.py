"""A stand-in for Tailscale's command line: it records what it is asked, and serves one port as told."""

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
elif asked[:3] == ["serve", "status", "--json"]:
    port = serving.read_text() if serving.exists() else None
    print(json.dumps({"Web": {f"desk.tail0.ts.net:{port}": {}}} if port else {}))
elif asked[:2] == ["serve", "--bg"]:
    serving.write_text(asked[2].removeprefix("--https="))
elif asked[0] == "serve" and asked[-1] == "off":
    serving.unlink(missing_ok=True)
