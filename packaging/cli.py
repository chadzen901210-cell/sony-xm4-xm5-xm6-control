"""Entry point of "sonyxm.exe", the command-line tool (sonyxm battery, sonyxm status, ...)."""

import sys

from sonyxm.cli import main

sys.exit(main())
