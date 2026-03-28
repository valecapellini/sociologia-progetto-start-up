# sociologia-progetto-start-up

# Specifica: Sistema di Download Dati Startup da Registro Imprese

## Scopo
Sistema automatizzato che raccoglie tutti i dati delle startup della Liguria dal Registro Imprese e li esporta in Excel.

## Requisiti Funzionali

### Ricerca e Filtraggio
- Accedere alla piattaforma `startup.registroimprese.it`
- Selezionare categoria "Startup"
- Filtrare per regione "Liguria"
- Eseguire ricerca avanzata

### Estrazione Dati
- Estrarre tutti i dati disponibili di ogni startup (ragione sociale, settori, tag, data aggiornamento, etc.)
- Gestire la paginazione e raccogliere risultati da tutte le pagine

### Export Excel
- Esportare in `.xlsx` con:
  - Header con nomi campi
  - Una riga per ogni startup
  - Formattazione leggibile
  - Nomefile descrittivo: `startup_liguria_[DATA].xlsx`

### Gestione Errori
- Gestire errori di connessione
- Gestire protezioni (WAF, CAPTCHA)
- Retry automatici
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