"""Run only tests/ with HTTP and socket connections blocked; never load .env."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def forbidden(*args, **kwargs):
    raise RuntimeError("Network and credential loading are forbidden in offline tests")


def audit(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname"}:
        forbidden()
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(args[0].decode() if isinstance(args[0], bytes) else args[0]).resolve()
        if path.parent == ROOT and (path.name == ".env" or path.name.startswith(".env.")) and not path.name.endswith(".example"):
            forbidden()


def main():
    sys.path.insert(0, str(ROOT))
    sys.addaudithook(audit)
    import dotenv
    import requests

    with patch.object(requests.sessions.Session, "send", forbidden), \
         patch.object(dotenv, "load_dotenv", forbidden), \
         patch.object(dotenv, "dotenv_values", forbidden):
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
