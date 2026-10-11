"""Serialise scene processing: our own studio/file edits re-fire hooks that must wait for us."""
import contextlib
import fcntl
import os

LOCK_PATH = f"/tmp/library-organizer-{os.getuid()}.lock"  # per user: a root-owned file would block the Stash user


@contextlib.contextmanager
def scene_lock():
    with open(LOCK_PATH, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
