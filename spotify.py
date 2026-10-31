#!/usr/bin/env python3
"""Entry point. The application lives in the spotifysync package.

Kept at this path so the existing build scripts keep working.
"""

import sys

from spotifysync.main import main

if __name__ == "__main__":
    sys.exit(main())
