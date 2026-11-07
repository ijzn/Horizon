import asyncio
from datetime import datetime,timezone
from src.models import ContentItem,SourceType
from src.processing.tools import WebSearchTool
from src.scrapers.base import FetchResult


def test_background_search_reads_body_without_news_time_filter(monkeypatch):
    from src.scrapers import web_search
    observed=[]
    class Search:
        def __init__(self,config,client):observed.append(config)
        async def fetch(self,since):
            return FetchResult(successful=1,items=[ContentItem(id='web:doc',source_type=SourceType.WEB_SEARCH,title='Official docs',url='https://example.com/docs',published_at=datetime.now(timezone.utc),content='Article text was extracted.\nVerified instructions')])
    monkeypatch.setattr(web_search,'WebSearchScraper',Search)
    result=asyncio.run(WebSearchTool().execute({'query':'official docs'}))
    assert observed[0].backend=='bing'
    assert observed[0].time_filter is None
    assert observed[0].fetch_body is True
    assert result[0]['text'].endswith('Verified instructions')
    assert result[0]['url']=='https://example.com/docs'
