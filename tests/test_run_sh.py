"""Exercise Bash scheduling with local CLI substitutes, never live collection."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('failed', ['', 'arxiv', 'journals'])
def test_shell_pipelines_overlap_wait_and_propagate_failure(tmp_path, failed):
    text=(ROOT/'run.sh').read_text()
    script=text.split("bash <<'PIPELINES'\n",1)[1].rsplit('\nPIPELINES',1)[0]
    deployment='data=/home/ansatz/data/obsidian/1/papertrack_datas'
    assert deployment in script
    script=script.replace(deployment,'data="$TEST_DATA"')
    binary=tmp_path/'bin';binary.mkdir()
    program=binary/'python'
    program.write_text('''#!/bin/bash
set -euo pipefail
printf '%s\\n' "$*" >> "$TEST_STATE/commands"
if [[ "$*" == *' arxiv '* ]]; then kind=arxiv; other=journals; else kind=journals; other=arxiv; fi
mkdir "$TEST_STATE/$kind-active"  # Reject simultaneous CLI calls in one pipeline.
touch "$TEST_STATE/$kind-started"
for attempt in {1..200}; do [[ -f "$TEST_STATE/$other-started" ]] && break; sleep 0.01; done
[[ -f "$TEST_STATE/$other-started" ]]
touch "$TEST_STATE/$kind-finished"
rmdir "$TEST_STATE/$kind-active"
[[ "$TEST_FAIL" != "$kind" ]] || exit 7
''')
    program.chmod(0o755)
    (tmp_path/'.papertrack_diagnostics').mkdir()
    data=tmp_path/'data/arxiv/quant-ph';data.mkdir(parents=True)
    (data/'existing.md').write_text('existing')
    env=dict(os.environ,PATH=str(binary)+os.pathsep+os.environ['PATH'],TEST_STATE=str(tmp_path),
             TEST_DATA=str(tmp_path/'data'),TEST_FAIL=failed)
    completed=subprocess.run(['bash','-c',script],cwd=tmp_path,env=env,timeout=10,capture_output=True,text=True)
    assert completed.returncode==(1 if failed else 0), completed.stderr
    assert (tmp_path/'arxiv-finished').exists()
    assert (tmp_path/'journals-finished').exists()
    commands=(tmp_path/'commands').read_text().splitlines()
    assert sum(' --journals ' in line for line in commands)==1
    assert len(commands)==(2 if failed=='arxiv' else 5)
    if failed!='arxiv':
        assert any('arxiv refresh --category quant-ph' in line for line in commands)
        assert any('arxiv backfill --category chem-ph' in line for line in commands)


def test_script_lock_rejects_overlapping_owner_and_releases(tmp_path):
    text=(ROOT/'run.sh').read_text()
    block=text.split('exec 9>.work/run.lock',1)[1].split('export TMPDIR=',1)[0]
    script='exec 9>.work/run.lock'+block
    (tmp_path/'.work').mkdir()
    owner=subprocess.Popen(['flock',str(tmp_path/'.work/run.lock'),'bash','-c',
        'echo ready; read -r done'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        assert owner.stdout.readline().strip()=='ready'
        result=subprocess.run(['bash','-c',script],cwd=tmp_path,capture_output=True,text=True,timeout=5)
        assert result.returncode!=0
        assert 'already running' in result.stderr
    finally:
        owner.communicate('done\n',timeout=5)
    assert subprocess.run(['bash','-c',script],cwd=tmp_path,timeout=5).returncode==0
