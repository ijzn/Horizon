"""Retrieve only this trusted workflow's newest encrypted state. Never silently reset."""
import base64
from datetime import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import zipfile

LIMIT = 48 * 1024 * 1024
OUTPUT = Path('/tmp/horizon-state.enc')


def api(path):
    result = subprocess.run(['gh', 'api', path], check=True, capture_output=True)
    if len(result.stdout) > LIMIT:
        raise RuntimeError('State response exceeds limit')
    return result.stdout


def main():
    OUTPUT.unlink(missing_ok=True)
    if os.environ.get('BOOTSTRAP') == 'true':
        raw = os.environ.get('HORIZON_BOOTSTRAP_STATE', '')
        if not raw:
            raise RuntimeError('Encrypted bootstrap secret is missing')
        OUTPUT.write_bytes(base64.b64decode(raw, validate=True))
    else:
        repo = os.environ['GH_REPO']
        # Artifact IDs are not guaranteed to be chronological. Use server time.
        artifacts = json.loads(api(f'repos/{repo}/actions/artifacts?name=personal-horizon-state&per_page=100'))['artifacts']
        chosen = None
        def created(artifact):
            stamp = datetime.fromisoformat(artifact['created_at'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError('Artifact creation time must include a timezone')
            return (stamp, artifact['id'])
        for artifact in sorted(artifacts, key=created, reverse=True):
            run_id = artifact.get('workflow_run', {}).get('id')
            if not run_id:
                continue
            run = json.loads(api(f'repos/{repo}/actions/runs/{run_id}'))
            if run.get('path') != os.environ['EXPECTED_WORKFLOW'] or run.get('head_branch') != 'main':
                continue
            chosen = artifact
            break
        if chosen is None or chosen.get('expired'):
            raise RuntimeError('No current state: explicit manual bootstrap required; automatic reset refused')
        raw = api(f"repos/{repo}/actions/artifacts/{chosen['id']}/zip")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            info = archive.infolist()
            if len(info) != 1 or info[0].filename != 'horizon-state.enc' or info[0].file_size > LIMIT:
                raise RuntimeError('Invalid state artifact')
            OUTPUT.write_bytes(archive.read(info[0]))
        print(f"Restoring state artifact {chosen['id']} created {chosen['created_at']}")
    OUTPUT.chmod(0o600)
    print('Encrypted state retrieved; plaintext is never uploaded.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        raise SystemExit(f'State restore failed ({type(exc).__name__}); stopping without resetting state')
