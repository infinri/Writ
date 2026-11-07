import sys
from pathlib import Path

M1 = Path(__file__).resolve().parents[2]
FIXTURES = M1 / "fixtures"
PROPS = M1 / "props"
if str(PROPS) not in sys.path:
    sys.path.insert(0, str(PROPS))
