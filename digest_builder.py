"""
Digest builder — fetches content from 50+ sources and uses Claude to synthesize
a structured 20-point AI news digest in Russian.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DUBAI_TZ = timezone(timedelta(hours=4))

# ── Seen headlines tracking (persisted to file) ───────────────────────────────
SEEN_HEADLINES_FILE = "/tmp/seen_headlines.json"
MAX_SEEN_HEADLINES = 500  # keep last N headlines to avoid repeats

def _load_seen_headlines() -> set:
    try:
        if os.path.exists(SEEN_HEADLINES_FILE):
            with open(SEEN_HEADLINES_FILE, "r") as f:
                return set(json.load(f))
    except Exception:
        pass
    return set()

def _save_seen_headlines(headlines: set) -> None:
    try:
        # Keep only the last MAX_SEEN_HEADLINES entries
        items = list(headlines)[-MAX_SEEN_HEADLINES:]
        with open(SEEN_HEADLINES_FILE, "w") as f:
            json.dump(items, f)
    except Exception as e:
        logger.warning(f"Could not save seen headlines: {e}")

# ── Source list (50+ sources) ────────────────────────────────────────────────

RSS_SOURCES = [
    # Official AI labs
    {"name": "Anthropic Blog",        "url": "https://www.anthropic.com/news/rss.xml"},
    {"name": "OpenAI Blog",           "url": "https://openai.com/news/rss.xml"},
    {"name": "Google DeepMind",       "url": "https://deepmind.google/blog/rss.xml"},
    {"name": "Meta AI Blog",          "url": "https://ai.meta.com/blog/rss/"},
    {"name": "Microsoft Research AI", "url": "https://www.microsoft.com/en-us/research/feed/"},
    {"name": "NVIDIA AI Blog",        "url": "https://blogs.nvidia.com/feed/"},
    {"name": "Mistral AI Blog",       "url": "https://mistral.ai/news/rss"},
    {"name": "Hugging Face Blog",     "url": "https://huggingface.co/blog/feed.xml"},
    {"name": "Cohere Blog",           "url": "https://cohere.com/blog/rss"},
    {"name": "Stability AI Blog",     "url": "https://stability.ai/news/rss.xml"},

    # Tech media
    {"name": "TechCrunch AI",         "url": "https://techcrunch.com/category/artificial-intelligence/feed/"},
    {"name": "VentureBeat AI",        "url": "https://venturebeat.com/category/ai/feed/"},
    {"name": "The Verge AI",          "url": "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml"},
    {"name": "Wired AI",              "url": "https://www.wired.com/feed/tag/ai/latest/rss"},
    {"name": "MIT Tech Review AI",    "url": "https://www.technologyreview.com/feed/"},
    {"name": "ZDNet AI",              "url": "https://www.zdnet.com/topic/artificial-intelligence/rss.xml"},
    {"name": "InfoQ AI",              "url": "https://feed.infoq.com/"},
    {"name": "Ars Technica AI",       "url": "https://arstechnica.com/gadgets/feed/"},
    {"name": "IEEE Spectrum AI",      "url": "https://spectrum.ieee.org/feeds/topic/artificial-intelligence.rss"},
    {"name": "Analytics Vidhya",      "url": "https://www.analyticsvidhya.com/feed/"},

    # Newsletters & aggregators
    {"name": "The Batch (Andrew Ng)", "url": "https://www.deeplearning.ai/the-batch/feed/"},
    {"name": "Import AI",             "url": "https://jack-clark.net/feed/"},
    {"name": "Ben's Bites",           "url": "https://bensbites.beehiiv.com/feed"},
    {"name": "TLDR AI",               "url": "https://tldr.tech/ai/rss"},
    {"name": "AlphaSignal",           "url": "https://alphasignal.ai/rss"},

    # Business & enterprise AI
    {"name": "Harvard Business Review AI", "url": "https://hbr.org/topic/ai/rss"},
    {"name": "McKinsey AI Insights",  "url": "https://www.mckinsey.com/capabilities/quantumblack/rss"},
    {"name": "Gartner Newsroom",      "url": "https://www.gartner.com/en/newsroom/rss"},
    {"name": "CB Insights AI",        "url": "https://www.cbinsights.com/research/feed/"},
    {"name": "Deloitte AI Institute", "url": "https://www2.deloitte.com/rss/insights.xml"},

    # PropTech & Real Estate
    {"name": "Propmodo",              "url": "https://propmodo.com/feed/"},
    {"name": "The Real Deal",         "url": "https://therealdeal.com/feed/"},
    {"name": "Inman News",            "url": "https://www.inman.com/feed/"},
    {"name": "GlobeSt",               "url": "https://www.globest.com/rss/news/"},
    {"name": "Bisnow",                "url": "https://www.bisnow.com/rss"},
    {"name": "PropTech Insider",      "url": "https://www.proptechinsider.com/feed/"},
    {"name": "JLL Research",          "url": "https://www.jll.com/en/trends-and-insights/rss"},

    # Fast food & hospitality tech
    {"name": "Nation's Restaurant News", "url": "https://www.nrn.com/rss.xml"},
    {"name": "QSR Magazine",          "url": "https://www.qsrmagazine.com/rss.xml"},
    {"name": "Restaurant Business",   "url": "https://www.restaurantbusinessonline.com/rss.xml"},
    {"name": "Food Tech Connect",     "url": "https://foodtechconnect.com/feed/"},
    {"name": "Modern Restaurant Mgmt","url": "https://modernrestaurantmanagement.com/feed/"},

    # Hacker News (via Algolia API — handled separately)
    # Reddit (via JSON API — handled separately)
    # GitHub Trending (via web scrape — handled separately)
    # Product Hunt (via web scrape — handled separately)
]

WEB_SOURCES = [
    {"name": "Hacker News Top AI",
     "url": "https://hn.algolia.com/api/v1/search?query=AI+machine+learning&tags=story&hitsPerPage=20&numericFilters=created_at_i>{}"},
    {"name": "GitHub Trending AI",
     "url": "https://github.com/trending?since=daily&spoken_language_code="},
    {"name": "Product Hunt AI",
     "url": "https://www.producthunt.com/topics/artificial-intelligence"},
    {"name": "Reddit r/MachineLearning",
     "url": "https://www.reddit.com/r/MachineLearning/hot.json?limit=10"},
    {"name": "Reddit r/LocalLLaMA",
     "url": "https://www.reddit.com/r/LocalLLaMA/hot.json?limit=10"},
    {"name": "Reddit r/artificial",
     "url": "https://www.reddit.com/r/artificial/hot.json?limit=10"},
    {"name": "Reddit r/AItools",
     "url": "https://www.reddit.com/r/AItools/hot.json?limit=10"},
    {"name": "Reddit r/PropTech",
     "url": "https://www.reddit.com/r/PropTech/hot.json?limit=5"},
    {"name": "Reddit r/realestateinvesting",
     "url": "https://www.reddit.com/r/realestateinvesting/search.json?q=AI&sort=hot&limit=5"},
]


# ── Fetchers ─────────────────────────────────────────────────────────────────

async def fetch_rss(client: httpx.AsyncClient, source: dict) -> str:
    """Fetch and parse an RSS feed, return a text summary."""
    try:
        resp = await client.get(source["url"], timeout=15, follow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "xml")
        items = soup.find_all("item")[:5] or soup.find_all("entry")[:5]
        lines = [f"=== {source['name']} ==="]
        for item in items:
            title = (item.find("title") or item.find("h1") or {}).get_text(strip=True)
            desc = (item.find("description") or item.find("summary") or {}).get_text(strip=True)
            link = (item.find("link") or {}).get_text(strip=True) or \
                   (item.find("link") or {}).get("href", "")
            if title:
                lines.append(f"• {title}")
            if desc:
                lines.append(f"  {desc[:600]}")
            if link:
                lines.append(f"  {link}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"RSS fetch failed for {source['name']}: {e}")
        return f"=== {source['name']} === [недоступен: {e}]"


async def fetch_hn(client: httpx.AsyncClient) -> str:
    """Fetch Hacker News top AI stories from the last 24 hours."""
    try:
        since = int((__import__("time").time()) - 86400)
        url = f"https://hn.algolia.com/api/v1/search?query=AI+LLM+machine+learning&tags=story&hitsPerPage=15&numericFilters=created_at_i>{since}"
        resp = await client.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        lines = ["=== Hacker News Top AI Stories ==="]
        for hit in data.get("hits", [])[:10]:
            title = hit.get("title", "")
            url_ = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            points = hit.get("points", 0)
            lines.append(f"• [{points}pts] {title}")
            lines.append(f"  {url_}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"HN fetch failed: {e}")
        return f"=== Hacker News === [недоступен: {e}]"


async def fetch_reddit(client: httpx.AsyncClient, source: dict) -> str:
    """Fetch Reddit hot posts."""
    try:
        headers = {"User-Agent": "AI-Digest-Bot/1.0"}
        resp = await client.get(source["url"], timeout=15, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        posts = data.get("data", {}).get("children", [])
        lines = [f"=== {source['name']} ==="]
        for post in posts[:5]:
            p = post.get("data", {})
            title = p.get("title", "")
            score = p.get("score", 0)
            url_ = p.get("url", "")
            lines.append(f"• [{score}↑] {title}")
            if url_:
                lines.append(f"  {url_}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Reddit fetch failed for {source['name']}: {e}")
        return f"=== {source['name']} === [недоступен: {e}]"


async def fetch_github_trending(client: httpx.AsyncClient) -> str:
    """Scrape GitHub trending AI repos."""
    try:
        resp = await client.get(
            "https://github.com/trending?since=daily",
            timeout=15,
            headers={"User-Agent": "AI-Digest-Bot/1.0"}
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        repos = soup.select("article.Box-row")[:8]
        lines = ["=== GitHub Trending (Daily) ==="]
        for repo in repos:
            name_tag = repo.select_one("h2 a")
            desc_tag = repo.select_one("p")
            stars_tag = repo.select_one("span[href$='/stargazers']") or \
                        repo.select_one("a[href$='/stargazers']")
            name = name_tag.get_text(strip=True).replace("\n", "").replace(" ", "") if name_tag else ""
            desc = desc_tag.get_text(strip=True) if desc_tag else ""
            stars = stars_tag.get_text(strip=True) if stars_tag else ""
            if name:
                lines.append(f"• {name} ⭐{stars}")
                if desc:
                    lines.append(f"  {desc[:200]}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"GitHub trending fetch failed: {e}")
        return f"=== GitHub Trending === [недоступен: {e}]"


async def gather_all_sources() -> str:
    """Fetch all sources concurrently, return combined raw text."""
    async with httpx.AsyncClient(
        headers={"User-Agent": "AI-Digest-Bot/1.0"},
        follow_redirects=True
    ) as client:
        tasks = []

        # RSS feeds
        for source in RSS_SOURCES:
            tasks.append(fetch_rss(client, source))

        # Special sources
        tasks.append(fetch_hn(client))
        tasks.append(fetch_github_trending(client))

        # Reddit
        reddit_sources = [s for s in WEB_SOURCES if "reddit" in s["url"]]
        for source in reddit_sources:
            tasks.append(fetch_reddit(client, source))

        results = await asyncio.gather(*tasks, return_exceptions=True)

    combined = []
    for r in results:
        if isinstance(r, str):
            combined.append(r)
        elif isinstance(r, Exception):
            logger.warning(f"Source exception: {r}")

    return "\n\n".join(combined)


# ── Claude synthesis ──────────────────────────────────────────────────────────

DIGEST_SYSTEM_PROMPT = """Ты — редактор ежедневного AI-дайджеста на русском языке.
Твоя задача — проанализировать сырые данные из 50+ источников и составить структурированный дайджест из 20 пунктов.

СТРУКТУРА ДАЙДЖЕСТА:
**🔥 ТОП-НОВОСТИ ДНЯ (пункты 1-3)**
Самые важные и значимые события в мире ИИ за последние 24 часа.

**⚡ НОВЫЕ ТЕХНОЛОГИИ И ИХ ПРИМЕНЕНИЕ (пункты 4-8)**
Новые модели, инструменты, фреймворки. Для каждого: что это, как применить на практике, что значит для рынка.

**🏢 AI В НЕДВИЖИМОСТИ И PROPTECH (пункты 9-11)**
Новости о применении ИИ в жилой и коммерческой недвижимости, управлении объектами, инвестициях.
Особое внимание — ОАЭ и рынку Дубая.

**🍕 AI В ОБЩЕПИТЕ И РЕСТОРАННОМ БИЗНЕСЕ (пункты 12-14)**
Новости о применении ИИ в фастфуде, сетевых ресторанах, автоматизации, доставке еды.

**💡 КАК ЛЮДИ ИСПОЛЬЗУЮТ AI (пункты 15-17)**
Реальные кейсы, эксперименты, лайфхаки от обычных пользователей и компаний.
Источники: Reddit, Hacker News, GitHub.

**🔮 ПРОГНОЗЫ И ПЕРСПЕКТИВЫ (пункты 18-19)**
Куда движется ИИ, что говорят эксперты, какие тренды нарастают.

**💎 ЖЕМЧУЖИНА ДНЯ (пункт 20)**
Самая неожиданная, вдохновляющая или провокационная идея/новость дня.

ПРАВИЛА ФОРМАТИРОВАНИЯ:
- Каждый пункт: порядковый номер, заголовок на русском (без эмодзи в тексте)
- Под заголовком: ровно 3-4 ПОЛНЫХ завершённых предложения. Каждое предложение должно заканчиваться точкой. НИКОГДА не обрывай предложение на середине — лучше напиши меньше, но каждое предложение должно быть завершённым.
- Первое предложение: суть новости (что произошло). Второе: детали или контекст. Третье: практическое значение для бизнеса/разработчиков. Четвёртое (если есть): ссылка или источник в формате "Источник: название".
- Без воды и хайпа — только факты и реальная ценность.
- Если по какой-то категории нет свежих новостей — напиши об этом честно и возьми лучшее из смежной темы.
- Дата в заголовке дайджеста.
- ВАЖНО: пиши только обычный текст, без markdown символов ** и ## внутри текста пунктов."""


async def build_digest() -> str:
    """Main entry point: fetch sources → synthesize with Claude → return text."""
    import httpx as _httpx

    logger.info("Gathering sources...")
    raw_content = await gather_all_sources()
    logger.info(f"Gathered {len(raw_content)} chars from sources")

    # Trim to fit context window (keep first 80k chars)
    if len(raw_content) > 80000:
        raw_content = raw_content[:80000] + "\n\n[... контент обрезан для экономии токенов ...]"

    now_dubai = datetime.now(DUBAI_TZ)
    date_str = now_dubai.strftime("%d %B %Y")

    # Load previously shown headlines to avoid repeats
    seen_headlines = _load_seen_headlines()
    seen_block = ""
    if seen_headlines:
        seen_sample = list(seen_headlines)[-100:]  # send last 100 to Claude
        seen_block = (
            "\n\nУЖЕ ПОКАЗАННЫЕ НОВОСТИ (НЕ ПОВТОРЯТЬ):\n"
            + "\n".join(f"- {h}" for h in seen_sample)
            + "\n\nЭти заголовки уже были в предыдущих дайджестах. "
            "Выбирай ТОЛЬКО новые, не упоминавшиеся ранее новости. "
            "Если свежих новостей мало, бери новости за последние 3-7 дней — "
            "лучше чуть более старая новая новость, чем вчерашний повтор.\n"
        )

    user_message = (
        f"Сегодня {date_str}. Вот сырые данные из 50+ источников. "
        f"Составь дайджест из 20 пунктов согласно инструкции."
        f"{seen_block}\n\n"
        f"ДАННЫЕ:\n{raw_content}"
    )

    logger.info("Sending to Claude for synthesis...")
    async with _httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 6000,
                "system": DIGEST_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_message}],
            },
        )
        resp.raise_for_status()
        data = resp.json()

    digest_text = data["content"][0]["text"]
    logger.info(f"Digest generated: {len(digest_text)} chars")

    # Extract headlines from digest and save to seen list
    import re
    new_headlines = re.findall(r"^\d+\.\s+(.+)$", digest_text, re.MULTILINE)
    if new_headlines:
        seen_headlines.update(new_headlines)
        _save_seen_headlines(seen_headlines)
        logger.info(f"Saved {len(new_headlines)} new headlines to seen list (total: {len(seen_headlines)})")

    return digest_text
