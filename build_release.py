"""Build an explicit-source deployment archive; never include credentials or private data."""
import hashlib
import json
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent


def main():
    names=['Dockerfile','compose.yaml','.dockerignore','requirements.txt','requirements-research.txt',
        'deploy.env.example','README.md','build_release.py','engine.py','server.py','wallex_settings.py',
        'mcp_link.py','wallex_mcp.py','llm_gateway.py','ta_indicators.py','challenge_rules.py',
        'competition.py','product_status.py','backtest.py','hybrid_research.py','run_hybrid_research.py',
        'challenge_research.py','freeze_research_history.py',
        'docs/DEPLOY.md','docs/ARCHITECTURE.md','docs/CHALLENGE_RULES.md','docs/SCORING_PLAN.md',
        'docs/SCORING_RESULTS.md','docs/HYBRID_RESEARCH.md','docs/HYBRID_AGENT_SYSTEM_DESIGN.md']
    files=[ROOT/name for name in names]
    for folder in ('static','rules','release','tests'):
        files.extend(p for p in (ROOT/folder).rglob('*') if p.is_file() and
                     '__pycache__' not in p.parts and p.suffix not in ('.pyc','.sqlite3') and p.name!='runtime.json')
    from wallex_settings import settings
    secrets=[v.encode() for k,v in settings().items() if k.endswith('KEY') and len(v)>=8]
    manifest={}
    for path in files:
        if path.name.startswith('.env'):raise ValueError('Credential file forbidden')
        content=path.read_bytes()
        if any(secret in content for secret in secrets):raise ValueError('Credential detected; packaging stopped')
        manifest[path.relative_to(ROOT).as_posix()]=hashlib.sha256(content).hexdigest()
    target=ROOT/'dist/madar-wallex-2.0.0.zip'
    target.parent.mkdir(exist_ok=True)
    if target.exists():raise ValueError('Release archive already exists; preserve prior release')
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:archive.write(path,path.relative_to(ROOT).as_posix())
        archive.writestr('SOURCE_SHA256.json',json.dumps(manifest,indent=2,sort_keys=True))
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert not any(Path(n).name.startswith('.env') for n in archive.namelist())
    digest=hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix('.sha256').write_text(digest+'  '+target.name+'\n',encoding='ascii')
    print(json.dumps(dict(artifact=str(target.relative_to(ROOT)),sha256=digest,
                         files=len(files),credential_scan='passed',docker_built=False,real_trading=False)))


if __name__=='__main__':main()
