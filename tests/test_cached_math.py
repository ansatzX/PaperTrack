"""Minimal structures retained from PRL and JCTC failed capture HTML."""
import pytest
from bs4 import BeautifulSoup

from papertrack.reports.reading import reading_markdown, reading_text


def render(markup, markdown=False):
    soup = BeautifulSoup(markup, 'html.parser')
    return (reading_markdown if markdown else reading_text)(soup)


def test_prl_space_keeps_separation_and_explicit_width():
    markup = '<mjx-container><mjx-math>x<mjx-mspace></mjx-mspace>y<mjx-mspace style="width: 1em;"></mjx-mspace>z</mjx-math></mjx-container>'
    assert render(markup) == 'x y z'
    assert render(markup, True) == r'$x\,y\hspace{1em}z$'


@pytest.mark.parametrize('code,pieces,glyph,tex', [
    ('203E', '<mjx-ext class="STX-SY"><mjx-spacer>‾‾‾</mjx-spacer></mjx-ext>', '‾', r'\overline{x}'),
    ('2192', '<mjx-ext class="STX-E"><mjx-spacer>−−−</mjx-spacer></mjx-ext><mjx-end class="STX-VX">→</mjx-end>', '→', r'\vec{x}'),
])
def test_prl_stretched_accents_are_single_glyphs(code, pieces, glyph, tex):
    markup = '<mjx-container><mjx-math><mjx-mover><mjx-over><mjx-stretchy-h class="mjx-c' + code + '">' + pieces + '</mjx-stretchy-h></mjx-over><mjx-base>x</mjx-base></mjx-mover></mjx-math></mjx-container>'
    assert render(markup) == 'overset(' + glyph + ', x)'
    assert render(markup, True) == '$' + tex + '$'


def test_jctc_mathml_spin_prescript_preserves_base_scripts():
    markup = '<math><mmultiscripts><mrow><msubsup><mrow><mi mathvariant="normal">Σ</mi></mrow><mrow><mi>g</mi></mrow><mrow><mo>+</mo></mrow></msubsup></mrow><mprescripts></mprescripts><none></none><mrow><mn>1</mn></mrow></mmultiscripts></math>'
    assert render(markup) == '{}^{1}{Σ_{g}^{+}}'
    assert render(markup, True) == '${}^{1}{Σ_{g}^{+}}$'


def test_mathml_multiple_tensor_pairs_preserve_nearest_first_order():
    markup = '<math><mmultiscripts><mi>T</mi><mi>a</mi><mi>b</mi><mi>c</mi><none/><mprescripts/><mi>i</mi><mi>j</mi><mi>k</mi><none/></mmultiscripts></math>'
    assert render(markup, True) == '${}_{k}{}_{i}^{j}{T}_{a}^{b}{}_{c}$'


def test_prl_boron_nitride_tensor_accepts_direct_mjx_identifier_base():
    # Retained PRL 137/5 HTML for DOI 10.1103/b8n2-tl4n uses a direct mjx-mi
    # tensor base; the empty MathJax script placeholders carry no glyph text.
    markup = '''<mjx-container><mjx-math><mjx-mmultiscripts>
      <mjx-prescripts><mjx-row><mjx-cell><mjx-mrow><mjx-mn>10</mjx-mn></mjx-mrow></mjx-cell></mjx-row>
        <mjx-row></mjx-row><mjx-row><mjx-cell><mjx-none></mjx-none></mjx-cell></mjx-row></mjx-prescripts>
      <mjx-mi>B</mjx-mi>
      <mjx-scripts><mjx-row><mjx-cell><mjx-none></mjx-none></mjx-cell></mjx-row>
        <mjx-row></mjx-row><mjx-row><mjx-cell><mjx-none></mjx-none></mjx-cell></mjx-row></mjx-scripts>
    </mjx-mmultiscripts></mjx-math></mjx-container>'''
    assert render(markup, True) == '$^{10}{B}$'


@pytest.mark.parametrize('markup', [
    '<math><mmultiscripts><mi>x</mi><mi>i</mi></mmultiscripts></math>',
    '<math><mmultiscripts><mi>x</mi><mprescripts/><mprescripts/></mmultiscripts></math>',
    '<math><mmultiscripts><mi>x</mi><none>lost</none><mn>1</mn></mmultiscripts></math>',
    '<mjx-mspace>lost</mjx-mspace>',
    '<mjx-mspace style="width: calc(1em);"/>',
    '<mjx-stretchy-h class="mjx-c9999">?</mjx-stretchy-h>',
    '<mjx-stretchy-h class="mjx-c203E"><mjx-ext>unexpected</mjx-ext></mjx-stretchy-h>',
    '<mjx-stretchy-h class="mjx-c2192"><mjx-ext><mjx-spacer>−−</mjx-spacer></mjx-ext></mjx-stretchy-h>',
])
def test_unknown_or_malformed_structures_still_fail(markup):
    with pytest.raises(ValueError):
        render(markup, True)


@pytest.mark.parametrize('length', ['3em', '-1.5em', '+.5ex', '0', '2px', '12pt', '5%'])
def test_mathml_equation_indentation_preserves_children(length):
    markup = f'<math><mstyle indentshift="{length}" indentshiftlast="3em"><mfrac><mi>x</mi><mi>b...</mi></mfrac><mo>+</mo><mi>y</mi></mstyle></math>'
    assert render(markup, True) == r'$\frac{x}{b...}+y$'
    assert render(markup) == '(x)/(b...)+y'


def test_mathml_normal_variant_with_indentation_stays_upright():
    assert render('<math><mstyle mathvariant="normal" indentshift="3em"><mi>x</mi></mstyle></math>', True) == r'$\mathrm{x}$'


@pytest.mark.parametrize('attributes', [
    'indentshift="calc(3em)"', 'indentshift="nan"', 'indentshift="3"',
    'indentshift="3em garbage"', 'indentshiftlast="auto"',
    'indentshift="3em" mathcolor="red"', 'indentshift="3em" mathvariant="bold"',
])
def test_mathml_indentation_does_not_allow_unknown_style(attributes):
    with pytest.raises(ValueError, match='Unsupported MathML style'):
        render(f'<math><mstyle {attributes}><mi>x</mi></mstyle></math>', True)


def test_mathml_indentation_does_not_hide_unsupported_child():
    with pytest.raises(ValueError, match='Unsupported MathML'):
        render('<math><mstyle indentshift="3em"><mover><mi>x</mi></mover></mstyle></math>', True)
