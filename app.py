import os
import re
import time
import asyncio
from collections import defaultdict

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from openai import OpenAI

# -----------------------------
# Env
# -----------------------------
TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_SESSION = os.environ["TG_SESSION"]  # Telethon StringSession
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

# 可选：只在这些 chat_id 里生效（白名单）。为空则所有聊天都生效。
ALLOW_CHAT_IDS_RAW = os.environ.get("ALLOW_CHAT_IDS", "").strip()
ALLOW_CHAT_IDS = (
    set(int(x) for x in ALLOW_CHAT_IDS_RAW.split(",") if x.strip())
    if ALLOW_CHAT_IDS_RAW
    else None
)

# 同一聊天内编辑节流（秒）——防止你短时间狂发导致风控
MIN_EDIT_INTERVAL_PER_CHAT = float(os.environ.get("MIN_EDIT_INTERVAL_PER_CHAT", "1.5"))

# 最大翻译长度，避免太长导致费用/超时
MAX_CHARS = int(os.environ.get("MAX_CHARS", "1500"))

# OpenAI model
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# 追加翻译块时的隐藏标记：用于防止重复编辑/无限循环
TRANSLATION_TAG = "\n\n<!--ja-translated-->"

# -----------------------------
# Clients
# -----------------------------
tg = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)
oa = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------------
# Helpers
# -----------------------------
_kana_re = re.compile(r"[\u3040-\u30ff]")  # 平/片假名
_ws_re = re.compile(r"\s+")

def normalize_text(text: str) -> str:
    return _ws_re.sub(" ", (text or "").strip())

def looks_like_japanese(text: str) -> bool:
    # 更保守一点：假名较多才认为已经是日语
    kana = len(_kana_re.findall(text or ""))
    return kana >= 6

async def translate_to_ja(text: str) -> str:
    # prompt = (
    #     "请把下面文本翻译成自然、地道的日语。"
    #     "保持语气（口语/礼貌程度）尽量一致。"
    #     "保留专有名词、数字、URL。"
    #     "不要添加解释或多余内容，只输出译文。\n\n"
    #     f"文本：\n{text}"
    # )
    prompt = (
        "你是母语为日语的日本人，正在和朋友用 Telegram 聊天。\n"
        "请把下面文本翻译成【自然、生活化、口语化】的日语，像日本人平时发消息那样。\n"
        "\n"
        "要求：\n"
        "1) 保持原文语气与关系距离：随意/礼貌/撒娇/吐槽/认真等要一致。\n"
        "2) 允许使用常见口语、省略、缩写（例如 〜だよ/〜じゃん/〜かも/了解→りょうかい）但不要过度卖萌。\n"
        "3) 保留专有名词、数字、URL、表情符号；不要擅自改动事实。\n"
        "4) 如果原文很短（例如“好”“OK”“哈哈”），也用日语里自然的短回复。\n"
        "5) 不要添加解释、注释、罗马音；只输出译文一行或两行即可。\n"
        "\n"
        f"文本：\n{text}"
    )

    # 使用 chat.completions（兼容性更好）
    resp = oa.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()

def build_edited_text(src: str, ja: str) -> str:
    # 保留原文 + 引用块译文
    return f"{src}\n<blockquote>{ja}</blockquote>{TRANSLATION_TAG}"

# -----------------------------
# Anti-loop / throttle
# -----------------------------
processed_msg_ids = set()  # 仅在进程生命周期内去重
last_edit_at = defaultdict(lambda: 0.0)  # chat_id -> ts

@tg.on(events.NewMessage(outgoing=True))
async def on_my_message(event: events.NewMessage.Event):
    # 只处理你自己发出的消息，不回复对方（outgoing=True 就是自己）
    chat_id = event.chat_id

    # 白名单过滤（可选）
    if ALLOW_CHAT_IDS is not None and chat_id not in ALLOW_CHAT_IDS:
        return

    msg = event.message
    if not msg or not msg.message:
        return

    # 去重：同一条消息只处理一次
    if msg.id in processed_msg_ids:
        return
    processed_msg_ids.add(msg.id)

    text = normalize_text(msg.message)

    # 已经翻译过（避免编辑后的消息再次触发）
    if TRANSLATION_TAG in text:
        return

    # 空文本/太短通常没必要
    if not text or len(text) < 2:
        return

    # 已经像日语就不翻
    if looks_like_japanese(text):
        return

    # 限制长度
    src = text
    if len(src) > MAX_CHARS:
        src = src[:MAX_CHARS] + "…"

    # 节流：同一聊天内短时间不要连续 edit
    now = time.time()
    if now - last_edit_at[chat_id] < MIN_EDIT_INTERVAL_PER_CHAT:
        return

    try:
        ja = await translate_to_ja(src)
        new_text = build_edited_text(src, ja)
        await msg.edit(new_text, parse_mode="html")
        last_edit_at[chat_id] = time.time()
    except Exception as e:
        print(f"[ERROR] edit failed: {e}")

async def main():
    await tg.start()
    print("Translator (edit-only) is running...")
    await tg.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())