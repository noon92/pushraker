# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

# pushraker: Moonraker loads components from its components folder by name;
# this file hands over to the pushraker package, which Bespok3d links into
# Moonraker's Python.
from pushraker.component import load_component  # noqa: F401
