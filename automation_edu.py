"""Генератор постов для канала «Мамам Узбекистана» (черновики для модерации).

Запускается в GitHub Actions. НЕ публикует в канал — только создаёт черновик
в pending.json и коммитит его. Бот-модератор на Render забирает черновик
и присылает админу на одобрение.
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import feedparser
import requests

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"
USER_AGENT = "Mozilla/5.0 (compatible; MamaUzBot/1.0)"

TOPICS_FILE = "topics.json"
PENDING_FILE = "pending.json"
KEEP_POSTED = 200

RSS_FEEDS = [
    "https://feeds.bbci.co.uk/news/education/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Education.xml",
]

KEYWORDS = ["child", "screen", "language", "dyslexia", "adhd", "school", "parent", "education"]


class GeminiUnavailable(Exception):
    pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def clean_text(raw, limit):
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


_client = None


def ask_gemini(prompt):
    global _client
    from google import genai
    from google.genai import types

    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)

    waits = [20, 60]
    for attempt in range(len(waits) + 1):
        try:
            response = _client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.6,
                ),
            )
            return response.text or ""
        except Exception as e:
            msg = str(e)
            code = getattr(e, "code", None)
            if "PerDay" in msg:
                raise GeminiUnavailable(f"дневной лимит исчерпан") from e
            temporary = code in (429, 500, 502, 503, 504) or any(
                s in msg for s in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE")
            )
            if not temporary or attempt == len(waits):
                raise GeminiUnavailable(f"ошибка Gemini ({msg[:200]})") from e
            time.sleep(waits[attempt])


def generate_post(topic, stages):
    """Просит Gemini написать следующий этап темы."""
    stages_text = "\n".join(f"- {s}" for s in stages) if stages else "этапов ещё нет"
    prompt = (
        "Ты — редактор Telegram-канала для родителей в Узбекистане.\n"
        "Ты не просто пишешь пост — ты РАЗВИВАЕШЬ тему.\n\n"
        f"Тема: {topic}\n"
        f"Уже опубликованные этапы:\n{stages_text}\n\n"
        "Твоя задача:\n"
        "1. Если этапы уже есть — придумай СЛЕДУЮЩИЙ этап, который логически продолжает предыдущие. Не повторяй то, что уже было.\n"
        "2. Если этапов нет — начни с этапа 1, но заложи потенциал для будущих этапов.\n"
        "3. Опирайся на реальные исследования и факты. Не выдумывай.\n"
        "4. Формат: «Боль (1 предложение) → Объяснение (2–3 предложения, с фактом) → Решение (1–2 предложения) → Хэштег».\n"
        "5. Язык: русский и узбекский (латиница).\n\n"
        "Верни JSON: {\"text_ru\": \"...\", \"text_uz\": \"...\", \"hashtag\": \"...\", \"new_stage\": \"...\"}"
    )
    raw = ask_gemini(prompt).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    data = json.loads(raw)
    for field in ("text_ru", "text_uz", "hashtag", "new_stage"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"нет поля {field}")
    return {k: data[k].strip() for k in ("text_ru", "text_uz", "hashtag", "new_stage")}


def fetch_news():
    """Ищет новость по ключевым словам в мировых RSS. Возвращает (title, link) или None."""
    for feed_url in RSS_FEEDS:
        try:
            r = requests.get(feed_url, headers={"User-Agent": USER_AGENT}, timeout=30)
            r.raise_for_status()
            entries = feedparser.parse(r.content).entries
        except requests.RequestException:
            continue
        for entry in entries[:10]:
            title = clean_text(entry.get("title"), 200)
            if any(k in title.lower() for k in KEYWORDS):
                link = entry.get("link")
                if link:
                    return title, link
    return None


def pick_topic(topics):
    """Выбирает тему, у которой меньше всего этапов (чтобы все темы развивались равномерно)."""
    return min(topics, key=lambda t: len(t.get("stages", [])))


def main():
    if not GEMINI_API_KEY:
        print("Не задан GEMINI_API_KEY")
        return 1

    topics = load_json(TOPICS_FILE, [])
    if not isinstance(topics, list) or not topics:
        print("topics.json пуст или повреждён")
        return 1

    pending = load_json(PENDING_FILE, [])
    if not isinstance(pending, list):
        pending = []

    topic = pick_topic(topics)
    print(f"Тема: {topic['title']} (этапов: {len(topic.get('stages', []))})")

    try:
        post = generate_post(topic["title"], topic.get("stages", []))
    except GeminiUnavailable as e:
        print(f"! Gemini недоступен: {e}")
        return 1
    except (ValueError, json.JSONDecodeError) as e:
        print(f"! Ошибка парсинга ответа Gemini: {e}")
        return 1

    news = fetch_news()
    if news:
        print(f"  Найдена новость: {news[0]}")
    else:
        print("  Новость не найдена — пост будет без ссылки на новость")

    draft = {
        "id": int(time.time()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "topic_id": topic["id"],
        "topic_title": topic["title"],
        "text_ru": post["text_ru"],
        "text_uz": post["text_uz"],
        "hashtag": post["hashtag"],
        "new_stage": post["new_stage"],
        "news_title": news[0] if news else None,
        "news_url": news[1] if news else None,
    }
    pending.append(draft)
    save_json(PENDING_FILE, pending[-KEEP_POSTED:])
    print(f"✓ Черновик сохранён (id={draft['id']})")
    print(f"Готово: создано 1 черновик, ошибок 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
