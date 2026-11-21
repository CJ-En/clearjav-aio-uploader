"""Start of the Windows program: without arguments the browser page, with arguments the terminal commands."""

import sys

from clearjav_aio.cli import main

if __name__ == "__main__":
    sys.exit(main())
