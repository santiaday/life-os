"""Cal AI nutrition ingester.

Cal AI is a Firebase-backed iOS calorie tracker (project `calai-app`). The food
diary lives in Firestore and is read via the Firestore REST API with a Firebase
ID token (refreshed from a stored refresh token). Each logged food maps onto the
existing Cronometer-shaped fact_food_log / fact_food_daily (tagged source='calai'),
so the mart + every nutrition tool keep working unchanged.

See RUNBOOK.md for the reverse-engineering notes and the capture still needed to
finalize the Firestore diary query.
"""
