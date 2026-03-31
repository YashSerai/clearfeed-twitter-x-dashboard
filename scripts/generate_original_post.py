import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clearfeed_dashboard.service import XAgentService
from clearfeed_dashboard.db import managed_connection, bootstrap


if __name__ == "__main__":
    service = XAgentService()
    topic = " ".join(sys.argv[1:]).strip()
    with managed_connection(service.config.database_path) as conn:
        bootstrap(conn)
        service._generate_original_post_drafts(conn, topic)
    print("Original post drafts created.")

