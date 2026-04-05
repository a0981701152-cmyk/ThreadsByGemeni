import asyncio
import os
import logging
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
    gemini_model = genai.GenerativeModel("gemini-2.5-flash-preview-04-17")
else:
    gemini_model = None

# ── Gemini 分析（全部留言一次送）────────────────────────
async def gemini_analysis(all_posts: list[dict], keyword: str) -> str:
    if not gemini_model:
        return "（未設定 GEMINI_API_KEY）"

    # 把所有貼文 + 留言組成一個結構化 prompt
    sections = []
    for i, post in enumerate(all_posts, 1):
        comments_text = "\n".join(f"  - {c}" for c in post["comments"][:30])  # 每篇最多 30 則
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


# ── Playwright 爬蟲 ───────────────────────────────────
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
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="zh-TW"
        )
        page = await context.new_page()

        # 前往搜尋頁
        await status_callback(f"🔍 正在搜尋「{keyword}」...")
        search_url = f"https://www.threads.net/search?q={keyword}&serp_type=default"
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

        # 滾動載入全部結果
        await status_callback("📜 載入搜尋結果中...")
        prev_count = 0
        for _ in range(10):  # 最多滾 10 次
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            post_links = await page.query_selector_all('a[href*="/post/"]')
            if len(post_links) == prev_count:
                break
            prev_count = len(post_links)

        # 收集所有貼文連結（去重）
        post_links = await page.query_selector_all('a[href*="/post/"]')
        post_urls = []
        seen = set()
        for link in post_links:
            href = await link.get_attribute("href")
            if href and "/post/" in href and href not in seen:
                seen.add(href)
                full_url = f"https://www.threads.net{href}" if href.startswith("/") else href
                post_urls.append(full_url)

        await status_callback(f"✅ 找到 {len(post_urls)} 篇貼文，開始逐篇爬取...")

        # 如果完全找不到連結，截圖 debug
        if not post_urls:
            screenshot = await page.screenshot(full_page=False)
            page_title = await page.title()
            current_url = page.url
            logger.error(f"No post URLs found. Title: {page_title}, URL: {current_url}")
            # 把頁面 HTML 前 2000 字存 log
            html = await page.content()
            logger.error(f"Page HTML (first 2000): {html[:2000]}")
            await browser.close()
            return []

        # 逐篇爬取
        for i, url in enumerate(post_urls, 1):
            await status_callback(f"📄 處理第 {i}/{len(post_urls)} 篇...")
            try:
                post_data = await scrape_post(context, url)
                if post_data:
                    results.append(post_data)
            except Exception as e:
                logger.error(f"Failed to scrape {url}: {e}")
            await asyncio.sleep(1.5)  # 避免太快被擋

        await browser.close()

    return results


async def scrape_post(context, url: str) -> dict | None:
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(2000)

        # 抓貼文主文
        post_text = ""
        try:
            post_el = await page.query_selector('div[data-pressable-container] span')
            if post_el:
                post_text = await post_el.inner_text()
        except:
            pass

        # 展開更多留言
        for _ in range(5):
            see_more = await page.query_selector_all('text="查看更多回覆"')
            if not see_more:
                see_more = await page.query_selector_all('text="顯示更多"')
            if not see_more:
                break
            for btn in see_more:
                try:
                    await btn.click()
                    await page.wait_for_timeout(1000)
                except:
                    pass

        # 抓所有留言文字
        comment_els = await page.query_selector_all('div[data-pressable-container] span')
        comments = []
        seen_texts = set()
        for el in comment_els:
            try:
                text = (await el.inner_text()).strip()
                if text and len(text) > 3 and text not in seen_texts and text != post_text:
                    seen_texts.add(text)
                    comments.append(text)
            except:
                pass

        return {
            "url": url,
            "post_text": post_text[:200] if post_text else "（無法取得貼文內容）",
            "comments": comments
        }
    except Exception as e:
        logger.error(f"scrape_post error {url}: {e}")
        return None
    finally:
        await page.close()


# ── 格式化輸出 ────────────────────────────────────────
def format_result(keyword: str, posts: list[dict], gemini_summary: str) -> list[str]:
    """回傳多則訊息（Telegram 單則上限 4096 字）"""
    total_comments = sum(len(p["comments"]) for p in posts)

    header = (
        f"🧵 Threads 關鍵字分析：{keyword}\n"
        f"{'─'*30}\n"
        f"📊 共分析 {len(posts)} 篇貼文、{total_comments} 則留言\n\n"
        f"🤖 Gemini 分析結果：\n{gemini_summary}\n"
        f"\n{'─'*30}\n📋 來源貼文：\n"
    )

    post_links = []
    for i, post in enumerate(posts, 1):
        post_links.append(
            f"{i}. {post['post_text'][:60]}...\n"
            f"   🔗 {post['url']}"
        )

    # 分批切割避免超過 4096 字
    messages = []
    current = header
    for link in post_links:
        if len(current) + len(link) + 2 > 4000:
            messages.append(current)
            current = ""
        current += "\n" + link

    if current:
        messages.append(current)

    return messages if messages else [f"找不到關於「{keyword}」的評價資料。"]


# ── Telegram 指令處理 ─────────────────────────────────
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        await update.message.reply_text("⛔ 無權限使用此 bot")
        return

    if not context.args:
        await update.message.reply_text("用法：/search 關鍵字\n例如：/search 好市多 評價")
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
                "• Threads 要求登入才能搜尋\n"
                "• 反爬蟲攔截\n"
                "• 搜尋頁面結構改版\n\n"
                "請到 Railway → Logs 查看詳細錯誤訊息。"
            )
            return

        total_comments = sum(len(p["comments"]) for p in posts)
        await update_status(f"🤖 送 {total_comments} 則留言給 Gemini 分析...")
        gemini_summary = await gemini_analysis(posts, keyword)

        await update_status("📝 整理結果中...")
        messages = format_result(keyword, posts, gemini_summary)

        await status_msg.delete()
        for msg in messages:
            await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"search_command error: {e}")
        await status_msg.edit_text(f"❌ 發生錯誤：{str(e)[:200]}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Threads 評價爬蟲 Bot\n\n"
        "用法：/search 關鍵字\n"
        "例如：/search 好市多 牛肉捲\n\n"
        "Bot 會搜尋 Threads 上的相關貼文，分析留言中的評價傾向。"
    )


# ── 主程式 ────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("search", search_command))
    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
