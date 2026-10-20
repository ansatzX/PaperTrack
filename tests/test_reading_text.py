import pytest
from bs4 import BeautifulSoup

from papertrack.reports.reading import reading_text


def render(html):
    return reading_text(BeautifulSoup(html, 'html.parser'))


@pytest.mark.parametrize('source', ['\ue5f8', '&#xe5f8;', '&amp;#xe5f8;'])
def test_publisher_private_character_is_preserved_without_guessing(source):
    from papertrack.reports.reading import reading_markdown

    html = ('<p>Symmetry <math><mover accent="true"><mn>1</mn>'
            f'<mo><private-char specific-use="loader-generated">{source}</private-char></mo>'
            '</mover></math>.</p>')
    node = BeautifulSoup(html, 'html.parser')
    assert reading_text(node) == 'Symmetry overset(\ue5f8, 1).'
    assert reading_markdown(node) == r'Symmetry $\overset{' + '\ue5f8' + r'}{1}$.'


def test_missing_private_character_still_fails():
    with pytest.raises(ValueError, match='Empty publisher private character'):
        render('<math><private-char specific-use="loader-generated"></private-char></math>')


def test_scripts_do_not_turn_powers_into_decimal_numbers():
    assert render('<mjx-msup><mjx-mrow>10</mjx-mrow><mjx-script>13.9</mjx-script></mjx-msup>') == '10^{13.9}'
    assert render('<mjx-msub><mjx-mrow>M</mjx-mrow><mjx-script>500c</mjx-script></mjx-msub>') == 'M_{500c}'
    assert render('<p>H<sub>2</sub>O and x<sup>2</sup></p>') == 'H_{2}O and x^{2}'


def test_upper_and_lower_rows_keep_their_roles():
    assert render('<mjx-msubsup><mjx-mrow>R</mjx-mrow><mjx-script>'
                  '<mjx-mrow>2l+1</mjx-mrow><mjx-spacer></mjx-spacer>'
                  '<mjx-mrow>S</mjx-mrow></mjx-script></mjx-msubsup>') == 'R_{S}^{2l+1}'


def test_fraction_root_and_accent_preserve_grouping():
    frac = ('<mjx-mfrac><mjx-frac><mjx-num>9</mjx-num><mjx-dbox>'
            '<mjx-dtable><mjx-row><mjx-den>2</mjx-den></mjx-row>'
            '</mjx-dtable></mjx-dbox></mjx-frac></mjx-mfrac>')
    assert render(frac) == '(9)/(2)'
    assert render('<mjx-msqrt><mjx-sqrt><mjx-surd>√</mjx-surd>'
                  '<mjx-box>t+1</mjx-box></mjx-sqrt></mjx-msqrt>') == '√(t+1)'
    assert render('<mjx-mover><mjx-over>¯</mjx-over><mjx-base>F</mjx-base></mjx-mover>') == 'overset(¯, F)'


def test_unrecognized_and_pending_math_fail_visibly():
    for markup in ('<mjx-lazy></mjx-lazy>', '<mjx-mroot>x3</mjx-mroot>'):
        with pytest.raises(ValueError):
            render(markup)


def test_nuclear_prescripts_are_not_appended_to_the_element_name():
    html = ('<mjx-mmultiscripts><mjx-prescripts><mjx-row><mjx-cell>3</mjx-cell>'
            '</mjx-row><mjx-row></mjx-row><mjx-row><mjx-cell>Λ</mjx-cell>'
            '</mjx-row></mjx-prescripts><mjx-mrow>H</mjx-mrow><mjx-scripts>'
            '<mjx-row><mjx-cell><mjx-none></mjx-none></mjx-cell></mjx-row>'
            '<mjx-row></mjx-row><mjx-row><mjx-cell><mjx-none></mjx-none>'
            '</mjx-cell></mjx-row></mjx-scripts></mjx-mmultiscripts>')
    assert render(html) == '_{Λ}^{3}{H}'


def test_publisher_mathml_keeps_radicals_chemical_charge_and_fences():
    assert render('<math><mn>1</mn><mo>/</mo><msqrt><msub><mi>β</mi>'
                  '<mn>1</mn></msub></msqrt></math>') == '1/√(β_{1})'
    assert render('<math><msup><mrow>[<msub><mi>SiO</mi><mn>4</mn></msub>]'
                  '</mrow><mrow>4−</mrow></msup></math>') == '[SiO_{4}]^{4−}'
    assert render('<math><mi>p</mi><mo>=</mo><mi>c</mi><mfenced>'
                  '<mrow>1−c</mrow></mfenced></math>') == 'p=c(1−c)'


def test_mathjax_explorer_widgets_are_not_abstract_text():
    assert render('<mjx-container><mjx-math><mjx-mi>x</mjx-mi></mjx-math>'
                  '<mjx-help><svg>help text</svg></mjx-help>'
                  '<mjx-speech>spoken duplicate</mjx-speech></mjx-container>') == 'x'
    with pytest.raises(ValueError, match='Missing or ambiguous'):
        render('<mjx-container><mjx-lazy></mjx-lazy></mjx-container>')


def test_cached_aip_mathml_accent_renders_without_refetch():
    from papertrack.reports.reading import reading_markdown
    soup = BeautifulSoup('<p>Use <math><mover accent="true"><mi>G</mi><mo>̃</mo></mover></math>.</p>', 'html.parser')
    assert reading_markdown(soup) == r'Use $\tilde{G}$.'

@pytest.mark.parametrize('base,mark,expected', [('n','~','ñ'),('u','¯','ū'),('a','˘','ă')])
def test_author_accents_are_source_composed_not_split_lines(base, mark, expected):
    from papertrack.reports.reading import author_text
    html = f'<span>Ab<math><mover accent="true"><mi>{base}</mi><mo>{mark}</mo></mover></math>cd</span>'
    assert author_text(BeautifulSoup(html,'html.parser')) == 'Ab'+expected+'cd'


def test_author_rendering_rejects_unrecognized_math():
    from papertrack.reports.reading import author_text
    with pytest.raises(ValueError,match='author-name'):
        author_text(BeautifulSoup('<span>A<math><mfrac><mi>x</mi><mi>y</mi></mfrac></math></span>','html.parser'))


def test_author_accent_uses_rendered_chtml_once_not_assistive_duplicate():
    from papertrack.reports.reading import author_text
    html = '<span>Nú<mjx-container><mjx-math><mjx-mover><mjx-over><mjx-mo><mjx-c class="mjx-c7E"></mjx-c></mjx-mo></mjx-over><mjx-base><mjx-mi><mjx-c class="mjx-c6E"></mjx-c></mjx-mi></mjx-base></mjx-mover></mjx-math><mjx-assistive-mml><math><mover><mi>n</mi><mo>~</mo></mover></math></mjx-assistive-mml></mjx-container>ez</span>'
    assert author_text(BeautifulSoup(html,'html.parser')) == 'Núñez'


def test_empty_css_glyph_without_explicit_mathml_is_not_silently_dropped():
    with pytest.raises(ValueError,match='explicit MathML'):
        render('<mjx-container><mjx-math><mjx-mi><mjx-c class="mjx-c6E"></mjx-c></mjx-mi></mjx-math></mjx-container>')


def test_explicit_normal_math_style_preserves_upright_subscript():
    from papertrack.reports.reading import reading_markdown
    html='<math><msub><mi>ν</mi><mstyle mathvariant="normal"><mi>tot</mi></mstyle></msub><mo>=</mo><mn>4</mn><mo>/</mo><mn>3</mn></math>'
    assert reading_markdown(BeautifulSoup(html,'html.parser')) == r'$ν_{\mathrm{tot}}=4/3$'
    with pytest.raises(ValueError,match='style attributes'):
        render('<math><mstyle scriptlevel="1"><mi>x</mi></mstyle></math>')


def test_nested_assistive_rerender_preserves_the_single_original_mathml():
    from papertrack.reports.reading import reading_markdown
    glyph='<mjx-math><mjx-mi><mjx-c class="mjx-c4E"></mjx-c></mjx-mi></mjx-math>'
    original='<math><msubsup><mtext>N</mtext><mn>2</mn><mo>+</mo></msubsup></math>'
    html='<mjx-container>'+glyph+'<mjx-assistive-mml><mjx-container>'+glyph+'<mjx-assistive-mml>'+original+'</mjx-assistive-mml></mjx-container></mjx-assistive-mml></mjx-container>'
    assert reading_markdown(BeautifulSoup(html,'html.parser')) == '$N_{2}^{+}$'
    with pytest.raises(ValueError,match='explicit MathML'):
        render(html.replace(original,original+original))
