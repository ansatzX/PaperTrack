# arXiv & Zotero Data Structure Findings

Date: 2026-04-26

## Purpose

Investigate the actual HTML structure returned by arXiv advanced search, and the data fields available in a local Zotero database, to verify and improve the current extraction and matching logic.

---

## 1. arXiv Search Result HTML Structure

### Test setup

```bash
/home/ansatz/soft/miniconda3/bin/conda run -n arxiv python3
```

```python
from urllib.parse import urlencode, re
import feedparser
from bs4 import BeautifulSoup
from papertrack.codex import quant_ph

q = quant_ph.copy()
q['date-from_date'] = '2024-06-04'
q['date-to_date'] = '2024-06-05'

url = 'https://arxiv.org/search/advanced?' + re.sub('%2B', '+', urlencode(q))
results = feedparser.parse(url)
summary = results['feed']['summary']
soup = BeautifulSoup(summary, 'lxml')
papers = soup.find_all(class_='arxiv-result')
```

### Full HTML of one `<li class="arxiv-result">` (paper with both Comments and Journal ref)

```html
<li class="arxiv-result">
 <div class="is-marginless">
  <p class="list-title is-inline-block">
   <a href="https://arxiv.org/abs/2406.01974">arXiv:2406.01974</a>
   <span>[ <a href="https://arxiv.org/pdf/2406.01974">pdf</a>, <a href="https://arxiv.org/ps/2406.01974">ps</a>, <a href="https://arxiv.org/format/2406.01974">other</a> ]</span>
  </p>
  <div class="tags is-inline-block">
   <span class="tag is-small is-link tooltip is-tooltip-top">cond-mat.str-el</span>
   <span class="tag is-small is-grey tooltip is-tooltip-top">cond-mat.quant-gas</span>
   <span class="tag is-small is-grey tooltip is-tooltip-top">cond-mat.stat-mech</span>
   <span class="tag is-small is-grey tooltip is-tooltip-top">quant-ph</span>
  </div>
 </div>
 <p class="title is-5 mathjax">Gifts from long-range interaction: Emergent gapless topological behaviors in quantum spin chain</p>
 <p class="authors">
  <span class="has-text-black-bis has-text-weight-semibold">Authors:</span>
  <a href="...">Sheng Yang</a>, <a href="...">Hai-Qing Lin</a>, <a href="...">Xue-Jia Yu</a>
 </p>
 <p class="abstract mathjax">
  <span class="has-text-black-bis has-text-weight-semibold">Abstract</span>:
  <span class="abstract-short ...">Topology in condensed matter physics ...</span>
  <span class="abstract-full ..." style="display: none;">Topology in condensed matter physics is typically associated with a bulk energy gap. However, recent research has shifted focus to topological phases without a bulk energy gap ...</span>
 </p>
 <p class="is-size-7">
  <span class="has-text-black-bis has-text-weight-semibold">Submitted</span> 4 June, 2024;
  <span class="has-text-black-bis has-text-weight-semibold">originally announced</span> June 2024.
 </p>
 <p class="comments is-size-7">
  <span class="has-text-black-bis has-text-weight-semibold">Comments:</span>
  <span class="has-text-grey-dark mathjax">4.1pages with supplemental materials, 15 figures. Any comments or suggestions are welcome !</span>
 </p>
 <p class="comments is-size-7">
  <span class="has-text-black-bis has-text-weight-semibold">Journal ref:</span>
  Communications Physics 8, 27 (2025)
 </p>
</li>
```

### Paper with external DOI tags

Having a paper with an external DOI adds these tag elements inside the `arxiv-result`:

```html
<span class="tag is-dark is-size-7">doi</span>
<span class="tag is-light is-size-7">
 <a href="https://doi.org/10.1038/s42005-025-01947-z">10.1038/s42005-025-01947-z</a>
</span>
```

The current code uses `tag.find(class_='fa fa-external-link')` to locate these. In the tested HTML, DOI tags use `is-dark`/`is-light` classes, but the resulting external DOI string is correctly extracted (see evidence below).

### Actual extraction test

```python
from papertrack.arxiv_index_fetch import query_arxiv_dict
from papertrack.codex import quant_ph

d = query_arxiv_dict('2024-06-04', '2024-06-05', quant_ph)

# 2024-06-04: 49 papers, 28 with external DOI, 21 without
# Sample output:
#   arXiv:2406.01974: ext_doi=('10.1038/s42005-025-01947-z', 'https://doi.org/10.1038/s42005-025-01947-z')
#   arXiv:2406.02115: ext_doi=('10.1103/PhysRevA.109.062403', 'https://doi.org/10.1103/PhysRevA.109.062403')
```

### Mapping: HTML class → extracted field

| HTML element | Field extracted | Current status |
|---|---|---|
| `<p class="list-title">` → `<a>` text | `arxiv_id` | ✅ Working |
| `<p class="title is-5 mathjax">` | `title` | ✅ Working |
| `<p class="authors">` → `<a>` texts | `authors` | ✅ Working |
| `<span class="abstract-full">` | `abstract` | ✅ Working |
| `<span class="tag is-light is-size-7">` → `<a>` | `external_doi`, `external_doi_url` | ✅ Working |
| Tags with `fa-external-link` icon child | `external_doi` (alternative path) | ✅ Working |
| `<p class="comments is-size-7">` starting "Comments:" | `comments` | ❌ Not extracted |
| `<p class="comments is-size-7">` starting "Journal ref:" | `journal_ref` | ❌ Not extracted |
| `<span class="tag is-link/is-grey">` | Subject categories | ⚠️ Partial (parsed but discarded after ext-doi check) |

---

## 2. Zotero Local Database Schema

### Test setup

```python
from pyzotero import zotero
zot = zotero.Zotero(library_id='000000', library_type='user', local=True)
items = zot.top(limit=5)
```

### Item types observed and their data keys

**journalArticle** (`itemType: journalArticle`):
```
data keys: ['DOI', 'ISSN', 'abstractNote', 'accessDate', 'callNumber', 'citationKey',
            'collections', 'creators', 'date', 'dateAdded', 'dateModified', 'extra',
            'issue', 'itemType', 'journalAbbreviation', 'key', 'language', 'libraryCatalog',
            'pages', 'publicationTitle', 'relations', 'shortTitle', 'tags', 'title',
            'url', 'version', 'volume']
```
- `DOI`: publisher DOI (e.g. `10.1063/5.0226214`)
- `journalAbbreviation`: e.g. `J. Chem. Phys.`
- `publicationTitle`: e.g. `The Journal of Chemical Physics`
- `date`: e.g. `2024/9/28`
- `url`: publisher URL (not arxiv)
- `extra`: user tags with emoji labels

**preprint** (`itemType: preprint` — arXiv papers):
```
data keys: ['DOI', 'abstractNote', 'accessDate', 'citationKey', 'collections',
            'creators', 'date', 'dateAdded', 'dateModified', 'extra', 'itemType',
            'key', 'language', 'libraryCatalog', 'relations', 'repository', 'rights',
            'shortTitle', 'tags', 'title', 'url', 'version']
```
- `DOI`: arXiv DOI (e.g. `10.48550/ARXIV.2510.26887`)
- `url`: arXiv URL (e.g. `https://arxiv.org/abs/2510.26887`)
- `repository`: field exists but value not checked
- `extra`: user tags with emoji labels

**webpage** (`itemType: webpage` — arXiv papers without proper import):
```
data keys: ['abstractNote', 'accessDate', 'citationKey', 'collections', 'creators',
            'date', 'dateAdded', 'dateModified', 'extra', 'itemType', 'key', 'language',
            'relations', 'shortTitle', 'tags', 'title', 'url', 'version', 'websiteTitle']
```
- `url`: arXiv URL (e.g. `https://arxiv.org/abs/2511.02824v2`)
- **No `DOI` field** — these are papers added via browser connector without DOI import

### Fields useful for arXiv→Zotero matching

| Zotero field | Reliability | Notes |
|---|---|---|
| `DOI` | High | Exact match. Present on journalArticle and preprint types |
| `url` | High | Extract arXiv ID from `arxiv.org/abs/XXXX.YYYYY` links |
| `extra` | Low | Contains user emoji tags, may have metadata |
| `journalAbbreviation` | Medium | Could match against journal_ref partial string |
| `publicationTitle` | Medium | Same as above |

### Key insight: webpage items

Some arXiv papers in Zotero are stored as `webpage` type without a `DOI` field. These cannot be matched by DOI. Matching by `url` (extract arXiv ID from arxiv.org/abs/ URL) is the fallback for these entries.

---

## 3. Current Code Issues Identified

### 3.1 `journal_ref` not extracted

The `<p class="comments is-size-7">` with "Journal ref:" text is completely ignored. This is valuable metadata for Zotero matching when both arXiv DOI and external DOI fail.

### 3.2 No fallback via Zotero `url` field

If a Zotero item is stored as `webpage` (no DOI), matching by `url` → arXiv ID extraction is not attempted.

### 3.3 Bare list data structure

`query_dict[arxiv_id] = [title, authors, abstract, (external_doi, href_link)]` — positional, no types, error-prone.

### 3.4 `get_everything()` downloads entire library

Inefficient for large Zotero libraries.

### 3.5 Subject category tags parsed but unused

Subject tags (e.g. `cond-mat.str-el`, `quant-ph`) are parsed in the `tag` loop but only used to search for external DOI. The actual subject list is discarded.

---

## 4. Scripts Used for Investigation

### Fetch and inspect arXiv result HTML

```bash
/home/ansatz/soft/miniconda3/bin/conda run -n arxiv python3 -c "
from urllib.parse import urlencode, re
import feedparser
from bs4 import BeautifulSoup
from papertrack.codex import quant_ph

q = quant_ph.copy()
q['date-from_date'] = '2024-06-04'
q['date-to_date'] = '2024-06-05'

url = 'https://arxiv.org/search/advanced?' + re.sub('%2B', '+', urlencode(q))
results = feedparser.parse(url)
summary = results['feed']['summary']
soup = BeautifulSoup(summary, 'lxml')
papers = soup.find_all(class_='arxiv-result')
print(f'Papers: {len(papers)}')

if papers:
    print(papers[0].prettify())
"
```

### Inspect Zotero item data fields

```bash
/home/ansatz/soft/miniconda3/bin/conda run -n arxiv python3 -c "
from pyzotero import zotero
zot = zotero.Zotero(library_id='000000', library_type='user', local=True)
items = zot.top(limit=5)
for i, item in enumerate(items):
    data = item.get('data', {})
    print(f'--- Item {i+1} ({data.get(\"itemType\")}) ---')
    print(f'  data keys: {sorted(data.keys())}')
    for k in ['title', 'DOI', 'url', 'extra', 'journalAbbreviation', 'publicationTitle', 'date']:
        if k in data:
            val = data[k]
            if isinstance(val, str) and len(val) > 200:
                val = val[:200] + '...'
            print(f'    {k}: {val}')
"
```

### Count papers with/without external DOI

```bash
/home/ansatz/soft/miniconda3/bin/conda run -n arxiv python3 -c "
from papertrack.arxiv_index_fetch import query_arxiv_dict
from papertrack.codex import quant_ph

d = query_arxiv_dict('2024-06-04', '2024-06-05', quant_ph)
count_ext = sum(1 for v in d.values() if v[3])
print(f'Total: {len(d)}, With ext DOI: {count_ext}, Without: {len(d) - count_ext}')
"
```
