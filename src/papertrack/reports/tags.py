"""Canonical Obsidian tags for generated PaperTrack reports."""


def _tag_component(value: str) -> str:
    return value.lower().replace(".", "-")


def arxiv_report_tag(category: str, report_date: str) -> str:
    return f"{_tag_component(category)}-{report_date}"


def journal_report_tag(journal_slug: str, volume: str, issue: str) -> str:
    return f"{_tag_component(journal_slug)}-v{volume}-i{issue}"
