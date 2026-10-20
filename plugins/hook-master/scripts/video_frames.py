#!/usr/bin/env python3
"""Extract frames and timing facts from a video's first 3 seconds.

Usage: python3 video_frames.py <video> <output_dir>
Writes frame_0.0s.jpg ... into output_dir and prints the facts as JSON.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hookcore import video  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    out = Path(argv[2])
    out.mkdir(parents=True, exist_ok=True)
    try:
        probe = video.probe(argv[1])
    except video.VideoError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    frames = []
    for frame in probe.frames:
        path = out / f"frame_{frame.t:.1f}s.jpg"
        path.write_bytes(frame.jpeg)
        frames.append(str(path))
    print(json.dumps({**probe.facts(), "facts_text": probe.facts_text(), "frames": frames}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
