# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""The default templates: templates/<notification>.title.j2 and
templates/<notification>.j2 (the contents)."""

import functools
import os
from typing import Optional, Tuple

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


@functools.lru_cache(maxsize=None)
def default(key: str) -> Optional[Tuple[str, str]]:
    """(title, contents) for a notification, or None when it has none."""
    try:
        with open(os.path.join(HERE, f"{key}.title.j2"),
                  encoding="utf-8") as f:
            title = f.read().strip("\n")
        with open(os.path.join(HERE, f"{key}.j2"), encoding="utf-8") as f:
            body = f.read().rstrip("\n")
    except OSError:
        return None
    return title, body
