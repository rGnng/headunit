"""Convert the Bootstrap Icons WOFF2 font into a TTF for Qt."""

import sys
from pathlib import Path

from fontTools.ttLib import TTFont

src = Path(sys.argv[1])
dst = Path(sys.argv[2])

font = TTFont(str(src))
font.flavor = None  # strip the WOFF2 wrapper
font.save(str(dst))
print("saved", dst, dst.stat().st_size, "bytes")
