"""Read or set one KEY in a .env file: ``env_value.py .env KEY`` prints it, ``env_value.py .env KEY VALUE`` sets it.
Values with spaces or shell characters are single-quoted so that ``set -a; . ./.env`` keeps working."""

import re
import sys
from pathlib import Path


def main(path: str, key: str, value: str | None = None) -> None:
    file = Path(path)
    lines = file.read_text(encoding="utf-8").splitlines() if file.exists() else []
    pattern = re.compile(rf"^\s*{re.escape(key)}=(.*)$")
    if value is None:
        for line in lines:
            m = pattern.match(line)
            if m:
                print(m.group(1).strip().strip('"').strip("'"))
                return
        return
    quoted = f"'{value}'" if re.search(r"""[\s#$`\\"]""", value) else value
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{key}={quoted}"
            break
    else:
        lines.append(f"{key}={quoted}")
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(*sys.argv[1:4])
