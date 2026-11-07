"""Presentation tests use dummy articles and never publish or send messages."""
from html.parser import HTMLParser
import re

import pytest

from src.services.briefing_presentation import BriefingItem, render_detail_html, render_wechat_teaser


class Inspect(HTMLParser):
    def __init__(self, value):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.links = []
        self.codes = []
        self.in_code = False
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag == "a":
            self.links.append(dict(attrs)["href"])
        if tag == "code":
            self.codes.append("")
            self.in_code = True

    def handle_endtag(self, tag):
        if tag == "code":
            self.in_code = False

    def handle_data(self, text):
        if self.in_code:
            self.codes[-1] += text


def test_mobile_detail_has_semantic_sections_scrollable_copyable_code():
    command = "git rev-list --objects --all | awk '$1==\"blob\" {print $3, $4}'\nprintf '中文value &amp; &#x27; <literal>'\n"
    source = f"## Git 大对象诊断\n\n### 具体做法\n\n```sh\n{command}```\n\n### 前置条件\n\n已安装 Git，使用副本。\n\n### 证据边界\n\n> 原文实验，未独立复测。\n\n[原文](https://example.com/article?a=1&b=2)"
    page = render_detail_html(source, "2026-11-07")
    parsed = Inspect(page)
    assert parsed.codes == [command]
    assert parsed.links == ["https://example.com/article?a=1&b=2"]
    assert 'name="viewport"' in page and "overflow-x:auto" in page
    assert "navigator.clipboard.writeText(text)" in page
    assert "querySelector('code').textContent" in page
    assert 'id="section-1"' in page
    assert "具体做法" in page and "前置条件" in page and "证据边界" in page
    assert 'http-equiv="Content-Security-Policy"' in page


def test_escaped_legacy_numbered_lists_are_repaired_except_inside_code():
    source = "\\1. 首先检查\n\\2. 然后记录\n\n```text\n\\1. must stay byte-identical\n```"
    page = render_detail_html(source, "2026-11-07")
    assert "<ol>" in page and "<li>首先检查</li>" in page
    assert Inspect(page).codes == ["\\1. must stay byte-identical\n"]


@pytest.mark.parametrize("attack", [
    '<script>alert(1)</script>', '<img src=x onerror="alert(1)">',
    '<iframe src="https://evil.example"></iframe>', '<a href="https://evil.example">raw HTML link</a>',
    '<svg onload=alert(1)>', '<style>body{display:none}</style>',
])
def test_raw_html_is_literal_and_cannot_become_active(attack):
    parsed = Inspect(render_detail_html(attack, "2026-11-07"))
    assert not parsed.links
    assert all(tag not in {"iframe", "svg", "img"} for tag, _ in parsed.tags)
    # Exactly the module's fixed copy script and stylesheet may execute.
    assert sum(tag == "script" for tag, _ in parsed.tags) == 1
    assert sum(tag == "style" for tag, _ in parsed.tags) == 1
    assert not any(any(key.startswith("on") for key in attrs) for _, attrs in parsed.tags)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,bad", "file:///etc/passwd", "//evil.example/a", "mailto:a@example.com", "https://user:password@example.com/"])
def test_dangerous_or_non_http_markdown_links_are_not_active(url):
    assert not Inspect(render_detail_html(f"[link]({url})", "2026-11-07")).links


def test_code_looks_like_html_but_is_only_text():
    code = '<script>alert("x")</script>\n[a](javascript:alert(1))\n'
    page = render_detail_html(f"```html\n{code}```", "2026-11-07")
    parsed = Inspect(page)
    assert parsed.codes == [code]
    assert not parsed.links and sum(tag == "script" for tag, _ in parsed.tags) == 1


def test_code_entities_and_backslashes_are_not_globally_unescaped():
    code = "printf '&amp; &#x27; \\| \\1. \\(x\\)'\n"
    assert Inspect(render_detail_html(f"```sh\n{code}```", "2026-11-07")).codes == [code]


def item(index=1):
    return BriefingItem(
        title=f"{index}｜用 Git 找出历史大对象",
        why_it_matters="文件删了仓库仍很大，可能是历史仍引用旧对象。",
        next_step="在副本列出大对象，记录路径与大小，再判断是否需要清理。",
        evidence_limit="作者单次实验，未独立复测。",
        anchor=f"section-{index}",
    )


def test_teaser_is_short_specific_no_scores_code_or_toc_and_ends_with_full_url():
    url = "https://example.com/daily/2026-11-07.html"
    text = render_wechat_teaser([item(1), item(2), item(3), item(4), item(5)], "2026-11-07", url)
    assert len(text) <= 900
    assert text.endswith("\n" + url)
    assert "看点：文件删了仓库仍很大" in text and "可做：在副本列出大对象" in text
    assert "边界：作者单次实验" in text
    assert "另有 1 条" in text
    assert "```" not in text and "目录" not in text and "⭐" not in text
    entries = [entry for entry in text.split("\n\n") if re.match(r"^\d\. ", entry)]
    assert all(80 <= len(entry) <= 140 for entry in entries)


def test_dict_interface_and_no_model_fact_invention():
    text = render_wechat_teaser([{"title": "唯一标题", "why_it_matters": "唯一事实", "next_step": "唯一动作", "evidence_limit": "唯一边界", "anchor": "section-1"}], "2026-11-07", "https://example.com/a")
    for fact in ["唯一标题", "唯一事实", "唯一动作", "唯一边界"]:
        assert fact in text
    assert "已验证" not in text and "亲测" not in text


def test_teaser_strips_markup_scores_code_and_inline_urls():
    content = {"title": "## 标题 ⭐ 8.5/10", "why_it_matters": "[事实](https://example.com/a) <img src=x onerror=alert(1)>", "next_step": "运行 `rm -rf /`，操作见详情。\n```sh\nSECRET_COMMAND\n```", "evidence_limit": "短边界"}
    text = render_wechat_teaser([content], "2026-11-07", "https://example.com/detail")
    assert "8.5/10" not in text and "⭐" not in text and "##" not in text
    assert "rm -rf" not in text and "SECRET_COMMAND" not in text and "<img" not in text
    assert "https://example.com/a" not in text


def test_teaser_long_fields_fail_instead_of_truncating_conditions():
    with pytest.raises(ValueError, match="shorten complete statements"):
        render_wechat_teaser([{key: "长内容" * 200 for key in ["title", "why_it_matters", "next_step", "evidence_limit"]}] * 4, "2026-11-07", "https://example.com/detail")


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://a.com/\nspoof", "https://a.com/" + "x" * 500, "//a.com", "https://user:secret@a.com"])
def test_teaser_rejects_unusable_detail_url(url):
    with pytest.raises(ValueError):
        render_wechat_teaser([item()], "2026-11-07", url)


def test_empty_teaser_and_invalid_dates():
    assert render_wechat_teaser([], "2026-11-07", "http://example.com").endswith("http://example.com")
    with pytest.raises(ValueError):
        render_detail_html("hello", '2026-11-07<script>')


def test_horizon_template_references_are_readable_without_mutating_code_html():
    source = '''<a id="item-1"></a>
## Article

<details><summary>参考资料</summary>
<ul><li><a href="https://example.com/source">原文</a></li></ul>
</details>

`<a id="literal-inline"></a>`

```html
<a id="literal-fenced"></a>
<details>literal</details>
```
'''
    page = render_detail_html(source, "2026-11-07")
    parsed = Inspect(page)
    assert "https://example.com/source" in parsed.links
    assert "参考资料" in page
    assert "item-1" not in page
    assert parsed.codes == ['<a id="literal-inline"></a>', '<a id="literal-fenced"></a>\n<details>literal</details>\n']



def classified(category, title=None):
    return BriefingItem(title=title or str(category) + "具体工作应用", why_it_matters="减少重复整理资料的时间。", next_step="仅在有脱敏样本和人工复核条件时试用，先比较一次人工结果。", evidence_limit="原文案例，效果需本地验证。", category=category)


def test_categories_are_fixed_order_one_per_category_with_explicit_labels():
    inputs = [classified("AI"), classified("运营"), classified("前端", "前端首条"), classified("产品"), classified("前端", "前端重复")]
    result = render_wechat_teaser(inputs, "2026-11-07", "https://example.com/detail")
    assert result.index("【前端】") < result.index("【产品】") < result.index("【运营】") < result.index("【AI】")
    assert result.count("【前端】") == 1 and "前端重复" not in result
    assert "本期未选" not in result
    assert len(result) <= 900 and result.endswith("https://example.com/detail")


def test_missing_categories_are_named_without_filler():
    inputs = [classified("AI"), classified("前端")]
    result = render_wechat_teaser(inputs, "2026-11-07", "https://example.com/detail")
    assert "本期未选：产品、运营" in result
    assert "【产品】" not in result and "【运营】" not in result
    assert result.index("【前端】") < result.index("【AI】")


def test_category_dicts_and_full_qualifying_conditions_are_preserved():
    condition = "先确保有可脱敏的真实工作样本、独立人工基线和负责人审核；只有三项都满足时，再做一次受控对照，不能直接替代生产流程。"
    result = render_wechat_teaser([{"category": "产品", "title": "需求整理", "why_it_matters": "减少重复工作", "next_step": condition, "evidence_limit": "尚未在本团队实测"}], "2026-11-07", "https://example.com/detail")
    assert condition in result
    assert "本期未选：前端、运营、AI" in result
    assert "…" not in result


@pytest.mark.parametrize("category", ["设计", "ai", "", 1])
def test_unknown_category_is_rejected_for_dataclass_and_dict(category):
    with pytest.raises(ValueError):
        classified(category)
    with pytest.raises(ValueError):
        render_wechat_teaser([{"category": category, "title": "bad"}], "2026-11-07", "https://example.com/detail")



def test_zero_selected_items_lists_all_missing_categories():
    result = render_wechat_teaser([], "2026-11-07", "https://example.com/detail")
    assert "本期未选：前端、产品、运营、AI" in result
    assert "【前端】" not in result
