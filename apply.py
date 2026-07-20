import os
import time
import json
import random
from loguru import logger
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# Load environment variables
load_dotenv()
PAUSE_MIN = int(os.getenv("HH_PAUSE_MIN", 10))
PAUSE_MAX = int(os.getenv("HH_PAUSE_MAX", 15))
LIMIT = int(os.getenv("HH_LIMIT", 300))
MAX_RETRIES = int(
    os.getenv("HH_MAX_RETRIES", 2)
)  # give up on a vacancy after this many failed attempts

APPLIED_FILE = "applied.txt"
FAILED_FILE = "failed.json"
COVER_LETTER_FILE = os.getenv("HH_COVER_LETTER_FILE", "cover_letter.txt")

# Substring hh.ru shows when the account hits its 200-responses-per-24h cap.
# get_by_text() does a substring match, so the truncated text codegen shows is enough.
DAILY_LIMIT_TEXT = "В течение 24"


class DailyLimitReached(Exception):
    """Raised when hh.ru reports the daily response limit has been hit."""
    pass


def daily_limit_hit(page):
    """Check (non-blocking) whether the daily-limit toast/message is visible."""
    try:
        locator = page.get_by_text(DAILY_LIMIT_TEXT)
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:
        return False

# Respond button selectors
RESPOND_SELECTORS = [
    "div.vacancy-action >> text=\u041e\u0442\u043a\u043b\u0438\u043a\u043d\u0443\u0442\u044c\u0441\u044f",
    'div:has-text("\u041e\u0442\u043a\u043b\u0438\u043a\u043d\u0443\u0442\u044c\u0441\u044f")',
    ".vacancy-action .magritte-button___Pubhr_5-2-29",
    'button[data-qa="vacancy-response-link-top"]',
    'button[data-qa="vacancy-apply-button-desktop"]',
]


def load_cover_letter():
    if os.path.exists(COVER_LETTER_FILE):
        with open(COVER_LETTER_FILE, "r", encoding="utf-8") as f:
            text = f.read().strip()
            if text:
                return text
    logger.warning(
        f"No cover letter found at {COVER_LETTER_FILE}; will apply without one."
    )
    return None


COVER_LETTER = load_cover_letter()


def load_applied():
    applied = set()
    if os.path.exists(APPLIED_FILE):
        with open(APPLIED_FILE, "r") as f:
            applied = set(line.strip() for line in f.readlines() if line.strip())
    return applied


def record_applied(link):
    with open(APPLIED_FILE, "a") as f:
        f.write(link + "\n")


def load_failed():
    """Returns a dict of {link: attempt_count} for vacancies that previously failed."""
    if os.path.exists(FAILED_FILE):
        try:
            with open(FAILED_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read {FAILED_FILE}, starting fresh: {e}")
    return {}


def save_failed(failed):
    with open(FAILED_FILE, "w") as f:
        json.dump(failed, f, indent=2, ensure_ascii=False)


def safe_goto(page, url, timeout=15000, retries=3, retry_delay=3):
    """
    Navigate to a URL with retries. Returns True on success, False if all
    attempts fail (e.g. net::ERR_CONNECTION_RESET, DNS errors, timeouts).
    This prevents a single flaky navigation from crashing the whole script.
    """
    for attempt in range(1, retries + 1):
        try:
            page.goto(url, timeout=timeout)
            return True
        except PlaywrightTimeoutError:
            logger.warning(
                f"Timeout navigating to {url} (attempt {attempt}/{retries}), retrying..."
            )
        except Exception as e:
            logger.warning(
                f"Navigation error for {url} (attempt {attempt}/{retries}): {e}"
            )
        time.sleep(retry_delay)
    logger.error(f"Failed to navigate to {url} after {retries} attempts.")
    return False


def cover_letter_textarea(page):
    """Returns the cover letter textbox locator if present and visible, else None."""
    locator = page.get_by_role("textbox", name="Сопроводительное письмо")
    try:
        if locator.count() > 0 and locator.first.is_visible():
            return locator.first
    except Exception:
        pass
    return None


def fill_cover_letter_if_present(page):
    if not COVER_LETTER:
        return False

    textarea = page.get_by_role("textbox", name="Сопроводительное письмо")

    try:
        textarea.wait_for(timeout=1000)
    except PlaywrightTimeoutError:
        return False

    textarea.fill(COVER_LETTER)
    logger.debug("Cover letter added.")
    return True


# Application function
def try_apply(page, link):
    if not safe_goto(page, link, timeout=15000):
        return False

    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        logger.debug("Page load timed out, continuing anyway.")

    # Закрытие всплывающих окон
    for sel in [
        'button:has-text("Принять")',
        'button:has-text("Хорошо")',
        'button:has-text("Да, верно")',
    ]:
        try:
            page.locator(sel).first.click(timeout=2000, force=True)
            logger.debug(f"Dismissed popup via {sel}")
        except PlaywrightTimeoutError:
            pass

    # Попытки отклика
    for _ in range(5):
        for sel in RESPOND_SELECTORS:
            locator = page.locator(sel).first

            if locator.count() > 0 and locator.is_visible():
                try:
                    locator.scroll_into_view_if_needed()

                    try:
                        locator.click(timeout=3000, force=True)
                        logger.debug(f"Clicked apply button via selector: {sel}")
                    except Exception as e:
                        logger.warning(f"Failed click on {sel}: {e}")

                        try:
                            page.evaluate("(el) => el.click()", locator)
                            logger.debug(
                                f"Clicked using JS fallback for selector: {sel}"
                            )
                        except Exception as js_e:
                            logger.error(
                                f"JS fallback failed for selector {sel}: {js_e}"
                            )
                            continue

                    # Wait for the response dialog
                    page.wait_for_timeout(700)

                    # hh.ru sometimes refuses to open the modal at all once the
                    # 200/24h cap is hit, showing this message instead. Catch it
                    # here so we don't waste retries waiting for elements that
                    # will never appear.
                    if daily_limit_hit(page):
                        raise DailyLimitReached(
                            "hh.ru reported the daily response limit (200/24h) has been reached."
                        )

                    # Fill cover letter if the field exists
                    fill_cover_letter_if_present(page)

                    # Click the second "Откликнуться"
                    submit_btn = page.get_by_role("button", name="Откликнуться").last

                    submit_btn.wait_for(timeout=5000)
                    submit_btn.click(timeout=3000)

                    # Wait for confirmation
                    page.wait_for_timeout(500)

                    # Belt-and-suspenders: also check after the final submit,
                    # in case the limit message shows up at that stage instead.
                    if daily_limit_hit(page):
                        raise DailyLimitReached(
                            "hh.ru reported the daily response limit (200/24h) has been reached."
                        )

                    confirmation_selectors = [
                        'div:has-text("Отклик отправлен")',
                        'div:has-text("Отклик уже отправлен")',
                        'div:has-text("Отклик был отправлен")',
                        'button:has-text("Отклик отменить")',
                        'button:has-text("Отклик отозван")',
                    ]

                    for conf_sel in confirmation_selectors:
                        try:
                            page.locator(conf_sel).first.wait_for(timeout=3000)
                            logger.debug(f"Detected confirmation via: {conf_sel}")
                            return True
                        except PlaywrightTimeoutError:
                            continue

                    logger.warning("No confirmation detected after submit.")
                    return False

                except DailyLimitReached:
                    raise
                except Exception as e:
                    logger.warning(f"Unexpected failure on {sel}: {e}")

        page.mouse.wheel(0, 800)
        page.wait_for_timeout(500)

        try:
            page.screenshot(
                path=f"debug_selector_fail_{int(time.time())}.png", timeout=5000
            )
        except Exception as e:
            logger.warning(f"Screenshot failed during scroll attempt: {e}")

    logger.error(f"No apply button found for {link}")

    try:
        page.screenshot(path=f"apply_error_{link.split('/')[-1]}.png", timeout=5000)
    except Exception as e:
        logger.warning(f"Screenshot failed on final error: {e}")

    return False


# Main function
def main():
    logger.info("Launching browser for manual login...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=100)
        page = browser.new_page()

        # Manual login
        page.goto("https://hh.ru/login")
        logger.info("Please log in manually in the opened browser window.")
        input("After manual login, press Enter to inspect and prepare...")
        logger.info("Browser will remain open for inspection.")
        input("Press Enter when ready to start automated application phase...")
        logger.success("Starting application phase...")

        search_url = "https://hh.ru/search/vacancy?enable_snippets=true&ored_clusters=true&resume=6c02328dff10706b660039ed1f537450767a61&items_on_page=100&search_session_id=6a089dba-02d2-41a9-84c1-92260dc7d590&hhtmFromLabel=search_order_button&hhtmFrom=vacancy_search_list"

        links = []
        page_num = 0
        max_pages = 20  # safety cap to avoid an infinite loop
        while len(links) < LIMIT and page_num < max_pages:
            paged_url = f"{search_url}&page={page_num}"
            if not safe_goto(page, paged_url, timeout=15000):
                logger.warning(
                    f"Could not load search results page {page_num}, stopping pagination."
                )
                break

            try:
                page.wait_for_selector('a[data-qa="serp-item__title"]', timeout=15000)
            except PlaywrightTimeoutError:
                logger.info(
                    f"No more vacancies found on page {page_num}, stopping pagination."
                )
                if page_num == 0:
                    page.screenshot(path="search_error.png", full_page=True)
                    browser.close()
                    return
                break

            page_links = [
                el.get_attribute("href")
                for el in page.query_selector_all('a[data-qa="serp-item__title"]')
            ]
            if not page_links:
                break

            links.extend(page_links)
            logger.info(
                f"Page {page_num}: collected {len(page_links)} links (total so far: {len(links)})."
            )
            page_num += 1
            time.sleep(random.uniform(1, 2))  # small pause between search pages

        applied_links = load_applied()
        failed_attempts = load_failed()

        gave_up = {
            link for link, count in failed_attempts.items() if count >= MAX_RETRIES
        }
        if gave_up:
            logger.info(
                f"Skipping {len(gave_up)} vacancies that already hit the {MAX_RETRIES}-retry limit."
            )

        filtered_links = [
            link
            for link in links
            if link and link not in applied_links and link not in gave_up
        ]
        total = len(filtered_links)
        logger.info(
            f"Collected {total} new vacancy links (filtered from {len(links)} total)."
        )

        for idx, link in enumerate(filtered_links[:LIMIT], start=1):
            logger.info(f"Applying [{idx}/{min(LIMIT, total)}]: {link}")

            try:
                if not safe_goto(page, link, timeout=15000):
                    # Navigation failed after retries — record as a failed attempt, don't crash.
                    failed_attempts[link] = failed_attempts.get(link, 0) + 1
                    save_failed(failed_attempts)
                    pause = random.randint(PAUSE_MIN, PAUSE_MAX)
                    time.sleep(pause)
                    continue

                time.sleep(2)
                if try_apply(page, link):
                    logger.success(f"Applied to {link}")
                    record_applied(link)
                    # Clear any prior failure history now that it succeeded.
                    if link in failed_attempts:
                        del failed_attempts[link]
                        save_failed(failed_attempts)
                else:
                    failed_attempts[link] = failed_attempts.get(link, 0) + 1
                    save_failed(failed_attempts)
                    attempts_so_far = failed_attempts[link]
                    if attempts_so_far >= MAX_RETRIES:
                        logger.warning(
                            f"Failed to apply to {link} ({attempts_so_far}/{MAX_RETRIES}) — will stop retrying it."
                        )
                    else:
                        logger.warning(
                            f"Failed to apply to {link} ({attempts_so_far}/{MAX_RETRIES}) — will retry on a future run."
                        )
            except DailyLimitReached as e:
                logger.warning(str(e))
                logger.warning(
                    "Stopping the run — hh.ru's daily response cap has been reached. "
                    "Not recording this vacancy as failed, since it's not the vacancy's fault."
                )
                break
            except Exception as e:
                # Catch-all so one bad vacancy page never kills the whole run.
                logger.error(f"Unexpected error while processing {link}: {e}")
                failed_attempts[link] = failed_attempts.get(link, 0) + 1
                save_failed(failed_attempts)

            pause = random.randint(PAUSE_MIN, PAUSE_MAX)
            time.sleep(pause)

        input("Done. Press Enter to close the browser and exit.")
        browser.close()
        logger.info("Browser closed. Exiting.")


if __name__ == "__main__":
    main()
