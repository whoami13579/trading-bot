"""Fixed project paths, independent of the process working directory.

Editable installs retain the checkout paths used by the legacy tools. A wheel
has no associated checkout: it uses a fixed, initially empty location in its
installation prefix and never searches the current directory for credentials.
"""
from pathlib import Path
import sys

_candidate = Path(__file__).resolve().parents[3]
ROOT = (_candidate if (_candidate / "pyproject.toml").is_file()
        and (_candidate / "src/trading_bot").is_dir()
        else Path(sys.prefix) / "share/trading-bot")
