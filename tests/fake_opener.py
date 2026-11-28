"""A stand-in for the command a machine opens files with: it records what it is asked to open, and opens nothing."""

import pathlib
import sys

with (pathlib.Path(sys.argv[1]) / "opened.log").open("a") as log:
    log.write(" ".join(sys.argv[2:]) + "\n")
