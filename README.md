# Scraper Startup Italia — Registro Imprese

Scarica i dati di tutte le startup innovative italiane da [startup.registroimprese.it](https://startup.registroimprese.it) e li esporta in file Excel (`.xlsx`) e CSV (`.csv`), filtrabili per **regione**.

## Requisiti

- **Python 3.9+** (già installato su macOS; su Windows scaricalo da [python.org](https://www.python.org/downloads/))
- Connessione internet

## Installazione (una sola volta)

Apri il Terminale (macOS) o il Prompt dei comandi (Windows), entra nella cartella del progetto e esegui:

```bash
# Crea un ambiente virtuale
python3 -m venv venv

# Attivalo
# macOS / Linux:
source venv/bin/activate
# Windows:
# venv\Scripts\activate

# Installa le dipendenze
pip install -r requirements.txt

# Installa il browser Chromium usato dallo scraper
playwright install chromium
```

## Uso

```bash
# Attiva l'ambiente virtuale (se non già attivo)
source venv/bin/activate   # macOS/Linux
# venv\Scripts\activate    # Windows

# Scarica le startup della Liguria (default)
python main.py

# Scarica le startup di un'altra regione
python main.py --regione lombardia
python main.py --regione veneto
python main.py --regione emilia-romagna

# Opzioni aggiuntive
python main.py --regione liguria --verbose      # Log dettagliato
python main.py --regione liguria --headless      # Senza finestra browser
python main.py --regione liguria --output ./dati # Salva nella cartella "dati"
```

### Regioni disponibili

Abruzzo, Basilicata, Calabria, Campania, Emilia-Romagna, Friuli-Venezia Giulia,
Lazio, Liguria, Lombardia, Marche, Molise, Piemonte, Puglia, Sardegna, Sicilia,
Toscana, Trentino-Alto Adige, Umbria, Valle d'Aosta, Veneto.

## CAPTCHA

Il sito può mostrare un CAPTCHA di verifica. Quando succede:

1. Lo script si **ferma automaticamente** e mostra un avviso nel terminale.
2. **Risolvi il CAPTCHA** manualmente nella finestra del browser che si è aperta.
3. Lo script **riprende da solo** dopo la risoluzione.

Alla prima esecuzione è quasi certo che venga chiesto; le esecuzioni successive usano i cookie salvati e in genere non lo richiedono più.

## Output

Lo script genera due file nella directory corrente (o in quella specificata con `--output`):

- **`.xlsx`** (Excel formattato con header blu, colonne auto-sized)
- **`.csv`** (UTF-8 con BOM, apribile in Excel su Windows)

Esempi di nomi file:
```
startup_liguria_20260328.xlsx
startup_liguria_20260328.csv
startup_lombardia_20260328.xlsx
startup_lombardia_20260328.csv
...
```

Colonne estratte per ogni startup:

| Colonna | Esempio |
|---|---|
| Denominazione | CLARITY STUDIO S.R.L. |
| Codice fiscale | 02879810998 |
| Natura giuridica | SOCIETÀ A RESPONSABILITÀ LIMITATA |
| Comune | GENOVA (GE) |
| Codice Ateco | 621000 |
| Classe Valore della Produzione | 1-100K euro |
| Classe di Addetti | non disponibile |
| Classe di Capitale | 5K-10K euro |
| Costituzione Impresa | 06/04/2023 |
| Sezione Startup | 14/04/2023 |
| Tag | Web, AI, DESIGN |

## Struttura del progetto

```
main.py          # Punto di ingresso (CLI)
scraper.py       # Logica di scraping (Playwright)
exporter.py      # Export in Excel (openpyxl)
requirements.txt # Dipendenze Python
```

## Licenza

MIT
- Logging degli errori

## Requisiti Non-Funzionali

| Requisito | Valore |
|-----------|--------|
| Tempo max completamento | < 3 ore |
| Memoria max | < 500 MB |
| User-Agent | Realistico (browser) |
| Delay tra richieste | ≥ 1 sec |
| Affidabilità | Completamento indipendente dal numero risultati |

## Flusso Principale

```
1. Naviga a homepage
2. Seleziona "Startup" + ricerca avanzata
3. Filtra Regione = Liguria
4. Esegui ricerca
5. LOOP pagine:
   - Estrai dati da pagina corrente
   - Se pagina successiva → vai a  pagina successiva
   - Altrimenti → esci
6. Esporta dati in Excel
7. Salva file
8. Notifica completamento
```

## Dati da Estrarre

| Campo | Obbligatorio |
|-------|-------------|
| Denominazione | ✓ |
| Settori | ✓ |
| Tag/Keywords | ✓ |
| Data Aggiornamento | ✓ |
| Ubicazione (se presente) | ✗ |
| Descrizione (se presente) | ✗ |

+ Qualsiasi altro campo presente nella ricerca

## Gestione Vincoli Tecnici

- ✓ Simulare browser reale (User-Agent)
- ✓ Attendere tempi realistici tra richieste
- ✓ Mantenere cookie e sessioni
- ✓ Gestire timeouts e riconeessioni

## Criteri di Accettazione

- [ ] Almeno 1 startup della Liguria raccolte
- [ ] File Excel generato e apribile
- [ ] Tutti i campi visibili presenti
- [ ] Esecuzione senza errori critici
- [ ] Delays appropriati (non abusivi)
- [ ] Completamento in tempo (< 3 ore)

---

Questa specifica è **technology-agnostic**: non prescrive se usare Node.js o Python, Puppeteer o Selenium, etc. Descrive solo **cosa** il sistema deve fare e **come** deve comportarsi.