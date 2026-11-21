# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""The values a template uses, and whether this version and this printer
have them: for the Import dialog's notices ("Uses values this printer
doesn't have", "Uses values this version doesn't know").

The values are read with Jinja's own parser: an attribute chain from one
of the template roots (job.time.remaining, printer.heater_bed.target),
through filters, tests, if and for; not text in strings, not names a
template sets itself ({% set job2 = ... %}, a for loop's variable), not a
method called on a value (job.file.upper()).

What pushraker knows, and on which printers it has data, is values_map
(generated from the Objects tree by tests/gen_values.py, with the docs
values.md and values-by-printer.md: one source for both).
"""

from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, \
    Tuple

import jinja2
from jinja2 import nodes

# pushraker's own roots (printer.* is Klipper's: any object it has)
OWN_ROOTS = ("job", "extruders", "event", "machine", "pushraker")
ROOTS = ("printer",) + OWN_ROOTS
_ENV = jinja2.Environment(extensions=["jinja2.ext.loopcontrols"])


def _chain(node: Any) -> Optional[List[str]]:
    """The names of an attribute chain from a root name, or None."""
    parts: List[str] = []
    while True:
        if isinstance(node, nodes.Getattr):
            parts.append(node.attr)
            node = node.node
        elif isinstance(node, nodes.Getitem):
            arg = node.arg
            if isinstance(arg, nodes.Const) and isinstance(arg.value, str):
                parts.append(arg.value)
            elif isinstance(arg, nodes.Const) and isinstance(arg.value, int):
                parts.append("#")
            else:
                return None
            node = node.node
        elif isinstance(node, nodes.Name):
            parts.append(node.name)
            return list(reversed(parts))
        else:
            return None


def _stored(tree: Any) -> Set[str]:
    """Names the template sets itself (set, for, macro arguments)."""
    out: Set[str] = set()
    for n in tree.find_all(nodes.Name):
        if n.ctx in ("store", "param"):
            out.add(n.name)
    for n in tree.find_all(nodes.Macro):
        out.update(a.name for a in n.args)
    return out


def referenced(texts: Iterable[str]) -> List[str]:
    """The values the templates read, as dotted paths ("job.time.remaining",
    "printer.temperature_sensor cavity.temperature"); a number index reads
    "#". Text Jinja can't parse gives nothing (validation says why)."""
    found: Set[str] = set()
    for text in texts:
        if not text:
            continue
        try:
            tree = _ENV.parse(text)
        except jinja2.TemplateSyntaxError:
            continue
        local = _stored(tree)
        # The outermost node of each chain: one that isn't the inside of
        # another attribute or item lookup
        inner: Set[int] = set()
        calls: Set[int] = set()
        for n in tree.find_all((nodes.Getattr, nodes.Getitem)):
            inner.add(id(n.node))
        for n in tree.find_all(nodes.Call):
            calls.add(id(n.node))
        for n in tree.find_all((nodes.Getattr, nodes.Getitem)):
            if id(n) in inner:
                continue
            parts = _chain(n)
            if not parts or parts[0] not in ROOTS or parts[0] in local:
                continue
            if id(n) in calls and len(parts) > 2:
                parts = parts[:-1]      # a method of the value
            if len(parts) > 1:
                found.add(".".join(parts))
    return sorted(found)


def norm(path: str) -> str:
    """A path with its numbers as # (filament2 -> filament#, [0] -> #),
    as values_map keeps them."""
    out = []
    for part in path.split("."):
        stripped = part.rstrip("0123456789")
        out.append(stripped + "#" if stripped != part and stripped
                   else ("#" if part.isdigit() else part))
    return ".".join(out)


def _covered(path: str, known: Iterable[str]) -> List[str]:
    """The known values a path is, is inside, or leads to."""
    return [k for k in known if path == k or path.startswith(k + ".")
            or k.startswith(path + ".")]


def check(paths: Sequence[str], printer: str, objects: Sequence[str],
          vmap: Optional[Dict[str, Any]] = None
          ) -> Tuple[List[str], List[str]]:
    """(values this printer doesn't have, values this version doesn't
    know), from referenced(). `printer`: the profile key in values_map
    ("snapmaker_u1", "creality", "generic"); `objects`: Klipper's object
    list here. printer.<object>.*: missing when this printer has no such
    object (Klipper's fields aren't pushraker's to know)."""
    if vmap is None:
        from . import values_map
        vmap = values_map.VALUES
    missing: List[str] = []
    unknown: List[str] = []
    objs = set(objects)
    for path in paths:
        root, _, rest = path.partition(".")
        if root == "printer":
            obj = rest.split(".", 1)[0]
            if objs and obj not in objs:
                missing.append(path)
            continue
        if root not in OWN_ROOTS:
            continue
        n = norm(path)
        ks = _covered(n, vmap)
        if not ks:
            unknown.append(path)
        elif not any(printer in vmap[k] for k in ks):
            missing.append(path)
    return missing, unknown
