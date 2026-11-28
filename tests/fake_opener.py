"""A stand-in for the command a machine opens files with: it records what it is asked to open, and opens nothing.

A file of the kind `.nokind` has no application to open it, which the stand-in says by failing, as the real one does.
"""

import pathlib
import sys

with (pathlib.Path(sys.argv[1]) / "opened.log").open("a") as log:
    log.write(" ".join(sys.argv[2:]) + "\n")
if sys.argv[-1].endswith(".nokind") and "-R" not in sys.argv:
    sys.exit("No application knows how to open it.")
