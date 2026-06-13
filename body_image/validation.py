"""Weekly reference-validation cron.

Runs a fixed set of reference photos through the rating pipeline every
Sunday and checks that the model's scores still correlate strongly
with their expected scores. If Pearson r drops below the threshold
(default 0.7), that's prompt drift or model regression — the prior
week's trend data is suspect and the operator gets pinged.

Reference set lives in body_image/calibration/validation/:
  <slug>.jpg               # the photo
  <slug>.score             # plaintext file with the expected 0-100 score

Lay out 5-8 references spanning the score range. Include at least one
"static control" — your own photo at a known baseline score — so a
true score shift on you is visible against a known constant.

Skips silently if the directory is empty (lets the cron job land on
the droplet before reference images are sourced).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from statistics import mean

from lifeos_core.alerts import _send_pushover, _send_slack
from lifeos_core.logging import get_logger

from .raters import run_llm_raters_once

log = get_logger(__name__)

VALIDATION_DIR = Path(__file__).parent / "calibration" / "validation"
PEARSON_ALERT_THRESHOLD = 0.7


def _load_refs() -> list[tuple[str, bytes, float]]:
    """Return [(slug, jpeg_bytes, expected_score), ...]. Filesystem
    layout is the source of truth — no DB."""
    if not VALIDATION_DIR.exists():
        return []
    out = []
    for jpg in sorted(VALIDATION_DIR.glob("*.jpg")):
        score_file = jpg.with_suffix(".score")
        if not score_file.exists():
            log.warning("body_image.validation.missing_score", file=str(jpg))
            continue
        try:
            score = float(score_file.read_text().strip())
        except ValueError:
            log.warning("body_image.validation.bad_score", file=str(score_file))
            continue
        out.append((jpg.stem, jpg.read_bytes(), score))
    return out


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Plain-Python Pearson r so we don't depend on scipy for a
    six-point correlation. NaN-safe-ish: returns 0 on degenerate input
    rather than raising."""
    if len(xs) < 2 or len(xs) != len(ys):
        return 0.0
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = sum((x - mx) ** 2 for x in xs) ** 0.5
    sy = sum((y - my) ** 2 for y in ys) ** 0.5
    if sx == 0 or sy == 0:
        return 0.0
    return num / (sx * sy)


def run_weekly_validation() -> dict:
    """Rate each reference photo with the live prompt + model, compute
    Pearson r against expected scores, push an alert if below threshold.
    Doesn't write to body_image_photo — these aren't user photos."""
    refs = _load_refs()
    if not refs:
        log.info("body_image.validation.skipped", reason="no reference images")
        return {"skipped": True, "reason": "no reference images"}

    expected: list[float] = []
    observed: list[float] = []
    per_ref: list[dict] = []
    for slug, jpeg_bytes, want in refs:
        # Single-run, no-DB pass. run_llm_raters_once returns the same
        # list shape as service._run_raters_parallel.
        results = run_llm_raters_once(jpeg_bytes)
        # Average all successful overall scores into one composite,
        # matching how the dashboard computes per-photo overall.
        overalls = [r["overall"] for r in results if isinstance(r.get("overall"), (int, float))]
        if not overalls:
            log.warning("body_image.validation.no_score", slug=slug)
            continue
        got = mean(overalls)
        expected.append(want)
        observed.append(got)
        per_ref.append({"slug": slug, "expected": want, "observed": got})

    r = _pearson(expected, observed) if len(expected) >= 2 else 0.0
    msg = (
        f"body-image validation: r={r:.3f} across {len(per_ref)} refs "
        f"(threshold {PEARSON_ALERT_THRESHOLD})"
    )
    log.info("body_image.validation.done", pearson_r=r, refs=len(per_ref))

    if r < PEARSON_ALERT_THRESHOLD and len(per_ref) >= 2:
        # Build a punch list for the alert so the operator can see WHICH
        # ref is most off (largest expected-vs-observed delta).
        deltas = sorted(per_ref, key=lambda x: abs(x["observed"] - x["expected"]), reverse=True)
        lines = [f"{r2['slug']}: want {r2['expected']:.0f}, got {r2['observed']:.0f}"
                 for r2 in deltas[:5]]
        title = "body-image: model drift"
        body = msg + "\n" + "\n".join(lines)
        # Same fallback logic as lifeos_core.alerts.check_and_alert:
        # Pushover preferred, Slack second. Both no-op silently if the
        # corresponding token env var isn't set.
        if not _send_pushover(title, body):
            _send_slack(title, body)

    return {
        "pearson_r": r,
        "refs": per_ref,
        "alerted": r < PEARSON_ALERT_THRESHOLD and len(per_ref) >= 2,
        "ran_at": datetime.utcnow().isoformat() + "Z",
    }


if __name__ == "__main__":
    # `python -m body_image.validation` from cron.
    import json as _json
    print(_json.dumps(run_weekly_validation(), indent=2))
