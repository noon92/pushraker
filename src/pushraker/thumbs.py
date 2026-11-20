# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""The slicer's thumbnail from a G-code file's header, for when Moonraker's
metadata has none: an older Moonraker (the Creality Hi's, 2022) doesn't
know the Orca family of slicers (OrcaSlicer, Creality Print, Snapmaker
Orca, Bambu Studio), so it extracts no thumbnails from their files, though
they carry the usual block:

  ; thumbnail begin 96x96 4200          (PNG; also thumbnail_PNG)
  ; iVBORw0KGgo...                      (base64, one comment per line)
  ; thumbnail end
  ; thumbnail_JPG begin 300x300 12000   (JPEG)

QOI thumbnails (thumbnail_QOI) are skipped: Pillow before 9.5 can't read
them. Blocking: reads the file.
"""

from __future__ import annotations
import base64
import binascii
import re
from typing import List, Optional, Tuple

HEADER_BYTES = 1024 * 1024      # thumbnails sit at the top of the file
RX_BEGIN = re.compile(
    rb";\s*thumbnail(?:_(PNG|JPG|JPEG))?\s+begin\s+(\d+)\s*x\s*(\d+)",
    re.I)
RX_END = re.compile(rb";\s*thumbnail(?:_\w+)?\s+end", re.I)
PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"


def extract(path: str, limit: int = HEADER_BYTES
            ) -> Optional[Tuple[bytes, str]]:
    """(image bytes, ".png" or ".jpg") of the largest PNG or JPEG thumbnail
    in the file's first `limit` bytes, or None."""
    try:
        with open(path, "rb") as f:
            head = f.read(limit)
    except OSError:
        return None
    found: List[Tuple[int, bytes, str]] = []
    lines = head.splitlines()
    i = 0
    while i < len(lines):
        m = RX_BEGIN.match(lines[i].strip())
        i += 1
        if not m:
            continue
        kind = (m.group(1) or b"PNG").upper()
        area = int(m.group(2)) * int(m.group(3))
        chunks = []
        while i < len(lines) and not RX_END.match(lines[i].strip()):
            chunks.append(lines[i].lstrip(b"; \t"))
            i += 1
        i += 1
        try:
            data = base64.b64decode(b"".join(chunks), validate=False)
        except (binascii.Error, ValueError):
            continue
        if kind == b"PNG" and data.startswith(PNG):
            found.append((area, data, ".png"))
        elif kind in (b"JPG", b"JPEG") and data.startswith(JPEG):
            found.append((area, data, ".jpg"))
    if not found:
        return None
    _, data, ext = max(found, key=lambda t: t[0])
    return data, ext
