"""Small Markdown presentation helpers; never infer missing scientific text."""
import re
from html import unescape


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
    value = value.replace('[[', r'\[\[').replace(']]', r'\]\]')
    return value


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
    in_callout = False
    for index, line in enumerate(lines):
        if line.startswith('> [!quote]- '):
            in_callout = True
        elif in_callout and line.startswith('>'):
            lines[index] = '> ' + abstract_markdown(line[1:].strip()) + '\n'
        elif line.strip():
            in_callout = False
    return ''.join(lines)
