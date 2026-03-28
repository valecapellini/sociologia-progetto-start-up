import json
import logging
import random
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, BrowserContext

logger = logging.getLogger(__name__)

COOKIES_FILE = "cookies.json"
BASE_URL = "https://startup.registroimprese.it"
SEARCH_URL = f"{BASE_URL}/isin/search"
LIGURIA_VALUE = "7"  # data-value for Liguria in region dropdown

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


def _random_delay(min_sec=1.0, max_sec=3.0):
    time.sleep(random.uniform(min_sec, max_sec))


def _save_cookies(context: BrowserContext, path=COOKIES_FILE):
    cookies = context.cookies()
    Path(path).write_text(json.dumps(cookies, indent=2))
    logger.debug("Cookie salvati")


def _load_cookies(context: BrowserContext, path=COOKIES_FILE):
    p = Path(path)
    if p.exists():
        cookies = json.loads(p.read_text())
        context.add_cookies(cookies)
        logger.info("Cookie caricati da sessione precedente")
        return True
    return False


def _wait_for_captcha(page: Page) -> bool:
    """Controlla se c'e un CAPTCHA e aspetta risoluzione manuale (max 5 min)."""
    captcha_indicators = [
        "identificazione del browser non riuscita",
        "what code is in the image",
    ]

    try:
        content = page.content().lower()
        if not any(ind in content for ind in captcha_indicators):
            return True

        logger.warning("=" * 60)
        logger.warning("CAPTCHA RILEVATO! Risolvi manualmente nel browser.")
        logger.warning("Lo script riprendera automaticamente.")
        logger.warning("=" * 60)

        for _ in range(300):
            time.sleep(1)
            try:
                content = page.content().lower()
                if not any(ind in content for ind in captcha_indicators):
                    logger.info("CAPTCHA risolto!")
                    _random_delay(2, 4)
                    return True
            except Exception:
                pass

        logger.error("Timeout: CAPTCHA non risolto in 5 minuti.")
        return False
    except Exception:
        return True


def _dismiss_overlays(page: Page):
    """Chiude eventuali modal/overlay/dimmer che bloccano i click."""
    page.evaluate("""
        document.querySelectorAll('.ui.dimmer, .ui.modal, .ui.popup').forEach(el => {
            el.style.display = 'none';
            el.classList.remove('active', 'visible');
        });
        document.querySelectorAll('[class*="overlay"]').forEach(el => el.style.display = 'none');
    """)


def _setup_filters_and_search(page: Page):
    """Configura filtri e avvia ricerca. Usa selettori stabili (name attr, non ID)."""
    logger.info("Configurazione filtri...")

    _dismiss_overlays(page)
    _random_delay()

    # 1. Check startup checkbox - use name attribute (stable across page loads)
    logger.info("Seleziono checkbox Startup...")
    page.evaluate("""
        var cb = document.querySelector('input[name="startupChk:chkFld"]');
        if (cb && !cb.checked) {
            cb.checked = true;
            cb.dispatchEvent(new Event('change', {bubbles: true}));
        }
    """)
    _random_delay(1, 2)

    # 2. Select Liguria - use name attribute for hidden input + Semantic UI dropdown
    logger.info("Seleziono regione Liguria...")
    page.evaluate("""
        // Find the region hidden input by name (stable)
        var input = document.querySelector('input[name="regionFld:contenitore:supplierSel"]');
        if (input) {
            input.value = '7';
            // Find the parent Semantic UI dropdown and use its API
            var dropdown = input.closest('.ui.dropdown');
            if (dropdown && typeof $ !== 'undefined') {
                $(dropdown).dropdown('set selected', '7');
            }
            input.dispatchEvent(new Event('change', {bubbles: true}));
        }
    """)
    _random_delay(2, 3)

    # 3. Submit via clicking the search button link (find by class/text, not ID)
    logger.info("Invio ricerca...")
    _dismiss_overlays(page)

    page.evaluate("""
        // Find search button by its stable characteristics
        var searchBtn = document.querySelector('a[id*="searchBtnVetrina"], a.searchBtnVetrina');
        if (!searchBtn) {
            // Fallback: find by the hidden submit input name
            var hiddenSubmit = document.querySelector('input[name="searchBtn"]');
            if (hiddenSubmit) {
                // The actual clickable button is referenced in the onclick
                var match = hiddenSubmit.getAttribute('onclick');
                if (match) {
                    var idMatch = match.match(/getElementById\\('([^']+)'\\)/);
                    if (idMatch) {
                        searchBtn = document.getElementById(idMatch[1]);
                    }
                }
            }
        }
        if (searchBtn) {
            searchBtn.click();
        } else {
            // Last resort: submit the form
            var form = document.querySelector('form.ui.form.styled');
            if (form) form.submit();
        }
    """)

    # Wait for results to load via AJAX
    _random_delay(3, 5)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        _random_delay(2, 3)


def _extract_startups_from_page(page: Page) -> list[dict]:
    """Estrae i dati delle startup dalla pagina corrente."""
    startups = []

    # Save HTML for debug
    Path("debug_results_current.html").write_text(page.content())

    # Each result card is a "twelve wide column right floated rounded bordered bgwhite"
    results = page.query_selector_all("div.twelve.wide.column.right.floated.rounded.bordered.bgwhite")

    if not results:
        # Fallback: try finding cards by the title div pattern
        results = page.query_selector_all("div.bgwhite:has(#title)")

    if not results:
        logger.warning("Nessun risultato trovato nella pagina.")
        return startups

    for card in results:
        startup = {}
        try:
            # Denominazione - in the title h5 a.link
            name_el = card.query_selector("#title h5 a.link, #title h5 a")
            if name_el:
                startup["Denominazione"] = name_el.inner_text().strip()

            # Extract all label-value pairs from .row.rowsmall divs
            rows = card.query_selector_all(".row.rowsmall")
            for row in rows:
                cols = row.query_selector_all("div[class*='wide column']")
                if len(cols) >= 2:
                    label = cols[0].inner_text().strip()
                    value = cols[1].inner_text().strip()
                    if label and value and label != value:
                        startup[label] = value

            # Extract dates (Costituzione Impresa, Sezione Startup)
            date_divs = card.query_selector_all(".five.wide.column.textcentered")
            for div in date_divs:
                text = div.inner_text().strip()
                if "Costituzione" in text:
                    span = div.query_selector("span")
                    if span:
                        startup["Costituzione Impresa"] = span.inner_text().strip()
                elif "Sezione" in text:
                    span = div.query_selector("span")
                    if span:
                        startup["Sezione Startup"] = span.inner_text().strip()

            # Tags/hashtags - labels at the bottom of the card
            tags = card.query_selector_all("a.ui.label, span.ui.label, .ui.label")
            tag_texts = []
            for tag in tags:
                text = tag.inner_text().strip()
                # Skip the title label (background #525252)
                if text and text != startup.get("Denominazione", "") and len(text) < 100:
                    tag_texts.append(text)
            if tag_texts:
                startup["Tag"] = ", ".join(tag_texts)

            # Website
            web_el = card.query_selector("a[target='_new'], a[target='_blank']")
            if web_el:
                href = web_el.get_attribute("href")
                if href and href != "javascript:;":
                    startup["Sito Web"] = href

            if startup.get("Denominazione"):
                startups.append(startup)

        except Exception as e:
            logger.debug(f"Errore estrazione card: {e}")

    logger.info(f"Estratte {len(startups)} startup dalla pagina")
    return startups


def _go_to_next_page(page: Page) -> bool:
    """Tenta di andare alla pagina successiva usando la paginazione Wicket."""
    try:
        # Find current page number (shown as <em>N</em> in the bottom navigator)
        current_em = page.query_selector("span.navigatorBottom em, .navigatorBottom em, em")
        if not current_em:
            # Try alternative: look for em inside navigator area
            current_em = page.query_selector("div[id*='navigatorBottom'] em")

        if current_em:
            current_num = int(current_em.inner_text().strip())
            next_num = current_num + 1
            logger.info(f"Pagina corrente: {current_num}, prossima: {next_num}")

            # Click the next page link
            next_link = page.query_selector(f"a[href*='navigatorBottom-navigation']:has-text('{next_num}')")
            if not next_link:
                # Try clicking ">" arrow link
                next_link = page.query_selector("a[href*='navigatorBottom-navigation-next']")

            if next_link and next_link.is_visible():
                next_link.click(force=True)
                _random_delay(2, 4)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                return True
            else:
                logger.info("Nessun link alla pagina successiva trovato - ultima pagina raggiunta.")
                return False

        # Fallback: look for any "next" style link in bottom navigator
        next_links = page.query_selector_all("a[href*='navigatorBottom']")
        if next_links:
            # The last link should be "next" or last page
            last_link = next_links[-1]
            if last_link.is_visible():
                last_link.click(force=True)
                _random_delay(2, 4)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                return True

    except Exception as e:
        logger.debug(f"Errore navigazione pagina: {e}")

    logger.info("Nessuna pagina successiva.")
    return False


def scrape_startups(headless: bool = False) -> list[dict]:
    """Funzione principale di scraping."""
    all_startups = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )

        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 768},
            locale="it-IT",
        )

        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['it-IT', 'it', 'en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)

        _load_cookies(context)
        page = context.new_page()

        try:
            # 1. Navigate to search page
            logger.info(f"Navigazione a {SEARCH_URL}...")
            page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
            _random_delay(2, 4)

            if not _wait_for_captcha(page):
                return all_startups
            _save_cookies(context)

            # Wait for form
            try:
                page.wait_for_selector("form.ui.form.styled", timeout=10000)
                logger.info("Form di ricerca caricato.")
            except Exception:
                logger.warning("Form non trovato, provo comunque...")

            # 2. Setup filters and search (all via JS to avoid click interception)
            _setup_filters_and_search(page)

            if not _wait_for_captcha(page):
                return all_startups
            _save_cookies(context)

            # Debug
            Path("debug_after_search.html").write_text(page.content())
            page.screenshot(path="debug_after_search.png", full_page=True)
            logger.info(f"URL dopo ricerca: {page.url}")

            # 3. Extract results
            page_num = 1
            while True:
                logger.info(f"Pagina {page_num}...")
                startups = _extract_startups_from_page(page)
                all_startups.extend(startups)

                if not _go_to_next_page(page):
                    break

                page_num += 1
                if not _wait_for_captcha(page):
                    break
                _save_cookies(context)

            logger.info(f"Scraping completato: {len(all_startups)} startup da {page_num} pagine")

        except Exception as e:
            logger.error(f"Errore scraping: {e}", exc_info=True)
            if all_startups:
                logger.info(f"Dati parziali: {len(all_startups)} startup")
            try:
                Path("debug_error.html").write_text(page.content())
                page.screenshot(path="debug_error.png", full_page=True)
            except Exception:
                pass

        finally:
            _save_cookies(context)
            browser.close()

    return all_startups