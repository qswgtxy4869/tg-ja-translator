import os
import re
import time
import asyncio
from collections import defaultdict, deque

from telethon import TelegramClient, events
from telethon.sessions import StringSession

from openai import OpenAI

# -----------------------------
# Env
# -----------------------------
TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_SESSION = os.environ["TG_SESSION"]  # StringSession
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

# 可选：只翻译这些 chat_id（白名单）。为空则全局。
ALLOW_CHAT_IDS = os.environ.get("ALLOW_CHAT_IDS", "").strip()
ALLOW_CHAT_IDS = set(int(x) for x in ALLOW_CHAT_IDS.split(",") if x.strip()) if ALLOW_CHAT_IDS else None

# 可选：不翻译自己发的消息（默认 true）
IGNORE_SELF = os.environ.get("IGNORE_SELF", "true").lower() == "true"

# 节流：同一 chat 最短间隔（秒）
MIN_INTERVAL_PER_CHAT = float(os.environ.get("MIN_INTERVAL_PER_CHAT", "2.5"))

# 合并窗口：同一 chat 在窗口内多条合并翻译（秒）
MERGE_WINDOW = float(os.environ.get("MERGE_WINDOW", "1.2"))

# 最大翻译长度（避免超长成本）
MAX_CHARS = int(os.environ.get("MAX_CHARS", "1500"))

# OpenAI model（按你账户可用的来）
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

# -----------------------------
# Clients
# -----------------------------
tg = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)
oa = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------------
# Helpers
# -----------------------------
_japanese_re = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")  # 粗略包含汉字/假名
_kana_re = re.compile(r"[\u3040-\u30ff]")

def looks_like_japanese(text: str) -> bool:
    # 如果包含一定比例的假名，认为已经是日语，跳过
    if not text:
        return False
    kana = len(_kana_re.findall(text))
    return kana >= 3  # 很粗略的阈值，可调

def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()

async def translate_to_ja(text: str) -> str:
    # 只输出译文，不要解释
    prompt = (
        "请把下面文本翻译成自然、地道的日语。"
        "保持语气（口语/礼貌程度）尽量一致。"
        "保留专有名词、数字、URL。"
        "不要添加解释或多余内容，只输出译文。\n\n"
        f"文本：\n{text}"
    )

    # Responses API（Python SDK 1.x）
    resp = oa.responses.create(
        model=OPENAI_MODEL,
        input=prompt,
    )
    return resp.output_text.strip()

def format_blockquote(src: str, ja: str) -> str:
    # Telegram/Telethon 对 HTML 支持：<blockquote>...</blockquote>
    # 如果你想只发引用块，把 src 那行去掉即可
    return f"{src}\n<blockquote>{ja}</blockquote>"

# -----------------------------
# Anti-spam / merge
# -----------------------------
last_sent_at = defaultdict(lambda: 0.0)
buffers = defaultdict(lambda: deque())  # chat_id -> deque[(ts, text, reply_to_msg_id)]

async def flush_chat(chat_id: int):
    """合并窗口到期后，把缓冲区里的消息合并翻译并发送。"""
    await asyncio.sleep(MERGE_WINDOW)

    buf = buffers[chat_id]
    if not buf:
        return

    # 取出并合并
    items = list(buf)
    buf.clear()

    # 节流：同一 chat 最短间隔
    now = time.time()
    if now - last_sent_at[chat_id] < MIN_INTERVAL_PER_CHAT:
        return

    texts = [t for _, t, _ in items]
    reply_to = items[-1][2]  # 回复最后一条更自然
    merged = "\n".join(texts)
    merged = normalize_text(merged)
    if not merged:
        return

    # 限制长度
    if len(merged) > MAX_CHARS:
        merged = merged[:MAX_CHARS] + "…"

    # 已经是日语则跳过
    if looks_like_japanese(merged):
        return

    try:
        ja = await translate_to_ja(merged)
        msg = format_blockquote(merged, ja)
        await tg.send_message(chat_id, msg, parse_mode="html", reply_to=reply_to)
        last_sent_at[chat_id] = time.time()
    except Exception as e:
        # 生产环境建议接入日志系统，这里先简单打印
        print(f"[ERROR] translate/send failed: {e}")

@tg.on(events.NewMessage())
async def handler(event: events.NewMessage.Event):
    # 白名单过滤
    if ALLOW_CHAT_IDS is not None and event.chat_id not in ALLOW_CHAT_IDS:
        return

    # 过滤自己（可选）
    if IGNORE_SELF:
        me = await tg.get_me()
        if event.sender_id == me.id:
            return

    # 只处理文本
    text = event.raw_text or ""
    text = normalize_text(text)
    if not text:
        return

    # 把消息放入缓冲队列，合并翻译
    buffers[event.chat_id].append((time.time(), text, event.message.id))

    # 启动一个 flush（如果短时间内多条，会被 merge_window 合并）
    asyncio.create_task(flush_chat(event.chat_id))

async def main():
    await tg.start()
    print("Translator is running...")
    await tg.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())