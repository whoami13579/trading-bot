"""Private atomic JSON report writes."""
import json
import os
from pathlib import Path
import tempfile

def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix="report-", delete=False) as stream:
        pending = Path(stream.name)
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, path)
