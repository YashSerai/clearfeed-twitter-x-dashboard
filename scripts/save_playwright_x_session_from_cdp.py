import os
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
DEFAULT_STATE = ROOT / "data" / "browser" / "x_storage_state.json"
DEFAULT_CDP_URL = "http://127.0.0.1:9222"


def load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def get_state_path() -> Path:
    value = os.environ.get("PLAYWRIGHT_STORAGE_STATE", "").strip()
    if not value:
        return DEFAULT_STATE
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path


def get_cdp_url() -> str:
    return os.environ.get("PLAYWRIGHT_CDP_URL", DEFAULT_CDP_URL).strip() or DEFAULT_CDP_URL


def main() -> int:
    load_env()
    state_path = get_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    cdp_url = get_cdp_url()

    print("Expecting a real Chrome/Edge instance already running with remote debugging enabled.")
    print(f"CDP URL: {cdp_url}")
    print("Log into X in that browser first, then press Enter here to capture storage state.")
    input()

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        contexts = browser.contexts
        if not contexts:
            raise SystemExit(
                "No browser contexts found over CDP. Make sure Chrome or Edge is running "
                "with --remote-debugging-port=9222, or set PLAYWRIGHT_CDP_URL."
            )

        context = contexts[0]
        context.storage_state(path=str(state_path))
        browser.close()

    print(f"Saved Playwright storage state to: {state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
