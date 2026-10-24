"""Entry point of "XM Control.exe": the app window, or a command when given arguments.

The app runs this exe again with arguments for the head tracking repair, which
needs administrator rights (headtracker.run_elevated).
"""

import sys

from sonyxm.cli import main

sys.exit(main(sys.argv[1:] or ["gui"]))
