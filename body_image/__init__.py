"""Body-image rating surface.

iOS Shortcut → POST /body-image/upload → Supabase Storage + parallel fan-out
to Claude vision + GPT-4o vision + MediaPipe geometry sidecar. Two DB
tables (body_image_photo, body_image_rating). See RUNBOOK.md.
"""
