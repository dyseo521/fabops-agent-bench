"""Verify the published code against the test run manifests (no data, no API calls).

Each test run wrote runs/<id>/manifest.json with SHA-256 hashes of the harness code, the system
prompt, the tool definitions of every condition, and the glossary. This script recomputes those
hashes from the files in this repository and compares them with every runs/test_*/manifest.json.
It also checks the files listed in docs/registered_files.json against their Git blob IDs at the
registration tags.

Usage (repository root, agent environment from requirements-agent.txt):
    python scripts/verify_manifests.py

Line endings are normalized before hashing, so the result does not depend on the checkout
settings. Two ontology files (CRLF_AT_RUN) had Windows line endings in the author's working copy
when the runs were made, and the recorded code hash covers them in that form; the script hashes
them the same way.
"""
import glob
import hashlib
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
os.chdir(ROOT)

from agent import config, prompt  # noqa: E402
from agent.session import specs  # noqa: E402
from agent.trace import sha256  # noqa: E402

# Files covered by the code hash, in the same order as scripts/agent_run.py code_hash()
CODE_FILES = ['agent/pricing.yaml', 'scripts/gate.py', 'scripts/score.py', 'ontology/shapes_gate.ttl',
              'ontology/fab.ttl', 'ontology/fab_mapping.r2rml.ttl', 'sql/condition_b_views.sql']
CRLF_AT_RUN = {'ontology/shapes_gate.ttl', 'ontology/fab_mapping.r2rml.ttl'}


CR, LF = b'\r', b'\n'


def lf(path):
    return io.open(path, 'rb').read().replace(CR + LF, LF)


def code_hash():
    files = sorted(glob.glob(os.path.join(ROOT, 'agent', '**', '*.py'), recursive=True))
    rel = [os.path.relpath(f, ROOT).replace(os.sep, '/') for f in files] + CODE_FILES
    parts = []
    for r in rel:
        data = lf(os.path.join(ROOT, r))
        if r in CRLF_AT_RUN:
            data = data.replace(LF, CR + LF)
        parts.append(r + ':' + hashlib.sha256(data).hexdigest())
    return sha256(chr(10).join(parts))


def blob_id(path):
    data = lf(path)
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def main():
    ok = True
    code = code_hash()
    _, prompt_hashes = prompt.system_prompt()
    glossary = sha256(open(config.GLOSSARY, encoding='utf-8').read())
    manifests = sorted(glob.glob(os.path.join('runs', 'test_*', 'manifest.json')))
    print(f'Run manifests: {len(manifests)}')
    for m in manifests:
        d = json.load(io.open(m, encoding='utf-8'))
        ablation = d.get('ablation', 'none')
        tools = {c: sha256(json.dumps([[s.name, s.description, s.input_schema] for s in specs(c, ablation)],
                                      ensure_ascii=False, sort_keys=True)) for c in config.CONDITIONS}
        checks = {'code': d['code_sha256'] == code, 'tools': d['tool_hashes'] == tools}
        if ablation == 'none':  # ablation runs change the prompt or the glossary by design
            checks['prompt'] = d['prompt_hashes'] == prompt_hashes
            checks['glossary'] = d['glossary_sha256'] == glossary
        ok &= all(checks.values())
        head = (d.get('git') or {}).get('head', '')[:7]
        print(f'  {os.path.dirname(m):32s} ablation={ablation:16s} commit={head}  '
              + '  '.join(f'{k}={"ok" if v else "MISMATCH"}' for k, v in checks.items()))

    reg_path = os.path.join('docs', 'registered_files.json')
    if os.path.exists(reg_path):
        reg = json.load(io.open(reg_path, encoding='utf-8'))
        for tag, entry in reg['tags'].items():
            bad = [p for p, b in entry['files'].items() if not os.path.exists(p) or blob_id(p) != b]
            ok &= not bad
            print(f'Tag {tag} (commit {entry["commit"][:7]}): {len(entry["files"]) - len(bad)}/{len(entry["files"])} files match'
                  + (''.join(f'\n  MISMATCH {p}' for p in bad)))
    print('ALL MATCH' if ok else 'MISMATCH FOUND')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
