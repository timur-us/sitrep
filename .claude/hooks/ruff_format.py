"""PostToolUse hook for Edit and Write: formats a changed Python file with ruff.

Never blocks: formatting is a convenience, the gate is `ruff format --check` in
CI. Started with `python3 -I`, so nothing from the working directory is
imported.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    try:
        path = Path(json.load(sys.stdin)["tool_input"]["file_path"])
    except Exception:
        return
    ruff = shutil.which("ruff")
    if ruff and path.suffix == ".py" and path.is_file():
        subprocess.run([ruff, "format", "--quiet", str(path)], capture_output=True, check=False)


if __name__ == "__main__":
    main()
    sys.exit(0)
