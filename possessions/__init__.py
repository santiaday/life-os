"""Possession inventory.

A photo-backed record of every physical thing owned, built to answer three
questions: what should I get rid of, which container is it in, and what do I
pack for this trip (and what am I missing).

Layout:
    vocab.py         controlled colour / subcategory vocabularies
    storage.py       Supabase Storage wrapper (original / cutout / thumb)
    segmentation.py  rembg background removal + thumbnails  (local-only dep)
    tagging.py       Claude vision auto-tagger, strict JSON + confidences
    ingest.py        batch pipeline: hash -> dedupe -> cutout -> tag -> persist
    service.py       every DB read and write the tools and UI share
    packing.py       build_packing_list + wardrobe gap analysis
    routes.py        FastAPI surface, mounted by mcp_server.server
    templates/       the review-queue / grid / purge / packing UI

The batch ingest runs on the machine holding the photos (`python -m
possessions ingest <folder>`); the droplet never needs rembg or the images.
See RUNBOOK.md.
"""
