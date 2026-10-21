"""Exercise the real subprocess boundary without a model or publisher service."""
import json
from pathlib import Path
import sys

import pytest

from papertrack import cli
from papertrack.journals import agent as journal_agent
from papertrack.config import journal_configs
from papertrack.journals.task import issue_task


def test_failed_capture_indexes_incomplete_issue_evidence(tmp_path):
    from papertrack.journals.agent import AgentCapture

    capture = tmp_path / 'capture.json'
    html = tmp_path / 'capture.json.incomplete.html'
    marker = tmp_path / 'capture.json.incomplete.json'
    html.write_text('<option selected>In Progress</option>')
    marker.write_text('{"reason":"publisher_issue_incomplete"}')
    AgentCapture(tmp_path, capture, {}).save('failed')
    record = json.loads((tmp_path / 'result.json').read_text())
    assert record['status'] == 'failed'
    assert record['artifacts']['incomplete_html']['path'] == str(html)
    assert record['artifacts']['incomplete_issue']['path'] == str(marker)
    assert 'capture' not in record['artifacts']


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    program = tmp_path / "codex"
    program.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys, time
prompt = sys.stdin.read()
mode = os.environ.get("PAPERTRACK_TEST_MODE", "ok")
print(json.dumps({"type": "thread.started", "thread_id": "fake-session"}), flush=True)
print("diagnostic stderr", file=sys.stderr, flush=True)
if mode == "hang": time.sleep(60)
if mode == "exit": sys.exit(7)
target = pathlib.Path(prompt.split("Capture checkpoint: ", 1)[1].splitlines()[0])
if mode != "missing":
    data = json.loads(os.environ["PAPERTRACK_TEST_CAPTURE"])
    if mode == "invalid": data["entries"][0]["abstract"] = ""
    if mode == "wrong": data["issue"] = "13"
    if "discoverCuaLatestIssue" in prompt and mode != "noarchive":
        latest = "11" if mode == "oldissue" else "12"
        pathlib.Path(str(target)+".archive.html").write_text(
            '<title>Archive</title><a href="/jcisd8/issue/66/'+latest+'">Latest</a>')
    target.write_text(json.dumps(data))
    if mode != "nohtml": pathlib.Path(str(target)+".html").write_text(
        '<div class="al-article-item-wrap"><span class="citation-label">'
        '<a href="https://doi.org/10.1021/example">DOI</a></span>'
        '<button class="showAbstractLink" id="control" data-abstract-type="abstract" '
        'aria-controls="panel">Abstract</button></div><div id="panel"><section class="abstract">Official expanded abstract.</section></div>')
    if mode == "unavailable":
        pathlib.Path(str(target)+".html").write_text(
            '<div class="al-article-item-wrap"><span class="citation-label">'
            '<a href="https://doi.org/10.1021/example">DOI</a></span></div>')
pathlib.Path(sys.argv[sys.argv.index("--output-last-message")+1]).write_text("All done")
''')
    program.chmod(0o755)
    scope = tmp_path / "systemd-run"
    scope.write_text(f"#!{sys.executable}\nimport os,sys\n"
                     "args=sys.argv[sys.argv.index('--')+1:]\nos.execv(args[0],args)\n")
    scope.chmod(0o755)
    control = tmp_path / "systemctl"
    control.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
    control.chmod(0o755)
    paths = {"codex": program, "systemd-run": scope, "systemctl": control}
    monkeypatch.setattr(journal_agent.shutil, "which", lambda name: str(paths[name]))
    data = {
        "schema": "papertrack-browser-issue-v1", "capture_method": "fixed_browser",
        "collector": "papertrack-browser-v2", "url": "https://pubs.acs.org/jcisd8/issue/66/12",
        "volume": "66", "issue": "12", "headings": ["Volume 66, Issue 12"],
        "inventory_complete": True, "card_count": 1, "errors": [], "remaining_abstracts": 0,
        "entries": [{"doi": "10.1021/example", "title": "Official paper", "authors": ["A Author"],
                     "citation": "JCIM (2026) 66 (12): 1–4.", "abstract_type": "abstract",
                     "abstract": "Official expanded abstract.", "control": "control", "panel": "panel"}],
    }
    monkeypatch.setenv("PAPERTRACK_TEST_CAPTURE", json.dumps(data))
    return program


def run(tmp_path, **kwargs):
    return journal_agent.run_issue_agent(journal="jcim", cfg=journal_configs["jcim"],
                                          volume="66", issue="12", year=2026,
                                          data_dir=str(tmp_path / "reports"), **kwargs)


def record(tmp_path):
    paths = list((tmp_path / ".papertrack-runs").glob("*/result.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text()), paths[0].parent


@pytest.mark.parametrize('evidence', [True, False])
def test_resume_preserves_original_and_only_reuses_verified_aps_text(tmp_path, evidence):
    import hashlib

    source = tmp_path / 'old.json'
    target = tmp_path / 'new.json'
    data = dict(schema='papertrack-browser-issue-v1', collector='papertrack-browser-v2',
                capture_method='fixed_browser', volume='137', issue='13', card_count=1,
                url='https://journals.aps.org/prl/issues/137/13',
                entries=[dict(doi='10.1103/example', title='Paper',
                              abstract_type='abstract', abstract='Actual abstract.')])
    source.write_text(json.dumps(data))
    original = source.read_bytes()
    if evidence:
        folder = Path(str(source) + '.articles')
        folder.mkdir()
        key = hashlib.sha256(b'10.1103/example').hexdigest()
        (folder / (key + '.html')).write_text(
            '<main><p>DOI: 10.1103/example</p><div id="abstract-section-content">'
            '<p>Actual abstract.</p></div></main>')
    journal_agent._seed_capture(source, target, journal_configs['prl'], '137', '13')
    copied = json.loads(target.read_text())
    assert copied['entries'][0]['abstract'] == ('Actual abstract.' if evidence else '')
    assert source.read_bytes() == original
    with pytest.raises(ValueError, match='requested formal issue'):
        journal_agent._seed_capture(source, tmp_path / 'wrong.json',
                                    journal_configs['prl'], '137', '12')
    assert not (tmp_path / 'wrong.json').exists()


def test_resume_launcher_records_source(fake_codex, tmp_path):
    import os

    source = tmp_path / 'prior.json'
    source.write_text(os.environ['PAPERTRACK_TEST_CAPTURE'])
    original = source.read_bytes()
    result = run(tmp_path, resume_capture=str(source))
    assert result.record['resumed_from'] == str(source)
    assert 'Resume that checkpoint' in (result.run_dir / 'task.txt').read_text()
    assert source.read_bytes() == original


def test_real_subprocess_acceptance_and_isolated_launch(fake_codex, tmp_path):
    result = run(tmp_path)
    data, folder = record(tmp_path)
    assert folder.parent == tmp_path / '.papertrack-runs'
    assert not (tmp_path / 'reports/.papertrack-runs').exists()
    assert data["status"] == "validated"
    assert data["exit_code"] == 0 and data["article_count"] == 1
    assert data['reading_coverage'] == {'abstract': 1, 'extract': 0, 'missing': 0, 'excluded': 0}
    assert data['artifacts']['issue_html']['path'] == str(result.capture_path) + '.html'
    assert data['artifacts']['capture']['bytes'] > 0
    assert data["session_id"] == "fake-session"
    command = data["command"]
    assert command[command.index("--model")+1] == "gpt-6-luna"
    assert command[command.index("--sandbox")+1] == "workspace-write"
    assert '--property=MemoryMax=8G' in command
    assert '--property=CPUQuota=200%' in command
    assert data['resource_unit'].endswith('.scope')
    assert command[command.index("--cd")+1] == str(folder)
    assert result.capture_path.parent == folder
    assert "diagnostic stderr" in (folder / "stderr.log").read_text()
    assert "fake-session" in (folder / "events.jsonl").read_text()
    prompt = (folder / "task.txt").read_text()
    assert "parent PaperTrack process" in prompt
    assert "Do not run the import command" in prompt
    assert not list((tmp_path / "reports").rglob("*.md"))


def test_process_deadline_is_also_the_agent_task_budget(fake_codex, tmp_path):
    result = run(tmp_path, timeout=1800)
    prompt = (result.run_dir / "task.txt").read_text()
    assert "1740-second budget" in prompt
    assert "10-minute budget" not in prompt


@pytest.mark.parametrize('official_control_absent', [True, False])
def test_unavailable_requires_official_html_evidence(
        fake_codex, tmp_path, monkeypatch, official_control_absent, capsys):
    import os

    capture = json.loads(os.environ['PAPERTRACK_TEST_CAPTURE'])
    capture['entries'][0].update(
        abstract='', abstract_type='unavailable',
        unavailable_reason='No Abstract or Extract control on the official issue card')
    monkeypatch.setenv('PAPERTRACK_TEST_CAPTURE', json.dumps(capture))
    if official_control_absent:
        monkeypatch.setenv('PAPERTRACK_TEST_MODE', 'unavailable')
    with pytest.raises(RuntimeError, match='Recent Abstract supplementation is unresolved'):
        run(tmp_path)
    data, _ = record(tmp_path)
    assert data['status'] == 'failed'
    assert not list((tmp_path/'reports').rglob('*.md'))


def test_explicit_issue_agent_validates_and_imports(fake_codex, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, '_build_zotero', lambda: None)
    monkeypatch.setattr('sys.argv', ['papertrack', 'journal', 'issue', '--journal', 'jcim',
            '--volume', '66', '--issue', '12', '--run-agent',
                                   '--data-dir', str(tmp_path/'reports')])
    cli.main()
    data, folder = record(tmp_path)
    assert data['status'] == 'complete'
    assert data['volume'] == '66' and data['issue'] == '12'
    assert (tmp_path/'reports/acs/jcim/66/12.md').is_file()
    assert 'Abstract: 1' in capsys.readouterr().out


@pytest.mark.parametrize('mode', ['noarchive', 'oldissue'])
def test_latest_rejects_missing_evidence_or_wrong_latest(fake_codex, tmp_path, monkeypatch, mode):
    monkeypatch.setenv('PAPERTRACK_TEST_MODE', mode)
    with pytest.raises(RuntimeError, match='Journal agent failed'):
        run(tmp_path, discover_latest=True)
    data, _ = record(tmp_path)
    assert data['status'] == 'failed'
    assert not list((tmp_path/'reports').rglob('*.md'))


@pytest.mark.parametrize("mode", ["exit", "missing", "invalid", "wrong", "nohtml"])
def test_model_success_cannot_publish_bad_artifacts(fake_codex, tmp_path, monkeypatch, mode):
    monkeypatch.setenv("PAPERTRACK_TEST_MODE", mode)
    report = tmp_path / "reports/acs/jcim/66/12.md"
    report.parent.mkdir(parents=True)
    report.write_text("prior checked reading state")
    with pytest.raises(RuntimeError, match="Journal agent failed"):
        run(tmp_path)
    data, folder = record(tmp_path)
    assert data["status"] == "failed"
    assert (folder / "events.jsonl").exists()
    assert report.read_text() == "prior checked reading state"


def test_timeout_stops_process_and_retains_evidence(fake_codex, tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERTRACK_TEST_MODE", "hang")
    with pytest.raises(RuntimeError, match="timed out"):
        run(tmp_path, timeout=0.2)
    data, _ = record(tmp_path)
    assert data["status"] == "timeout"
    assert data["exit_code"] != 0


def test_existing_capture_is_not_accepted_as_a_new_run(fake_codex, tmp_path):
    target = tmp_path / "prior.json"
    target.write_text("previous capture")
    with pytest.raises(ValueError, match="already exists"):
        run(tmp_path, capture_path=str(target))
    assert target.read_text() == "previous capture"
    assert not (tmp_path / "reports").exists()


def test_missing_cli_is_explicit_and_does_not_start_a_run(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_agent.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="Codex CLI not found"):
        run(tmp_path)
    assert not (tmp_path / "reports").exists()


def test_missing_systemd_never_falls_back_to_an_unbounded_run(fake_codex, tmp_path, monkeypatch):
    monkeypatch.setattr(journal_agent.shutil, "which", lambda name: str(fake_codex) if name == 'codex' else None)
    with pytest.raises(RuntimeError, match='refusing an unbounded'):
        run(tmp_path)
    assert not (tmp_path / 'reports').exists()


def test_cli_launches_then_parent_imports(fake_codex, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_build_zotero", lambda: None)
    monkeypatch.setattr(sys, "argv", ["papertrack", "journal", "issue", "--journal", "jcim",
                                      "--volume", "66", "--issue", "12", "--run-agent",
                                      "--data-dir", str(tmp_path / "reports")])
    cli.main()
    data, _ = record(tmp_path)
    assert data["status"] == "complete"
    report = Path(data["report_path"])
    assert "Official expanded abstract" in report.read_text()
    assert str(report) in capsys.readouterr().out


def test_failed_import_is_visible_and_preserves_prior_report(fake_codex, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_build_zotero", lambda: None)
    report = tmp_path / "reports/acs/jcim/66/12.md"
    report.parent.mkdir(parents=True)
    report.write_text("prior durable report")
    def refuse(**kwargs):
        return False
    monkeypatch.setattr(cli, "filter_journal_to_md", refuse)
    args = cli._build_parser().parse_args(["journal", "issue", "--journal", "jcim", "--volume", "66",
                                           "--issue", "12", "--run-agent", "--data-dir", str(tmp_path / "reports")])
    with pytest.raises(RuntimeError, match="not written"):
        args.handler(args, None)
    assert record(tmp_path)[0]["status"] == "import_failed"
    assert report.read_text() == "prior durable report"


def test_snapshot_and_launch_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["journal", "issue", "--journal", "jcim", "--volume", "66",
                                        "--issue", "12", "--run-agent", "--browser-snapshot", "capture.json"])


def test_installed_task_uses_packaged_modules_and_module_entrypoint(tmp_path):
    prompt = issue_task("jcim", journal_configs["jcim"], "66", "12", 2026,
                        str(tmp_path / "capture.json"), str(tmp_path))
    packaged = Path(journal_agent.__file__).parents[1] / "browser/cua.mjs"
    assert str(packaged) in prompt and packaged.exists()
    assert "-m papertrack" in prompt
    assert "main.py" not in prompt

@pytest.mark.parametrize('conflict', [False, True])
def test_silverchair_resume_rechecks_text_and_preserves_old_evidence(fake_codex, tmp_path, conflict):
    import os, hashlib
    source=tmp_path/'source.json';target=tmp_path/'target.json'
    data=json.loads(os.environ['PAPERTRACK_TEST_CAPTURE'])
    source.write_text(json.dumps(data));before=source.read_bytes()
    card='<div class="al-article-item-wrap"><span class="citation-label"><a href="https://doi.org/10.1021/example">DOI</a></span><button class="showAbstractLink" id="control" data-abstract-type="abstract" aria-controls="panel">Abstract</button></div>'
    panel='<div id="panel"><section class="abstract">'+('Different source text.' if conflict else 'Official expanded abstract.')+'</section></div>'
    Path(str(source)+'.html').write_text(card+panel)
    folder=Path(str(source)+'.readings');folder.mkdir()
    key=hashlib.sha256(b'10.1021/example').hexdigest()+'.json'
    sidecar=folder/key;sidecar.write_text(json.dumps(dict(schema='papertrack-reading-html-v1',url=data['url'],doi='10.1021/example',card_html=card,panel_html=panel)))
    old_evidence=sidecar.read_bytes()
    journal_agent._seed_capture(source,target,journal_configs['jcim'],'66','12')
    seeded=json.loads(target.read_text())
    assert seeded['entries'][0]['abstract']==('' if conflict else 'Official expanded abstract.')
    assert source.read_bytes()==before and sidecar.read_bytes()==old_evidence
    copied=Path(str(target)+'.readings')
    assert (copied/key).exists() == (not conflict)
    if conflict:
        assert len(list(copied.glob('*.previous-*.json')))==1
        assert seeded['error_history'][-1]['reason']=='resume_source_evidence_missing_or_invalid'

@pytest.mark.parametrize('sidecar', [False, True])
@pytest.mark.parametrize('kind', ['abstract', 'extract'])
def test_resume_isolates_only_unrenderable_formula_and_accepts_fixed_source(fake_codex, tmp_path, sidecar, kind):
    import os
    import hashlib
    from papertrack.journals.capture import read_browser_issue, validate_reading_controls

    source, target = tmp_path/'source.json', tmp_path/'target.json'
    data = json.loads(os.environ['PAPERTRACK_TEST_CAPTURE'])
    good = data['entries'][0]
    good.update(abstract='H_{2}' if kind == 'extract' else 'H2', abstract_type=kind)
    bad = dict(good, doi='10.1021/bad', control='bad-control', panel='bad-panel', abstract='r')
    data['entries'].append(bad)
    data['card_count'] = 2

    def card(row):
        return (f'<div class="al-article-item-wrap"><span class="citation-label"><a href="https://doi.org/{row["doi"]}">DOI</a></span>'
                f'<button class="showAbstractLink" id="{row["control"]}" data-abstract-type="{kind}" aria-controls="{row["panel"]}">{kind}</button></div>')

    def panel(row, contents):
        body = f'<section class="abstract">{contents}</section>' if kind == 'abstract' else f'<p>{contents}</p>'
        return f'<div id="{row["panel"]}">{body}</div>'

    broken = 'r<math><mover accent="true"><mi></mi></mover></math>'
    source.write_text(json.dumps(data))
    html = card(good)+panel(good, 'H<sub>2</sub>')+card(bad)+panel(bad, broken)
    Path(str(source)+'.html').write_text(html)
    key = hashlib.sha256(b'10.1021/bad').hexdigest()+'.json'
    saved = dict(schema='papertrack-reading-html-v1', url=data['url'], doi=bad['doi'],
                 card_html=card(bad), panel_html=panel(bad, broken))
    if sidecar:
        folder = Path(str(source)+'.readings')
        folder.mkdir()
        (folder/key).write_text(json.dumps(saved))
    originals = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    journal_agent._seed_capture(source, target, journal_configs['jcim'], '66', '12')
    seeded = json.loads(target.read_text())
    assert seeded['entries'][0]['abstract'] == good['abstract']
    assert seeded['entries'][1]['abstract'] == ''
    failure = seeded['error_history'][-1]
    assert failure['doi'] == bad['doi']
    assert '10.1021/bad' in failure['message'] and 'Missing usable literal' in failure['message']
    assert all(p.read_bytes() == contents for p, contents in originals.items())
    if sidecar:
        cloned = Path(str(target)+'.readings')
        assert not (cloned/key).exists()
        assert len(list(cloned.glob('*.previous-*.json'))) == 1

    # Restoring the flattened text alone must not make malformed HTML usable.
    seeded['entries'][1]['abstract'] = 'r'
    target.write_text(json.dumps(seeded))
    kwargs = dict(provider='acs', code='jcisd8', volume='66', issue='12', journal_name='JCIM')
    with pytest.raises(ValueError, match='10.1021/bad.*Missing usable literal'):
        read_browser_issue(str(target), **kwargs)

    # A new, explicit official-style panel is now accepted ahead of old issue HTML.
    folder = Path(str(target)+'.readings')
    folder.mkdir(exist_ok=True)
    fixed = '<math><mover accent="true"><mi>r</mi><mo>→</mo></mover></math>'
    saved['panel_html'] = panel(bad, fixed)
    (folder/key).write_text(json.dumps(saved))
    seeded['entries'][1]['abstract'] = 'r→'
    target.write_text(json.dumps(seeded))
    validate_reading_controls(seeded, Path(str(target)+'.html'), provider='acs')
    result = read_browser_issue(str(target), **kwargs)
    assert result.entries[bad['doi']].abstract == r'$\vec{r}$'
    assert result.entries[good['doi']].abstract == 'H$_{2}$'


def test_browser_prompts_use_real_cua_and_bound_tabs(tmp_path):
    from papertrack.journals.task import archive_page_task
    prompt = issue_task('prl', journal_configs['prl'], '137', '13', 2026,
                        str(tmp_path/'capture.json'), str(tmp_path), discover_latest=True)
    assert 'collectCuaIssueBatch(cua, browserId, tab.id' in prompt
    assert 'collectCuaRecentBatch' not in prompt  # APS has no recent-page phase.
    assert 'discoverCuaLatestIssue(cua, browserId, tab.id' in prompt
    assert 'tab, checkpointPath:' in prompt
    assert 'saveHtml: true, abstractTab' in prompt
    assert 'Never construct, mock, or overwrite cua' in prompt
    assert 'agent.browsers binding lives' not in prompt
    archive = archive_page_task(journal_configs['prl'], 'https://journals.aps.org/prl/issues',
                                str(tmp_path/'archive.json'), 660)
    assert 'collectCuaArchivePage(cua, browserId, tab.id' in archive
    assert '{tab, capturePath:' in archive
    assert 'Never construct/mock/overwrite cua' in archive
    for task_prompt in (prompt, archive):
        assert 'record the existing tab group IDs' in task_prompt
        assert 'delete any tab groups created by' in task_prompt
        assert 'including groups automatically created by the browser tool' in task_prompt
        assert 'Never delete pre-existing groups' in task_prompt
        assert 'report the exact remaining group IDs' in task_prompt
