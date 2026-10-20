from datetime import date

import pytest

from papertrack.config import journal_configs
from papertrack.journals.inventory import parse_archive_page, validate_inventory


def parse(tmp_path, journal, html, url):
    p = tmp_path / 'archive.html'
    p.write_text('<title>Archive</title>' + html)
    return parse_archive_page(journal_configs[journal], p, url, date(2026, 1, 1), date(2026, 10, 17))


def test_acs_inventory_keeps_all_dated_issues_and_excludes_future(tmp_path):
    rows, pages = parse(tmp_path, 'jcim', '''
    <div><a href="/jcisd8/issue/66/1">Issue 1</a>January 12, 2026</div>
    <div><a href="/jcisd8/issue/66/2">Issue 2</a>January 26, 2026</div>
    <div><a href="/jcisd8/issue/66/21">Future</a>November 09, 2026</div>
    <div><a href="https://evil.example/jcisd8/issue/66/99">Other</a>January 26, 2026</div>
    ''', 'https://pubs.acs.org/loi/jcisd8')
    assert [(r.volume, r.issue) for r in rows] == [('66', '1'), ('66', '2')]
    assert not pages
    validate_inventory(rows, date(2026, 1, 1), date(2026, 10, 17))


def test_prl_archive_follows_prior_volume_in_same_year(tmp_path):
    rows, pages = parse(tmp_path, 'prl', '''
    <a href="/prl/issues/137/1">Issue #1: 3 July 2026</a>
    <table><tr><td><a href="/prl/issues/136#v136">Volume 136</a></td><td>January - Present</td></tr>
    <tr><td><a href="/prl/issues/135#v135">Volume 135</a></td><td>June - December 2025</td></tr></table>
    ''', 'https://journals.aps.org/prl/issues')
    assert pages == ['https://journals.aps.org/prl/issues/136']
    assert len(rows) == 1


def test_aip_current_page_directs_to_archive_not_empty_success(tmp_path):
    rows, pages = parse(tmp_path, 'jcp', '<a href="/aip/jcp/issue/browse-by-year">All Issues</a>',
                        'https://pubs.aip.org/aip/jcp/issue')
    assert rows == []
    assert pages == ['https://pubs.aip.org/aip/jcp/issue/browse-by-year']


def test_missing_issue_numbers_fail_without_inventing_membership(tmp_path):
    rows, _ = parse(tmp_path, 'prl', '''
    <a href="/prl/issues/136/1">Issue #1: 2 January 2026</a>
    <a href="/prl/issues/136/3">Issue #3: 16 January 2026</a>''',
                    'https://journals.aps.org/prl/issues/136')
    with pytest.raises(ValueError, match='Incomplete archive'):
        validate_inventory(rows, date(2026, 1, 1), date(2026, 10, 17))


def test_aip_inherits_year_from_official_heading_and_excludes_future_issue(tmp_path):
    rows, _ = parse(tmp_path, 'jcp', '''<h1>All Issues: 2026, Volume 164 and 165</h1>
    <a href="/aip/jcp/issue/164/1">Volume 164, Number 1: 7 January</a>
    <a href="/aip/jcp/issue/165/14">Volume 165, Number 14: 14 October</a>
    <a href="/aip/jcp/issue/165/15">Volume 165, Number 15: 21 October</a>''',
                        'https://pubs.aip.org/aip/jcp/issue/browse-by-year/2026')
    assert [(r.volume, r.issue) for r in rows] == [('164','1'),('165','14')]
    assert rows[0].published == date(2026,1,7)
