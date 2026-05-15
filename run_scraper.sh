 #!/bin/bash
# Wrapper script that auto-restarts the scraper on OOM kills
# Usage: ./run_scraper.sh [province1 province2 ...]

cd "$(dirname "$0")"
source venv/bin/activate

TARGET_CSVS=1048
MAX_STALE=10  # stop after this many runs with no new CSVs
PROVINCES="${@:-NA SA}"
LOG_PREFIX="scraper_auto"

stale_count=0
run_num=0
prev_count=0

while true; do
    run_num=$((run_num + 1))
    logfile="${LOG_PREFIX}_${run_num}.log"
    
    # Count current CSVs
    current_count=$(ls dati/startup_campania_*_csv/*.csv 2>/dev/null | xargs -n1 basename | sort -u | wc -l | tr -d ' ')
    echo "[Wrapper] Run #${run_num} — ${current_count}/${TARGET_CSVS} CSVs — provinces: ${PROVINCES}"
    
    if [ "$current_count" -ge "$TARGET_CSVS" ]; then
        echo "[Wrapper] Target reached! ${current_count} >= ${TARGET_CSVS}"
        break
    fi
    
    # Check for stale progress
    if [ "$current_count" -eq "$prev_count" ] && [ "$run_num" -gt 1 ]; then
        stale_count=$((stale_count + 1))
        echo "[Wrapper] No new CSVs (stale count: ${stale_count}/${MAX_STALE})"
        if [ "$stale_count" -ge "$MAX_STALE" ]; then
            echo "[Wrapper] Stopping — no progress after ${MAX_STALE} consecutive runs"
            break
        fi
    else
        stale_count=0
    fi
    prev_count=$current_count
    
    # Run scraper
    echo "[Wrapper] Starting scraper... (log: ${logfile})"
    python main.py --regione campania --filled-profile --headless --province $PROVINCES > "$logfile" 2>&1
    exit_code=$?
    
    # Check what happened
    new_count=$(ls dati/startup_campania_*_csv/*.csv 2>/dev/null | xargs -n1 basename | sort -u | wc -l | tr -d ' ')
    new_csvs=$((new_count - current_count))
    echo "[Wrapper] Exited with code ${exit_code} — downloaded ${new_csvs} new CSVs (total: ${new_count}/${TARGET_CSVS})"
    
    if [ "$exit_code" -eq 0 ]; then
        echo "[Wrapper] Clean exit — checking if complete"
        if [ "$new_count" -ge "$TARGET_CSVS" ]; then
            echo "[Wrapper] Target reached!"
            break
        fi
    fi
    
    # Clean up zombie browsers
    pkill -9 -f chromium 2>/dev/null
    pkill -9 -f firefox 2>/dev/null
    
    # Wait before restart — longer if stale (bot cooldown)
    if [ "$new_csvs" -eq 0 ]; then
        wait_time=$((60 + stale_count * 60))
        echo "[Wrapper] Waiting ${wait_time}s before restart (cooldown — no new CSVs)..."
    else
        wait_time=30
        echo "[Wrapper] Waiting ${wait_time}s before restart..."
    fi
    sleep $wait_time
done

final_count=$(ls dati/startup_campania_*_csv/*.csv 2>/dev/null | xargs -n1 basename | sort -u | wc -l | tr -d ' ')
echo "[Wrapper] DONE — ${final_count} unique CSVs"
