"""Validate the shipped four-role config and its actual selection quotas, offline."""
from datetime import datetime, timezone
import asyncio
import json
from pathlib import Path
import pytest

from src.models import Config, ContentItem, SourceType, ProcessingResult, ClassificationResult, ContentAnalysis
from src.orchestrator import HorizonOrchestrator
from src.processing import ProfileRegistry
from src.processing.content import select_content
from src.ai.analyzer import ContentAnalyzer

ROOT = Path(__file__).parents[1]
IDS = ['work-frontend', 'work-product', 'work-operations', 'work-ai']


def configuration():
    return Config.model_validate(json.loads((ROOT / 'profiles/personal-radar/config.public-web.json').read_text()))


def test_work_config_and_profiles_load_with_chinese_section_order():
    cfg = configuration()
    registry = ProfileRegistry.load(ROOT / cfg.processing.profiles_dir, cfg.processing.default_profile)
    registry.validate_source_references(cfg.sources.model_dump(mode='json'))
    registry.validate_digest_references(cfg.digest)
    assert cfg.processing.default_profile == 'work-ai'
    assert cfg.digest.profile_order == IDS
    assert [registry.get(pid).definition.display_names['zh'] for pid in IDS] == ['前端', '产品', '运营', 'AI']
    assert set(cfg.processing.profile_settings) == set(IDS)
    for pid in IDS:
        assert cfg.processing.profile_settings[pid].threshold == 7
        impact = next(block for block in registry.get(pid).definition.enrichment.blocks if block.id == 'impact')
        assert not impact.optional


def test_public_sources_only_route_to_the_four_candidate_profiles():
    cfg = configuration()
    source_dict = cfg.sources.model_dump(mode='json')
    actual = [entry for entry in source_dict['rss']]
    actual += [source_dict[k] for k in ['hackernews', 'web_search', 'google_news']]
    assert len(source_dict['rss']) >= 11
    for entry in actual:
        assert entry['profile'] == IDS
    search = source_dict['web_search']
    # Every query can contribute without the global cap truncating later roles.
    assert len(search['queries']) * search['max_results_per_query'] <= search['max_total_results'] <= 60
    assert len(search['queries']) >= 8
    assert all(not query.startswith('site:') for query in search['queries'][:8])
    assert sum(query.startswith('site:') for query in search['queries']) >= 4


def item(pid, rank, score):
    return ContentItem(id=f'{pid}-{rank}', title=f'{pid}-{rank}', source_type=SourceType.RSS,
        url=f'https://example.com/{pid}/{rank}', published_at=datetime.now(timezone.utc),
        metadata={'category': 'same-source-category'}, profile=IDS,
        processing=ProcessingResult(classification=ClassificationResult(profile=pid, method='ai_match'),
            analysis=ContentAnalysis(score=score, reason='fixture evidence', summary='fixture task')))


def select(items):
    orchestrator = HorizonOrchestrator.__new__(HorizonOrchestrator)
    orchestrator.config = configuration()
    eligible = [value for value in items if orchestrator.passes_profile_filter(value)]
    return orchestrator.apply_balanced_digest(eligible, log=False)


def test_each_role_gets_at_most_one_even_when_frontend_dominates_scores():
    values = [item('work-frontend', n, 10 - n * .1) for n in range(8)]
    values += [item(pid, 0, 7.5) for pid in IDS[1:]]
    result = select(values)
    assert len(result.items) == 4
    assert result.group_counts == {pid: 1 for pid in IDS}
    assert result.items[0].id == 'work-frontend-0'


def test_missing_or_low_quality_roles_do_not_get_filler():
    result = select([item('work-frontend', 0, 8), item('work-frontend', 1, 7.9),
                     item('work-product', 0, 6.9), item('work-ai', 0, 7)])
    assert [value.id for value in result.items] == ['work-frontend-0', 'work-ai-0']
    assert result.group_counts == {'work-frontend': 1, 'work-ai': 1}


@pytest.mark.parametrize('pid', IDS)
def test_real_analyzer_receives_late_practice_evidence_without_model_calls(pid):
    """Regression: the former 1000-char prefix hid a long tutorial's verification."""
    registry = ProfileRegistry.load(ROOT / 'profiles', 'work-ai')
    evidence = 'VERIFICATION_EVIDENCE: compare the generated output against the reference fixture.'
    content = 'Introductory context. ' * 250 + 'Detailed implementation. ' * 500 + evidence
    assert evidence not in select_content(content, 1000, 'prefix')
    captured = []

    class OfflineClient:
        async def complete(self, **kwargs):
            captured.append(kwargs)
            return json.dumps({'score': 7, 'reason': 'fixture only', 'summary': 'fixture', 'tags': ['fixture']})

    value = item(pid, 0, 8)
    value.profile = pid  # Source override avoids an unrelated classifier call.
    value.content = content
    asyncio.run(ContentAnalyzer(OfflineClient(), registry)._analyze_item(value))
    assert len(captured) == 1
    assert evidence in captured[0]['user']
    assert '[Opening excerpt]' in captured[0]['user']
    assert '[Closing excerpt]' in captured[0]['user']
    assert len(select_content(content, registry.get(pid).definition.content.analysis_max_chars,
                              registry.get(pid).definition.content.sampling)) <= 8000
    assert value.processing.analysis.score == 7
