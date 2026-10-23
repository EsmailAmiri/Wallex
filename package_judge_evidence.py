"""Explicit allowlist for a credential-free, offline replay submission artifact."""
import hashlib
import json
import zipfile
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parent


def main():
    study=ROOT/'reports/challenge-ev-v4'
    if not (study/'llm-summary.json').exists():
        raise ValueError('Complete the bounded LLM evaluation before packaging')
    source=('challenge_research.py','hybrid_research.py','run_hybrid_research.py',
            'engine.py','ta_indicators.py','llm_gateway.py','wallex_settings.py',
            'challenge_rules.py','competition.py','requirements.txt','requirements-research.txt',
            'rules/blocked_markets.txt','docs/SCORING_PLAN.md')
    files=[ROOT/name for name in source]
    for folder,patterns in ((study,('*.json','*.joblib')),
                            (ROOT/'data/hybrid-history-60d',('*.json',)),
                            (ROOT/'data/hybrid-feature-cache',('*.json',))):
        for pattern in patterns:files.extend(folder.rglob(pattern))
    # Compare in memory only; do not print any secret, even on failure.
    from wallex_settings import settings
    secrets=[v.encode() for k,v in settings().items() if k.endswith('KEY') and len(v)>=8]
    checksums={}
    for path in files:
        if path.name.startswith('.env'):
            raise ValueError('Forbidden credential file')
        raw=path.read_bytes()
        if any(secret in raw for secret in secrets):
            raise ValueError('Credential detected; submission not generated')
        checksums[path.relative_to(ROOT).as_posix()]=hashlib.sha256(raw).hexdigest()
    target=ROOT/'reports/judge-evidence.zip'
    if target.exists():raise ValueError('Preserve prior submission bundle')
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:archive.write(path,path.relative_to(ROOT).as_posix())
        archive.writestr('SHA256SUMS.json',json.dumps(checksums,sort_keys=True,indent=2))
        archive.writestr('REPLAY.txt',
            'OFFLINE RESEARCH ONLY. No exchange orders. No API keys required for replay.\n'
            'Create Python 3.13 venv and install requirements-research.txt. Then:\n'
            '.venv/Scripts/python.exe challenge_research.py --llm replay\n'
            'All fills, fees and account results are hypothetical. Historical data is exploratory.\n'
            'See docs/SCORING_PLAN.md for limitations and score criteria.\n'
            'Only load the supplied locally generated model artifacts if you trust this source.\n')
    print(json.dumps(dict(artifact=str(target.relative_to(ROOT)),files=len(files),
                          credential_scan='passed',sha256=hashlib.sha256(target.read_bytes()).hexdigest())))
    # Verify extracted sources rather than accidentally importing the working tree.
    destination=(ROOT/'artifacts'/('judge-replay-'+uuid.uuid4().hex)).resolve()
    destination.mkdir(parents=True)
    with zipfile.ZipFile(target) as archive:
        if not all((destination/name).resolve().is_relative_to(destination) for name in archive.namelist()):
            raise ValueError('Unsafe artifact path')
        archive.extractall(destination)
    environment={k:v for k,v in os.environ.items()
                 if not k.startswith(('LLM_','WALLEX_','LANGSMITH_','LANGCHAIN_')) and k!='PYTHONPATH'}
    program=('import socket,runpy,sys; '
             'block=lambda *a,**k: (_ for _ in ()).throw(RuntimeError("Network forbidden during replay")); '
             'socket.create_connection=block; socket.socket.connect=block; '
             'sys.argv=["challenge_research.py","--llm","replay"]; '
             'runpy.run_path("challenge_research.py",run_name="__main__")')
    result=subprocess.run([sys.executable,'-X','utf8','-c',program],cwd=destination,
                          env=environment,capture_output=True,text=True,encoding='utf-8')
    if result.returncode:
        # Do not echo arbitrary exception bodies into exports or logs.
        raise RuntimeError('Extracted offline replay failed; inspect locally without exposing credentials')
    print(json.dumps(dict(extracted_bundle_replay='passed',network='blocked',
                          api_credentials='absent',source_directory=str(destination.relative_to(ROOT)))))


if __name__=='__main__':main()
