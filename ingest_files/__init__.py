"""One-shot loaders for vendor data-export files.

These sources have no usable ongoing API (Garmin export, Zero, the Whoop
official CSV export, the Cal AI PDF report), or the export is simply richer
than the API (Lose It). Each loader lands its records verbatim in
`raw_import` keyed on a stable natural key, so re-running is idempotent and
the original payload survives every later re-derivation.

Nothing here writes to a canonical fact table directly -- `unify` does that,
reading back out of `raw_import`.
"""
