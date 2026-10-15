#!/usr/bin/env bash
# Full unattended run: step 1 collects, steps 2 and 3 work on rolling batches while step 1 is
# still running, then a final pass of each step. Every step resumes, so this can be re-run safely.
# Logs: output/step1_run.log, output/step2_run.log, output/step3_run.log, output/pipeline.log
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
mkdir -p output
rm -f output/.step1_done output/.step2_done output/.step3_done
trap "kill 0" EXIT INT TERM
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> output/pipeline.log; }

S2_WORKERS=${S2_WORKERS:-16}   # threads per step 2 process (more than ~64 in total floods local DNS)
S2_SHARDS=${S2_SHARDS:-4}       # parallel step 2 processes (CPU bound; each holds ~2 GB RAM)

run_step2() {
  for k in $(seq 0 $((S2_SHARDS - 1))); do
    python -u step2_website_check.py --shard "$k/$S2_SHARDS" --workers "$S2_WORKERS" >> "output/step2_run.$k.log" 2>&1 &
  done
  wait
}
S3_WORKERS=${S3_WORKERS:-8}

(
  log "step 1 start"
  python -u step1_google_maps.py --workers 16 >> output/step1_run.log 2>&1
  log "step 1 retry pass (failed queries)"
  python -u step1_google_maps.py --workers 8 >> output/step1_run.log 2>&1
  touch output/.step1_done
  log "step 1 done"
) &

(
  while [ ! -f output/.step1_done ]; do
    if [ -f output/step1_maps.csv ]; then
      log "step 2 rolling batch"
      run_step2
    fi
    sleep 300
  done
  log "step 2 final pass"
  run_step2
  touch output/.step2_done
  log "step 2 done"
) &

(
  while [ ! -f output/.step2_done ]; do
    if [ -f output/step2_websites.csv ]; then
      log "step 3 rolling batch"
      python -u step3_selenium_check.py --js-only --max-rank 3 --workers "$S3_WORKERS" >> output/step3_run.log 2>&1
    fi
    sleep 300
  done
  log "step 3 final pass"
  python -u step3_selenium_check.py --js-only --max-rank 3 --workers "$S3_WORKERS" >> output/step3_run.log 2>&1
  touch output/.step3_done
  log "pipeline done"
) &

wait
