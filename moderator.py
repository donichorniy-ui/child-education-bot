"""Бот-модератор для канала «Мамам Узбекистана».

Работает на Render. Раз в 30 секунд проверяет pending.json в GitHub.
Если есть новый черновик — присылает админу с кнопками «Одобрить/Отклонить».
После одобрения публикует в канал и удаляет черновик.
"""
import base64
import json
import os
import threading
import time

import requests
from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool

app = FastAPI()

# --- Переменные окружения ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/contents/pending.json"

# Множество, чтобы не отправлять один и тот же черновик дважды за сессию
seen_ids = set()


def tg(method, **payload):
    """Вызов Telegram API."""
    if not BOT_TOKEN:
        print("[tg] BOT_TOKEN не задан")
        return {"ok": False}
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload, timeout=15
        )
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[tg] {method}: {type(e).__name__}")
        return {"ok": False}
    if not data.get("ok"):
        print(f"[tg] {method} не удался: {data.get('description')}")
    return data


def fetch_pending():
    """Читает pending.json из GitHub."""
    if not GITHUB_REPO or not GITHUB_TOKEN:
        print("[github] GITHUB_REPO или GITHUB_TOKEN не заданы")
        return []
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    try:
        r = requests.get(GITHUB_API, headers=headers, timeout=15)
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            print(f"[github] fetch status {r.status_code}")
            return []
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return json.loads(content)
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"[github] fetch error: {type(e).__name__}")
        return []


def remove_pending(draft_id):
    """Удаляет черновик из pending.json после публикации."""
    if not GITHUB_REPO or not GITHUB_TOKEN:
        return
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    try:
        r = requests.get(GITHUB_API, headers=headers, timeout=15)
        if r.status_code != 200:
            return
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        pending = json.loads(content)
        pending = [p for p in pending if p.get("id") != draft_id]
        new_content = base64.b64encode(
            json.dumps(pending, ensure_ascii=False, indent=2).encode()
        ).decode()
        requests.put(
            GITHUB_API,
            headers=headers,
            json={
                "message": f"Remove published draft {draft_id}",
                "content": new_content,
                "sha": data["sha"],
            },
            timeout=15,
        )
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"[github] remove error: {type(e).__name__}")


def build_draft_message(draft):
    text = (
        f"📝 <b>Черновик #{draft['id']}</b>\n"
        f"Тема: <i>{draft['topic_title']}</i>\n"
        f"Новый этап: <i>{draft['new_stage']}</i>\n\n"
        f"🇷🇺 <b>Русский:</b>\n{draft['text_ru']}\n\n"
        f"🇺🇿 <b>Узбекский:</b>\n{draft['text_uz']}\n\n"
        f"Хэштег: {draft['hashtag']}\n"
    )
    if draft.get("news_url"):
        text += f"📰 Новость: {draft['news_title']}\n{draft['news_url']}\n"
    return text


def publish_to_channel(draft):
    """Публикует одобренный пост в канал."""
    caption = (
        f"{draft['text_ru']}\n\n"
        f"━━━━━━━━━━\n\n"
        f"{draft['text_uz']}\n\n"
        f"{draft['hashtag']}"
    )
    if draft.get("news_url"):
        caption += f"\n\n📰 {draft['news_title']}\n{draft['news_url']}"
    result = tg(
        "sendMessage",
        chat_id=CHANNEL_ID,
        text=caption,
        parse_mode="HTML",
        link_preview_options={"is_disabled": False},
    )
    return result.get("ok", False)


def checker_loop():
    """Фоновый цикл: каждые 30 секунд проверяет новые черновики."""
    while True:
        try:
            pending = fetch_pending()
            for draft in pending:
                draft_id = draft.get("id")
                if not draft_id or draft_id in seen_ids:
                    continue
                seen_ids.add(draft_id)
                msg = build_draft_message(draft)
                keyboard = {
                    "inline_keyboard": [
                        [
                            {"text": "✅ Одобрить", "callback_data": f"approve:{draft_id}"},
                            {"text": "❌ Отклонить", "callback_data": f"reject:{draft_id}"},
                        ]
                    ]
                }
                tg(
                    "sendMessage",
                    chat_id=ADMIN_CHAT_ID,
                    text=msg,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
        except Exception as e:
            print(f"[checker] {type(e).__name__}: {e}")
        time.sleep(30)


@app.on_event("startup")
async def start_checker():
    threading.Thread(target=checker_loop, daemon=True).start()


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"status": "Moderator bot is running"}


@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
        await run_in_threadpool(handle_update, update)
    except Exception as e:
        print(f"[webhook] {type(e).__name__}: {e}")
    return Response(status_code=200)


def handle_update(update):
    callback = update.get("callback_query")
    if not callback:
        return
    data = callback.get("data", "")
    if ":" not in data:
        return
    action, draft_id_str = data.split(":", 1)
    try:
        draft_id = int(draft_id_str)
    except ValueError:
        return

    tg("answerCallbackQuery", callback_query_id=callback["id"])

    if action == "approve":
        pending = fetch_pending()
        draft = next((p for p in pending if p.get("id") == draft_id), None)
        if not draft:
            tg(
                "editMessageText",
                chat_id=ADMIN_CHAT_ID,
                message_id=callback["message"]["message_id"],
                text="⚠️ Черновик не найден (возможно, уже опубликован).",
            )
            return
        if publish_to_channel(draft):
            remove_pending(draft_id)
            tg(
                "editMessageText",
                chat_id=ADMIN_CHAT_ID,
                message_id=callback["message"]["message_id"],
                text=f"✅ Черновик #{draft_id} опубликован в канал.",
            )
        else:
            tg(
                "editMessageText",
                chat_id=ADMIN_CHAT_ID,
                message_id=callback["message"]["message_id"],
                text=f"❌ Не удалось опубликовать #{draft_id}. Проверьте права бота в канале.",
            )
    elif action == "reject":
        remove_pending(draft_id)
        tg(
            "editMessageText",
            chat_id=ADMIN_CHAT_ID,
            message_id=callback["message"]["message_id"],
            text=f"🗑 Черновик #{draft_id} отклонён и удалён.",
        )
