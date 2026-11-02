# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

# Moonraker loads components from its own components directory: this file
# goes there (moonraker/components/push_notifications.py) and hands over to
# the installed pushraker package. `[push_notifications]` in moonraker.conf
# enables it.
from pushraker.component import load_component  # noqa: F401
