"""One backup, maintenance or restore run at a time, across all repositories."""
import contextlib
import fcntl
import time

from . import config

WAIT_SECONDS = 4 * 3600


@contextlib.contextmanager
def held(log=print):
    config.LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(config.LOCK, 'w') as lock:
        deadline = time.monotonic() + WAIT_SECONDS
        waiting = False
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise RuntimeError('another backup, maintenance or restore run held the lock for 4 hours')
                if not waiting:
                    log('waiting for another backup, maintenance or restore run to finish')
                    waiting = True
                time.sleep(10)
        yield
