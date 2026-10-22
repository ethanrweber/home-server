#!/bin/sh
# pings the "drive health" check on healthchecks.io. scrutiny-healthcheck.service runs this script
# after each good collector run. the ping is:
# - success, if scrutiny shows every disk as passed, with data from the last 12 hours
# - fail, if not. the ping body lists the problem disks, and healthchecks.io emails you
# if scrutiny does not answer, the script stops and sends no ping. healthchecks.io then emails you
# when the grace time ends. README.md explains the ping body
set -eu

summary=$(curl -fsS --max-time 30 "$SCRUTINY_URL/api/summary")

# collector_date is utc, for example 2026-10-22T07:00:31Z. its first 19 characters plus "Z" are
# the format that jq's fromdateiso8601 reads. a disk without data gets 1970, so it shows as stale
problems=$(printf '%s' "$summary" | jq -r '
  .data.summary[]
  | select(.device.archived | not)
  | ((.smart.collector_date // "1970-01-01T00:00:00")[0:19] + "Z" | fromdateiso8601) as $date
  | select(.device.device_status != 0 or $date < now - 12 * 3600)
  | "\(.device.device_name) \(.device.model_name) \(.device.serial_number): device_status=\(.device.device_status)"
    + (if $date < now - 12 * 3600 then ", stale" else "" end)')

if [ -z "$problems" ]; then
    curl -fsS --max-time 30 --retry 3 -o /dev/null "$HC_URL"
else
    printf '%s\n' "$problems" # for the journal
    curl -fsS --max-time 30 --retry 3 -o /dev/null --data-raw "$problems" "$HC_URL/fail"
fi
