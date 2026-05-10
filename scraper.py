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

# Mapping region name (lowercase) → site data-value (0-based alphabetical index)
REGIONI: dict[str, str] = {
    "abruzzo": "0",
    "basilicata": "1",
    "calabria": "2",
    "campania": "3",
    "emilia-romagna": "4",
    "friuli-venezia giulia": "5",
    "lazio": "6",
    "liguria": "7",
    "lombardia": "8",
    "marche": "9",
    "molise": "10",
    "piemonte": "11",
    "puglia": "12",
    "sardegna": "13",
    "sicilia": "14",
    "toscana": "15",
    "trentino-alto adige": "16",
    "umbria": "17",
    "valle d'aosta": "18",
    "veneto": "19",
}

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


# Rate-limiting defaults (seconds) — be polite to the server
DELAY_MIN = 2.0
DELAY_MAX = 4.0
DELAY_PAGE_MIN = 4.0
DELAY_PAGE_MAX = 7.0

# Exponential backoff
MAX_RETRIES = 5
BACKOFF_BASE = 5.0   # first retry waits ~5-10 s
BACKOFF_FACTOR = 2.0  # each subsequent retry doubles
BACKOFF_JITTER = 0.3  # ±30 % jitter


def _random_delay(min_sec=DELAY_MIN, max_sec=DELAY_MAX):
    time.sleep(random.uniform(min_sec, max_sec))


def _backoff_delay(attempt: int) -> float:
    """Return a jittered exponential backoff delay for the given attempt (0-based)."""
    base = BACKOFF_BASE * (BACKOFF_FACTOR ** attempt)
    jitter = base * BACKOFF_JITTER
    delay = random.uniform(base - jitter, base + jitter)
    return delay


def _is_access_denied(page: Page) -> bool:
    """Return True if the current page is an Access Denied / rate-limit page."""
    try:
        title = page.title().lower()
        if "access denied" in title:
            return True
        # Also check body text for short error pages
        body = page.inner_text("body")
        if len(body) < 500 and "access denied" in body.lower():
            return True
    except Exception:
        pass
    return False


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


def _setup_filters_and_search(page: Page, region_value: str = "7", filled_profile: bool = False):
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

    # 1b. Check "Filled Profile" checkbox (optional)
    if filled_profile:
        logger.info("Selecting Filled Profile checkbox...")
        page.evaluate("""
            var cb = document.querySelector('input[name="filledProfileFld:chkFld"]');
            if (cb && !cb.checked) {
                cb.checked = true;
                cb.dispatchEvent(new Event('change', {bubbles: true}));
            }
        """)
        _random_delay(1, 2)

    # 2. Select region - use name attribute for hidden input + Semantic UI dropdown
    logger.info(f"Seleziono regione (value={region_value})...")
    page.evaluate(f"""
        // Find the region hidden input by name (stable)
        var input = document.querySelector('input[name="regionFld:contenitore:supplierSel"]');
        if (input) {{
            input.value = '{region_value}';
            // Find the parent Semantic UI dropdown and use its API
            var dropdown = input.closest('.ui.dropdown');
            if (dropdown && typeof $ !== 'undefined') {{
                $(dropdown).dropdown('set selected', '{region_value}');
            }}
            input.dispatchEvent(new Event('change', {{bubbles: true}}));
        }}
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
    _random_delay(DELAY_PAGE_MIN, DELAY_PAGE_MAX)
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        _random_delay(3, 5)


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
    for attempt in range(MAX_RETRIES):
        try:
            # Try bottom navigator first, then top navigator as fallback
            next_link = page.query_selector("a[rel='next'][href*='navigatorBottom-next']")
            if not next_link:
                next_link = page.query_selector("a[rel='next'][href*='navigatorTop-next']")
            if not next_link:
                logger.info("Nessun link 'next' trovato - ultima pagina raggiunta.")
                return False

            logger.info("Click pulsante pagina successiva...")
            next_link.click(force=True)
            _random_delay(DELAY_PAGE_MIN, DELAY_PAGE_MAX)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                _random_delay(2, 3)

            if _is_access_denied(page):
                delay = _backoff_delay(attempt)
                logger.warning(f"Access Denied sulla pagina successiva — attesa {delay:.0f}s (tentativo {attempt + 1}/{MAX_RETRIES})")
                time.sleep(delay)
                page.go_back(wait_until="domcontentloaded", timeout=30000)
                _random_delay()
                continue

            return True

        except Exception as e:
            delay = _backoff_delay(attempt)
            logger.warning(f"Errore navigazione pagina: {e} — attesa {delay:.0f}s (tentativo {attempt + 1}/{MAX_RETRIES})")
            time.sleep(delay)

    logger.error("Navigazione pagina fallita dopo tutti i tentativi.")
    return False


def _go_to_last_page(page: Page) -> bool:
    """Navigate to the last page of results using the 'Go to last page' link."""
    for attempt in range(MAX_RETRIES):
        try:
            last_link = page.query_selector("a[title='Go to last page'][href*='navigatorBottom-last']")
            if not last_link:
                last_link = page.query_selector("a[title='Go to last page'][href*='navigatorTop-last']")
            if not last_link:
                logger.warning("Link 'ultima pagina' non trovato.")
                return False

            # Check if it's disabled (we're already on the last page)
            if last_link.get_attribute("disabled"):
                logger.info("Già sull'ultima pagina.")
                return True

            logger.info("Navigazione all'ultima pagina...")
            last_link.click(force=True)
            _random_delay(DELAY_PAGE_MIN, DELAY_PAGE_MAX)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                _random_delay(2, 3)

            if _is_access_denied(page):
                delay = _backoff_delay(attempt)
                logger.warning(f"Access Denied — attesa {delay:.0f}s (tentativo {attempt + 1}/{MAX_RETRIES})")
                time.sleep(delay)
                page.go_back(wait_until="domcontentloaded", timeout=30000)
                _random_delay()
                continue

            return True

        except Exception as e:
            delay = _backoff_delay(attempt)
            logger.warning(f"Errore navigazione ultima pagina: {e} — attesa {delay:.0f}s (tentativo {attempt + 1}/{MAX_RETRIES})")
            time.sleep(delay)

    logger.error("Navigazione ultima pagina fallita dopo tutti i tentativi.")
    return False


def _go_to_prev_page(page: Page) -> bool:
    """Navigate to the previous page of results."""
    for attempt in range(MAX_RETRIES):
        try:
            prev_link = page.query_selector("a[rel='prev'][href*='navigatorBottom-prev']")
            if not prev_link:
                prev_link = page.query_selector("a[rel='prev'][href*='navigatorTop-prev']")
            if not prev_link:
                logger.info("Nessun link 'prev' trovato - prima pagina raggiunta.")
                return False

            # Check if disabled
            if prev_link.get_attribute("disabled"):
                logger.info("Link 'prev' disabilitato - prima pagina raggiunta.")
                return False

            logger.info("Click pulsante pagina precedente...")
            prev_link.click(force=True)
            _random_delay(DELAY_PAGE_MIN, DELAY_PAGE_MAX)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                _random_delay(2, 3)

            if _is_access_denied(page):
                delay = _backoff_delay(attempt)
                logger.warning(f"Access Denied — attesa {delay:.0f}s (tentativo {attempt + 1}/{MAX_RETRIES})")
                time.sleep(delay)
                page.go_back(wait_until="domcontentloaded", timeout=30000)
                _random_delay()
                continue

            return True

        except Exception as e:
            delay = _backoff_delay(attempt)
            logger.warning(f"Errore navigazione pagina precedente: {e} — attesa {delay:.0f}s (tentativo {attempt + 1}/{MAX_RETRIES})")
            time.sleep(delay)

    logger.error("Navigazione pagina precedente fallita dopo tutti i tentativi.")
    return False


def _jump_to_page(page: Page, target: int) -> bool:
    """Navigate to a specific page by clicking numbered page links.

    The pagination shows a window of ~8 links at a time. To reach distant
    pages, this function hops through intermediate page links.
    Each hop costs one Wicket AJAX call.
    """
    max_hops = 8
    for _ in range(max_hops):
        # Detect current page number (the disabled link in pagination)
        current = _get_current_page_number(page)
        if current == target:
            return True

        # Try clicking the target directly
        target_link = page.query_selector(f"a[title='Go to page {target}']")
        if target_link and not target_link.get_attribute("disabled"):
            logger.info(f"Jump diretto a pagina {target}...")
            target_link.click(force=True)
            _random_delay(DELAY_PAGE_MIN, DELAY_PAGE_MAX)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                _random_delay(2, 3)
            return not _is_access_denied(page)

        # Target not visible — hop to the closest visible page towards target
        page_links = page.query_selector_all(
            "span[style*='padding'] > a[href*='pageLink']:not([disabled])"
        )
        if not page_links:
            logger.warning("Nessun link di pagina trovato per il jump.")
            return False

        best_link = None
        best_pg = None
        for link in page_links:
            try:
                pg = int(link.inner_text().strip())
            except (ValueError, TypeError):
                continue
            if target > current:
                # Going forward — pick highest page <= target
                if pg > current and (best_pg is None or pg > best_pg):
                    best_pg = pg
                    best_link = link
            else:
                # Going backward — pick lowest page >= target
                if pg < current and (best_pg is None or pg < best_pg):
                    best_pg = pg
                    best_link = link

        if best_link is None:
            logger.warning(f"Impossibile avvicinarsi a pagina {target} (corrente={current}).")
            return False

        logger.info(f"Hop intermedio: pagina {current} → {best_pg} (target={target})...")
        best_link.click(force=True)
        _random_delay(DELAY_PAGE_MIN, DELAY_PAGE_MAX)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            _random_delay(2, 3)

        if _is_access_denied(page):
            return False

    return _get_current_page_number(page) == target


def _get_current_page_number(page: Page) -> int:
    """Return the current result page number (1-based), or 0 if unknown."""
    try:
        disabled = page.query_selector(
            "span[style*='padding'] > a[disabled]"
        )
        if disabled:
            return int(disabled.inner_text().strip())
    except (ValueError, TypeError):
        pass
    return 0


def _do_fresh_search(page: Page, context: BrowserContext, region_value: str, filled_profile: bool = False) -> bool:
    """Navigate to the search page, set up filters, submit. Returns True on success."""
    for attempt in range(MAX_RETRIES):
        logger.info(f"Navigazione a {SEARCH_URL}...")
        try:
            page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
            _random_delay(2, 4)
            if _is_access_denied(page):
                delay = _backoff_delay(attempt)
                logger.warning(f"Access Denied al caricamento — attesa {delay:.0f}s")
                time.sleep(delay)
                continue
            break
        except Exception as e:
            delay = _backoff_delay(attempt)
            logger.warning(f"Errore navigazione: {e} — attesa {delay:.0f}s")
            time.sleep(delay)
    else:
        logger.error("Impossibile caricare la pagina di ricerca.")
        return False

    if not _wait_for_captcha(page):
        return False
    _save_cookies(context)

    try:
        page.wait_for_selector("form.ui.form.styled", timeout=10000)
    except Exception:
        pass

    _setup_filters_and_search(page, region_value=region_value, filled_profile=filled_profile)
    if not _wait_for_captcha(page):
        return False
    _save_cookies(context)

    # Wait for first result card
    try:
        page.wait_for_selector(
            "div.twelve.wide.column.right.floated.rounded.bordered.bgwhite",
            timeout=15000,
        )
    except Exception:
        logger.warning("Risultati non trovati dopo la ricerca.")
        return False

    return True


def scrape_startups(region: str = "liguria", headless: bool = False, filled_profile: bool = False) -> list[dict]:
    """Funzione principale di scraping.

    Uses a multi-pass strategy to work around Wicket's AJAX pagination limit
    (~19 AJAX calls per session). Each pass:
      1. Fresh search (resets Wicket AJAX counter)
      2. Navigate to a target area (forward, backward, or jump to gap)
      3. Paginate until stall (all-duplicate page = Wicket stale data)
    Passes continue until no new items are found or a maximum is reached.
    """
    region_key = region.strip().lower()
    if region_key not in REGIONI:
        raise ValueError(f"Regione '{region}' non valida. Valori: {', '.join(sorted(REGIONI))}")
    region_value = REGIONI[region_key]

    all_startups = []
    seen_cf: set[str] = set()

    MAX_PASSES = 10
    CONSECUTIVE_DUP_PAGES_LIMIT = 3  # stop direction after N all-dup pages

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
            # ── Pass 1: Forward from page 1 ──────────────────────────
            logger.info("═══ Passo 1: paginazione in avanti da pagina 1 ═══")
            if not _do_fresh_search(page, context, region_value, filled_profile=filled_profile):
                return all_startups

            # Debug snapshot
            try:
                Path("debug_after_search.html").write_text(page.content())
                page.screenshot(path="debug_after_search.png", full_page=True)
            except Exception:
                pass

            forward_stall_page = _paginate_and_collect(
                page, context, all_startups, seen_cf, direction="forward"
            )
            logger.info(f"Passo 1 completato: {len(all_startups)} startup (stallo a pagina ~{forward_stall_page})")

            # ── Pass 2: Backward from last page ──────────────────────
            logger.info("═══ Passo 2: paginazione inversa dall'ultima pagina ═══")
            if not _do_fresh_search(page, context, region_value, filled_profile=filled_profile):
                return all_startups

            if not _go_to_last_page(page):
                logger.warning("Impossibile navigare all'ultima pagina.")
            else:
                backward_stall_page = _paginate_and_collect(
                    page, context, all_startups, seen_cf, direction="backward"
                )
                logger.info(f"Passo 2 completato: {len(all_startups)} startup (stallo a pagina ~{backward_stall_page})")

            # ── Passes 3+: Jump to evenly-spaced target pages ─────────
            # Forward covers the start, backward covers the end; the gap
            # is in the middle.  Try pages at regular intervals to fill it.
            # Each fresh search gives ~6-10 pagination clicks before Wicket
            # stalls, so STEP ≈ 5 ensures good overlap.
            STEP = 5
            # Build list of target start pages (skip 1 and last, already done)
            targets = list(range(1 + STEP, 26, STEP))  # e.g. [6, 11, 16, 21]
            pass_num = 2
            for target in targets:
                prev_total = len(all_startups)

                pass_num += 1
                logger.info(
                    f"═══ Passo {pass_num}: jump a pagina {target} ═══"
                )

                if not _do_fresh_search(page, context, region_value, filled_profile=filled_profile):
                    break

                if not _jump_to_page(page, target):
                    logger.warning(f"Jump a pagina {target} fallito — skip.")
                    continue

                # Forward from the jumped page
                stall = _paginate_and_collect(
                    page, context, all_startups, seen_cf, direction="forward"
                )
                logger.info(
                    f"Passo {pass_num}: {len(all_startups)} startup totali "
                    f"(+{len(all_startups) - prev_total} nuovi, stallo ~{stall})"
                )

                # If no new items found in this pass AND the previous, stop
                if len(all_startups) == prev_total:
                    logger.info("Nessun nuovo risultato — tutte le pagine coperte.")
                    break

            logger.info(f"Scraping completato: {len(all_startups)} startup in {pass_num} passi")

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


def _paginate_and_collect(
    page: Page,
    context: BrowserContext,
    all_startups: list[dict],
    seen_cf: set[str],
    direction: str = "forward",
) -> int:
    """Paginate in the given direction, collecting unique startups.

    Returns the page number where the stall was detected (all-duplicate page),
    or the last successfully extracted page number.
    """
    CONSECUTIVE_DUP_LIMIT = 3
    consecutive_dup_pages = 0
    access_denied_retries = 0
    pages_extracted = 0
    last_page_num = _get_current_page_number(page) or 1

    nav_fn = _go_to_next_page if direction == "forward" else _go_to_prev_page

    while True:
        current = _get_current_page_number(page) or last_page_num
        logger.info(f"[{direction}] Pagina {current}...")

        # Access Denied check
        if _is_access_denied(page):
            if access_denied_retries >= MAX_RETRIES:
                logger.error("Troppe pagine Access Denied — interruzione.")
                break
            delay = _backoff_delay(access_denied_retries)
            logger.warning(f"Access Denied — attesa {delay:.0f}s")
            time.sleep(delay)
            access_denied_retries += 1
            page.go_back(wait_until="domcontentloaded", timeout=30000)
            _random_delay()
            continue

        access_denied_retries = 0
        startups = _extract_startups_from_page(page)
        new_on_page = 0
        for s in startups:
            cf = s.get("Codice fiscale", "")
            key = cf if cf else s.get("Denominazione", "")
            if key and key not in seen_cf:
                seen_cf.add(key)
                all_startups.append(s)
                new_on_page += 1
            elif key:
                logger.debug(f"Duplicato saltato: {key}")

        pages_extracted += 1
        logger.info(
            f"[{direction}] Pagina {current}: {new_on_page} nuovi, "
            f"{len(all_startups)} totali"
        )

        if startups and new_on_page == 0:
            consecutive_dup_pages += 1
        else:
            consecutive_dup_pages = 0

        if consecutive_dup_pages >= CONSECUTIVE_DUP_LIMIT:
            logger.info(
                f"[{direction}] {CONSECUTIVE_DUP_LIMIT} pagine consecutive duplicate "
                f"— stallo a pagina {current}."
            )
            return current

        last_page_num = current
        if not nav_fn(page):
            break

        if not _wait_for_captcha(page):
            break
        _save_cookies(context)

    return last_page_num