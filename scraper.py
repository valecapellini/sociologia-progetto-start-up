import gc
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Optional

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

# Province per regione — data-value è 0-indexed in ordine alfabetico del codice
PROVINCE: dict[str, list[tuple[str, str]]] = {
    "campania": [
        ("0", "AV"),  # Avellino
        ("1", "BN"),  # Benevento
        ("2", "CE"),  # Caserta
        ("3", "NA"),  # Napoli
        ("4", "SA"),  # Salerno
    ],
    "lombardia": [
        ("0", "BG"),  ("1", "BS"),  ("2", "CO"),  ("3", "CR"),
        ("4", "LC"),  ("5", "LO"),  ("6", "MN"),  ("7", "MB"),
        ("8", "MI"),  ("9", "PV"),  ("10", "SO"), ("11", "VA"),
    ],
    "lazio": [
        ("0", "FR"),  ("1", "LT"),  ("2", "RI"),  ("3", "RM"),  ("4", "VT"),
    ],
    "veneto": [
        ("0", "BL"),  ("1", "PD"),  ("2", "RO"),  ("3", "TV"),
        ("4", "VE"),  ("5", "VR"),  ("6", "VI"),
    ],
    "piemonte": [
        ("0", "AL"),  ("1", "AT"),  ("2", "BI"),  ("3", "CN"),
        ("4", "NO"),  ("5", "TO"),  ("6", "VB"),  ("7", "VC"),
    ],
    "emilia-romagna": [
        ("0", "BO"),  ("1", "FE"),  ("2", "FC"),  ("3", "MO"),
        ("4", "PR"),  ("5", "PC"),  ("6", "RA"),  ("7", "RE"),  ("8", "RN"),
    ],
    "sicilia": [
        ("0", "AG"),  ("1", "CL"),  ("2", "CT"),  ("3", "EN"),
        ("4", "ME"),  ("5", "PA"),  ("6", "RG"),  ("7", "SR"),  ("8", "TP"),
    ],
    "puglia": [
        ("0", "BA"),  ("1", "BAT"), ("2", "BR"),
        ("3", "FG"),  ("4", "LE"),  ("5", "TA"),
    ],
    "toscana": [
        ("0", "AR"),  ("1", "FI"),  ("2", "GR"),  ("3", "LI"),  ("4", "LU"),
        ("5", "MS"),  ("6", "PI"),  ("7", "PT"),  ("8", "PO"),  ("9", "SI"),
    ],
}

PROVINCE_SPLIT_THRESHOLD = 500  # total results above this trigger province splitting
PAGES_PER_SESSION = 20  # max page.goto() navigations before fresh search (anti-bot)
ENABLE_SMART_START = False  # disabled while missing CSVs can be scattered across pages


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
    try:
        cookies = context.cookies()
        Path(path).write_text(json.dumps(cookies, indent=2))
        logger.debug("Cookie salvati")
    except Exception:
        logger.warning("Could not save cookies (context may be closed)")


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


def _is_bot_detected(page: Page) -> bool:
    """Return True if the server triggered bot detection."""
    try:
        content = page.content().lower()
        indicators = [
            "identificazione del browser non riuscita",
            "richiesta non valida",
            "what code is in the image",
        ]
        return any(ind in content for ind in indicators)
    except Exception:
        return False


def _get_total_results(page: Page) -> int:
    """Parse 'visualizzati X di Y' from the results page. Returns Y or 0."""
    try:
        content = page.content()
        match = re.search(r"visualizzati\s+\d+\s+di\s+(\d+)", content)
        if match:
            return int(match.group(1))
    except Exception as e:
        logger.debug(f"Could not parse total results: {e}")
    return 0


def _goto_nav_url(page: Page, kind: str = "next") -> bool:
    """Navigate to a pagination URL via page.goto() instead of click.

    This bypasses the Wicket AJAX budget entirely.
    kind: "next", "prev", "last", "first"
    Returns True on success.
    """
    href = page.evaluate("""
        (kind) => {
            var el = document.querySelector("a[href*='navigatorTop-" + kind + "']");
            if (!el) el = document.querySelector("a[href*='navigatorBottom-" + kind + "']");
            if (el && el.getAttribute('disabled')) return null;
            return el ? el.getAttribute('href') : null;
        }
    """, kind)

    if not href:
        logger.debug(f"No '{kind}' pagination link found")
        return False

    full_url = f"{BASE_URL}/isin/{href.lstrip('./')}"
    logger.debug(f"goto_nav_url({kind}): {full_url}")

    _random_delay(DELAY_PAGE_MIN, DELAY_PAGE_MAX)
    try:
        page.goto(full_url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        logger.warning(f"goto_nav_url({kind}) failed: {e}")
        return False

    _random_delay(2, 3)

    if _is_access_denied(page):
        logger.warning(f"Access denied after goto_nav_url({kind})")
        return False

    if _is_bot_detected(page):
        logger.warning(f"Bot detected after goto_nav_url({kind})")
        return False

    return True


def _setup_filters_and_search(page: Page, region_value: str = "7", filled_profile: bool = False, province_value: Optional[str] = None):
    """Configura filtri e avvia ricerca. Usa selettori stabili (name attr, non ID)."""
    logger.info("Configurazione filtri...")

    _dismiss_overlays(page)

    # 1. Check startup checkbox - use name attribute (stable across page loads)
    logger.info("Seleziono checkbox Startup...")
    page.evaluate("""
        var cb = document.querySelector('input[name="startupChk:chkFld"]');
        if (cb && !cb.checked) {
            cb.checked = true;
            cb.dispatchEvent(new Event('change', {bubbles: true}));
        }
    """)
    _random_delay(0.3, 0.6)

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
        _random_delay(0.3, 0.6)

    # 2. Select region - use Semantic UI dropdown API + click to trigger Wicket AJAX
    logger.info(f"Seleziono regione (value={region_value})...")
    page.evaluate(f"""
        var input = document.querySelector('input[name="regionFld:contenitore:supplierSel"]');
        if (input) {{
            input.value = '{region_value}';
            var dropdown = input.closest('.ui.dropdown');
            if (dropdown && typeof $ !== 'undefined') {{
                $(dropdown).dropdown('set selected', '{region_value}');
            }}
            input.dispatchEvent(new Event('change', {{bubbles: true}}));
        }}
    """)
    _random_delay(0.5, 1)

    # 2b. Select province (optional)
    if province_value is not None:
        logger.info(f"Seleziono provincia (value={province_value})...")
        # Wait for the province dropdown to be populated by Wicket AJAX
        # The region change triggers an AJAX call to populate province options
        try:
            page.wait_for_function("""
                () => {
                    var pvDropdown = document.querySelector('input[name="pvFld:contenitore:supplierSel"]');
                    if (!pvDropdown) return false;
                    var dropdown = pvDropdown.closest('.ui.dropdown');
                    if (!dropdown) return false;
                    var items = dropdown.querySelectorAll('.item');
                    return items.length > 1;
                }
            """, timeout=10000)
            logger.info("Province dropdown populated successfully")
        except Exception:
            logger.warning("Province dropdown may not be populated — waiting extra time")
            _random_delay(3.0, 5.0)

        page.evaluate(f"""
            var input = document.querySelector('input[name="pvFld:contenitore:supplierSel"]');
            if (input) {{
                input.value = '{province_value}';
                var dropdown = input.closest('.ui.dropdown');
                if (dropdown && typeof $ !== 'undefined') {{
                    $(dropdown).dropdown('set selected', '{province_value}');
                }}
                input.dispatchEvent(new Event('change', {{bubbles: true}}));
            }}
        """)
        _random_delay(0.5, 1)

    # 3. Submit via clicking the search button link
    logger.info("Invio ricerca...")
    _dismiss_overlays(page)

    page.evaluate("""
        var searchBtn = document.querySelector('a[id*="searchBtnVetrina"], a.searchBtnVetrina');
        if (!searchBtn) {
            var hiddenSubmit = document.querySelector('input[name="searchBtn"]');
            if (hiddenSubmit) {
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
            var form = document.querySelector('form.ui.form.styled');
            if (form) form.submit();
        }
    """)

    # Wait for results to load — use selector instead of networkidle
    try:
        page.wait_for_selector(
            "div.twelve.wide.column.right.floated.rounded.bordered.bgwhite",
            timeout=30000,
        )
        _random_delay(0.5, 1)
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
            prev_page = _get_current_page_number(page) or 0
            next_link.click(force=True)
            _random_delay(DELAY_PAGE_MIN, DELAY_PAGE_MAX)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                _random_delay(2, 3)

            # Detect Wicket stall: page number didn't change
            new_page = _get_current_page_number(page) or 0
            if new_page and prev_page and new_page <= prev_page:
                logger.warning(
                    f"Wicket stall detected: page stayed at {new_page} "
                    f"(was {prev_page}) — AJAX session exhausted"
                )
                return False

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
            prev_page_num = _get_current_page_number(page) or 0
            prev_link.click(force=True)
            _random_delay(DELAY_PAGE_MIN, DELAY_PAGE_MAX)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                _random_delay(2, 3)

            # Detect Wicket stall
            new_page_num = _get_current_page_number(page) or 0
            if new_page_num and prev_page_num and new_page_num >= prev_page_num:
                logger.warning(
                    f"Wicket stall detected (prev): page stayed at {new_page_num} "
                    f"(was {prev_page_num}) — AJAX session exhausted"
                )
                return False

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
    max_hops = 50  # enough for ~100 pages (25 × ~4 pages/hop)
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
                if pg > current and pg <= target and (best_pg is None or pg > best_pg):
                    best_pg = pg
                    best_link = link
            else:
                # Going backward — pick lowest page >= target
                if pg < current and pg >= target and (best_pg is None or pg < best_pg):
                    best_pg = pg
                    best_link = link

        if best_link is None:
            # Fallback: if target is just current+1, use the "next page" button
            if target == current + 1:
                next_btn = page.query_selector("a[rel='next'][href*='navigatorBottom-next']")
                if not next_btn:
                    next_btn = page.query_selector("a[rel='next'][href*='navigatorTop-next']")
                if next_btn:
                    logger.info(f"Hop fallback: click 'next' button da pagina {current} → {target}...")
                    next_btn.click(force=True)
                    _random_delay(DELAY_PAGE_MIN, DELAY_PAGE_MAX)
                    try:
                        page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        _random_delay(2, 3)
                    if _is_access_denied(page):
                        return False
                    continue  # re-check current page in next iteration
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

        # Detect stuck hop: if page didn't change, abort
        new_current = _get_current_page_number(page)
        if new_current == current:
            logger.warning(f"Hop stuck at page {current} (target={target}) — aborting jump")
            return False

    return _get_current_page_number(page) == target


def _get_current_page_number(page: Page) -> int:
    """Return the current result page number (1-based), or 0 if unknown."""
    # Strategy 1: disabled link in pagination span
    try:
        disabled = page.query_selector("span[style*='padding'] > a[disabled]")
        if disabled:
            return int(disabled.inner_text().strip())
    except (ValueError, TypeError):
        pass
    # Strategy 2: any disabled pageLink
    try:
        disabled = page.query_selector("a[disabled][href*='pageLink']")
        if disabled:
            return int(disabled.inner_text().strip())
    except (ValueError, TypeError):
        pass
    # Strategy 3: active class
    try:
        current = page.query_selector("a.active[href*='pageLink']")
        if current:
            return int(current.inner_text().strip())
    except (ValueError, TypeError):
        pass
    return 0


def _is_bot_challenge(page: Page) -> bool:
    """Return True if the page is a TSPD/Imperva JS challenge page."""
    try:
        content = page.content()
        if "/TSPD/" in content or "bobcmn" in content or "failureConfig" in content:
            return True
        # Detect obfuscated challenge pages (window.nqq pattern)
        if "window.nqq" in content or "fromCharCode.apply" in content:
            return True
        # Also check for very short body (challenge pages have no visible content)
        body_text = page.inner_text("body").strip()
        if len(body_text) < 50 and ("support ID" in body_text or "JavaScript" in body_text):
            return True
        # Challenge pages have no form but have heavy obfuscated JS
        if "form.ui.form" not in content and len(content) > 10000 and content.count("\\x") > 50:
            return True
    except Exception:
        pass
    return False


def _do_fresh_search(page: Page, context: BrowserContext, region_value: str, filled_profile: bool = False, province_value: Optional[str] = None) -> bool:
    """Navigate to the search page, set up filters, submit. Returns True on success."""
    for attempt in range(MAX_RETRIES):
        logger.info(f"Navigazione a {SEARCH_URL}...")
        try:
            # Use commit — just wait for server response, then wait for form
            page.goto(SEARCH_URL, wait_until="commit", timeout=60000)

            # Wait for the actual search form to appear.
            # If TSPD challenge is served, its JS will solve, set cookies,
            # and redirect — the form will appear after redirect completes.
            try:
                page.wait_for_selector("form.ui.form.styled", timeout=45000)
                logger.info("Search page loaded successfully")
                _random_delay(1, 2)
            except Exception:
                # Check what we actually got
                if _is_bot_challenge(page):
                    logger.warning(f"Bot challenge (attempt {attempt+1}) — form not loaded after 45s")
                    # Save cookies that the challenge JS may have set
                    _save_cookies(context)
                    delay = _backoff_delay(attempt)
                    logger.warning(f"Waiting {delay:.0f}s before retry...")
                    time.sleep(delay)
                    continue
                elif _is_access_denied(page):
                    delay = _backoff_delay(attempt)
                    logger.warning(f"Access Denied — waiting {delay:.0f}s")
                    time.sleep(delay)
                    continue
                else:
                    logger.warning("Form not found but page loaded — trying anyway")
                    # Double-check: if the page has no real content, treat as challenge
                    try:
                        body_len = len(page.inner_text("body").strip())
                        if body_len < 200:
                            logger.warning(f"Page body very short ({body_len} chars) — treating as bot challenge")
                            _save_cookies(context)
                            delay = _backoff_delay(attempt)
                            time.sleep(delay)
                            continue
                    except Exception:
                        pass

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

    _setup_filters_and_search(page, region_value=region_value, filled_profile=filled_profile, province_value=province_value)
    if not _wait_for_captcha(page):
        return False
    _save_cookies(context)

    # Wait for first result card (longer timeout for large result sets)
    try:
        page.wait_for_selector(
            "div.twelve.wide.column.right.floated.rounded.bordered.bgwhite",
            timeout=30000,
        )
    except Exception:
        # Debug: save HTML to diagnose
        try:
            Path("debug_fresh_search_fail.html").write_text(page.content())
            logger.debug("Saved debug HTML to debug_fresh_search_fail.html")
        except Exception:
            pass
        logger.warning("Risultati non trovati dopo la ricerca.")
        return False

    return True


def _extract_cf_from_card(card) -> str:
    """Extract Codice Fiscale from a search result card."""
    rows = card.query_selector_all(".row.rowsmall")
    for row in rows:
        cols = row.query_selector_all("div[class*='wide column']")
        if len(cols) >= 2:
            label = cols[0].inner_text().strip()
            value = cols[1].inner_text().strip()
            if "codice fiscale" in label.lower():
                return value
    return ""


def _download_filled_profile_csvs(
    page: Page,
    context: BrowserContext,
    region_value: str,
    download_dir: str,
    expected_total: int,
) -> int:
    """Download CSV profile for each startup in the search results.

    Processes results page-by-page: clicks each startup title, opens the
    download dropdown on the detail page, clicks CSV, captures the download.
    Handles Wicket AJAX limits with periodic fresh searches.

    Returns the number of files downloaded.
    """
    download_path = Path(download_dir)
    download_path.mkdir(parents=True, exist_ok=True)

    startups_per_page = 10
    total_pages = (expected_total + startups_per_page - 1) // startups_per_page
    DOWNLOADS_PER_SESSION = 8  # 8×2 title/download clicks + 1 search = 17 AJAX calls (limit ~19)

    downloaded = 0
    skipped = 0
    processed_cfs: set = set()  # track unique processed CFs (prevents double-counting)
    current_page = 1
    resume_card_idx = 0  # resume from this card index after a session-budget break
    downloads_in_session = 0

    while current_page <= total_pages and len(processed_cfs) < expected_total:
        # Fresh search on first page or after Wicket stall
        if downloads_in_session == 0:
            logger.info(f"Fresh search for CSV downloads — target page {current_page}")
            if not _do_fresh_search(page, context, region_value, filled_profile=True):
                logger.error("Fresh search failed during CSV download phase")
                break
            if current_page > 1:
                if not _jump_to_page(page, current_page):
                    logger.warning(f"Could not jump to page {current_page}, skipping this page")
                    current_page += 1
                    downloads_in_session = 0
                    continue
            downloads_in_session = 0

        # Get result cards on current page
        CARD_SELECTOR = (
            "div.twelve.wide.column.right.floated.rounded.bordered.bgwhite"
        )
        CARD_FALLBACK = "div.bgwhite:has(#title)"

        cards = page.query_selector_all(CARD_SELECTOR)
        if not cards:
            cards = page.query_selector_all(CARD_FALLBACK)

        if not cards:
            logger.warning(f"No result cards found on page {current_page}")
            break

        # ── Phase 1: collect card info (CF, name, index) before any navigation ──
        # This avoids detached-element errors after page.go_back()
        card_info: list = []  # (index, cf, name)
        for idx, card in enumerate(cards):
            cf = _extract_cf_from_card(card)
            title_link = card.query_selector("h5 a, #title a")
            name = title_link.inner_text().strip() if title_link else ""
            card_info.append((idx, cf, name))

        # ── Phase 2: process each card, re-querying after each back navigation ──
        page_fully_processed = True
        for card_idx, cf, name in card_info:
            # Skip cards already processed before a session-budget break
            if card_idx < resume_card_idx:
                continue  # already counted in a previous session on this page

            # Skip if this CF was already processed from another page/session
            if cf and cf in processed_cfs:
                continue

            target_file = download_path / f"{cf}.csv" if cf else None

            if target_file and target_file.exists():
                logger.info(
                    f"[{len(processed_cfs) + 1}/{expected_total}] {cf} "
                    f"— already downloaded, skipping"
                )
                skipped += 1
                processed_cfs.add(cf)
                continue

            if downloads_in_session >= DOWNLOADS_PER_SESSION:
                logger.info(
                    f"Wicket session budget used ({downloads_in_session} downloads) "
                    f"— will re-search to resume on same page"
                )
                resume_card_idx = card_idx  # resume from THIS card (the unprocessed one)
                page_fully_processed = False
                break

            logger.info(
                f"[{len(processed_cfs) + 1}/{expected_total}] "
                f"Downloading CSV for: {name}"
            )

            # Re-query cards — previous references are stale after go_back()
            cards = page.query_selector_all(CARD_SELECTOR)
            if not cards:
                cards = page.query_selector_all(CARD_FALLBACK)
            if card_idx >= len(cards):
                logger.warning(f"  Card {card_idx} no longer on page — skipping")
                skipped += 1
                if cf:
                    processed_cfs.add(cf)
                continue

            title_link = cards[card_idx].query_selector("h5 a, #title a")
            if not title_link:
                logger.warning(f"  Title link not found for card {card_idx} — skipping")
                skipped += 1
                if cf:
                    processed_cfs.add(cf)
                continue

            # Click title → detail page
            title_link.click()
            try:
                page.wait_for_selector("#downloadPnl", timeout=15000)
                _random_delay(0.3, 0.6)
            except Exception:
                _random_delay(2, 3)

            # Open download dropdown and click CSV
            download_icon = page.query_selector(
                "#downloadPnl i.download.icon, #downloadPnl"
            )
            csv_link = page.query_selector(
                "#downloadPnl a.item:has(i.file.excel), #downloadPnl #idf5"
            )
            if not csv_link:
                # Fallback: first link in dropdown menu
                csv_link = page.query_selector("#downloadPnl .menu a.item")

            if download_icon and csv_link:
                download_icon.click()
                _random_delay(0.5, 1)

                try:
                    with page.expect_download(timeout=30000) as download_info:
                        csv_link.click()

                    download = download_info.value
                    if cf:
                        save_path = download_path / f"{cf}.csv"
                    else:
                        save_path = download_path / download.suggested_filename
                    download.save_as(str(save_path))
                    logger.info(f"  Saved: {save_path.name} ({save_path.stat().st_size} bytes)")
                    downloaded += 1
                    downloads_in_session += 1
                    if cf:
                        processed_cfs.add(cf)
                except Exception as dl_err:
                    logger.warning(
                        f"  Download failed (likely Wicket AJAX limit): {dl_err}"
                    )
                    skipped += 1
                    if cf:
                        processed_cfs.add(cf)
                    # Force re-search for next batch
                    downloads_in_session = DOWNLOADS_PER_SESSION
            else:
                logger.warning(f"  Download elements not found for {name}")
                skipped += 1
                if cf:
                    processed_cfs.add(cf)
                try:
                    Path(f"debug_download_{cf or 'unknown'}.html").write_text(
                        page.content()
                    )
                except Exception:
                    pass

            # Back to results
            page.go_back()
            try:
                page.wait_for_selector(
                    "div.twelve.wide.column.right.floated.rounded.bordered.bgwhite",
                    timeout=15000,
                )
                _random_delay(0.3, 0.6)
            except Exception:
                _random_delay(2, 3)

            if _is_access_denied(page):
                delay = _backoff_delay(0)
                logger.warning(f"Access denied during download — waiting {delay:.0f}s")
                time.sleep(delay)
                resume_card_idx = card_idx  # resume from current card
                downloads_in_session = DOWNLOADS_PER_SESSION
                break

        # Move to next page or re-search same page
        if page_fully_processed:
            resume_card_idx = 0
            current_page += 1
        downloads_in_session = 0  # always fresh search next iteration

    logger.info(
        f"CSV downloads finished: {downloaded} downloaded, "
        f"{skipped} skipped, {len(processed_cfs)} unique → {download_path}"
    )
    return downloaded


def _single_pass_scrape_and_download(
    page: Page,
    context: BrowserContext,
    region_value: str,
    province_value: Optional[str],
    filled_profile: bool,
    download_dir: Optional[str],
    all_startups: list,
    seen_cf: set,
    resume_page: int = 1,
) -> int:
    """Scrape metadata AND download CSVs in a single pass using URL navigation.

    Strategy:
    - goto("last") to reach the last page instantly (costs 0 AJAX)
    - Chain goto("prev") backward, collecting cards + downloading CSVs
    - After PAGES_PER_SESSION goto navigations, fresh search + goto("last") to resume
    - After DOWNLOADS_PER_SESSION title clicks, fresh search (Wicket AJAX budget)
    - Skip already-downloaded files on disk

    Returns total number of new startups found.
    """
    download_path = Path(download_dir) if download_dir else None
    if download_path:
        download_path.mkdir(parents=True, exist_ok=True)

    CARD_SELECTOR = "div.twelve.wide.column.right.floated.rounded.bordered.bgwhite"
    CARD_FALLBACK = "div.bgwhite:has(#title)"
    DOWNLOADS_PER_SESSION = 8

    processed_cfs: set = set()
    downloaded = 0
    skipped = 0
    total_results = 0
    total_pages = 0
    consecutive_failures = 0
    pages_navigated_in_session = 0
    downloads_in_session = 0

    # Load existing files to skip
    existing_cfs: set = set()
    if download_path and download_path.exists():
        for f in download_path.glob("*.csv"):
            existing_cfs.add(f.stem)
        if existing_cfs:
            logger.info(f"Found {len(existing_cfs)} existing CSV files — will skip")

    # Initial search to get total results
    if not _do_fresh_search(page, context, region_value,
                            filled_profile=filled_profile,
                            province_value=province_value):
        logger.error("Initial search failed")
        return 0

    total_results = _get_total_results(page)
    total_pages = (total_results + 9) // 10 if total_results else 0
    logger.info(f"Total results: {total_results} ({total_pages} pages)")

    if total_results == 0:
        return 0

    start_page = 1
    if ENABLE_SMART_START and existing_cfs and total_results > 0:
        estimated_done = max(0, min(len(existing_cfs), total_results) // 10 - 5)
        estimated_done = min(estimated_done, max(1, total_pages - 3))
        if estimated_done > 1:
            start_page = estimated_done
            logger.info(
                f"Smart start: {len(existing_cfs)} existing CSVs, {total_results} results → "
                f"jumping to page {start_page}/{total_pages} (skipping ~{estimated_done - 1} pages)"
            )
            _jump_to_page(page, start_page)
            pages_navigated_in_session += 1

    # Collect forward from current page
    current_page = _get_current_page_number(page) or start_page

    # Track all pages we've processed
    processed_pages: set = set()
    if start_page > 1:
        for p_num in range(1, start_page):
            processed_pages.add(p_num)

    while len(processed_pages) < total_pages and consecutive_failures < 5:
        # === Process current page ===
        actual_page = _get_current_page_number(page) or current_page

        if actual_page in processed_pages:
            logger.debug(f"Page {actual_page} already processed, skipping")
        else:
            logger.info(f"[Page {actual_page}/{total_pages}] Processing... "
                       f"({len(all_startups)} startups, {downloaded} CSVs so far)")

            if _is_access_denied(page) or _is_bot_detected(page):
                delay = _backoff_delay(consecutive_failures)
                logger.warning(f"Access issue — waiting {delay:.0f}s")
                time.sleep(delay)
                consecutive_failures += 1
                # Force fresh search
                pages_navigated_in_session = PAGES_PER_SESSION
                downloads_in_session = DOWNLOADS_PER_SESSION
            else:
                # Extract cards from this page
                cards = page.query_selector_all(CARD_SELECTOR)
                if not cards:
                    cards = page.query_selector_all(CARD_FALLBACK)

                if cards:
                    consecutive_failures = 0
                    # Collect card info first
                    card_infos = []
                    for idx, card in enumerate(cards):
                        cf = _extract_cf_from_card(card)
                        title_el = card.query_selector("h5 a, #title a")
                        name = title_el.inner_text().strip() if title_el else f"card_{idx}"
                        card_infos.append((idx, cf, name))

                    # Extract metadata from all cards
                    for idx, cf, name in card_infos:
                        if cf and cf in seen_cf:
                            continue
                        try:
                            cards_now = page.query_selector_all(CARD_SELECTOR)
                            if not cards_now:
                                cards_now = page.query_selector_all(CARD_FALLBACK)
                            if idx < len(cards_now):
                                startup_data = _extract_single_card(cards_now[idx])
                                if startup_data:
                                    key = cf or startup_data.get("Denominazione", "")
                                    if key and key not in seen_cf:
                                        seen_cf.add(key)
                                        all_startups.append(startup_data)
                        except Exception as e:
                            logger.debug(f"Error extracting card {idx}: {e}")

                    # Download CSVs (if enabled and budget allows)
                    if download_path:
                        for idx, cf, name in card_infos:
                            if cf and cf in processed_cfs:
                                continue
                            if cf and cf in existing_cfs:
                                logger.debug(f"  {cf} — file exists, skip")
                                processed_cfs.add(cf)
                                skipped += 1
                                continue

                            # Check Wicket AJAX budget
                            if downloads_in_session >= DOWNLOADS_PER_SESSION:
                                logger.info(f"  Session download budget exhausted ({downloads_in_session})")
                                break

                            # Re-query cards (may be stale after go_back)
                            cards_now = page.query_selector_all(CARD_SELECTOR)
                            if not cards_now:
                                cards_now = page.query_selector_all(CARD_FALLBACK)
                            if idx >= len(cards_now):
                                logger.warning(f"  Card {idx} disappeared after go_back")
                                if cf:
                                    processed_cfs.add(cf)
                                continue

                            title_link = cards_now[idx].query_selector("h5 a, #title a")
                            if not title_link:
                                logger.warning(f"  No title link for card {idx}")
                                if cf:
                                    processed_cfs.add(cf)
                                continue

                            # Click title → detail page → download CSV → go_back
                            success = _download_single_csv(page, title_link, cf, name, download_path)
                            downloads_in_session += 1
                            if success:
                                downloaded += 1
                            else:
                                skipped += 1
                            if cf:
                                processed_cfs.add(cf)

                            # Check for access denied after go_back
                            if _is_access_denied(page) or _is_bot_detected(page):
                                logger.warning("Access issue during downloads — forcing fresh search")
                                downloads_in_session = DOWNLOADS_PER_SESSION
                                break

                    processed_pages.add(actual_page)
                else:
                    logger.warning(f"No cards found on page {actual_page}")
                    processed_pages.add(actual_page)

        # === Decide next action ===
        need_fresh_search = (
            downloads_in_session >= DOWNLOADS_PER_SESSION or
            pages_navigated_in_session >= PAGES_PER_SESSION
        )

        if need_fresh_search:
            # Find next unprocessed page
            target_page = None
            for p_num in range(1, total_pages + 1):
                if p_num not in processed_pages:
                    target_page = p_num
                    break

            if target_page is None:
                logger.info("All pages processed!")
                break

            logger.info(f"Session refresh — next target: page {target_page}")
            if not _do_fresh_search(page, context, region_value,
                                    filled_profile=filled_profile,
                                    province_value=province_value):
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    break
                time.sleep(_backoff_delay(consecutive_failures))
                continue

            pages_navigated_in_session = 0
            downloads_in_session = 0

            # Navigate to target page
            if target_page == 1:
                pass  # already on page 1
            elif target_page > total_pages - 5:
                # Close to end — goto("last") then prev
                if _goto_nav_url(page, "last"):
                    pages_navigated_in_session += 1
                    current_pg = total_pages  # We KNOW we're on last page
                    while current_pg > target_page and pages_navigated_in_session < PAGES_PER_SESSION:
                        if not _goto_nav_url(page, "prev"):
                            break
                        pages_navigated_in_session += 1
                        current_pg -= 1
            else:
                # Use _jump_to_page for middle pages
                _jump_to_page(page, target_page)

            continue

        # Try navigating to next unprocessed page
        next_page = actual_page + 1
        prev_page = actual_page - 1

        # Prefer going to next unprocessed adjacent page
        if next_page <= total_pages and next_page not in processed_pages:
            if _goto_nav_url(page, "next"):
                pages_navigated_in_session += 1
                continue
        elif prev_page >= 1 and prev_page not in processed_pages:
            if _goto_nav_url(page, "prev"):
                pages_navigated_in_session += 1
                continue

        # No adjacent unprocessed — force fresh search
        pages_navigated_in_session = PAGES_PER_SESSION  # trigger fresh search

    logger.info(
        f"Single-pass complete: {len(all_startups)} startups, "
        f"{downloaded} CSVs downloaded, {skipped} skipped, "
        f"{len(processed_pages)}/{total_pages} pages processed"
    )
    return len(all_startups)


def _extract_single_card(card) -> dict:
    """Extract startup metadata from a single result card element."""
    startup = {}
    try:
        name_el = card.query_selector("#title h5 a.link, #title h5 a, h5 a")
        if name_el:
            startup["Denominazione"] = name_el.inner_text().strip()

        rows = card.query_selector_all(".row.rowsmall")
        for row in rows:
            cols = row.query_selector_all("div[class*='wide column']")
            if len(cols) >= 2:
                label = cols[0].inner_text().strip()
                value = cols[1].inner_text().strip()
                if label and value and label != value:
                    startup[label] = value

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

        tags = card.query_selector_all("a.ui.label, span.ui.label, .ui.label")
        tag_texts = [
            t.inner_text().strip() for t in tags
            if t.inner_text().strip()
            and t.inner_text().strip() != startup.get("Denominazione", "")
            and len(t.inner_text().strip()) < 100
        ]
        if tag_texts:
            startup["Tag"] = ", ".join(tag_texts)

        web_el = card.query_selector("a[target='_new'], a[target='_blank']")
        if web_el:
            href = web_el.get_attribute("href")
            if href and href != "javascript:;":
                startup["Sito Web"] = href

    except Exception as e:
        logger.debug(f"Error extracting card: {e}")

    return startup


def _download_single_csv(
    page: Page,
    title_link,
    cf: str,
    name: str,
    download_path: Path,
) -> bool:
    """Click into detail page, download CSV, go back. Returns True on success."""
    logger.info(f"  Downloading: {name} ({cf})")
    success = False

    try:
        title_link.click()
        try:
            page.wait_for_selector("#downloadPnl", timeout=15000)
            _random_delay(0.3, 0.6)
        except Exception:
            _random_delay(2, 3)

        download_icon = page.query_selector(
            "#downloadPnl i.download.icon, #downloadPnl"
        )
        csv_link = page.query_selector(
            "#downloadPnl a.item:has(i.file.excel), #downloadPnl #idf5"
        )
        if not csv_link:
            csv_link = page.query_selector("#downloadPnl .menu a.item")

        if download_icon and csv_link:
            download_icon.click()
            _random_delay(0.5, 1)

            try:
                with page.expect_download(timeout=30000) as dl_info:
                    csv_link.click()
                download = dl_info.value
                save_path = download_path / f"{cf}.csv" if cf else download_path / download.suggested_filename
                download.save_as(str(save_path))
                logger.info(f"    Saved: {save_path.name} ({save_path.stat().st_size} bytes)")
                success = True
            except Exception as e:
                logger.warning(f"    Download failed: {e}")
        else:
            logger.warning(f"    Download panel not found for {name}")

    except Exception as e:
        logger.warning(f"    Error in download flow for {name}: {e}")

    # ALWAYS go back to results list
    try:
        page.go_back()
        page.wait_for_selector(
            "div.twelve.wide.column.right.floated.rounded.bordered.bgwhite",
            timeout=15000,
        )
        _random_delay(0.3, 0.6)
    except Exception:
        _random_delay(2, 3)

    return success


def scrape_startups(region: str = "liguria", headless: bool = False, filled_profile: bool = False, download_dir=None, resume_page: int = 1, only_provinces: list = None) -> list:
    """Main scraping function with province splitting for large regions.

    Uses URL-based navigation (page.goto) to bypass Wicket AJAX budget limits.
    For regions with > 500 results, automatically splits by province.
    """
    region_key = region.strip().lower()
    if region_key not in REGIONI:
        raise ValueError(f"Regione '{region}' non valida. Valori: {', '.join(sorted(REGIONI))}")
    region_value = REGIONI[region_key]

    all_startups = []
    seen_cf: set = set()

    CHROME_ARGS = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-sandbox",
        "--single-process",
        "--disable-extensions",
        "--js-flags=--max-old-space-size=256",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-sync",
        "--disable-translate",
        "--metrics-recording-only",
        "--no-first-run",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        "--disable-component-update",
    ]

    def _open_browser(pw):
        """Launch browser + context + page, returns (browser, context, page)."""
        br = pw.chromium.launch(
            headless=headless,
            channel="chrome",
            args=CHROME_ARGS,
        )
        ctx = br.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 768},
            locale="it-IT",
        )
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
        _load_cookies(ctx)
        pg = ctx.new_page()
        return br, ctx, pg

    def _close_browser(br, ctx):
        """Save cookies, close browser, force GC."""
        _save_cookies(ctx)
        br.close()
        gc.collect()

    with sync_playwright() as p:
        # Initial search to determine total results and routing
        browser, context, page = _open_browser(p)
        try:
            if not _do_fresh_search(page, context, region_value,
                                    filled_profile=filled_profile):
                _close_browser(browser, context)
                return all_startups

            total_results = _get_total_results(page)
            logger.info(f"Total results for {region}: {total_results}")
            needs_province_split = (
                total_results > PROVINCE_SPLIT_THRESHOLD and region_key in PROVINCE
            )
        except Exception as e:
            logger.error(f"Initial search error: {e}", exc_info=True)
            _close_browser(browser, context)
            return all_startups

        # Close the initial browser — we'll reopen per province/pass
        _close_browser(browser, context)

        if needs_province_split:
            logger.info(
                f"Results ({total_results}) > threshold ({PROVINCE_SPLIT_THRESHOLD}) "
                f"— splitting by province"
            )
            provinces = PROVINCE[region_key]
            for pv_value, pv_code in provinces:
                if only_provinces and pv_code not in only_provinces:
                    logger.info(f"Skipping province {pv_code} (not in filter: {only_provinces})")
                    continue
                logger.info(f"{'='*60}")
                logger.info(f"Province: {pv_code} (value={pv_value})")
                logger.info(f"{'='*60}")

                # Retry loop: restart browser on OOM/crash, skip existing CSVs
                MAX_PROVINCE_RETRIES = 10
                for attempt in range(1, MAX_PROVINCE_RETRIES + 1):
                    browser, context, page = _open_browser(p)
                    try:
                        _single_pass_scrape_and_download(
                            page, context, region_value,
                            province_value=pv_value,
                            filled_profile=filled_profile,
                            download_dir=download_dir,
                            all_startups=all_startups,
                            seen_cf=seen_cf,
                            resume_page=1,
                        )
                        logger.info(f"Province {pv_code} done: {len(all_startups)} total startups")
                        break  # success — exit retry loop
                    except Exception as e:
                        logger.warning(f"Province {pv_code} attempt {attempt}/{MAX_PROVINCE_RETRIES} crashed: {e}")
                    finally:
                        _close_browser(browser, context)
                        logger.info(f"Browser closed after province {pv_code} attempt {attempt} — memory released")

                    # Check if all CSVs for this province might already be downloaded
                    if download_dir:
                        current_csvs = len(list(Path(download_dir).glob("*.csv")))
                        logger.info(f"CSVs on disk after attempt {attempt}: {current_csvs}")
                    time.sleep(5)  # brief pause before retry
        else:
            browser, context, page = _open_browser(p)
            try:
                _single_pass_scrape_and_download(
                    page, context, region_value,
                    province_value=None,
                    filled_profile=filled_profile,
                    download_dir=download_dir,
                    all_startups=all_startups,
                    seen_cf=seen_cf,
                    resume_page=resume_page,
                )
            except Exception as e:
                logger.error(f"Scraping error: {e}", exc_info=True)
            finally:
                _close_browser(browser, context)

        logger.info(f"Scraping complete: {len(all_startups)} total startups")

    return all_startups


def _paginate_and_collect(
    page: Page,
    context: BrowserContext,
    all_startups: list,
    seen_cf: set,
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