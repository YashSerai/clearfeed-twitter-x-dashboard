from __future__ import annotations

import atexit
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def single_instance(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(f"Another instance is already running: {lock_path.name}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()

        def _cleanup() -> None:
            try:
                handle.seek(0)
                handle.truncate()
                handle.flush()
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            try:
                handle.close()
            except OSError:
                pass

        atexit.register(_cleanup)
        yield
    finally:
        try:
            handle.seek(0)
            handle.truncate()
            handle.flush()
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass
