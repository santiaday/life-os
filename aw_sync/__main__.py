"""`python -m aw_sync` — wraps aw_sync.sync.main."""

from aw_sync.sync import main

if __name__ == "__main__":
    raise SystemExit(main())
