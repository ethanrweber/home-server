#!/bin/sh
# pings the "drive health" check on healthchecks.io. scrutiny-healthcheck.service runs this script
# after each good collector run. the ping is:
# - success, if scrutiny shows at least one disk, and every disk as passed with data from the last
#   12 hours. for an ssd, only a failure of its own SMART thresholds counts (see below)
# - fail, if not. the ping body lists the problems, and healthchecks.io emails you
# if scrutiny does not answer, the script stops and sends no ping. healthchecks.io then emails you
# when the grace time ends. README.md explains the ping body
set -eu

summary=$(curl -fsS --max-time 30 "$SCRUTINY_URL/api/summary")

# collector_date is utc, for example 2026-10-22T07:00:31Z. its first 19 characters plus "Z" are
# the format that jq's fromdateiso8601 reads. a disk without data gets 1970, so it shows as stale.
# an ssd (rotational_speed 0) ignores device_status 2: scrutiny's own thresholds come from
# backblaze's statistics for hard drives, and ssds use the same attribute ids for other counters
problems=$(printf '%s' "$summary" | jq -r '
  .data.summary[]
  | select(.device.archived | not)
  | ((.smart.collector_date // "1970-01-01T00:00:00")[0:19] + "Z" | fromdateiso8601) as $date
  | (if .device.rotational_speed == 0 and .device.device_status == 2 then 0 else .device.device_status end) as $status
  | select($status != 0 or $date < now - 12 * 3600)
  | "\(.device.device_name) \(.device.model_name) \(.device.serial_number): device_status=\($status)"
    + (if $date < now - 12 * 3600 then ", stale" else "" end)')

# no disks at all is a problem too, for example a new scrutiny database and a collector that finds
# no disks
disks=$(printf '%s' "$summary" | jq '[.data.summary[] | select(.device.archived | not)] | length')
if [ "$disks" -eq 0 ]; then
    problems="scrutiny lists no disks"
fi

if [ -z "$problems" ]; then
    curl -fsS --max-time 30 --retry 3 -o /dev/null "$HC_URL"
else
    printf '%s\n' "$problems" # for the journal
    curl -fsS --max-time 30 --retry 3 -o /dev/null --data-raw "$problems" "$HC_URL/fail"
fi
