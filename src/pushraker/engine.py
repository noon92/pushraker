# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-PackageHomePage: https://github.com/noon92/pushraker
# SPDX-FileCopyrightText: Copyright (c) 2026 @noon92

"""Notification templates: stock Jinja, the language Klipper macros use,
sandboxed, with Klipper's values (printer.*), pushraker's (job, extruders,
event, machine) and a few filters of pushraker's own.

    {{ printer.extruder1.temperature|round(0) }}
    {% if job.queue.remaining %}...{% endif %}
    {# a comment #}

A missing value reads "n/a" and is false in an {% if %}. A line holding only
{% %} tags leaves nothing behind (no blank line from a false if); blank
lines written in the template stay.
"""

import re
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, \
    Set, Tuple

import jinja2
from jinja2 import nodes
from jinja2.sandbox import SandboxedEnvironment

from . import templating as tp

# The names a template starts from
ROOTS = ("printer", "job", "extruders", "event", "machine")
MAX_OUTPUT = 20000          # characters: a runaway loop stops here
MAX_RANGE = 1000            # range() in a template (Jinja's own: 100000)
MAX_POWER = 100             # the largest exponent ** takes


class NA(jinja2.ChainableUndefined):
    """A value that isn't there: reads "n/a", is false, compares false,
    and stays n/a through arithmetic and further attributes."""
    __slots__ = ()

    def __str__(self) -> str:
        return tp.NA

    def _same(self, *_a: Any, **_k: Any) -> "NA":
        return self

    def _false(self, *_a: Any) -> bool:
        return False

    __add__ = __radd__ = __sub__ = __rsub__ = __mul__ = __rmul__ = _same
    __truediv__ = __rtruediv__ = __floordiv__ = __rfloordiv__ = _same
    __mod__ = __rmod__ = __pow__ = __rpow__ = __neg__ = __pos__ = _same
    __lt__ = __le__ = __gt__ = __ge__ = _false
    __round__ = _same


def raw(value: Any) -> Any:
    """A View's value; None for n/a; anything else as is."""
    if isinstance(value, View):
        return value._value()
    if isinstance(value, jinja2.Undefined):
        return None
    return value


class View:
    """pushraker's values under a name (job, extruders): looked up when a
    template reads them, so a notification computes only what its template
    uses. A path can be a value and have values under it at once:
    {{ job.time.remaining }} and {{ job.time.remaining.blend }}."""
    __slots__ = ("_look", "_branch", "_items", "_path", "_name")

    def __init__(self, look: Callable[[List[str]], Any],
                 branch: Callable[[List[str]], bool],
                 items: Callable[[List[str]], Optional[list]],
                 path: List[str], name: str) -> None:
        self._look, self._branch, self._items = look, branch, items
        self._path, self._name = path, name

    def _value(self) -> Any:
        return self._look(self._path) if self._path else None

    def _child(self, key: str) -> Any:
        path = self._path + [key]
        name = f"{self._name}.{key}"
        if self._branch(path):
            return View(self._look, self._branch, self._items, path, name)
        value = self._look(path)
        if value is None:
            return NA(name=name)
        return value

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        return self._child(key)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            seq = self._items(self._path)
            if seq is None:
                value = self._value()
                seq = value if isinstance(value, (list, tuple)) else None
            if seq is not None and -len(seq) <= key < len(seq):
                return seq[key]
            return NA(name=f"{self._name}[{key}]")
        return self._child(str(key))

    def __iter__(self) -> Iterator[Any]:
        seq = self._items(self._path)
        if seq is None:
            value = self._value()
            seq = value if isinstance(value, (list, tuple)) else []
        return iter(seq)

    def __len__(self) -> int:
        return len(list(self.__iter__()))

    def __bool__(self) -> bool:
        value = self._value()
        if isinstance(value, tp.Duration):
            return True         # 0m left is still something to say
        if value is None:
            return self._items(self._path) is not None and \
                bool(self._items(self._path))
        return bool(value)

    def __str__(self) -> str:
        return tp.fmt_value(self._value(), "")

    # Comparisons and arithmetic work on the value; n/a when there's none
    def _op(self, other: Any, fn: Callable[[Any, Any], Any],
            missing: Any) -> Any:
        a, b = self._value(), raw(other)
        if a is None or (b is None and not isinstance(other, (str,))
                         and other is not None):
            return missing
        try:
            return fn(a, b)
        except TypeError:
            return missing

    def __eq__(self, other: Any) -> bool:   # type: ignore[override]
        return self._value() == raw(other)

    def __ne__(self, other: Any) -> bool:   # type: ignore[override]
        return self._value() != raw(other)

    __hash__ = None  # type: ignore[assignment]

    def __lt__(self, o: Any) -> bool:
        return self._op(o, lambda a, b: a < b, False)

    def __le__(self, o: Any) -> bool:
        return self._op(o, lambda a, b: a <= b, False)

    def __gt__(self, o: Any) -> bool:
        return self._op(o, lambda a, b: a > b, False)

    def __ge__(self, o: Any) -> bool:
        return self._op(o, lambda a, b: a >= b, False)

    def __add__(self, o: Any) -> Any:
        return self._op(o, lambda a, b: a + b, NA(name=self._name))

    def __radd__(self, o: Any) -> Any:
        return self._op(o, lambda a, b: b + a, NA(name=self._name))

    def __sub__(self, o: Any) -> Any:
        return self._op(o, lambda a, b: a - b, NA(name=self._name))

    def __rsub__(self, o: Any) -> Any:
        return self._op(o, lambda a, b: b - a, NA(name=self._name))

    def __mul__(self, o: Any) -> Any:
        return self._op(o, lambda a, b: a * b, NA(name=self._name))

    __rmul__ = __mul__

    def __truediv__(self, o: Any) -> Any:
        return self._op(o, lambda a, b: a / b, NA(name=self._name))

    def __rtruediv__(self, o: Any) -> Any:
        return self._op(o, lambda a, b: b / a, NA(name=self._name))

    def __floordiv__(self, o: Any) -> Any:
        return self._op(o, lambda a, b: a // b, NA(name=self._name))

    def __mod__(self, o: Any) -> Any:
        return self._op(o, lambda a, b: a % b, NA(name=self._name))

    def __neg__(self) -> Any:
        value = self._value()
        return -value if isinstance(value, (int, float)) else \
            NA(name=self._name)

    def __abs__(self) -> Any:
        value = self._value()
        return abs(value) if isinstance(value, (int, float)) else \
            NA(name=self._name)

    def __contains__(self, item: Any) -> bool:
        return raw(item) in list(self.__iter__())

    def __int__(self) -> int:
        return int(self._value() or 0)

    def __float__(self) -> float:
        return float(self._value() or 0.)

    def __round__(self, n: int = 0) -> Any:
        value = self._value()
        return round(value, n) if isinstance(value, (int, float)) else \
            NA(name=self._name)


# ---- filters: pushraker's own (Jinja's own work as in Klipper) --------------

def _duration(value: Any, pattern: str = "") -> str:
    """57h 22m, or a pattern of D/H/M: |duration("H:MM") -> 57:22."""
    return tp.fmt_value(raw(value), pattern)


def _clock(value: Any, pattern: str = "") -> str:
    """A clock time (an ETA): |clock("H:MM") -> 9:05, "+1d " tomorrow."""
    return tp.fmt_value(raw(value), pattern)


def _toolhead(value: Any, pattern: str = "Toolhead #") -> str:
    """A toolhead name (extruder1), or a list of them, by number from 1:
    |toolhead("T#") -> T2."""
    return tp.fmt_value(raw(value), pattern)


def _pct(value: Any, decimals: int = 0) -> str:
    """A 0-1 fraction as a percentage: 0.42 -> 42% (each, for a list)."""
    v = raw(value)
    if isinstance(v, (list, tuple)):
        return ", ".join(_pct(x, decimals) for x in v if x is not None)
    try:
        return f"{float(v) * 100.:.{int(decimals)}f}%"
    except (TypeError, ValueError):
        return tp.NA


def _plural(value: Any, one: str, many: str) -> str:
    """|plural("a pause", "pauses"): 1 -> a pause, 3 -> 3 pauses."""
    v = raw(value)
    try:
        n = int(v)
    except (TypeError, ValueError):
        return tp.NA
    return one if n == 1 else f"{n} {many}"


def _ordinal(value: Any) -> str:
    """2 -> 2nd."""
    v = raw(value)
    try:
        n = int(v)
    except (TypeError, ValueError):
        return tp.NA
    suffix = "th" if 10 <= n % 100 <= 20 else \
        {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _lines(value: Any) -> str:
    """A list, one item per line."""
    return tp.fmt_value(raw(value) if isinstance(value, View)
                        else list(value) if not isinstance(value, str)
                        else value, "line")


FILTERS = {"duration": _duration, "clock": _clock, "toolhead": _toolhead,
           "pct": _pct, "plural": _plural, "ordinal": _ordinal,
           "lines": _lines}


def _finalize(value: Any) -> Any:
    """How a value reads when printed: pushraker's formats (57h 22m, 220
    for 220.0, lists with commas); n/a for nothing."""
    if isinstance(value, (str, jinja2.Undefined, View)):
        return value
    return tp.fmt_value(value, "")


class _Sandbox(SandboxedEnvironment):
    """Jinja's sandbox (no _attributes, no unsafe callables, str.format
    checked), with smaller limits: range() up to MAX_RANGE, ** up to
    MAX_POWER."""
    intercepted_binops = frozenset(["**"])

    def call_binop(self, context: Any, operator: str, left: Any,
                   right: Any) -> Any:
        if operator == "**":
            try:
                if abs(float(raw(right))) > MAX_POWER:
                    raise jinja2.exceptions.SecurityError(
                        f"** is limited to powers up to {MAX_POWER}")
            except (TypeError, ValueError):
                pass
            return raw(left) ** raw(right)
        return super().call_binop(context, operator, left, right)


def _range(*args: int) -> range:
    r = range(*args)
    if len(r) > MAX_RANGE:
        raise jinja2.exceptions.SecurityError(
            f"range() is limited to {MAX_RANGE} items")
    return r


ENV = _Sandbox(undefined=NA, keep_trailing_newline=False,
               autoescape=False, finalize=_finalize)
ENV.globals["range"] = _range

# A line holding only {% %} tags and {# #} comments (and spaces)
RX_TAG_LINE = re.compile(
    r"[ \t]*(?:(?:\{%(?:(?!%\}).)*%\}|\{#(?:(?!#\}).)*#\})[ \t]*)+")


def _tag_lines(text: str) -> str:
    """A line holding only tags leaves nothing behind: its spaces go and its
    line break moves into a comment (so error line numbers still match).
    Tags in a line with text keep the line as written."""
    lines = (text or "").split("\n")
    out = []
    for i, line in enumerate(lines):
        last = i == len(lines) - 1
        if RX_TAG_LINE.fullmatch(line):
            out.append(line.strip() + ("" if last else "{#\n#}"))
        else:
            out.append(line + ("" if last else "\n"))
    return "".join(out)
ENV.filters.update(FILTERS)


class TemplateError(Exception):
    """A template that can't be read or run: the message says where."""


def compile_template(text: str) -> jinja2.Template:
    try:
        return ENV.from_string(_tag_lines(text))
    except jinja2.TemplateSyntaxError as e:
        raise TemplateError(f"line {e.lineno}: {e.message}") from None


# A value still holding the # placeholder (job.filament#.name): the
# number is to be typed in. Not a # inside a filter's text (|toolhead('T#')).
RX_PLACEHOLDER = re.compile(r"(?<![\w'\"#])[a-z_]+\d*(?:\.\w+)*#[\w.]*")


def check(text: str) -> Optional[str]:
    """None, or what's wrong with a template (for the page and on save)."""
    try:
        compile_template(text)
    except TemplateError as e:
        return str(e)
    return None


def render(text: str, context: Dict[str, Any]) -> str:
    """The template's text. Raises TemplateError on a mistake."""
    return render_vars(text, context)[0]


def render_vars(text: str, context: Dict[str, Any]
                ) -> Tuple[str, Dict[str, Any]]:
    """The template's text, and the variables it set at its top level
    ({% set priority = "max" %}, also inside an if). Raises TemplateError
    on a mistake."""
    tpl = compile_template(text)
    ctx = tpl.new_context(dict(context))
    try:
        out: List[str] = []
        size = 0
        try:
            for piece in tpl.root_render_func(ctx):
                out.append(piece)
                size += len(piece)
                if size > MAX_OUTPUT:
                    raise TemplateError(
                        f"longer than {MAX_OUTPUT} characters")
        except TemplateError:
            raise
        except Exception:
            # Jinja's own: the error as raised at the template's line
            tpl.environment.handle_exception()
        text = "".join(out)
    except TemplateError:
        raise
    except jinja2.TemplateError as e:
        line = _error_line(e)
        raise TemplateError(f"{line}{e}") from None
    except Exception as e:
        line = _error_line(e)
        raise TemplateError(f"{line}{type(e).__name__}: {e}") from None
    return text.strip("\n"), {k: ctx.vars[k] for k in ctx.exported_vars
                               if k in ctx.vars}


def _error_line(e: BaseException) -> str:
    tb = e.__traceback__
    line = None
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == "<template>":
            line = tb.tb_lineno
        tb = tb.tb_next
    return f"line {line}: " if line else ""


def prune(value: Any) -> Any:
    """Data for a template: None values left out, so they read n/a, are
    false and take |default()."""
    if isinstance(value, dict):
        return {k: prune(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [prune(v) for v in value]
    return value


# ---- what a template reads --------------------------------------------------

def chains(text: str) -> List[Tuple[str, List[str]]]:
    """The names a template reads, as (root, path) with fixed keys:
    printer.extruder1.temperature -> ("printer", ["extruder1",
    "temperature"]); printer["temperature_sensor cavity"].temperature ->
    ("printer", ["temperature_sensor cavity", "temperature"]). A name
    read with a computed key ends where the key is computed."""
    try:
        ast = ENV.parse(_tag_lines(text))
    except jinja2.TemplateSyntaxError:
        return []
    found: List[Tuple[str, List[str]]] = []
    seen: Set[int] = set()

    def walk(node: nodes.Node) -> None:
        if isinstance(node, (nodes.Getattr, nodes.Getitem)) and \
                id(node) not in seen:
            path: List[str] = []
            cur: nodes.Node = node
            while isinstance(cur, (nodes.Getattr, nodes.Getitem)):
                seen.add(id(cur))
                if isinstance(cur, nodes.Getattr):
                    path.insert(0, cur.attr)
                elif isinstance(cur.arg, nodes.Const):
                    path.insert(0, str(cur.arg.value))
                else:
                    path = []           # a computed key: from here on
                    walk(cur.arg)
                cur = cur.node
            if isinstance(cur, nodes.Name) and cur.name in ROOTS:
                found.append((cur.name, path))
            else:
                walk(cur)
            return
        if isinstance(node, nodes.Name) and node.name in ROOTS and \
                node.ctx == "load":
            found.append((node.name, []))
            return
        for child in node.iter_child_nodes():
            walk(child)

    walk(ast)
    return found


def printer_needs(text: str) -> Dict[str, Optional[List[str]]]:
    """The Klipper objects (and fields) a template reads from printer.*:
    one Moonraker query fills them. None: the whole object."""
    need: Dict[str, Optional[List[str]]] = {}
    for root, path in chains(text):
        if root != "printer":
            continue
        if not path:
            need["*"] = None            # printer itself, iterated
            continue
        obj = path[0]
        if len(path) == 1:
            need[obj] = None
            continue
        fields = need.setdefault(obj, [])
        if fields is not None and path[1] not in fields:
            fields.append(path[1])
    return need


def comments(text: str) -> List[str]:
    """The template's {# comments #}, for the page."""
    return [m.strip() for m in re.findall(r"\{#(.*?)#\}", text or "", re.S)]
