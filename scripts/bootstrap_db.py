import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from x_signal_dashboard import db


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


if __name__ == "__main__":
    load_env(ROOT / ".env")
    database_path = (ROOT / os.environ.get("DATABASE_PATH", "./data/marketing.sqlite3")).resolve()
    with db.managed_connection(database_path) as conn:
        db.bootstrap(conn)
    print(f"Database bootstrapped at {database_path}")
