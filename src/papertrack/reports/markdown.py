"""Small Markdown presentation helpers; never infer missing scientific text."""
import re
from html import unescape


_LITERAL_SPANS = re.compile(
    r'(?P<ticks>`+)[\s\S]*?(?P=ticks)'
    r'|(?<!\\)\$\$[\s\S]*?(?<!\\)\$\$'
    r'|(?<!\\)\$(?:\\.|[^$\\])*(?<!\\)\$'
    r'|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]')


def _escape_wikilinks(value: str) -> str:
    result = []
    offset = 0
    for match in _LITERAL_SPANS.finditer(value):
        result.append(value[offset:match.start()].replace('[[', r'\[\[').replace(']]', r'\]\]'))
        result.append(match.group())
        offset = match.end()
    result.append(value[offset:].replace('[[', r'\[\[').replace(']]', r'\]\]'))
    return ''.join(result)


def remove_generated_provenance(content: str) -> str:
    """Remove generated source JSON lines; source evidence stays in captures."""
    return re.sub(
        r'(?m)^(?:<!-- papertrack_provenance: [^\r\n]* -->|'
        r'%% papertrack_provenance: [^\r\n]* %%)[ \t]*(?:\r?\n(?:[ \t]*\r?\n)?)?',
        '', content)


def abstract_markdown(value: str) -> str:
    # Entity text belongs to HTML transport, not TeX/Markdown source.
    value = unescape(value)
    value = re.sub(r'(?m)^\s*[△▵]\s*Less\s*$', '', value).strip()
    # Prevent accidental Obsidian links without adding math delimiters.
    return _escape_wikilinks(value)


def repair_presentation(content: str) -> str:
    """Repair only generated callouts/comments, leaving user text and tasks alone."""
    content = re.sub(
        r'(?m)^(?:Abstract|Extract(?:（原文摘录）)?):[ \t]*\n(?=> \[!quote\]- )',
        '', content)
    content = re.sub(r'(?m)(^> \[!quote\]- [^\n]+)\n(?:[ \t]*\n)+(?=>)', r'\1\n', content)
    content = re.sub(r'(?m)^>[ \t]*[△▵][ \t]*Less[ \t]*\n?', '', content)
    content = remove_generated_provenance(content)
    # Trim generated spacing around author/callout and provenance boundaries.
    content = re.sub(r'(?m)(^Authors:[^\n]*\n)\n+(?=> \[!quote\])', r'\1\n', content)
    content = re.sub(r'\n{3,}(?=> \[!quote\]- )', '\n\n', content)
    # Each quoted paragraph stays inside the callout, even an empty paragraph.
    lines = content.splitlines(keepends=True)
    result = []
    index = 0
    while index < len(lines):
        line = lines[index]
        result.append(line)
        index += 1
        if not line.startswith('> [!quote]- '):
            continue
        quoted = []
        while index < len(lines) and lines[index].startswith('>'):
            quoted.append(lines[index][1:].strip())
            index += 1
        if quoted:
            # Keep display-math/code spans intact across quoted lines.
            result.extend('> ' + part + '\n'
                          for part in abstract_markdown('\n'.join(quoted)).split('\n'))
    return ''.join(result)
