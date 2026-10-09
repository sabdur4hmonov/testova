"""Exercise backup failure/retention paths with a fake Docker command and private files."""
import json, os, shutil, subprocess, time, zipfile
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
POWERSHELL=shutil.which('powershell.exe') or shutil.which('pwsh')
pytestmark=pytest.mark.skipif(not POWERSHELL,reason='PowerShell not available')

def test_compatibility_entrypoint_forwards_named_options(tmp_path):
    entry=tmp_path/'_backup.ps1'
    entry.write_bytes((ROOT/'_backup.ps1').read_bytes())
    scripts=tmp_path/'scripts'; scripts.mkdir()
    # Parameter-only stub: never dump a database or write any backup file.
    (scripts/'backup.ps1').write_text('''param([string]$RepositoryRoot,[string]$BackupDirectory,[int]$KeepDays)
@{repo=$RepositoryRoot;backup=$BackupDirectory;days=$KeepDays}|ConvertTo-Json -Compress
''',encoding='utf-8-sig')
    result=subprocess.run([POWERSHELL,'-NoProfile','-ExecutionPolicy','Bypass','-File',str(entry),
                           '-RepositoryRoot','synthetic-repository','-BackupDirectory','synthetic-backups','-KeepDays','9'],
                          capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'repo':'synthetic-repository','backup':'synthetic-backups','days':9}

def _quote(value):
    return "'"+str(value).replace("'","''")+"'"

def run_backup(tmp_path, failure='none', setup=None):
    repo=tmp_path/'repo'; backup=tmp_path/'backups'
    repo.mkdir(); backup.mkdir()
    (repo/'storage').mkdir(); (repo/'storage/doc.txt').write_text('private fixture')
    (repo/'temp_images').mkdir(); (repo/'temp_images/figure.txt').write_text('private figure')
    old=backup/'testova_20000101_000000.dump'; old.write_text('protected legacy backup')
    recovered=backup/'recovered_20000101.dump'; recovered.write_text('protected recovered backup')
    if setup:
        setup(repo,backup)
    old_time="(Get-Date).AddDays(-30)"
    legacy=ROOT/'_backup.ps1'
    modern=ROOT/'scripts/backup.ps1'
    if modern.exists():
        invocation=f"& {_quote(modern)} -RepositoryRoot {_quote(repo)} -BackupDirectory {_quote(backup)}"
    else:
        # Redirect only the legacy script's fixed output directory; preserve its logic.
        script=repo/'legacy.ps1'
        script.write_text(legacy.read_text(encoding='utf-8-sig').replace("'D:\\testova_backups'",_quote(backup)),encoding='utf-8-sig')
        invocation=f"& {_quote(script)}"
    runner=tmp_path/'run.ps1'
    runner.write_text(f'''
$ErrorActionPreference='Stop'
(Get-Item -LiteralPath {_quote(old)}).LastWriteTime={old_time}
(Get-Item -LiteralPath {_quote(recovered)}).LastWriteTime={old_time}
function global:docker {{
    if ({_quote(failure)} -eq 'stderr_warning') {{
        # Real compose prints warnings (e.g. "the attribute `version` is
        # obsolete") on stderr while succeeding; emit one from a NATIVE command.
        cmd /c "echo compose warning: attribute version is obsolete 1>&2"
    }}
    $global:LASTEXITCODE=0
    if ($args -contains 'cp') {{
        if ({_quote(failure)} -eq 'copy') {{ $global:LASTEXITCODE=7; return }}
        $bytes=[System.Text.Encoding]::ASCII.GetBytes('PGDMP'+('x'*100))
        [System.IO.File]::WriteAllBytes([string]$args[-1],$bytes)
    }}
    if ($args -contains 'pg_restore') {{
        if ({_quote(failure)} -eq 'toc') {{ $global:LASTEXITCODE=8; return }}
        'TABLE DATA public users'
    }}
}}
{invocation}
''',encoding='utf-8-sig')
    completed=subprocess.run([POWERSHELL,'-NoProfile','-ExecutionPolicy','Bypass','-File',str(runner)],
                             capture_output=True,text=True,timeout=45)
    return completed,backup,old,recovered

@pytest.mark.parametrize('failure',['copy','toc'])
def test_failed_backup_never_reports_success_or_removes_old_files(tmp_path,failure):
    result,backup,old,recovered=run_backup(tmp_path,failure)
    assert result.returncode!=0, result.stdout
    assert 'Backup tayyor' not in result.stdout
    assert old.exists() and recovered.exists()

def test_complete_backup_contains_both_asset_archives_and_manifest(tmp_path):
    result,backup,old,recovered=run_backup(tmp_path)
    assert result.returncode==0,result.stderr
    manifests=list(backup.glob('manifest_*.json'))
    assert len(manifests)==1
    data=json.loads(manifests[0].read_text(encoding='utf-8-sig'))
    assert data['complete'] is True
    for key in ('dump','storage','images'):
        assert (backup/data[key]).is_file()
    assert old.exists() and recovered.exists(), 'Legacy backups must not be silently purged'

def test_a_successful_docker_step_with_a_stderr_warning_still_backs_up(tmp_path):
    """2026-10-09, first real run: compose wrote its "version is obsolete"
    warning to stderr, Windows PowerShell 5.1 turned that line into an
    ErrorRecord under ErrorActionPreference=Stop, and the backup aborted after
    pg_dump had already succeeded. Steps are judged by exit code only."""
    result,backup,old,recovered=run_backup(tmp_path,'stderr_warning')
    assert result.returncode==0,result.stderr
    assert 'Backup tayyor' in result.stdout
    assert len(list(backup.glob('manifest_*.json')))==1

def test_retention_only_removes_complete_matching_bundles_inside_its_directory(tmp_path):
    sentinel=tmp_path/'do-not-delete.dump'; sentinel.write_text('outside backup directory')
    stem='20000101_000000_01234567'
    def setup(repo,backup):
        names={'dump':f'testova_{stem}.dump','storage':f'storage_{stem}.zip','images':f'images_{stem}.zip'}
        for name in names.values(): (backup/name).write_text('expired private fixture')
        valid=backup/f'manifest_{stem}.json'
        valid.write_text(json.dumps({'complete':True,**names}))
        bad=backup/'manifest_20000101_000000_89abcdef.json'
        bad.write_text(json.dumps({'complete':True,'dump':str(sentinel),'storage':'bad.zip','images':'bad.zip'}))
        for path in (valid,bad): os.utime(path,(time.time()-30*86400,)*2)
    result,backup,old,recovered=run_backup(tmp_path,setup=setup)
    assert result.returncode==0,result.stderr
    assert not (backup/f'manifest_{stem}.json').exists()
    assert not (backup/f'testova_{stem}.dump').exists()
    assert sentinel.exists() and old.exists() and recovered.exists()
    assert (backup/'manifest_20000101_000000_89abcdef.json').exists()

def test_asset_archives_include_hidden_files(tmp_path):
    def setup(repo,backup):
        for folder in ('storage','temp_images'):
            (repo/folder/'.hidden.txt').write_text('hidden private fixture')
    result,backup,_,_=run_backup(tmp_path,setup=setup)
    assert result.returncode==0,result.stderr
    data=json.loads(next(backup.glob('manifest_*.json')).read_text(encoding='utf-8-sig'))
    for key in ('storage','images'):
        with zipfile.ZipFile(backup/data[key]) as archive:
            assert '.hidden.txt' in archive.namelist()
