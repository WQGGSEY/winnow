"""One bounded mathematical syntax for manuscript previews and TeX export."""
from __future__ import annotations

from functools import lru_cache
import hashlib
from io import BytesIO
import re
from xml.etree import ElementTree as ET

# These commands have the same mathematical meaning in Mathtext and AMS LaTeX.
_COMMANDS = set('''alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta
 iota kappa lambda mu nu xi pi varpi rho varrho sigma varsigma tau upsilon phi
 varphi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
 frac dfrac sqrt left right quad qquad mathbb mathbf mathrm mathcal mathsf mathtt mathit
 mathfrak boldsymbol text operatorname overset underset overline
 langle rangle lvert rvert vert Vert mid ell infty emptyset varnothing partial
 nabla cdot times div otimes oplus leq geq neq approx equiv sim simeq propto
 in notin subset subseteq supset supseteq forall exists neg land lor
 to rightarrow leftarrow leftrightarrow Rightarrow Leftarrow Leftrightarrow
 mapsto uparrow downarrow sum prod int iint oint lim limsup liminf max min sup inf
 log ln exp sin cos tan det gcd Pr hat bar tilde vec dot ddot widehat widetilde
 ldots cdots dots pm mp cup cap setminus perp parallel angle'''.split())
_COMMANDS.update({',', '!', ';', ':', ' ', '{', '}', '|', '%', '#'})
_DELIMITERS = re.compile(r'\\\((.*?)\\\)|\\\[(.*?)\\\]', re.DOTALL)


@lru_cache(maxsize=256)
def validate_math(expression: str) -> None:
    from matplotlib.mathtext import MathTextParser

    if not expression.isascii():
        raise ValueError('Use TeX commands for mathematical symbols, for example \\alpha instead of a literal Greek character.')
    if not expression.strip() or re.search(r'(?<!\\)[%&#$]', expression):
        raise ValueError('Math must be nonempty and cannot contain raw %, &, #, or $.')
    unknown = set(re.findall(r'\\([A-Za-z]+|.)', expression)) - _COMMANDS
    if unknown:
        raise ValueError('Unsupported mathematical commands: ' + ', '.join(sorted(unknown)))
    # This also rejects malformed grouping and unsupported command arguments.
    MathTextParser('path').parse('$' + expression + '$')


def math_parts(text: str) -> list[tuple[str, str]]:
    """Split a text node into prose, inline math and display math."""
    parts = []
    end = 0
    for match in _DELIMITERS.finditer(text):
        parts.append(('text', text[end:match.start()]))
        expression = match.group(1) if match.group(1) is not None else match.group(2)
        validate_math(expression)
        parts.append(('inline' if match.group(1) is not None else 'display', expression))
        end = match.end()
    parts.append(('text', text[end:]))
    if any(kind == 'text' and any(marker in value for marker in (r'\(', r'\)', r'\[', r'\]'))
           for kind, value in parts):
        raise ValueError('Unbalanced math delimiters; keep each formula within one HTML text node.')
    return parts


@lru_cache(maxsize=256)
def math_svg(expression: str, display: bool) -> str:
    from matplotlib import mathtext, rc_context
    from matplotlib.font_manager import FontProperties

    validate_math(expression)
    output = BytesIO()
    with rc_context({'svg.hashsalt': hashlib.sha256(expression.encode()).hexdigest(), 'svg.fonttype': 'path'}):
        mathtext.math_to_image('$' + expression + '$', output, format='svg', prop=FontProperties(size=12))
    namespace = 'http://www.w3.org/2000/svg'
    ET.register_namespace('', namespace)
    ET.register_namespace('xlink', 'http://www.w3.org/1999/xlink')
    root = ET.fromstring(output.getvalue())
    for metadata in root.findall('{' + namespace + '}metadata'):
        root.remove(metadata)
    for dimension in ('width', 'height'):
        root.set(dimension, f"{float(root.attrib[dimension].removesuffix('pt')) / 12:.4f}em")
    root.set('role', 'img')
    root.set('aria-label', expression)
    root.set('data-latex', expression)
    root.set('style', 'display:block;margin:1em auto;max-width:100%' if display else 'vertical-align:middle;max-width:100%')
    return ET.tostring(root, encoding='unicode')
