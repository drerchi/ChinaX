
import os
import logging
import aiohttp
from aiogram import Bot, Dispatcher, types, executor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from deep_translator import GoogleTranslator
from datetime import datetime
import pytz
import hashlib
import urllib.parse
import re
import random

# === CONFIG ===
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID"))  # channel or group ID
APP_KEY = os.getenv("APP_KEY")
APP_SECRET = os.getenv("APP_SECRET")
TRACKING_ID = os.getenv("TRACKING_ID")

KEYWORDS_POOL = [
    "", "gadgets", "home", "fitness", "kitchen", "accessories",
    "travel", "electronics", "outdoor", "beauty", "toys", "tools", "pet", "office", "gaming", "fashion"
]

# === INIT ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(bot)
scheduler = AsyncIOScheduler()

def translate_to_ukrainian(text):
    try:
        return GoogleTranslator(source='auto', target='uk').translate(text)
    except Exception:
        return text

def escape_markdown(text: str) -> str:
    if not text:
        return ""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', text)

def get_china_timestamp():
    china_tz = pytz.timezone("Asia/Shanghai")
    now = datetime.now(china_tz)
    return now.strftime("%Y-%m-%d %H:%M:%S")

def create_md5_sign(params, secret):
    keys = sorted(k for k in params if k != "sign" and params[k] not in (None, ""))
    raw_string = ''.join(f"{k}{params[k]}" for k in keys)
    to_sign = f"{secret}{raw_string}{secret}"
    return hashlib.md5(to_sign.encode('utf-8')).hexdigest().upper()

async def fetch_hot_products():
    url = "https://api-sg.aliexpress.com/sync"
    ts = get_china_timestamp()
    keyword = random.choice(KEYWORDS_POOL)
    page_no = random.randint(1, 3)
    params = {
        "method": "aliexpress.affiliate.hotproduct.query",
        "app_key": APP_KEY,
        "sign_method": "md5",
        "format": "json",
        "v": "2.0",
        "timestamp": ts,
        "page_no": page_no,
        "page_size": 5,
        "target_currency": "USD",
        "target_language": "EN",
        "tracking_id": TRACKING_ID,
        "sort": "LAST_VOLUME_DESC",
    }
    if keyword:
        params["keywords"] = keyword
    params["sign"] = create_md5_sign(params, APP_SECRET)
    headers = {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=params, headers=headers) as resp:
            text = await resp.text()
            logging.info(f"RAW AliExpress JSON → {text}")
            try:
                return await resp.json()
            except Exception as ex:
                logging.error(f"Error parsing JSON: {ex}")
                return None

def parse_product(data):
    if not isinstance(data, dict):
        return None
    resp = data.get("aliexpress_affiliate_hotproduct_query_response", {})
    resp_result = resp.get("resp_result", {})
    result = resp_result.get("result", {})
    products_wrapper = result.get("products", {})
    product_list = products_wrapper.get("product") or []
    if not product_list:
        return None
    p = random.choice(product_list)
    url = p.get("promotion_link") or p.get("product_detail_url") or ""
    return {
        "title": p.get("product_title", "Без назви"),
        "image": p.get("product_main_image_url", ""),
        "original_url": p.get("product_detail_url") or "",
        "promotion_link": p.get("promotion_link") or "",
        "url": url,
        "sale_price": p.get("sale_price") or p.get("app_sale_price") or "",
        "original_price": p.get("original_price") or "",
        "discount": p.get("discount") or "",
        "shop_name": p.get("shop_name") or "",
    }

async def generate_affiliate_link_via_api(source_link: str):
    if not source_link:
        return ""
    url = "https://api-sg.aliexpress.com/sync"
    ts = get_china_timestamp()
    params = {
        "method": "aliexpress.affiliate.link.generate",
        "app_key": APP_KEY,
        "sign_method": "md5",
        "format": "json",
        "v": "2.0",
        "timestamp": ts,
        "tracking_id": TRACKING_ID,
        "source_values": source_link,
        "promotion_link_type": 0,
        "ship_to_country": "UA",
    }
    params["sign"] = create_md5_sign(params, APP_SECRET)
    headers = {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=params, headers=headers) as resp:
            try:
                j = await resp.json()
            except Exception as ex:
                logging.error(f"Affiliate link JSON parse error: {ex}")
                return ""
    try:
        resp_wrapper = j.get("aliexpress_affiliate_link_generate_response", {})
        resp_result = resp_wrapper.get("resp_result", {})
        result = resp_result.get("result", {}) or {}
        promotion_links = result.get("promotion_links", {}) or {}
        link_list = promotion_links.get("promotion_link") or []
        if isinstance(link_list, list) and link_list:
            first = link_list[0]
            link = first.get("promotion_link") or ""
            if link:
                return link
        return result.get("promotion_link") or result.get("url") or ""
    except Exception as e:
        logging.error("Error extracting affiliate link from response: %s; full response: %s", e, j)
        return ""

async def post_product():
    data = await fetch_hot_products()
    if not data:
        logging.warning("Немає даних від AliExpress")
        return
    prod = parse_product(data)
    if not prod:
        logging.warning("Немає товарів у відповіді AliExpress")
        return
    source_for_affiliate = prod["promotion_link"] or prod["original_url"]
    affiliate_link = await generate_affiliate_link_via_api(source_for_affiliate)
    if not affiliate_link:
        logging.warning("Не вдалося створити афілійоване посилання, пропускаю.")
        return
    ua_title_raw = translate_to_ukrainian(prod["title"])
    ua_title = escape_markdown(ua_title_raw)
    sale = escape_markdown(prod.get("sale_price", ""))
    orig = escape_markdown(prod.get("original_price", ""))
    if orig:
        price_info = f"💰 Ціна: {sale} $ \(до: {orig} $\)"
    else:
        price_info = f"💰 Ціна: {sale} $"
    discount = f"🔥 Знижка: {escape_markdown(prod.get('discount',''))}" if prod.get("discount") else ""
    shop = f"🏪 {escape_markdown(prod.get('shop_name',''))}" if prod.get("shop_name") else ""
    caption_parts = [
        f"🛍 {ua_title}",
        price_info,
    ]
    if discount:
        caption_parts.append(discount)
    if shop:
        caption_parts.append(shop)
    caption_parts.append(f"👉 [Перейти до товару]({affiliate_link})")
    caption = "\n".join(caption_parts)
    logging.debug("Caption to send:\n%s", caption)
    try:
        await bot.send_photo(
            chat_id=CHAT_ID,
            photo=prod["image"],
            caption=caption,
            parse_mode="MarkdownV2"
        )
        logging.info("✅ Опубліковано товар з картинкою")
    except Exception as ex_photo:
        logging.error(f"Помилка при публікації фото: {ex_photo}")
        fallback_title = escape_markdown(ua_title_raw)
        fallback_text = (
            f"🛍 {fallback_title}\n"
            f"{price_info}\n"
            f"👉 Перейти: {affiliate_link}"
        )
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=fallback_text,
                parse_mode="MarkdownV2"
            )
            logging.info("✅ Опубліковано текстом (fallback)")
        except Exception as ex_msg:
            logging.error(f"Фолбек не вдався: {ex_msg}")

@dp.message_handler(commands=["test"])
async def cmd_test(msg: types.Message):
    await post_product()
    await msg.reply("✅ Зроблено")

async def on_startup(_):
    scheduler.add_job(post_product, "interval", minutes=60, next_run_time=datetime.now())
    scheduler.start()
    logging.info("Планувальник запущено")

if __name__ == "__main__":
    executor.start_polling(dp, on_startup=on_startup)
