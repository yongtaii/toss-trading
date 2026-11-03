"""Local operator controls and single-host account ownership."""

import fcntl
import json
from pathlib import Path
from tempfile import NamedTemporaryFile


def read_control(path: str | Path) -> str:
    try:
        value = json.loads(Path(path).read_text())["action"]
        return value if value in {"run", "pause", "flatten"} else "pause"
    except (OSError, ValueError, KeyError, TypeError):
        return "pause"


def write_control(path: str | Path, action: str) -> None:
    if action not in {"run", "pause", "flatten"}:
        raise ValueError("Invalid live action")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=target.parent, delete=False) as handle:
        json.dump({"action": action}, handle)
        temporary = Path(handle.name)
    temporary.replace(target)


class AccountLock:
    """SQLite live deployments run on one host; hold this through all broker I/O."""

    def __init__(self, account_seq: int, directory: Path = Path("/tmp")):
        self.path = directory / f"toss-live-account-{account_seq}.lock"
        self.handle = None

    def __enter__(self):
        self.handle = self.path.open("a")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError("Another live process owns this account on this host") from None
        return self

    def __exit__(self, *args):
        self.handle.close()
