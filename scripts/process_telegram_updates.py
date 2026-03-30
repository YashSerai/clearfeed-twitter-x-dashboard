import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from x_signal_dashboard.service import XAgentService


if __name__ == "__main__":
    service = XAgentService()
    service.bootstrap()
    service.process_telegram_updates()
    print("Telegram updates processed.")
