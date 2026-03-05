import sys
from pathlib import Path


# Ensure repo root (feb_purchase_bot_py/) is importable in tests.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

