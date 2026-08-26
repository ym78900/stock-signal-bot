import sys
from pathlib import Path

# api_server.py and its sibling modules (config, scanner, signals) live at
# the repo root, one level up from this Vercel function entrypoint.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api_server import app  # noqa: E402,F401
