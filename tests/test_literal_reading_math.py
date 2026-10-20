import logging

from bs4 import BeautifulSoup
import pytest

from papertrack.reports.reading import reading_content, reading_markdown


def node(html):
    return BeautifulSoup(html, 'lxml')


@pytest.mark.parametrize('math, literal', [
    ('<mover accent="true"><mi>r</mi></mover>', 'r'),
    ('<mfrac><mi>x</mi></mfrac>', 'x'),
    ('<mtable><mtr><mtd><mi>A</mi></mtd></mtr><mtr><mtd><mi>B</mi></mtd></mtr></mtable>', 'AB'),
])
@pytest.mark.parametrize('render_math', [True, False])
def test_only_broken_formula_uses_literal_source_preserving_prose(math, literal, render_math, caplog):
    html = f'<p>Before <math>{math}</math> between <math><msub><mi>x</mi><mi>i</mi></msub></math> after.</p>'
    original = node(html)
    with caplog.at_level(logging.WARNING):
        result = reading_content(original, render_math=render_math, doi='10.1063/example', source='capture.json.html')
    normal = '$x_{i}$' if render_math else 'x_{i}'
    assert result == f'Before {literal} between {normal} after.'
    assert str(original) == str(node(html))
    assert '10.1063/example' in caplog.text and 'capture.json.html' in caplog.text
    assert 'Literal mathematical source retained' in caplog.text
    with pytest.raises(ValueError):
        reading_markdown(original)  # Titles and strict renderer are unchanged.


def test_hidden_annotation_and_help_do_not_enter_literal_formula():
    html = '<p><math><mover><mi>r</mi></mover><annotation>guessed hat r</annotation><mjx-speech>speech duplicate</mjx-speech><span hidden>help</span><span style="display:none">hidden</span></math></p>'
    assert reading_content(node(html)) == 'r'


@pytest.mark.parametrize('math', [
    '<mover><mi></mi></mover>',
    '<mover><mi>Loading...</mi></mover>',
    '<mover><mi>Unknown node type private-char</mi></mover>',
    '<merror><mtext>Math input error</mtext></merror>',
    '<mover><annotation>Only hidden annotation</annotation></mover>',
])
def test_absent_or_error_formula_text_still_fails(math):
    with pytest.raises(ValueError):
        reading_content(node(f'<p>Valid prose <math>{math}</math></p>'))


def test_mathjax_fallback_does_not_duplicate_assistive_or_speech_text():
    html = ('<p>Before <mjx-container><mjx-math><mjx-mfrac><mjx-num>r</mjx-num></mjx-mfrac></mjx-math>'
            '<mjx-assistive-mml><math><annotation>Duplicate r</annotation></math></mjx-assistive-mml>'
            '<mjx-speech>Speech r</mjx-speech><mjx-explorer>Help r</mjx-explorer>'
            '</mjx-container> after.</p>')
    assert reading_content(node(html)) == 'Before r after.'


def test_visible_aria_hidden_math_is_not_accessibility_help():
    html = ('<p><mjx-container><mjx-math aria-hidden="true"><mjx-mfrac><mjx-num>r</mjx-num></mjx-mfrac></mjx-math>'
            '<span>Outside helper text</span><mjx-assistive-mml aria-hidden="true">Duplicate r</mjx-assistive-mml>'
            '</mjx-container></p>')
    assert reading_content(node(html)) == 'r'


@pytest.mark.parametrize('attributes', ['hidden', 'style="display:none"', 'style="visibility: hidden"'])
@pytest.mark.parametrize('kind', ['math', 'mjx-container', 'mjx-math'])
def test_visually_hidden_literal_root_cannot_supply_formula(attributes, kind):
    if kind == 'math':
        html = f'<math {attributes}><mover><mi>r</mi></mover></math>'
    elif kind == 'mjx-container':
        html = f'<mjx-container {attributes}><mjx-math><mjx-mfrac><mjx-num>r</mjx-num></mjx-mfrac></mjx-math></mjx-container>'
    else:
        html = f'<mjx-container><mjx-math {attributes}><mjx-mfrac><mjx-num>r</mjx-num></mjx-mfrac></mjx-math></mjx-container>'
    with pytest.raises(ValueError, match='Missing usable literal'):
        reading_content(node('<p>' + html + '</p>'))
