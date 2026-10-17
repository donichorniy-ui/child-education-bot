def publish_to_channel(draft):
    caption = (
        f"{draft['text_ru']}\n\n"
        f"━━━━━━━━━━\n\n"
        f"{draft['text_uz']}\n\n"
        f"{draft['hashtag']}"
    )
    if draft.get("news_url"):
        caption += f"\n\n📰 {draft['news_title']}\n{draft['news_url']}"

    image_url = draft.get("image_url")
    if image_url:
        result = tg(
            "sendPhoto",
            chat_id=CHANNEL_ID,
            photo=image_url,
            caption=caption,
            parse_mode="HTML",
        )
        if result.get("ok"):
            return True
        print(f"[publish] sendPhoto не прошёл: {result.get('description')}. Пробую текстом.")

    result = tg(
        "sendMessage",
        chat_id=CHANNEL_ID,
        text=caption,
        parse_mode="HTML",
        link_preview_options={"is_disabled": False},
    )
    return result.get("ok", False)
