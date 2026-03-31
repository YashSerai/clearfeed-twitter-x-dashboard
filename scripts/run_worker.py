import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clearfeed_dashboard.service import XAgentService
from clearfeed_dashboard.singleton import single_instance


if __name__ == "__main__":
    lock_path = ROOT / "data" / "runtime" / "worker.lock"
    with single_instance(lock_path):
        XAgentService().run_worker_loop()

