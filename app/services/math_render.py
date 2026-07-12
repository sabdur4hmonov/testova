"""
Math renderer for the variants PDF — PARSE, then RENDER (never string-replace).

The DB holds a clean, uniform canonical notation:
    sqrt(expr) | root(n, expr) | base^(exp) | base^exp | (num)/(den) |
    var_sub | whole frac (mixed) | repeating decimal a,(b) | plain text

This module tokenizes that grammar into an AST, converts the AST to LaTeX, and
renders each *structural* math fragment to a cached transparent PNG via
matplotlib mathtext, returned as an inline ReportLab <img> tag. Prose stays as
real (wrapping) text.

THE ONE INVIOLABLE RULE — never change mathematical meaning:
  - The full exponent is ONE node (2^21 is the exponent 21, never "2" + "1").
  - A repeating decimal a,(b) (e.g. 4,(2)) is an ATOMIC literal, never a power.
  - If ANY part does not parse cleanly, that segment is emitted VERBATIM as
    text. Correct-but-ugly always beats pretty-but-wrong. The whole entry
    point is wrapped so a failure can only ever fall back to plain text.

The banned approach (regex/string swaps over ASCII math) already corrupted
"2^21"->"2²1" and "4,(2)"->"4²"; it is NOT used here.
"""
from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path

from app.utils.logging import get_logger

logger = get_logger(__name__)

# matplotlib is an optional runtime dep: if it is missing, we degrade to
# verbatim text (never crash the PDF build).
try:
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.font_manager import FontProperties
    matplotlib.rcParams["mathtext.fontset"] = "dejavusans"  # match our prose font
    _MPL_OK = True
except Exception as _e:  # pragma: no cover - only when dep missing
    _MPL_OK = False
    logger.warning("matplotlib_unavailable", error=str(_e))

# Bump _CACHE_VERSION whenever the AST→LaTeX or the PNG rendering changes, so a
# stale image from an older build can NEVER be served (it is baked into both the
# cache dir name and the per-image key).
_CACHE_VERSION = "v2"
_CACHE_DIR = Path(tempfile.gettempdir()) / f"testova_math_cache_{_CACHE_VERSION}"
_FONT_PT = 12.0      # a touch larger than the 10pt body so fractions stay legible
_RENDER_DPI = 300    # crisp for print; displayed back at natural point size


# ── escape (local, to avoid importing pdf_generator) ─────────────────────────

def _esc(text: str) -> str:
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


# ── AST ──────────────────────────────────────────────────────────────────────

class Node:
    structural = False  # True => worth rendering as a typeset image

    def latex(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


class Seq(Node):
    def __init__(self, items: list[Node]):
        self.items = items

    @property
    def structural(self) -> bool:
        return any(i.structural for i in self.items)

    def latex(self) -> str:
        return "".join(i.latex() for i in self.items)


class Text(Node):
    """Verbatim run: a number, variable, operator or symbol — no structure."""
    def __init__(self, latex: str):
        self._latex = latex

    def latex(self) -> str:
        return self._latex


class Sup(Node):
    structural = True

    def __init__(self, base: Node, exp: Node):
        self.base, self.exp = base, exp

    def latex(self) -> str:
        return "{%s}^{%s}" % (self.base.latex(), self.exp.latex())


class Sub(Node):
    structural = True

    def __init__(self, base: Node, idx: Node):
        self.base, self.idx = base, idx

    def latex(self) -> str:
        return "{%s}_{%s}" % (self.base.latex(), self.idx.latex())


class Paren(Node):
    """A parenthesised sub-expression. Kept visible normally, but UNWRAPPED
    when it is a fraction/script operand ((5)/(3) → 5/3, not (5)/(3))."""
    def __init__(self, inner: Node):
        self.inner = inner

    @property
    def structural(self) -> bool:
        return self.inner.structural

    def latex(self) -> str:
        return "\\left(%s\\right)" % self.inner.latex()


def _unwrap(node: Node) -> Node:
    return node.inner if isinstance(node, Paren) else node


class Frac(Node):
    structural = True

    def __init__(self, num: Node, den: Node):
        self.num, self.den = _unwrap(num), _unwrap(den)

    def latex(self) -> str:
        return "\\frac{%s}{%s}" % (self.num.latex(), self.den.latex())


class Mixed(Node):
    structural = True

    def __init__(self, whole: str, num: str, den: str):
        self.whole, self.num, self.den = whole, num, den

    def latex(self) -> str:
        return "%s\\frac{%s}{%s}" % (self.whole, self.num, self.den)


class Sqrt(Node):
    structural = True

    def __init__(self, expr: Node):
        self.expr = expr

    def latex(self) -> str:
        return "\\sqrt{%s}" % self.expr.latex()


class Root(Node):
    structural = True

    def __init__(self, index: Node, expr: Node):
        self.index, self.expr = index, expr

    def latex(self) -> str:
        return "\\sqrt[%s]{%s}" % (self.index.latex(), self.expr.latex())


# ── tokenizer ────────────────────────────────────────────────────────────────

# Repeating decimal FIRST (atomic) so it can never be read as a power/fraction.
_REPEATING = re.compile(r"\d+,\(\d+\)")
_NUMBER = re.compile(r"\d+(?:,\d+)?")
_WORD = re.compile(r"[A-Za-z]+")

_FUNCS = {"sqrt", "root"}
# multi-letter function names that are still MATH (not prose)
_NAMED = {
    "sin": "\\sin", "cos": "\\cos", "tg": "\\mathrm{tg}", "ctg": "\\mathrm{ctg}",
    "cot": "\\cot", "tan": "\\tan", "log": "\\log", "ln": "\\ln",
    "lim": "\\lim", "arcsin": "\\arcsin", "arccos": "\\arccos",
    "arctg": "\\mathrm{arctg}",
}

# operators / symbols allowed INSIDE a structural run → LaTeX.
_SYMS = {
    "+": "+", "-": "-", "−": "-", "*": "\\cdot ", "·": "\\cdot ", "⋅": "\\cdot ",
    "÷": "\\div ",
    "=": "=", "<": "<", ">": ">", "≤": "\\leq ", "≥": "\\geq ", "≠": "\\neq ",
    "≈": "\\approx ", "±": "\\pm ", "∞": "\\infty ", "°": "^{\\circ}",
    "∈": "\\in ", "∉": "\\notin ", "∅": "\\emptyset ", "∪": "\\cup ",
    "∩": "\\cap ", "⊂": "\\subset ", "⊆": "\\subseteq ", "→": "\\rightarrow ",
    "∠": "\\angle ", "π": "\\pi ", "α": "\\alpha ", "β": "\\beta ",
    "γ": "\\gamma ", "δ": "\\delta ", "θ": "\\theta ", "λ": "\\lambda ",
    "μ": "\\mu ", "σ": "\\sigma ", "ω": "\\omega ", "φ": "\\varphi ",
    "ψ": "\\psi ", ":": ":", ";": ";", ",": "{,}", "|": "\\mid ",
    "!": "!", "%": "\\%", ".": ".",
}
_LETTER_SET = "ℝℕℤℚ"  # blackboard set letters as themselves (dejavusans has them)


class _Tok:
    __slots__ = ("kind", "val")

    def __init__(self, kind: str, val: str):
        self.kind, self.val = kind, val


class BailOut(Exception):
    """A token/character we will not parse — caller falls back to verbatim."""


def _tokenize_all(s: str) -> list[_Tok]:
    """Tokenize the WHOLE field. Never raises: a real prose word or an unknown
    character becomes a 'prose' token (a segment boundary), so math runs are
    cleanly separated from surrounding Uzbek text."""
    toks: list[_Tok] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            toks.append(_Tok("sp", c))
            i += 1
            continue
        m = _REPEATING.match(s, i)
        if m:
            toks.append(_Tok("rep", m.group()))
            i = m.end()
            continue
        m = _NUMBER.match(s, i)
        if m:
            toks.append(_Tok("num", m.group()))
            i = m.end()
            continue
        m = _WORD.match(s, i)
        if m:
            w = m.group()
            prev = s[i - 1] if i > 0 else ""
            nxt = s[m.end()] if m.end() < n else ""
            # A multi-letter run is a math atom (a variable product / unit) ONLY
            # when it is glued to math: a following super/subscript ("dm^2") OR
            # a leading coefficient / closing paren ("2mn", "3ab", ")xy"). A run
            # sitting on its own with spaces around it is an Uzbek word (va, ga,
            # bo, ko, ...) and MUST end the math run.
            glued = nxt in ("^", "_") or prev.isdigit() or prev == ")"
            if w in _FUNCS:
                toks.append(_Tok("func", w))
            elif w in _NAMED:
                toks.append(_Tok("named", w))
            elif len(w) == 1:
                toks.append(_Tok("var", w))  # single letter = variable
            elif glued:
                toks.append(_Tok("var", w))
            else:
                toks.append(_Tok("prose", w))  # real word → boundary
            i = m.end()
            continue
        if c in "()":
            toks.append(_Tok(c, c))
            i += 1
            continue
        if c in "{}":
            toks.append(_Tok("brace", c))
            i += 1
            continue
        if c in "^_/":
            toks.append(_Tok(c, c))
            i += 1
            continue
        if c == ",":
            toks.append(_Tok("comma", c))
            i += 1
            continue
        if c in _LETTER_SET:
            toks.append(_Tok("var", c))
            i += 1
            continue
        if c in _SYMS:
            toks.append(_Tok("sym", c))
            i += 1
            continue
        toks.append(_Tok("prose", c))  # unknown char → boundary (never bail)
        i += 1
    return toks


# ── recursive-descent parser (operates on ONE math run, no prose) ────────────

class _Parser:
    def __init__(self, toks: list[_Tok]):
        self.toks = toks
        self.i = 0

    def _peek(self, k: int = 0) -> _Tok | None:
        j = self.i + k
        return self.toks[j] if 0 <= j < len(self.toks) else None

    def _next(self) -> _Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def _skip_sp(self) -> None:
        while self._peek() and self._peek().kind == "sp":
            self.i += 1

    def parse(self) -> Seq:
        seq = Seq(self._sequence(stop=set()))
        if self._peek() is not None:  # leftover (e.g. unbalanced ')')
            raise BailOut("trailing tokens")
        return seq

    def _sequence(self, stop: set[str]) -> list[Node]:
        items: list[Node] = []
        while True:
            t = self._peek()
            if t is None or t.kind in stop:
                break
            items.append(self._term(stop))
        return items

    def _term(self, stop: set[str]) -> Node:
        node = self._factor(stop)
        while True:
            t = self._peek()
            if t is None or t.kind in stop:
                break
            if t.kind == "^":
                self._next()
                node = Sup(node, self._script_operand())
            elif t.kind == "_":
                self._next()
                node = Sub(node, self._script_operand())
            elif t.kind == "/":
                self._next()
                self._skip_sp()
                node = Frac(node, self._factor(stop))
            else:
                break
        return node

    def _group(self, open_kind: str, close_kind: str) -> Seq:
        self._next()  # opening
        inner = Seq(self._sequence(stop={close_kind}))
        if not self._peek() or self._peek().kind != close_kind:
            raise BailOut("unclosed " + open_kind)
        self._next()  # closing
        return inner

    def _script_operand(self) -> Node:
        """Operand of ^ or _ : a (...) / {...} group, or ONE whole atom.
        A number token is taken WHOLE (2^21 -> exponent 21, never 2 then 1)."""
        t = self._peek()
        if t is None:
            raise BailOut("dangling script")
        if t.kind == "(":
            return self._group("(", ")")
        if t.kind == "brace":
            return self._group("brace", "brace")
        if t.kind in ("num", "rep"):
            return Text(_num_latex(self._next().val))
        if t.kind == "var":
            return Text(self._next().val)
        if t.kind == "sym" and t.val in ("+", "-", "−"):
            sign = _SYMS[self._next().val]  # signed exponent 10^-3
            return Seq([Text(sign), self._script_operand()])
        raise BailOut("bad script operand")

    def _factor(self, stop: set[str]) -> Node:
        t = self._peek()
        if t is None:
            raise BailOut("empty factor")
        k = t.kind
        if k == "sp":
            self._next()
            return Text(" ")
        if k == "func":
            return self._func_call()
        if k == "named":
            return Text(_NAMED[self._next().val] + " ")
        if k == "(":
            return Paren(self._group("(", ")"))  # visible unless a frac operand
        if k == "brace":
            return self._group("brace", "brace")
        if k == "num":
            # mixed number: NUM sp NUM / NUM
            if (self._peek(1) and self._peek(1).kind == "sp"
                    and self._peek(2) and self._peek(2).kind == "num"
                    and self._peek(3) and self._peek(3).kind == "/"
                    and self._peek(4) and self._peek(4).kind == "num"):
                whole = self._next().val
                self._next()  # sp
                num = self._next().val
                self._next()  # /
                den = self._next().val
                return Mixed(whole, num, den)
            return Text(_num_latex(self._next().val))
        if k == "rep":
            return Text(_num_latex(self._next().val))
        if k == "var":
            return Text(self._next().val)
        if k == "comma":
            self._next()
            return Text("{,}")
        if k == "sym":
            return Text(_SYMS[self._next().val])
        raise BailOut("unparsed factor: " + k)

    def _func_call(self) -> Node:
        name = self._next().val  # sqrt | root
        self._skip_sp()
        if not self._peek() or self._peek().kind != "(":
            raise BailOut("func without args")
        self._next()  # (
        args: list[Seq] = [Seq(self._sequence(stop={"comma", ")"}))]
        while self._peek() and self._peek().kind == "comma":
            self._next()
            self._skip_sp()
            args.append(Seq(self._sequence(stop={"comma", ")"})))
        if not self._peek() or self._peek().kind != ")":
            raise BailOut("unclosed func")
        self._next()  # )
        if name == "sqrt":
            if len(args) != 1:
                raise BailOut("sqrt arity")
            return Sqrt(args[0])
        if name == "root":
            if len(args) != 2:
                raise BailOut("root arity")
            return Root(args[0], args[1])
        raise BailOut("unknown func")


def _num_latex(v: str) -> str:
    # keep Uzbek decimal comma tight, and repeating decimals verbatim
    return v.replace(",", "{,}")


def parse(fragment: str) -> Seq:
    """Parse a math run (no prose) into an AST. Raises BailOut if unrecognised."""
    return _Parser(_tokenize_all(fragment)).parse()


# ── segmentation: split prose vs math runs (token-based) ─────────────────────

_MATH_KINDS = {
    "rep", "num", "func", "named", "var", "(", ")", "brace",
    "^", "_", "/", "comma", "sym",
}


def _emit_run(cur: list[_Tok], segments: list[tuple[str, object]]) -> None:
    """Flush a buffer of math+space tokens. Edge spaces become prose so we
    never imageify padding; the middle becomes a math run to be parsed."""
    a, b = 0, len(cur)
    while a < b and cur[a].kind == "sp":
        a += 1
    while b > a and cur[b - 1].kind == "sp":
        b -= 1
    if a > 0:
        segments.append(("prose", "".join(t.val for t in cur[:a])))
    if b > a:
        segments.append(("run", cur[a:b]))
    if b < len(cur):
        segments.append(("prose", "".join(t.val for t in cur[b:])))


def _iter_segments(text: str):
    """Yield (is_math, payload). For prose: payload is the text string. For a
    math run: payload is (ast, src) — but only when the run parses cleanly AND
    carries real structure; otherwise it degrades to verbatim prose."""
    toks = _tokenize_all(text)
    segments: list[tuple[str, object]] = []
    cur: list[_Tok] = []
    for t in toks:
        if t.kind == "prose":
            _emit_run(cur, segments)
            cur = []
            if segments and segments[-1][0] == "prose":
                segments[-1] = ("prose", segments[-1][1] + t.val)
            else:
                segments.append(("prose", t.val))
        else:
            cur.append(t)
    _emit_run(cur, segments)

    for kind, payload in segments:
        if kind == "prose":
            yield False, payload
            continue
        run_toks = payload  # list[_Tok]
        src = "".join(t.val for t in run_toks)
        try:
            ast = _Parser(list(run_toks)).parse()
        except BailOut:
            yield False, src
            continue
        if ast.structural:
            yield True, (ast, src)
        else:
            yield False, src


# ── matplotlib rendering (cached) ────────────────────────────────────────────

_mem_cache: dict[str, tuple[str, float, float, float]] = {}


def _render_png(latex: str) -> tuple[str, float, float, float] | None:
    """Render `$latex$` to a cached transparent PNG. Returns
    (path, width_pt, height_pt, depth_pt) or None on failure."""
    if not _MPL_OK:
        return None
    if latex in _mem_cache:
        return _mem_cache[latex]
    key = hashlib.sha1(
        f"{_CACHE_VERSION}|{_FONT_PT}|{latex}".encode("utf-8")
    ).hexdigest()[:16]
    path = _CACHE_DIR / f"m_{key}.png"
    prop = FontProperties(size=_FONT_PT)
    try:
        fig = Figure()
        canvas = FigureCanvasAgg(fig)
        r = canvas.get_renderer()
        w_px, h_px, d_px = r.get_text_width_height_descent(
            f"${latex}$", prop, ismath=True
        )
        to_pt = 72.0 / r.dpi
        w_pt, h_pt, d_pt = w_px * to_pt, h_px * to_pt, d_px * to_pt
        if not path.exists():
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)
            fig2 = Figure(figsize=(max(w_px, 1) / r.dpi, max(h_px, 1) / r.dpi),
                          dpi=_RENDER_DPI)
            FigureCanvasAgg(fig2)
            fig2.patch.set_alpha(0.0)
            fig2.text(
                0.0, d_px / h_px if h_px else 0.0, f"${latex}$",
                fontsize=_FONT_PT, color="black", ha="left", va="baseline",
            )
            fig2.savefig(path, transparent=True, dpi=_RENDER_DPI,
                         bbox_inches=None, pad_inches=0.0)
        result = (path.as_posix(), w_pt, h_pt, d_pt)
        _mem_cache[latex] = result
        return result
    except Exception as e:
        logger.info("mathtext_render_failed", latex=ascii(latex[:60]), error=str(e))
        return None


def _img_markup(latex: str) -> str | None:
    got = _render_png(latex)
    if got is None:
        return None
    path, w_pt, h_pt, d_pt = got
    # valign = rise: drop the descent below the text baseline
    return (
        f'<img src="{path}" width="{w_pt:.2f}" height="{h_pt:.2f}" '
        f'valign="{-d_pt:.2f}"/>'
    )


# ── public entry point ───────────────────────────────────────────────────────

def render_to_markup(text: str | None) -> str:
    """
    Turn a stem/option/context string into ReportLab Paragraph markup:
    prose stays as escaped (wrapping) text; each structural math run becomes an
    inline typeset <img>. TOTALLY bail-safe — any failure falls back to the
    verbatim escaped text so meaning is never altered.
    """
    if not text:
        return ""
    try:
        out: list[str] = []
        for is_math, payload in _iter_segments(text):
            if not is_math:
                out.append(_esc(payload))
                continue
            ast, src = payload
            markup = _img_markup(ast.latex())
            out.append(markup if markup is not None else _esc(src))
        return "".join(out)
    except Exception as e:  # absolute safety net
        logger.info("render_to_markup_fallback", error=str(e))
        return _esc(text)
