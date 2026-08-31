import os
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _runtime_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[1]


def _configure_paths() -> None:
    root = _runtime_root()
    py_src = root / "py-src"
    if str(py_src) not in sys.path:
        sys.path.insert(0, str(py_src))

    # Keep user-editable configuration next to the executable when frozen.
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        os.environ.setdefault("DATA_FORMULATOR_CONFIG_DIR", str(exe_dir))


def main() -> None:
    _configure_paths()

    from data_formulator.app import app

    port = int(os.environ.get("DATA_FORMULATOR_PORT", "5000"))
    url = f"http://127.0.0.1:{port}"

    def open_browser() -> None:
        time.sleep(1.5)
        webbrowser.open(url, new=2)

    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
