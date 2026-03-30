import os
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
DEFAULT_STATE = ROOT / "data" / "browser" / "x_storage_state.json"
DEFAULT_BROWSERS = [
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]


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


def get_browser_executable() -> str | None:
    configured = os.environ.get("PLAYWRIGHT_BROWSER_EXECUTABLE", "").strip()
    if configured:
        path = Path(configured)
        if path.exists():
            return str(path)
    for path in DEFAULT_BROWSERS:
        if path.exists():
            return str(path)
    return None


def main() -> int:
    load_env()
    state_path = get_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    executable_path = get_browser_executable()

    with sync_playwright() as p:
        launch_kwargs = {"headless": False}
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
            print(f"Launching installed browser: {executable_path}")
        else:
            print("No installed Chrome/Edge found. Falling back to Playwright Chromium.")
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(viewport={"width": 1440, "height": 1200})
        page = context.new_page()

        print("Opening X login flow in Chromium...")
        page.goto("https://x.com/home", wait_until="domcontentloaded")
        print()
        print("1. Log into X in the browser window.")
        print("2. Make sure you can open your lists or profile while logged in.")
        print("3. If prompted for email, phone, 2FA, or captcha, finish that now.")
        print("4. Return here and press Enter to save the session.")
        input()

        context.storage_state(path=str(state_path))
        browser.close()

    print(f"Saved Playwright storage state to: {state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
