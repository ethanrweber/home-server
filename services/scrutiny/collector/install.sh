#!/bin/bash
# installs or updates the scrutiny collector on the machine with the disks. run it as root:
#   curl -fsSL https://raw.githubusercontent.com/ethanrweber/home-server/main/services/scrutiny/collector/install.sh | bash
# to update, run the same command again. it installs the files and the collector version from main,
# and it keeps the ping url from the first installation
set -euo pipefail

# VERSION must be the same release as the image tag in ../scrutiny.yml. SHA256 is the sha256 digest
# of scrutiny-collector-metrics-linux-amd64 on the release page
VERSION=0.10.0
SHA256=d1d5180b10b2c183670c931c38ae149544627a0525640664b754d3a0fc48a5ad
FILES=https://raw.githubusercontent.com/ethanrweber/home-server/main/services/scrutiny/collector

# everything is in a function, so bash runs nothing if the download of this script stops halfway
main() {
    apt-get install -y smartmontools jq

    # the service account of the collector. the unit sets Group=disk, which gives it the disks
    id scrutiny-svc >/dev/null 2>&1 || adduser --system --group --home /opt/scrutiny-svc scrutiny-svc

    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT

    # set -e stops the script if the checksum does not match
    curl -fsSL -o "$tmp/collector" "https://github.com/AnalogJ/scrutiny/releases/download/v$VERSION/scrutiny-collector-metrics-linux-amd64"
    echo "$SHA256  $tmp/collector" | sha256sum -c
    install -D -m 755 "$tmp/collector" /opt/scrutiny/bin/scrutiny-collector-metrics

    for f in collector.yaml scrutiny-healthcheck.sh scrutiny-collector.service scrutiny-collector.timer scrutiny-healthcheck.service; do
        curl -fsSL -o "$tmp/$f" "$FILES/$f"
    done
    install -D -m 644 "$tmp/collector.yaml" /opt/scrutiny/config/collector.yaml
    install -m 755 "$tmp/scrutiny-healthcheck.sh" /opt/scrutiny/bin/
    install -m 644 "$tmp/scrutiny-collector.service" "$tmp/scrutiny-collector.timer" "$tmp/scrutiny-healthcheck.service" /etc/systemd/system/

    # the ping url is a secret, so read it from the terminal without an echo. stdin is this script
    if [ ! -s /etc/scrutiny/healthcheck.env ]; then
        install -d -m 700 /etc/scrutiny
        read -rsp 'ping url of the "drive health" check: ' hc_url </dev/tty
        echo
        case "$hc_url" in
            https://*) ;;
            *) echo "the ping url must start with https://" >&2; exit 1 ;;
        esac
        install -m 600 /dev/null /etc/scrutiny/healthcheck.env
        printf 'HC_URL=%s\n' "$hc_url" >/etc/scrutiny/healthcheck.env
    fi

    systemctl daemon-reload
    systemctl enable --now scrutiny-collector.timer

    # run the collector once to test the installation. the healthcheck normally starts after it.
    # starting it here too waits for it to finish, so its log is complete
    systemctl start scrutiny-collector.service && systemctl start scrutiny-healthcheck.service || {
        journalctl -u scrutiny-collector -u scrutiny-healthcheck -n 30 --no-pager
        exit 1
    }
    journalctl -u scrutiny-collector -u scrutiny-healthcheck -n 30 --no-pager
    systemctl list-timers scrutiny-collector.timer --no-pager
    echo "done. check the scrutiny ui, and that the drive health check on healthchecks.io is up"
}

main "$@"
