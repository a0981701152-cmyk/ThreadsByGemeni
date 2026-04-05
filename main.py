import asyncio
import os
import logging
from urllib.parse import quote
from playwright.async_api import async_playwright
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── 環境變數 ──────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))  # 0 = 不限制

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel("gemini-2.5-flash")
else:
    gemini_model = None


# ── Gemini 分析 ───────────────────────────────────────
async def gemini_analysis(all_posts: list[dict], keyword: str) -> str:
    if not gemini_model:
        return "（未設定 GEMINI_API_KEY）"

    sections = []
    for i, post in enumerate(all_posts, 1):
        comments_text = "\n".join(f"  - {c}" for c in post["comments"][:30])
        sections.append(
            f"【貼文 {i}】{post['post_text'][:100]}\n"
            f"留言：\n{comments_text if comments_text else '  （無留言）'}"
        )

    full_text = "\n\n".join(sections)
    prompt = (
        f"以下是 Threads 上搜尋「{keyword}」的貼文與留言內容。\n"
        f"請用繁體中文整理社群上大家對這個話題的看法，格式如下：\n\n"
        f"📌 話題概況\n（簡短說明這批貼文主要在討論什麼面向）\n\n"
        f"💬 社群上常見的討論方向\n（條列整理大家實際提到的內容、經驗、觀點，"
        f"忠實呈現社群的聲音，可以直接引用有代表性的留言）\n\n"
        f"🔍 值得注意的細節或分歧點\n（如果有人說法不一、或提到特定條件差異，列出來）\n\n"
        f"注意：不要做推薦或不推薦的結論，只要客觀整理社群討論內容。\n\n"
        f"{full_text}"
    )

    try:
        response = await asyncio.to_thread(gemini_model.generate_content, prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return f"（Gemini 分析失敗：{e}）"


# ── Playwright 爬蟲：搜尋頁 ───────────────────────────
async def scrape_threads(keyword: str, status_callback) -> list[dict]:
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="zh-TW",
            viewport={"width": 1280, "height": 800}
        )
        page = await context.new_page()

        # 1. 直接帶參數前往搜尋結果頁
        await status_callback(f"🔍 正在搜尋「{keyword}」...")
        encoded = quote(keyword)
        search_url = f"https://www.threads.com/search?q={encoded}&serp_type=default&hl=zh-tw"
        logger.info(f"Navigating to: {search_url}")
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(8000)  # 等 React 完整渲染

        # 等貼文連結出現，最多等 15 秒
        try:
            await page.wait_for_function(
                "() => document.querySelectorAll('a[href*=\"/post/\"]').length > 0",
                timeout=15000
            )
        except:
            pass  # 等不到就繼續，後面再判斷

        # 2. 等第一批結果出現再開始滾動
        await status_callback("📜 載入搜尋結果中...")
        try:
            await page.wait_for_selector('a[href*="/post/"]', timeout=10000)
        except:
            pass

        prev_count = 0
        no_change_streak = 0
        for _ in range(30):  # 最多滾 30 次
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(3000)
            count = await page.evaluate(
                "() => document.querySelectorAll('a[href*=\"/post/\"]').length"
            )
            logger.info(f"Post links found so far: {count}")
            if count == prev_count:
                no_change_streak += 1
                if no_change_streak >= 3:  # 連續 3 次沒變才停
                    break
            else:
                no_change_streak = 0
            prev_count = count

        # 3. 收集貼文連結
        all_hrefs = await page.evaluate("""
            () => Array.from(document.querySelectorAll('a'))
                .map(a => a.getAttribute('href'))
                .filter(h => h && h.includes('/post/'))
        """)

        post_urls = []
        seen = set()
        for href in all_hrefs:
            if href not in seen:
                seen.add(href)
                if href.startswith("/"):
                    post_urls.append(f"https://www.threads.com{href}")
                elif href.startswith("http"):
                    post_urls.append(href)

        logger.info(f"Total unique post URLs: {len(post_urls)}")

        # debug：找不到就 log HTML
        if not post_urls:
            html = await page.content()
            logger.error(f"No posts found. URL: {page.url}, Title: {await page.title()}")
            logger.error(f"HTML snippet: {html[:3000]}")
            await browser.close()
            return []

        await status_callback(f"✅ 找到 {len(post_urls)} 篇貼文，開始逐篇爬取...")

        # 4. 逐篇爬取
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

        # 抓貼文主文（取最長的文字區塊）
        post_text = await page.evaluate("""
            () => {
                const candidates = Array.from(document.querySelectorAll('span, p'))
                    .map(el => el.innerText?.trim())
                    .filter(t => t && t.length > 10);
                candidates.sort((a, b) => b.length - a.length);
                return candidates[0] || '';
            }
        """)

        # 展開更多留言（繁中 + 英文按鈕）
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

        # 抓所有留言文字
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

        # 過濾 UI 雜訊
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


# ── 格式化輸出 ────────────────────────────────────────
def format_result(keyword: str, posts: list[dict], gemini_summary: str) -> list[str]:
    total_comments = sum(len(p["comments"]) for p in posts)

    header = (
        f"🧵 Threads 社群討論：{keyword}\n"
        f"{'─'*30}\n"
        f"📊 共分析 {len(posts)} 篇貼文、{total_comments} 則留言\n\n"
        f"🤖 Gemini 整理：\n{gemini_summary}\n"
        f"\n{'─'*30}\n📋 來源貼文：\n"
    )

    post_links = []
    for i, post in enumerate(posts, 1):
        post_links.append(
            f"{i}. {post['post_text'][:60]}...\n"
            f"   🔗 {post['url']}"
        )

    messages = []
    current = header
    for link in post_links:
        if len(current) + len(link) + 2 > 4000:
            messages.append(current)
            current = ""
        current += "\n" + link

    if current:
        messages.append(current)

    return messages if messages else [f"找不到關於「{keyword}」的討論內容。"]


# ── Telegram 指令處理 ─────────────────────────────────
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
                "• 搜尋結果為空\n\n"
                "請到 Railway → Logs 查看詳細錯誤。"
            )
            return

        total_comments = sum(len(p["comments"]) for p in posts)
        await update_status(f"🤖 送 {total_comments} 則留言給 Gemini 分析...")
        gemini_summary = await gemini_analysis(posts, keyword)

        await update_status("📝 整理結果中...")
        messages = format_result(keyword, posts, gemini_summary)

        await status_msg.delete()
        for msg in messages:
            await update.message.reply_text(msg)

    except Exception as e:
        logger.error(f"search_command error: {e}")
        await status_msg.edit_text(f"❌ 發生錯誤：{str(e)[:200]}")


async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """截圖一個 Threads 貼文頁面，回傳圖片 + 頁面所有文字，方便確認 selector"""
    if not context.args:
        await update.message.reply_text("用法：/debug <貼文URL>\n例如：/debug https://www.threads.com/@xxx/post/xxx")
        return

    url = context.args[0]
    status_msg = await update.message.reply_text(f"🔍 載入頁面中...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context_pw = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="zh-TW", viewport={"width": 1280, "height": 800}
        )
        page = await context_pw.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(4000)

        # 截圖
        screenshot = await page.screenshot(full_page=False)

        # 抓所有文字
        all_texts = await page.evaluate("""
            () => Array.from(document.querySelectorAll('span, p, div'))
                .map(el => el.innerText?.trim())
                .filter(t => t && t.length > 5 && t.length < 300)
                .slice(0, 80)
        """)

        await browser.close()

    # 回傳截圖
    await status_msg.delete()
    await update.message.reply_photo(photo=screenshot, caption=f"頁面截圖：{url}")

    # 回傳抓到的文字清單
    text_dump = "\n".join(f"{i+1}. {t}" for i, t in enumerate(all_texts[:40]))
    await update.message.reply_text(f"頁面文字（前40筆）：\n\n{text_dump}"[:4000])


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Threads 社群討論整理 Bot\n\n"
        "用法：/search 關鍵字\n"
        "例如：/search 好市多牛肉捲\n\n"
        "Bot 會搜尋 Threads 上的相關貼文，整理社群上的討論與看法。"
    )


# ── 主程式 ────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("debug", debug_command))
    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
