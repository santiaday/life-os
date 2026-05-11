"""Manual integration test for the Cronometer mobile-API write tools.

Run: .venv/bin/python -m scripts.test_cronometer_mobile

Walks the full happy path against the real Cronometer servers:
  1. search_foods("egg") — pick the top result
  2. log_food — log 100g to today's breakfast
  3. delete_food_entry — clean up the test entry

Surfaces all responses so you can inspect the wire shapes. Does NOT exercise
create_custom_food (would leave junk foods in the user's account).
"""

from __future__ import annotations

import json
import sys
import time

from mcp_server import cronometer_write_tools as CW


def _dump(label: str, value: object) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(value, indent=2, default=str)[:2000])


def main() -> int:
    print("Cronometer mobile-API integration test")
    print("--------------------------------------")

    # 1) search
    search = CW.search_foods("egg, hard-boiled", limit=5)
    _dump("search_foods", search)
    if not search.get("ok") or not search.get("rows"):
        print("\nFAIL: search_foods returned no rows or errored")
        return 1
    top = search["rows"][0]
    food_id = top["food_id"]
    measure_id = top["measure_id"]
    translation_id = top["translation_id"]
    print(f"\nPicked: food_id={food_id} name={top['name']!r} "
          f"measure_id={measure_id} source={top['source']}")

    # 2) log
    print("\nLogging 100g to today's breakfast...")
    logged = CW.log_food(
        food_id=int(food_id),
        grams=100.0,
        measure_id=int(measure_id) if measure_id is not None else None,
        meal_window="breakfast",
        translation_id=int(translation_id or 0),
    )
    _dump("log_food", logged)
    if not logged.get("ok"):
        print("\nFAIL: log_food errored")
        return 1
    entry_id = logged["rows"][0]["entry_id"]
    print(f"\nNew entry_id: {entry_id}")
    print("Verify in the Cronometer mobile app, then press Enter to delete...")
    try:
        input()
    except EOFError:
        # Non-interactive: pause briefly so the entry is visible in the app
        time.sleep(2)

    # 3) delete
    print("\nDeleting test entry...")
    deleted = CW.delete_food_entry([entry_id])
    _dump("delete_food_entry", deleted)
    if not deleted.get("ok"):
        print("\nFAIL: delete_food_entry errored")
        return 1
    if deleted["rows"][0]["count"] != 1:
        print(f"\nWARN: expected count=1, got {deleted['rows'][0]['count']}")

    print("\nAll three calls succeeded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
