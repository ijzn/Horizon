"""Safe mobile detail pages and compact, source-grounded WeChat introductions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date
import base64
import hashlib
import html
from html.parser import HTMLParser
import re
from typing import Mapping, Sequence
from urllib.parse import urlsplit

import markdown as markdown_library

from ..ai.markdown_utils import clean_app_summary_markdown


CATEGORIES = ("前端", "产品", "运营", "AI")


@dataclass(frozen=True)
class BriefingItem:
    title: str
    why_it_matters: str
    next_step: str
    evidence_limit: str
    anchor: str = ""
    category: str | None = None

    def __post_init__(self):
        if self.category is not None and self.category not in CATEGORIES:
            raise ValueError("Unsupported briefing category")


def _http_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or re.search(r"[\s\x00-\x1f\x7f]", value):
        raise ValueError("Invalid detail URL")
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError("Invalid detail URL")
        parts.port
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid detail URL") from exc
    return value


def _date(value: str) -> str:
    return Date.fromisoformat(value).isoformat()


def _normalize_escaped_lists(value: str) -> str:
    """Repair old literal list escapes only outside fenced code blocks."""
    lines, fence = [], None
    for line in value.splitlines(keepends=True):
        mark = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence is not None:
            lines.append(line)
            if mark and mark[1][0] == fence[0] and len(mark[1]) >= len(fence) and not mark[2].strip():
                fence = None
            continue
        if mark and not (mark[1][0] == "`" and "`" in mark[2]):
            fence = mark[1]
            lines.append(line)
            continue
        lines.append(re.sub(r"^( {0,3})\\(\d+[.)])(?=\s)", r"\1\2", line))
    return "".join(lines)


def _flatten_template_html(value: str) -> str:
    """Flatten only Horizon template HTML, without editing code contents."""
    output, prose, fence = [], [], None

    def flush():
        if not prose:
            return
        fragment = "".join(prose)
        prefix = "HORIZONCODETOKEN"
        while prefix in fragment:
            prefix += "X"
        codes = []

        def protect(match):
            codes.append(match.group(0))
            return f"{prefix}{len(codes) - 1}END"

        fragment = re.sub(r"(?<!`)(`+)(?!`)([^\n]*?)(?<!`)\1(?!`)", protect, fragment)
        fragment = clean_app_summary_markdown(fragment)
        for index, code in enumerate(codes):
            fragment = fragment.replace(f"{prefix}{index}END", code)
        output.append(fragment)
        prose.clear()

    for line in value.splitlines(keepends=True):
        mark = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
        if fence is not None:
            output.append(line)
            if mark and mark[1][0] == fence[0] and len(mark[1]) >= len(fence) and not mark[2].strip():
                fence = None
        elif mark and not (mark[1][0] == "`" and "`" in mark[2]):
            flush()
            fence = mark[1]
            output.append(line)
        else:
            prose.append(line)
    flush()
    return "".join(output)


class _SafeHTML(HTMLParser):
    """Sanitize renderer output with a narrow, explicit HTML vocabulary."""
    TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "strong", "em", "blockquote", "pre", "code", "a", "hr", "br", "table", "thead", "tbody", "tr", "th", "td", "del"}
    VOID = {"hr", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.stack = []
        self.section = 0

    def handle_starttag(self, tag, attrs):
        if tag not in self.TAGS:
            return
        attributes = dict(attrs)
        rendered = ""
        emitted = True
        if tag == "a":
            try:
                href = _http_url(attributes.get("href", ""))
            except ValueError:
                emitted = False
            else:
                rendered = f' href="{html.escape(href, quote=True)}" target="_blank" rel="noopener noreferrer"'
        elif tag == "code":
            language = attributes.get("class", "")
            if re.fullmatch(r"language-[A-Za-z0-9_+.-]{1,32}", language):
                rendered = f' class="{language}"'
        elif tag == "ol" and re.fullmatch(r"[0-9]{1,6}", attributes.get("start", "")):
            rendered = f' start="{attributes["start"]}"'
        elif tag == "h2":
            self.section += 1
            rendered = f' id="section-{self.section}"'
        if tag == "pre":
            self.output.append('<div class="code-panel"><button class="copy-code" type="button">复制代码</button>')
        if emitted:
            self.output.append(f"<{tag}{rendered}>")
        if tag not in self.VOID:
            self.stack.append((tag, emitted))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1][0] != tag:
            return
        _, emitted = self.stack.pop()
        if emitted:
            self.output.append(f"</{tag}>")
        if tag == "pre":
            self.output.append("</div>")

    def handle_data(self, data):
        self.output.append(html.escape(data, quote=False))


_COPY_SCRIPT = """document.querySelectorAll('.copy-code').forEach(button=>{button.addEventListener('click',async()=>{const text=button.parentElement.querySelector('code').textContent;try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(text);}else{const area=document.createElement('textarea');area.value=text;area.style.position='fixed';area.style.opacity='0';document.body.appendChild(area);area.select();const ok=document.execCommand('copy');area.remove();if(!ok)throw new Error('copy');}button.textContent='已复制';}catch(error){button.textContent='请长按代码复制';}setTimeout(()=>button.textContent='复制代码',2500);});});"""
_STYLE = """*{box-sizing:border-box}html{color-scheme:light}body{margin:0;background:#f4f6f8;color:#182230;font:17px/1.85 system-ui,-apple-system,'PingFang SC',sans-serif;overflow-wrap:anywhere}main{max-width:760px;margin:auto;padding:28px 20px 72px;background:#fff;min-height:100vh}header{border-bottom:1px solid #e3e8ef;padding-bottom:20px;margin-bottom:28px}header h1{font-size:28px;margin:0}header p{margin:6px 0;color:#526173;font-size:14px}h1,h2,h3,h4{line-height:1.5;scroll-margin-top:20px}h2{font-size:23px;border-top:1px solid #e3e8ef;padding-top:28px;margin-top:36px}h3{font-size:19px}p{margin:16px 0}a{color:#1264ba;text-decoration-thickness:1px;text-underline-offset:3px}li{margin:10px 0}blockquote{margin:20px 0;padding:10px 16px;border-left:4px solid #d29b28;background:#fff8e8;color:#534733}blockquote p{margin:4px 0}code{font:14px/1.65 ui-monospace,SFMono-Regular,Consolas,monospace;background:#eef2f6;padding:2px 4px;border-radius:4px}pre{margin:0;overflow-x:auto;padding:44px 16px 16px;white-space:pre;overflow-wrap:normal;background:#122033;color:#edf4ff;border-radius:10px}pre code{background:none;padding:0;color:inherit;white-space:pre} .code-panel{position:relative;margin:20px 0;min-width:0}.copy-code{position:absolute;right:8px;top:8px;background:#eaf1fa;color:#18314e;border:0;border-radius:5px;padding:5px 10px;min-height:30px;cursor:pointer}table{display:block;overflow-x:auto;border-collapse:collapse;font-size:14px}td,th{padding:8px 12px;border:1px solid #dae1e9}hr{border:0;border-top:1px solid #e3e8ef;margin:30px 0}@media(max-width:420px){main{padding:22px 16px 56px}header h1{font-size:25px}h2{font-size:21px}body{font-size:16px}}"""


def render_detail_html(markdown: str, date: str) -> str:
    """Render untrusted Markdown as a standalone, mobile-friendly detail page.

    Raw HTML is displayed literally, never activated. Shell code is not globally
    unescaped or normalized; only the HTML serializer escapes its text safely.
    """
    day = _date(date)
    converter = markdown_library.Markdown(extensions=["fenced_code", "tables", "sane_lists"])
    converter.preprocessors.deregister("html_block")
    converter.inlinePatterns.deregister("html")
    rendered = converter.convert(_flatten_template_html(_normalize_escaped_lists(markdown)))
    sanitizer = _SafeHTML()
    sanitizer.feed(rendered)
    sanitizer.close()
    content = "".join(sanitizer.output)
    script_hash = base64.b64encode(hashlib.sha256(_COPY_SCRIPT.encode()).digest()).decode()
    policy = f"default-src 'none'; script-src 'sha256-{script_hash}'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer"><meta http-equiv="Content-Security-Policy" content="{html.escape(policy, quote=True)}"><title>{day} · Horizon 日报详情</title><style>{_STYLE}</style></head><body><main><header><h1>Horizon 日报详情</h1><p>{day} · 完整分析与实践步骤</p><p>操作前请核对正文中的前置条件、验证方式与证据边界。</p></header><article>{content}</article></main><script>{_COPY_SCRIPT}</script></body></html>'''


def _plain(value: object, limit: int | None = None) -> str:
    text = str(value or "")
    text = re.sub(r"(?ms)^\s*(`{3,}|~{3,}).*?^\s*\1\s*$", "", text)
    text = re.sub(r"`+[^`]*`+", "", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]*>", "", html.unescape(text))
    text = re.sub(r"(?:⭐\ufe0f?\s*)?\d+(?:\.\d+)?\s*/\s*10", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[\x00-\x1f\x7f*_#`|]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if limit is None or len(text) <= limit else text[:limit - 1].rstrip("，。；、:： ") + "…"


def render_wechat_teaser(items: Sequence[BriefingItem | Mapping[str, object]], date: str, url: str) -> str:
    """Render supplied facts only; no model calls or invented recommendations.

    Classified items use the fixed category order, with at most one per category.
    Legacy unclassified callers retain their original first-four behavior.
    Complete semantic fields are never truncated. The standalone HTTP(S) URL is
    always the final line, and the entire introduction fits 900 characters.
    """
    day, url = _date(date), _http_url(url)
    if len(url) > 400:
        raise ValueError("Detail URL is too long for a compact teaser")
    title = f"Horizon 日报｜{day}\n工作里怎么用 AI？先看本期精选，步骤和条件见详情。"
    rendered = []

    def get(item, key):
        return item.get(key) if isinstance(item, Mapping) else getattr(item, key, None)

    categorized = not items or any(get(item, "category") is not None for item in items)
    selected = []
    missing = []
    if categorized:
        by_category = {}
        for item in items:
            category = get(item, "category")
            if category is None:
                continue
            if category not in CATEGORIES:
                raise ValueError("Unsupported briefing category")
            by_category.setdefault(category, item)
        selected = [(category, by_category[category]) for category in CATEGORIES if category in by_category]
        missing = [category for category in CATEGORIES if category not in by_category]
    else:
        # Compatibility for existing callers that predate classified briefings.
        selected = [(None, item) for item in items[:4]]
    for index, (category, item) in enumerate(selected, 1):
        heading = _plain(get(item, "title"), 24) or "今日观察"
        # Conditions and evidence qualifiers are semantic, not expendable tails.
        # Upstream must shorten these complete statements if the budget fails.
        why = _plain(get(item, "why_it_matters"))
        action = _plain(get(item, "next_step"))
        evidence = _plain(get(item, "evidence_limit"))
        label = f"【{category}】" if category else f"{index}. "
        lines = [f"{label}{heading}"]
        if why:
            lines.append(f"看点：{why}")
        if action:
            lines.append(f"可做：{action}")
        if evidence:
            lines.append(f"边界：{evidence}")
        rendered.append("\n".join(lines))
    if categorized and missing:
        rendered.append("本期未选：" + "、".join(missing))
    elif not categorized and len(items) > len(selected):
        rendered.append(f"另有 {len(items) - len(selected)} 条内容，见完整详情。")
    elif not items:
        rendered.append("今天的完整内容已整理，点击下方查看。")
    result = title + "\n\n" + "\n\n".join(rendered) + "\n\n完整详情（含步骤、条件与原文）：\n" + url
    if len(result) > 900:
        raise ValueError("Teaser exceeds presentation budget; shorten complete statements upstream")
    return result
