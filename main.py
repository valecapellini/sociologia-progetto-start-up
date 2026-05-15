#!/usr/bin/env python3
"""
Scraper Startup Italia - Registro Imprese
Raccoglie dati delle startup per regione e li esporta in Excel.

Uso:
    python main.py --regione liguria
    python main.py --regione lombardia --headless
    python main.py --regione veneto -v
    python main.py --regione liguria --filled-profile
"""
import argparse
import logging
import sys
from datetime import datetime

from scraper import scrape_startups, REGIONI
from exporter import export_to_excel


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scraper.log", encoding="utf-8"),
    ])


def main():
    regioni_help = ", ".join(sorted(REGIONI))
    parser = argparse.ArgumentParser(
        description="Scraper Startup Italia da Registro Imprese",
    )
    parser.add_argument(
        "--regione", "-r",
        default="liguria",
        help=f"Regione da cercare (default: liguria). Valori: {regioni_help}",
    )
    parser.add_argument("--headless", action="store_true", help="Esegui senza finestra browser")
    parser.add_argument("--verbose", "-v", action="store_true", help="Output dettagliato")
    parser.add_argument("--output", "-o", default="dati", help="Directory di output per il file Excel")
    parser.add_argument(
        "--filled-profile", "--fp",
        action="store_true",
        help="Filter only startups with a filled profile",
    )
    parser.add_argument(
        "--resume-page",
        type=int,
        default=1,
        help="Resume scraping from this page number (1-based)",
    )
    parser.add_argument(
        "--province",
        nargs="+",
        help="Only process these province codes (e.g. --province NA SA)",
    )
    args = parser.parse_args()

    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    logger.info(f"Avvio scraping startup - regione: {args.regione}")

    try:
        # Compute download directory if filled-profile is active
        if args.filled_profile:
            today = datetime.now().strftime("%Y%m%d")
            region_slug = args.regione.strip().lower().replace(" ", "_").replace("'", "")
            csv_dir = f"{args.output}/startup_{region_slug}_{today}_csv"
        else:
            csv_dir = None

        startups = scrape_startups(
            region=args.regione,
            headless=args.headless,
            filled_profile=args.filled_profile,
            download_dir=csv_dir,
            resume_page=args.resume_page,
            only_provinces=[c.upper() for c in args.province] if args.province else None,
        )
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    if not startups:
        logger.warning("Nessuna startup trovata.")
        sys.exit(1)

    filename = export_to_excel(startups, region=args.regione, output_dir=args.output)
    if filename:
        logger.info(f"Completato! File salvato: {filename}")
    else:
        logger.error("Errore durante l'export Excel.")
        sys.exit(1)


if __name__ == "__main__":
    main()
