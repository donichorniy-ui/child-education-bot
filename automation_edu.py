"""Генератор постов для канала «Мамам Узбекистана» (черновики для модерации)."""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

import feedparser
import requests
from openai import OpenAI

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL_NAME = os.getenv("GROQ_MODEL") or "openai/gpt-oss-120b"
USER_AGENT = "Mozilla/5.0 (compatible; MamaUzBot/1.0)"

TOPICS_FILE = "topics.json"
PENDING_FILE = "pending.json"
KEEP_POSTED = 200

RSS_FEEDS = [
    "https://feeds.bbci.co.uk/news/education/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Education.xml",
]

KEYWORDS = ["child", "screen", "language", "dyslexia", "adhd", "school", "parent", "education"]


class LLMUnavailable(Exception):
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


client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
)


def ask_llm(prompt):
    waits = [20, 60]
    for attempt in range(len(waits) + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
                response_format={"type": "json_object"},
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            msg = str(e)
            code = getattr(e, "status_code", None)
            if code == 400 and "json" in msg.lower():
                raise LLMUnavailable(f"Ошибка JSON-режима Groq: {msg}") from e
            temporary = code in (429, 500, 502, 503, 504) or any(
                s in msg for s in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "rate limit")
            )
            if not temporary or attempt == len(waits):
                raise LLMUnavailable(f"Ошибка LLM ({msg[:200]})") from e
            time.sleep(waits[attempt])


def generate_post(topic, stages):
    stages_text = "\n".join(f"- {s}" for s in stages) if stages else "этапов ещё нет"
    prompt = (
        "Ты — редактор Telegram-канала для родителей в Узбекистане.\n"
        "Ты не просто пишешь пост — ты РАЗВИВАЕШЬ тему.\n\n"
        f"Тема: {topic}\n"
        f"Уже опубликованные этапы:\n{stages_text}\n\n"
        "Твоя задача:\n"
        "1. Если этапы уже есть — придумай СЛЕДУЮЩИЙ этап, который логически продолжает предыдущие, но НЕ ПОВТОРЯЕТ их смысл. "
        "Если этап 1 был про этику, этап 2 должен быть про другое (например, про техническую защиту, про возраст согласия, про то, как объяснить ребёнку).\n"
        "2. Если этапов нет — начни с этапа 1, но заложи потенциал для будущих этапов.\n"
        "3. Опирайся на реальные исследования и факты. Не выдумывай.\n"
        "4. Формат: «Боль (1 предложение) → Объяснение (2–3 предложения, с фактом) → Решение (1–2 предложения) → Хэштег».\n"
        "5. Язык: русский и узбекский (латиница).\n"
        "6. Дополнительно верни поле image_prompt на английском (макс. 15 слов) для генератора картинок. Без текста и логотипов на картинке.\n\n"
        "Верни ответ в формате JSON: {\"text_ru\": \"...\", \"text_uz\": \"...\", \"hashtag\": \"...\", \"new_stage\": \"...\", \"image_prompt\": \"...\"}"
    )
    raw = ask_llm(prompt).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    data = json.loads(raw)
    for field in ("text_ru", "text_uz", "hashtag", "new_stage", "image_prompt"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"нет поля {field}")
    return {k: data[k].strip() for k in ("text_ru", "text_uz", "hashtag", "new_stage", "image_prompt")}


def make_image(prompt):
    url = f"https://image.pollinations.ai/prompt/{quote(prompt)}?width=1024&height=1024&nologo=true"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=90)
        if r.ok and r.headers.get("content-type", "").startswith("image/"):
            return url
        print(f"  ! Картинка не подошла (код {r.status_code})")
    except requests.RequestException as e:
        print(f"  ! Ошибка загрузки картинки: {type(e).__name__}")
    return None


def fetch_news():
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
    """Выбирает тему, которая дольше всего не использовалась."""
    def sort_key(t):
        last = t.get("last_used")
        if not last:
            return (0, "")
        return (1, last)
    return min(topics, key=sort_key)


def main():
    if not GROQ_API_KEY:
        print("Не задан GROQ_API_KEY")
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

    # Помечаем тему как использованную
    topic["last_used"] = datetime.now(timezone.utc).isoformat()
    save_json(TOPICS_FILE, topics)

    try:
        post = generate_post(topic["title"], topic.get("stages", []))
    except LLMUnavailable as e:
        print(f"! LLM недоступен: {e}")
        return 1
    except (ValueError, json.JSONDecodeError) as e:
        print(f"! Ошибка парсинга ответа LLM: {e}")
        return 1

    # Добавляем новый этап в тему
    if post["new_stage"] not in topic.get("stages", []):
        topic.setdefault("stages", []).append(post["new_stage"])
        save_json(TOPICS_FILE, topics)

    image_url = make_image(post["image_prompt"])
    if image_url:
        print(f"  Картинка готова: {image_url[:60]}...")
    else:
        print("  Картинка не сгенерирована — пост будет без неё")

    news = fetch_news()
    if news:
        print(f"  Найдена новость: {news[0]}")
    else:
        print("  Новость не найдена — пост будет без ссылки")

    draft = {
        "id": int(time.time()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "topic_id": topic["id"],
        "topic_title": topic["title"],
        "text_ru": post["text_ru"],
        "text_uz": post["text_uz"],
        "hashtag": post["hashtag"],
        "new_stage": post["new_stage"],
        "image_url": image_url,
        "news_title": news[0] if news else None,
        "news_url": news[1] if news else None,
    }
    pending.append(draft)
    save_json(PENDING_FILE, pending[-KEEP_POSTED:])
    print(f"✓ Черновик сохранён (id={draft['id']})")
    print("Готово: создано 1 черновик, ошибок 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
