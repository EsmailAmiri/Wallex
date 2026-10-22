"""Start the local app and open the dashboard when the HTTP endpoint is ready."""
import json
import threading
import time
import webbrowser
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError
from wallex_settings import dashboard_port

URL = f'http://127.0.0.1:{dashboard_port()}'


def is_ready():
    try:
        with urlopen(URL + '/api/state', timeout=1) as response:
            state = json.load(response)
        return 'config' in state and 'decisions' in state and 'account' in state
    except (URLError, TimeoutError, ValueError):
        return False


def open_when_ready():
    for _ in range(40):
        if is_ready():
            webbrowser.open(URL)
            return
        time.sleep(.25)


if __name__ == '__main__':
    virtual_python = Path(__file__).resolve().parent / '.venv' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    if virtual_python.exists() and Path(sys.executable).resolve() != virtual_python.resolve():
        raise SystemExit(subprocess.call([str(virtual_python), str(Path(__file__).resolve())]))
    if is_ready():
        webbrowser.open(URL)
    else:
        from server import main
        threading.Thread(target=open_when_ready, daemon=True).start()
        main()
