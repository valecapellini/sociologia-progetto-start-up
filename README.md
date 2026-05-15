# Scraper Startup Italia — Registro Imprese

Scraper Playwright per esportare le startup innovative dal portale del Registro Imprese, con supporto a filtri per regione, profilo compilato e province selezionate.

## Requisiti

- Python 3.9+
- connessione internet
- Chromium installato tramite Playwright

## Installazione

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Se non attivi l'ambiente virtuale, usa `./venv/bin/python` al posto di `python`.

## Uso rapido

```bash
source venv/bin/activate

# Regione default: liguria
python main.py

# Regione esplicita
python main.py --regione campania

# Solo startup con profilo compilato
python main.py --regione campania --filled-profile

# Solo alcune province
python main.py --regione campania --filled-profile --province NA SA

# Senza finestra browser
python main.py --regione campania --filled-profile --headless

# Riprendi da una pagina specifica
python main.py --regione campania --resume-page 20

# Log dettagliato
python main.py --regione campania --filled-profile -v
```

## Opzioni CLI

- `--regione`, `-r`: regione da scaricare
- `--headless`: esegue Chromium senza UI
- `--verbose`, `-v`: abilita log dettagliato
- `--output`, `-o`: directory di output, default `dati`
- `--filled-profile`, `--fp`: limita la ricerca alle startup con profilo compilato e abilita il download del CSV di dettaglio per ogni startup
- `--resume-page`: riparte da una pagina 1-based
- `--province`: limita l'esecuzione ad alcune province, ad esempio `--province NA SA`

## Output

Ogni esecuzione genera un export aggregato nella directory di output:

- `startup_<regione>_<data>.xlsx`
- `startup_<regione>_<data>.csv`

Quando usi `--filled-profile`, i CSV di dettaglio delle singole startup vengono salvati in:

- `dati/startup_<regione>_<data>_csv/`

## Esecuzione robusta per regioni grandi

Per regioni molto grandi o quando il sito inizia a bloccare le sessioni, usa il wrapper:

```bash
source venv/bin/activate
./run_scraper.sh NA SA
```

Il wrapper:

- riavvia automaticamente lo scraper dopo crash o blocchi del browser
- conta i CSV già scaricati e continua dal progresso attuale
- aumenta il cooldown quando un run non produce nuovi CSV

## Come lavora lo scraper

- usa Playwright con Chromium e cookie persistenti
- divide automaticamente per provincia le regioni molto grandi
- scarica i risultati aggregati in Excel/CSV
- con `--filled-profile` apre le schede delle startup e salva il CSV di dettaglio

## Note operative

- il sito può mostrare CAPTCHA o blocchi anti-bot: quando succede, conviene aspettare e rilanciare
- i cookie vengono salvati in `cookies.json`
- i file generati in `dati/` e i log locali sono esclusi dal versionamento

## Struttura del progetto

```text
main.py          CLI
scraper.py       logica Playwright e navigazione
exporter.py      export Excel/CSV
run_scraper.sh   wrapper di restart per run lunghi
requirements.txt dipendenze Python
```

## Licenza

MIT