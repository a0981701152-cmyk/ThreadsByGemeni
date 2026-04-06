import asyncio
import os
import json
import logging
from urllib.parse import quote
from playwright.async_api import async_playwright
import google.generativeai as genai
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, ConversationHandler, filters
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── 環境變數 ──────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "")
ALLOWED_USER_ID  = int(os.environ.get("ALLOWED_USER_ID", "0"))
THREADS_COOKIES  = os.environ.get("THREADS_COOKIES", "")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel("gemini-2.5-flash")
else:
    gemini_model = None

WAITING_QUESTION = 1


# ── Cookie 解析 ───────────────────────────────────────
def parse_cookies() -> list[dict]:
    if not THREADS_COOKIES:
        return []
    try:
        raw = json.loads(THREADS_COOKIES)
        cookies = []
        for c in raw:
            cookie = {
                "name":   c.get("name", ""),
                "value":  c.get("value", ""),
                "domain": c.get("domain", ".threads.com"),
                "path":   c.get("path", "/"),
            }
            if c.get("expirationDate"):
                cookie["expires"] = int(c["expirationDate"])
            if "httpOnly" in c:
                cookie["httpOnly"] = c["httpOnly"]
            if "secure" in c:
                cookie["secure"] = c["secure"]
            if c.get("sameSite") in ("Strict", "Lax", "None"):
                cookie["sameSite"] = c["sameSite"]
            cookies.append(cookie)
        logger.info(f"Loaded {len(cookies)} cookies")
        return cookies
    except Exception as e:
        logger.error(f"Cookie parse error: {e}")
        return []


# ── Gemini 問答 ───────────────────────────────────────
async def gemini_ask(posts: list[dict], keyword: str, question: str) -> str:
    if not gemini_model:
        return "（未設定 GEMINI_API_KEY）"
    sections = []
    for i, post in enumerate(posts, 1):
        comments_text = "\n".join(f"  - {c}" for c in post["comments"][:30])
        sections.append(
            f"【貼文 {i}】{post['post_text'][:150]}\n"
            f"留言：\n{comments_text if comments_text else '  （無留言）'}"
        )
    context_text = "\n\n".join(sections)
    prompt = (
        f"以下是 Threads 上搜尋「{keyword}」的貼文與留言內容：\n\n"
        f"{context_text}\n\n"
        f"---\n"
        f"請根據以上社群內容，用繁體中文回答這個問題：\n"
        f"「{question}」\n\n"
        f"只根據上面的社群內容回答，忠實呈現社群的聲音。"
    )
    try:
        response = await asyncio.to_thread(gemini_model.generate_content, prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return f"（Gemini 失敗：{e}）"


# ── Playwright 爬蟲：搜尋頁 ───────────────────────────
async def scrape_threads(keyword: str, status_callback) -> list[dict]:
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--window-size=1280,800",
            ]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-TW",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={
                "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
            }
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-TW', 'zh', 'en-US'] });
            window.chrome = { runtime: {} };
        """)

        # 注入登入 cookie
        cookies = parse_cookies()
        if cookies:
            await context.add_cookies(cookies)
            logger.info("Cookies injected")
        else:
            logger.warning("No cookies, proceeding without login")

        page = await context.new_page()

        await status_callback(f"🔍 正在搜尋「{keyword}」...")
        encoded = quote(keyword)
        search_url = f"https://www.threads.com/search?q={encoded}&serp_type=default&hl=zh-tw"
        logger.info(f"Navigating to: {search_url}")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(8000)

        try:
            await page.wait_for_function(
                "() => document.querySelectorAll('a[href*=\"/post/\"]').length > 0",
                timeout=15000
            )
        except:
            pass

        await status_callback("📜 載入搜尋結果中...")
        collected_urls = set()
        no_change_streak = 0
        prev_total = 0

        for scroll_round in range(50):
            # 每次滾動前先收集當前可見的連結
            hrefs = await page.evaluate("""
                () => Array.from(document.querySelectorAll('a'))
                    .map(a => a.getAttribute('href'))
                    .filter(h => h && h.includes('/post/'))
            """)
            for href in hrefs:
                if href.startswith("/"):
                    collected_urls.add(f"https://www.threads.com{href}")
                elif href.startswith("http"):
                    collected_urls.add(href)

            total = len(collected_urls)
            logger.info(f"Scroll {scroll_round+1}: visible={len(hrefs)}, collected={total}")

            if total >= 50:
                break

            if total == prev_total:
                no_change_streak += 1
                if total >= 20 and no_change_streak >= 3:
                    break
                if no_change_streak >= 6:
                    break
            else:
                no_change_streak = 0
            prev_total = total

            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(3500)

        post_urls = list(collected_urls)[:50]
        await status_callback(f"📊 共收集 {len(post_urls)} 篇貼文，開始逐篇爬取...")

        logger.info(f"Total unique post URLs: {len(post_urls)}")

        if not post_urls:
            html = await page.content()
            logger.error(f"No posts found. URL: {page.url}, Title: {await page.title()}")
            logger.error(f"HTML snippet: {html[:3000]}")
            await browser.close()
            return []

        await status_callback(f"✅ 找到 {len(post_urls)} 篇貼文，開始逐篇爬取...")

        for i, url in enumerate(post_urls, 1):
            await status_callback(f"📄 處理第 {i}/{len(post_urls)} 篇...")
            try:
                post_data = await scrape_post(context, url)
                if post_data:
                    results.append(post_data)
            except Exception as e:
                logger.error(f"Failed to scrape {url}: {e}")
            await asyncio.sleep(1.5)

        await browser.close()

    return results


# ── Playwright 爬蟲：單篇貼文 ─────────────────────────
async def scrape_post(context, url: str) -> dict | None:
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(3000)

        post_text = await page.evaluate("""
            () => {
                const candidates = Array.from(document.querySelectorAll('span, p'))
                    .map(el => el.innerText?.trim())
                    .filter(t => t && t.length > 10);
                candidates.sort((a, b) => b.length - a.length);
                return candidates[0] || '';
            }
        """)

        for _ in range(8):
            clicked = False
            for btn_text in ["查看更多回覆", "顯示更多", "Show more replies", "View more replies", "更多回覆"]:
                try:
                    btns = await page.get_by_text(btn_text, exact=False).all()
                    for btn in btns:
                        if await btn.is_visible():
                            await btn.click()
                            await page.wait_for_timeout(1200)
                            clicked = True
                except:
                    pass
            if not clicked:
                break

        comments = await page.evaluate("""
            () => {
                const texts = new Set();
                document.querySelectorAll('span, p').forEach(el => {
                    const t = el.innerText?.trim();
                    if (t && t.length > 3 && t.length < 500) texts.add(t);
                });
                return Array.from(texts);
            }
        """)

        ui_noise = {"查看更多回覆", "顯示更多", "Show more replies", "View more replies", "更多回覆", "讚", "回覆", "分享"}
        comments = [c for c in comments if c != post_text and c not in ui_noise]

        return {
            "url": url,
            "post_text": post_text[:200] if post_text else "（無法取得貼文內容）",
            "comments": comments[:50]
        }
    except Exception as e:
        logger.error(f"scrape_post error {url}: {e}")
        return None
    finally:
        await page.close()


# ── Telegram 指令 ─────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Threads 社群討論整理 Bot\n\n"
        "用法：\n"
        "/search 關鍵字 — 搜尋並爬取貼文\n"
        "爬完後直接輸入問題，Gemini 會根據內容回答\n"
        "/done — 結束問答、清除資料\n\n"
        "例如：\n"
        "/search 好市多牛肉捲\n"
        "→ 大家覺得好吃嗎？\n"
        "→ 有人提到價格嗎？"
    )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        await update.message.reply_text("⛔ 無權限使用此 bot")
        return

    if not context.args:
        await update.message.reply_text("用法：/search 關鍵字\n例如：/search 好市多牛肉捲")
        return

    keyword = " ".join(context.args)
    status_msg = await update.message.reply_text(f"🔍 開始搜尋「{keyword}」，請稍候...")

    async def update_status(text: str):
        try:
            await status_msg.edit_text(text)
        except:
            pass

    try:
        posts = await scrape_threads(keyword, update_status)

        if not posts:
            await status_msg.edit_text(
                "❌ 找不到貼文或爬取失敗。\n\n"
                "可能原因：\n"
                "• Threads 反爬蟲攔截\n"
                "• Cookie 已過期\n"
                "• 搜尋結果為空\n\n"
                "請到 Railway → Logs 查看詳細錯誤。"
            )
            return

        context.user_data["posts"] = posts
        context.user_data["keyword"] = keyword
        total_comments = sum(len(p["comments"]) for p in posts)

        await status_msg.delete()
        await update.message.reply_text(
            f"✅ 爬取完成！\n"
            f"📊 共 {len(posts)} 篇貼文、{total_comments} 則留言\n\n"
            f"💬 請輸入你想問的問題，Gemini 會根據這些內容回答。\n"
            f"輸入 /done 結束問答。"
        )
        return WAITING_QUESTION

    except Exception as e:
        logger.error(f"search_command error: {e}")
        await status_msg.edit_text(f"❌ 發生錯誤：{str(e)[:200]}")


async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = update.message.text.strip()
    posts = context.user_data.get("posts", [])
    keyword = context.user_data.get("keyword", "")

    if not posts:
        await update.message.reply_text("❌ 沒有資料，請先用 /search 搜尋")
        return ConversationHandler.END

    status_msg = await update.message.reply_text("🤖 Gemini 思考中...")
    answer = await gemini_ask(posts, keyword, question)
    await status_msg.delete()
    await update.message.reply_text(answer)
    await update.message.reply_text("💬 還有其他問題嗎？繼續輸入，或 /done 結束。")
    return WAITING_QUESTION


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("✅ 問答結束，資料已清除。")
    return ConversationHandler.END


# ── 主程式 ────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("search", search_command)],
        states={
            WAITING_QUESTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_question)
            ]
        },
        fallbacks=[
            CommandHandler("done", done_command),
            CommandHandler("start", start_command),
            CommandHandler("search", search_command),
        ],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start_command), group=0)
    app.add_handler(CommandHandler("done", done_command), group=0)
    app.add_handler(conv, group=1)
    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
