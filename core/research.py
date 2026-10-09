"""Deep research mode — Perplexity/Grok-style research, Jade's spin.

``/research <topic>`` orchestrates multi-step web research:

1. Plan — the LLM turns the topic into a bounded set of sub-queries.
2. Search + fetch — each query goes through web search; top pages are
   fetched with ``tools/browser.py`` (honest backend probing, no fake JS).
3. Extract — relevant snippets are pulled around query terms.
4. Report — the LLM compiles a structured Markdown report with numbered
   citations. Every ``[n]`` is validated against the fetched source list;
   out-of-range citations are stripped and flagged. No fabricated
   citations, ever.

Bounded by default: 4 queries, 8 pages, 20s per fetch. Progress shown live.
"""
import re
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()

DEFAULT_MAX_QUERIES = 4
DEFAULT_MAX_PAGES = 8
DEFAULT_FETCH_TIMEOUT = 20.0
SNIPPET_CHARS = 1200


def _extract_json(text: str) -> Dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found")
    import json
    return json.loads(text[start:end + 1])


def plan_queries(topic: str, max_queries: int = DEFAULT_MAX_QUERIES) -> List[str]:
    """Turn a topic into bounded search sub-queries. Falls back to [topic]."""
    from core.brain import think_structured
    prompt = (
        "You plan web research. Given the topic below, write search-engine "
        f"queries (at most {max_queries}) that together cover it from "
        "different angles. Return STRICT JSON only, no commentary:\n"
        '{"queries": ["query one", "query two"]}\n\n'
        f"TOPIC: {topic}"
    )
    try:
        res = think_structured("Research Planner", prompt)
        data = _extract_json(res.get("text", ""))
        queries = [str(q)[:200] for q in data.get("queries", []) if str(q).strip()]
        if queries:
            return queries[:max_queries]
    except Exception:
        pass
    return [topic]


def parse_search_links(search_text: str) -> List[Dict]:
    """Parse 'Title: / Link: / Snippet:' blocks from tools/search.py output."""
    out, current = [], {}
    for line in (search_text or "").splitlines():
        line = line.strip()
        if line.startswith("Title:"):
            current = {"title": line[6:].strip()}
        elif line.startswith("Link:") and current is not None:
            current["url"] = line[5:].strip()
        elif line.startswith("Snippet:") and current is not None:
            current["snippet"] = line[8:].strip()
            if current.get("url"):
                out.append(current)
            current = {}
    return out


def _relevant_windows(text: str, terms: List[str],
                      window: int = SNIPPET_CHARS) -> str:
    """Extract text windows around query-term hits; fall back to the head."""
    lowered = text.lower()
    hits = []
    for term in terms:
        idx = lowered.find(term.lower())
        if idx != -1:
            hits.append(idx)
    if not hits:
        return text[:window]
    hits.sort()
    chunks, last_end = [], -1
    per = window // max(1, min(len(hits), 3))
    for h in hits[:3]:
        s = max(0, h - per // 3)
        e = min(len(text), h + per * 2 // 3)
        if s > last_end:
            chunks.append(text[s:e].strip())
            last_end = e
    return "\n…\n".join(chunks)[:window]


def gather_sources(topic: str, queries: List[str],
                   max_pages: int = DEFAULT_MAX_PAGES,
                   fetch_timeout: float = DEFAULT_FETCH_TIMEOUT,
                   show_progress: bool = True) -> List[Dict]:
    """Search + fetch + extract. Returns [{url, title, snippet}]."""
    from tools.search import web_search
    from tools.browser import fetch_text

    sources: List[Dict] = []
    seen = set()

    def _step(desc: str):
        if show_progress:
            console.print(f"[dim]{desc}[/dim]")

    for qi, q in enumerate(queries):
        if len(sources) >= max_pages:
            break
        _step(f"🔎 query {qi + 1}/{len(queries)}: {q[:70]}")
        try:
            raw = web_search(q)
        except Exception as e:
            _step(f"search failed: {e}")
            continue
        if not isinstance(raw, str) or "Link:" not in raw:
            _step("no usable search results")
            continue
        terms = [t for t in re.findall(r"\w+", q.lower()) if len(t) > 3][:6]
        for link in parse_search_links(raw):
            if len(sources) >= max_pages:
                break
            url = link.get("url", "")
            host = urlparse(url).netloc
            if not url or url in seen or not host:
                continue
            seen.add(url)
            _step(f"📄 fetching {host}…")
            try:
                page = fetch_text(url, timeout=fetch_timeout)
            except Exception as e:
                _step(f"fetch failed: {e}")
                continue
            text = (page.get("text") or "").strip()
            if not text:
                continue
            sources.append({
                "url": url,
                "title": link.get("title") or host,
                "snippet": _relevant_windows(text, terms),
                "backend": page.get("backend", "?"),
            })
    return sources


def _validate_citations(report: str, n_sources: int) -> tuple:
    """Rewrite out-of-range [n] citations; return (report, n_stripped).

    The model is instructed to cite only listed sources, but this is the
    enforcement: a citation that doesn't map to a fetched source is removed
    rather than left dangling.
    """
    stripped = 0

    def _fix(m):
        nonlocal stripped
        n = int(m.group(1))
        if 1 <= n <= n_sources:
            return m.group(0)
        stripped += 1
        return "[citation unavailable]"

    fixed = re.sub(r"\[(\d+)\]", _fix, report)
    return fixed, stripped


def compile_report(topic: str, sources: List[Dict]) -> Dict:
    """Synthesize the final cited report from fetched sources."""
    from core.brain import think_structured
    if not sources:
        return {"ok": False, "error": "no sources fetched",
                "next_steps": "Check network access, then retry. "
                              "Web search needs the 'ddgs' package."}
    numbered = "\n\n".join(
        f"[{i + 1}] {s['title']} — {s['url']}\n{s['snippet']}"
        for i, s in enumerate(sources)
    )
    prompt = (
        "Write a structured research report in Markdown on the topic below, "
        "using ONLY the numbered sources provided. Rules:\n"
        "- Every factual claim must end with a citation like [1], [2].\n"
        "- Cite ONLY the sources listed — never invent a source number.\n"
        "- Sections: ## Summary, ## Key findings, ## Details, "
        "## Open questions / caveats, ## Sources (list all with URLs).\n"
        "- If sources disagree, say so explicitly.\n\n"
        f"TOPIC: {topic}\n\nSOURCES:\n{numbered}"
    )
    res = think_structured("Deep Research", prompt)
    text = (res.get("text") or "").strip()
    if not res.get("ok") or not text:
        return {"ok": False, "error": res.get("error") or "synthesis failed"}
    text, stripped = _validate_citations(text, len(sources))
    if stripped:
        text += (f"\n\n> ⚠️ {stripped} citation(s) the model generated did not "
                 "match any fetched source and were removed.")
    return {"ok": True, "report": text, "stripped_citations": stripped}


def run_research(topic: str, max_queries: int = DEFAULT_MAX_QUERIES,
                 max_pages: int = DEFAULT_MAX_PAGES,
                 fetch_timeout: float = DEFAULT_FETCH_TIMEOUT) -> Dict:
    """Full pipeline: plan → gather → compile. Returns a rich result dict."""
    topic = topic.strip()
    if not topic:
        return {"ok": False, "error": "empty topic"}
    started = time.time()
    console.print("[bold cyan]🔬 Planning research…[/bold cyan]")
    queries = plan_queries(topic, max_queries=max_queries)
    console.print(f"[dim]{len(queries)} sub-queries planned[/dim]")
    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  transient=True) as progress:
        progress.add_task("gathering sources…", total=None)
        sources = gather_sources(topic, queries, max_pages=max_pages,
                                 fetch_timeout=fetch_timeout,
                                 show_progress=False)
    console.print(f"[dim]{len(sources)} page(s) fetched[/dim]")
    if not sources:
        return {"ok": False, "error": "no sources could be fetched",
                "next_steps": "Check network access and the 'ddgs' package, then retry."}
    console.print("[bold cyan]🔬 Compiling report…[/bold cyan]")
    compiled = compile_report(topic, sources)
    if not compiled.get("ok"):
        return compiled
    return {"ok": True, "report": compiled["report"], "sources": sources,
            "queries": queries, "elapsed_s": round(time.time() - started, 1),
            "stripped_citations": compiled.get("stripped_citations", 0)}


def display_research_result(res: Dict) -> None:
    if not res.get("ok"):
        from core.ui import ui_error
        console.print(ui_error("Research failed", res.get("error", "unknown"),
                               next_steps=res.get("next_steps", "Try again.")))
        return
    from rich.markdown import Markdown
    console.print(Panel(Markdown(res["report"]), title="[bold cyan]🔬 Research report[/bold cyan]",
                        border_style="cyan"))
    console.print(f"[dim]Sources: {len(res.get('sources', []))} · "
                  f"{res.get('elapsed_s', '?')}s[/dim]")
