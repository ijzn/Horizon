"""Generate early; deliver once per channel at 09:00 Asia/Shanghai."""
import argparse
import asyncio
import fcntl
import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from src.storage.manager import StorageManager
from src.orchestrator import HorizonOrchestrator
from src.services.wechat_subscribers import SubscriberStore
from src.services.briefing_publish import load_preview, verify_published_detail

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
os.environ['HORIZON_CHATGPT_AUTH_DIR'] = str(ROOT / 'data/chatgpt-auth')
load_dotenv(ROOT / '.env')

def save(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)
    path.chmod(0o600)

async def main(stage):
    cfg = StorageManager().load_config()
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    report = ROOT / f'data/summaries/horizon-{today}-zh.md'
    ready = ROOT / 'data/ready.json'
    if stage == 'generate':
        if cfg.ai.model == 'PENDING_AUTH':
            raise RuntimeError('ChatGPT login and model selection are incomplete')
        local = cfg.model_copy(deep=True)
        local.webhook.enabled = False
        local.wechat.enabled = False
        started = time.time()
        class DailyOrchestrator(HorizonOrchestrator):
            async def analyze_items(self, items):
                analyzed = await super().analyze_items(items)
                save(ROOT / 'data/analysis-snapshot.json', {
                    'date': today,
                    'items': [item.model_dump(mode='json') for item in analyzed],
                })
                scored = sorted(
                    [item for item in analyzed if item.processing and item.processing.analysis],
                    key=lambda item: item.processing.analysis.score if item.processing.analysis.score is not None else -1,
                    reverse=True,
                )
                for item in scored[:8]:
                    print('Scoring diagnostic:', json.dumps({
                        'title': item.title[:160],
                        'profile': item.processing.classification.profile,
                        'score': item.processing.analysis.score,
                        'reason': item.processing.analysis.reason[:500],
                    }, ensure_ascii=False))
                return analyzed
        await DailyOrchestrator(local, StorageManager()).run(force_hours=24)
        if not report.exists() or report.stat().st_mtime < started:
            raise RuntimeError('No fresh Chinese briefing was generated')
        save(ready, {'date': today, 'generated_at': datetime.now().isoformat()})
        print('Fresh briefing prepared:', report.name)
        return
    if not ready.exists() or json.loads(ready.read_text()).get('date') != today:
        raise RuntimeError('No successfully prepared briefing for today; refusing stale delivery')
    preview = load_preview(ROOT, today, report.read_text())
    await verify_published_detail(ROOT, preview)
    summary = preview['message']
    if not cfg.wechat or not cfg.wechat.enabled:
        raise RuntimeError('WeChat delivery is disabled')
    # Scheduled delivery is limited to the owner until friends are explicitly enabled.
    # The manual CLI retains all-reader and selected-reader delivery.
    outcomes = await SubscriberStore(ROOT / 'data').broadcast(
        summary, cfg.wechat, subscriber_ids=['owner'], lang='zh'
    )
    for result in outcomes:
        print(f'{result.name}: {result.status}')
    if not outcomes:
        raise RuntimeError('No WeChat readers are registered')
    if any(result.status == 'failed' for result in outcomes):
        raise RuntimeError('Some WeChat readers did not receive the briefing; retry failed readers')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['generate', 'deliver'])
    args = parser.parse_args()
    with (ROOT / 'data/daily.lock').open('w') as lock:
        # Delivery waits if generation is still running; duplicate generators exit.
        flags = fcntl.LOCK_EX if args.stage == 'deliver' else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(lock, flags)
        asyncio.run(main(args.stage))
