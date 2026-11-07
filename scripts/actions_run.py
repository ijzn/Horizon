"""Run the personal daily pipeline in an ephemeral Actions worker."""
import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.models import Config
from src.services.briefing_publish import prepare_presentation

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['generate', 'deliver', 'run', 'format'])
    phases = parser.add_mutually_exclusive_group()
    phases.add_argument('--prepare-only', action='store_true')
    phases.add_argument('--send-only', action='store_true')
    args = parser.parse_args()
    os.chdir(ROOT)
    os.environ['HORIZON_CHATGPT_AUTH_DIR'] = str(ROOT / 'data/chatgpt-auth')
    config = json.loads((ROOT / 'profiles/personal-radar/config.public-web.json').read_text())
    config['ai']['model'] = os.environ.get('HORIZON_MODEL', 'gpt-6.1-sol')
    (ROOT / 'data').mkdir(mode=0o700, exist_ok=True)
    path = ROOT / 'data/config.json'
    path.write_text(json.dumps(config, ensure_ascii=False))
    path.chmod(0o600)
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    report = ROOT / f'data/summaries/horizon-{today}-zh.md'
    ready = ROOT / 'data/ready.json'

    def current():
        try:
            marker = json.loads(ready.read_text())
            return marker.get('date') == today and marker.get('sha256') == hashlib.sha256(report.read_bytes()).hexdigest() and report.stat().st_size > 0
        except (OSError, ValueError):
            return False

    if not args.send_only and args.stage in {'generate', 'run'} and (args.stage == 'generate' or not current()):
        subprocess.run([sys.executable, 'daily-local.py', 'generate'], check=True)
        marker = json.loads(ready.read_text())
        if marker.get('date') != today or not report.read_text().strip():
            raise RuntimeError('Fresh daily report was not generated')
        marker['sha256'] = hashlib.sha256(report.read_bytes()).hexdigest()
        ready.write_text(json.dumps(marker))
        ready.chmod(0o600)
    if not args.send_only:
        if not current():
            raise RuntimeError('No verified report for today; run generate first')
        asyncio.run(prepare_presentation(ROOT, today, report.read_text(), Config.model_validate(config).ai))
    if args.prepare_only:
        return
    if args.stage in {'deliver', 'run'}:
        if not current():
            raise RuntimeError('No verified report for today; run generate first')
        subprocess.run([sys.executable, 'daily-local.py', 'deliver'], check=True)


if __name__ == '__main__':
    main()
