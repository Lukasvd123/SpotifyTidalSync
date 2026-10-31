import os
import sys
import tempfile

# Must happen before spotifysync is imported: config creates its data dir at
# import time, and tests must never touch the real settings or mappings.
os.environ["SPOTIFYSYNC_HOME"] = tempfile.mkdtemp(prefix="spotifysync-tests-")
# Never read or write the real OS keyring (Tidal and Spotify tokens live there).
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
