import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clearfeed_dashboard.dashboard import run_dashboard
from clearfeed_dashboard.singleton import single_instance


if __name__ == "__main__":
    lock_path = ROOT / "data" / "runtime" / "dashboard.lock"
    with single_instance(lock_path):
        run_dashboard()

