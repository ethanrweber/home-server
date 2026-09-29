"""Pinging healthchecks.io. A ping that fails is logged, never fatal."""
import time
import urllib.request


def ping(url, kind='', body='', log=print):
    """kind is '' (success), 'start' or 'fail'. body shows up in the check's event log."""
    if not url:
        return
    target = url.rstrip('/') + (f'/{kind}' if kind else '')
    data = body.encode()[:100_000] if body else None   # healthchecks stores at most 100 kB
    for attempt in range(3):
        try:
            urllib.request.urlopen(urllib.request.Request(target, data=data, method='POST'), timeout=10)
            return
        except ValueError as e:   # a malformed URL: retrying won't help
            log(f'  healthchecks ping {kind or "success"} failed: {e}')
            return
        except OSError as e:
            if attempt == 2:
                log(f'  healthchecks ping {kind or "success"} failed: {e}')
            else:
                time.sleep(5)
