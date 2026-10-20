"""Preserve mathematical structure from retained, rendered publisher HTML."""
import re
from html import unescape

from bs4 import NavigableString, Tag


def reading_text(node, in_math: bool = False, *, tex: bool = False) -> str:
    """Use explicit text notation for scripts/fractions instead of flattening them.

    This consumes DOM markup only, never speech labels or model-generated text.
    CHTML script layout is upper row, spacer, lower row; tensor columns use the
    same order. Unknown mathematical structures fail rather than losing meaning.
    """
    if isinstance(node, NavigableString):
        return _tex_token(str(node)) if tex else str(node)
    if not isinstance(node, Tag):
        return ''
    name = node.name
    in_math = in_math or name == 'math'
    children = list(node.children)
    elements = list(node.find_all(recursive=False))
    text = lambda n: reading_text(n, in_math, tex=tex)
    joined = lambda: ''.join(text(n) for n in children)
    if name == 'mjx-container':
        # Rendered mathematics is the direct mjx-math child. Explorer help,
        # speech and accessibility widgets beside it are not source content.
        maths = node.find_all('mjx-math', recursive=False)
        if len(maths) != 1:
            raise ValueError('Missing or ambiguous rendered MathJax content')
        if any(not glyph.get_text() for glyph in maths[0].select('mjx-c')):
            # MathJax 3 can draw CSS glyphs with empty text nodes. Its retained
            # explicit MathML is source structure; a glyph class or speech label
            # alone is not enough to reconstruct missing mathematical text.
            assistive = node.find('mjx-assistive-mml', recursive=False)
            # Some retained publisher pages have repeatedly rendered the
            # assistive subtree. Accept only its single explicit original.
            originals = assistive.find_all('math') if assistive else []
            if len(originals) != 1:
                raise ValueError('Empty MathJax glyphs without explicit MathML source')
            return text(originals[0])
        return text(maths[0])
    if name in ('sup', 'sub'):
        return ('^' if name == 'sup' else '_') + '{' + joined() + '}'
    if in_math:
        if name == 'private-char':
            # Publisher font-specific characters are still source text. Keep
            # their Unicode value without guessing a standard mathematical
            # symbol; a missing font must not block the entire issue import.
            value = unescape(node.get_text())
            if not value.strip():
                raise ValueError('Empty publisher private character')
            return _tex_token(value) if tex else value
        if name == 'mstyle':
            allowed = {'mathvariant', 'indentshift', 'indentshiftlast'}
            if not node.attrs or set(node.attrs) - allowed:
                raise ValueError('Unsupported MathML style attributes')
            variant = node.get('mathvariant')
            if variant is not None and variant != 'normal':
                raise ValueError('Unsupported MathML style attributes')
            for attribute in ('indentshift', 'indentshiftlast'):
                value = node.get(attribute)
                if value is not None and not re.fullmatch(
                    r'[+-]?(?:0|(?:\d+(?:\.\d*)?|\.\d+)(?:em|ex|px|pt|pc|in|cm|mm|%))', value
                ):
                    raise ValueError(f'Unsupported MathML style length: {attribute}={value!r}')
            # Indentation positions the displayed equation; it does not change
            # its mathematical children. Preserve their full structured render.
            rendered = joined()
            return r'\mathrm{' + rendered + '}' if tex and variant == 'normal' else rendered
        if name == 'mmultiscripts':
            if not elements or elements[0].name in ('none', 'mprescripts'):
                raise ValueError('Unsupported MathML tensor base')
            groups = [[], []]
            side = 0
            for element in elements[1:]:
                if element.name == 'mprescripts':
                    if side or element.contents:
                        raise ValueError('Unsupported MathML tensor separator')
                    side = 1
                else:
                    groups[side].append(element)

            def pairs(group):
                if len(group) % 2:
                    raise ValueError('Unsupported MathML tensor script pairs')
                result = []
                for lower, upper in zip(group[::2], group[1::2]):
                    scripts = ''
                    for marker, element in (('_', lower), ('^', upper)):
                        if element.name == 'none':
                            if element.contents:
                                raise ValueError('Nonempty MathML tensor placeholder')
                        else:
                            scripts += marker + '{' + text(element) + '}'
                    result.append(scripts)
                return result

            post, pre = (pairs(group) for group in groups)
            # MathML prescript pairs are ordered from nearest to farthest.
            prefix = ''.join('{}' + pair for pair in reversed(pre))
            suffix = ''.join(pair if i == 0 else '{}' + pair
                             for i, pair in enumerate(post))
            return prefix + '{' + text(elements[0]) + '}' + suffix
        if name in ('msup', 'msub') and len(elements) == 2:
            return text(elements[0]) + ('^' if name == 'msup' else '_') + '{' + text(elements[1]) + '}'
        if name == 'msubsup' and len(elements) == 3:
            return text(elements[0]) + '_{' + text(elements[1]) + '}^{' + text(elements[2]) + '}'
        if name == 'mfrac' and len(elements) == 2:
            return (r'\frac{' + text(elements[0]) + '}{' + text(elements[1]) + '}'
                    if tex else '(' + text(elements[0]) + ')/(' + text(elements[1]) + ')')
        if name == 'msqrt':
            return r'\sqrt{' + joined() + '}' if tex else '√(' + joined() + ')'
        if name == 'mfenced':
            separators = ''.join(node.get('separators', ',').split())
            contents = ''.join((separators[min(i - 1, len(separators) - 1)]
                                if i and separators else '') + text(e)
                               for i, e in enumerate(elements))
            return node.get('open', '(') + contents + node.get('close', ')')
        if name in ('mover', 'munder') and len(elements) == 2:
            base, mark = text(elements[0]), text(elements[1])
            operator = 'overset' if name == 'mover' else 'underset'
            if tex:
                accent = {'̃': 'tilde', '~': 'tilde', '¯': 'bar', '̄': 'bar',
                          '^': 'hat', '̂': 'hat', '→': 'vec'}.get(mark)
                if name == 'mover' and node.get('accent') == 'true' and accent:
                    return '\\' + accent + '{' + base + '}'
                return '\\' + operator + '{' + mark + '}{' + base + '}'
            return operator + '(' + mark + ', ' + base + ')'
        if name == 'munderover' and len(elements) == 3:
            base, under, over = (text(e) for e in elements)
            if tex:
                return r'\overset{' + over + r'}{\underset{' + under + '}{' + base + '}}'
            return 'overset(' + over + ', underset(' + under + ', ' + base + '))'
        if name == 'semantics' and elements:
            return text(elements[0])
        if name not in ('math', 'mrow', 'mi', 'mn', 'mo', 'mtext', 'mspace'):
            raise ValueError(f'Unsupported MathML structure: {name}')
    if name in ('mjx-speech', 'mjx-none', 'mjx-spacer', 'mjx-break',
                'mjx-nstrut', 'mjx-dstrut', 'mjx-line'):
        return ''
    if name == 'mjx-lazy':
        raise ValueError('Unrendered math in reading text')
    if name == 'mjx-mspace':
        if elements or node.get_text().strip():
            raise ValueError('Nonempty MathJax space')

        style = node.get('style', '')
        width = re.search(r'(?:^|;)\s*width:\s*(-?\d+(?:\.\d+)?(?:em|ex|px|pt))\s*(?:;|$)', style)
        if 'width' in style and width is None:
            raise ValueError('Unsupported MathJax space width')
        return (r'\hspace{' + width.group(1) + '}' if width else r'\,') if tex else ' '
    if name == 'mjx-stretchy-h':
        # CHTML builds one glyph from repeated extender pieces. The glyph's
        # Unicode class is authoritative; concatenating pieces repeats accents.
        glyphs = {'mjx-c203E': ('‾', {'mjx-ext'}, {'‾'}),
                  'mjx-c2192': ('→', {'mjx-ext', 'mjx-end'}, {'−', '→'})}
        matches = [glyphs[c] for c in node.get('class', []) if c in glyphs]
        if len(matches) != 1:
            raise ValueError('Unsupported MathJax stretched glyph')
        glyph, parts, chars = matches[0]
        if (not elements or any(e.name not in parts for e in elements)
                or not node.get_text().strip()
                or any(c not in chars for c in node.get_text() if not c.isspace())
                or any(e.name not in parts | {'mjx-spacer'} for e in node.find_all())):
            raise ValueError('Unsupported MathJax stretched glyph pieces')
        if glyph == '→' and not any(e.name == 'mjx-end' and e.get_text() == glyph for e in elements):
            raise ValueError('Missing MathJax arrow head')
        return glyph
    if name in ('mjx-msup', 'mjx-msub'):
        if len(elements) != 2:
            raise ValueError('Unsupported MathJax script layout')
        return text(elements[0]) + ('^' if name == 'mjx-msup' else '_') + '{' + text(elements[1]) + '}'
    if name == 'mjx-msubsup':
        if len(elements) != 2:
            raise ValueError('Unsupported MathJax subsup layout')
        scripts = elements[1].find_all(recursive=False)
        if len(scripts) != 3 or scripts[1].name != 'mjx-spacer':
            raise ValueError('Unsupported MathJax subsup rows')
        return text(elements[0]) + '_{' + text(scripts[2]) + '}^{' + text(scripts[0]) + '}'
    if name == 'mjx-mmultiscripts':
        def column(col):
            rows = col.find_all(recursive=False)
            if len(rows) != 3 or any(r.name != 'mjx-row' for r in rows):
                raise ValueError('Unsupported MathJax tensor rows')
            upper, lower = text(rows[0]), text(rows[2])
            return ('_{' + lower + '}' if lower else '') + ('^{' + upper + '}' if upper else '')
        if (len(elements) != 3
                or elements[0].name != 'mjx-prescripts'
                or elements[1].name not in ('mjx-mrow', 'mjx-mi', 'mjx-mn', 'mjx-mo')
                or elements[2].name != 'mjx-scripts'):
            raise ValueError('Unsupported MathJax tensor layout')
        return column(elements[0]) + '{' + text(elements[1]) + '}' + column(elements[2])
    if name == 'mjx-mfrac':
        num = node.select_one(':scope > mjx-frac > mjx-num')
        den = node.select_one(':scope > mjx-frac > mjx-dbox > mjx-dtable > mjx-row > mjx-den')
        if num is None or den is None:
            raise ValueError('Unsupported MathJax fraction layout')
        return (r'\frac{' + text(num) + '}{' + text(den) + '}'
                if tex else '(' + text(num) + ')/(' + text(den) + ')')
    if name == 'mjx-msqrt':
        box = node.find('mjx-box')
        if box is None:
            raise ValueError('Unsupported MathJax square root layout')
        return r'\sqrt{' + text(box) + '}' if tex else '√(' + text(box) + ')'
    if name in ('mjx-mover', 'mjx-munder'):
        mark = node.find('mjx-over' if name == 'mjx-mover' else 'mjx-under', recursive=False)
        base = node.find('mjx-base', recursive=False)
        if mark is None or base is None:
            raise ValueError('Unsupported MathJax accent layout')
        operator = 'overset' if name == 'mjx-mover' else 'underset'
        if tex:
            if name == 'mjx-mover' and text(mark) in ('‾', '→'):
                return ('\\overline{' if text(mark) == '‾' else '\\vec{') + text(base) + '}'
            return '\\' + operator + '{' + text(mark) + '}{' + text(base) + '}'
        return operator + '(' + text(mark) + ', ' + text(base) + ')'
    wrappers = {'mjx-container', 'mjx-math', 'mjx-mrow', 'mjx-mi', 'mjx-mo',
                'mjx-mn', 'mjx-mtext', 'mjx-c', 'mjx-utext', 'mjx-script',
                'mjx-row', 'mjx-cell', 'mjx-num', 'mjx-den', 'mjx-box',
                'mjx-over', 'mjx-under', 'mjx-base'}
    if name.startswith('mjx-') and name not in wrappers:
        raise ValueError(f'Unsupported MathJax structure: {name}')
    return joined()


def _tex_token(value: str) -> str:
    import unicodedata

    value = unicodedata.normalize('NFKC', value)
    symbols = {'−': '-', '⁢': r'\,', '≤': r'\le ', '≥': r'\ge ',
               '≲': r'\lesssim ', '≳': r'\gtrsim ', '⊙': r'\odot ',
               '×': r'\times ', '±': r'\pm ', '%': r'\%',
               '#': r'\#', '&': r'\&', '$': r'\$'}
    return ''.join(symbols.get(char, char) for char in value)


def reading_markdown(node) -> str:
    """Retain DOM formula boundaries as inline TeX, not guessed prose spans."""
    from copy import deepcopy

    node = deepcopy(node)
    for formula in list(node.select('mjx-container, math, sup, sub')):
        # A parent formula owns all nested scripts; never render them twice.
        if any(parent.name in ('mjx-container', 'math', 'sup', 'sub')
               for parent in formula.parents):
            continue
        formula.replace_with('$' + reading_text(formula, tex=True) + '$')
    return reading_text(node)



def reading_content(node, *, render_math=True, doi='', source='') -> str:
    """Render Abstract/Extract math, retaining literal source text on failure.

    This deliberately does not apply to titles/authors or replace source checks.
    Missing symbols are never inferred, and empty/loading/error-only formulas
    remain failures. The policy is fixed so cached imports use the same choice.
    """
    from copy import deepcopy
    import logging

    node = deepcopy(node)
    for formula in list(node.select('mjx-container, math, sup, sub')):
        if any(p.name in ('mjx-container', 'math', 'sup', 'sub') for p in formula.parents):
            continue
        try:
            rendered = reading_text(formula, tex=render_math)
            if not rendered.strip():
                raise ValueError('Empty source mathematical content')
            replacement = '$' + rendered + '$' if render_math else rendered
        except ValueError as error:
            # Access/loading failures and ambiguous or absent glyph evidence are
            # not mathematical structure errors and must never become success.
            if not str(error).startswith(('Unsupported MathML', 'Unsupported MathJax')):
                raise
            def visually_hidden(element):
                return (element.has_attr('hidden') or bool(re.search(
                    r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', element.get('style', ''), re.I)))

            if visually_hidden(formula):
                raise ValueError('Missing usable literal mathematical source text') from error
            literal = deepcopy(formula)
            if formula.name == 'mjx-container':
                # Only the rendered math owns visible formula text. Sibling
                # explorer/help/assistive content is never a fallback source.
                maths = literal.find_all('mjx-math', recursive=False)
                if len(maths) != 1 or visually_hidden(maths[0]):
                    raise ValueError('Missing usable literal mathematical source text') from error
                literal = maths[0]
            for hidden in list(literal.select(
                'annotation, annotation-xml, script, style, mjx-assistive-mml, '
                'mjx-speech, mjx-assistive, mjx-explorer, mjx-tooltip, .mjx-speech, .MJX_LiveRegion, .MJX_Assistive_MathML, '
                '[hidden], .loading, .js-loading'
            )):
                hidden.decompose()
            for hidden in list(literal.select('[style]')):
                if re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', hidden.get('style', ''), re.I):
                    hidden.decompose()
            value = literal.get_text().strip()
            if (not value or re.search(r'loading|unknown node type|math input error|math processing error', value, re.I)
                    or literal.select_one('mjx-lazy, merror, mjx-merror')):
                raise ValueError('Missing usable literal mathematical source text') from error
            logging.getLogger(__name__).warning(
                'Literal mathematical source retained: doi=%s source=%s error=%s text=%r',
                doi, source, error, value,
            )
            replacement = value
        formula.replace_with(replacement)
    return reading_text(node)

def author_text(node) -> str:
    """Compose explicitly marked single-letter accents in publisher names."""
    from copy import deepcopy
    import unicodedata

    node = deepcopy(node)
    accents = {'~': '\u0303', '̃': '\u0303', '¯': '\u0304', '‾': '\u0304',
               '̄': '\u0304', '^': '\u0302', '̂': '\u0302', '´': '\u0301',
               '́': '\u0301', '`': '\u0300', '̀': '\u0300', '¨': '\u0308',
               '̈': '\u0308', 'ˇ': '\u030c', '̌': '\u030c',
               '˘': '\u0306', '̆': '\u0306'}
    for formula in list(node.select('mjx-container, math')):
        if any(p.name in ('mjx-container', 'math') for p in formula.parents):
            continue
        rendered = reading_text(formula)
        match = re.fullmatch(r'overset\((.), (.)\)', rendered)
        if match and match[1] in accents and match[2].isalpha():
            rendered = unicodedata.normalize('NFC', match[2] + accents[match[1]])
        elif len(rendered) != 1 or not rendered.isalpha():
            raise ValueError('Unsupported mathematical author-name structure')
        formula.replace_with(rendered)
    return unicodedata.normalize('NFC', re.sub(r'\s+', ' ', reading_text(node)).strip())
