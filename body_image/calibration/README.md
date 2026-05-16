# calibration anchors

Three reference photos with known crowd-rated scores. When
`BODY_IMAGE_USE_CALIBRATION_ANCHORS=true`, every LLM rating call
prepends these three images with their scores. The model is told:
"the last image is the subject — score it on the same scale."

## Why

LLM vision models drift. Same photo, different day, different score.
Self-anchoring against your own past photos creates a feedback loop —
the model just learns to predict its own prior output. External ground
truth pins the scale to something that doesn't move.

## Source

[SCUT-FBP5500](https://github.com/HCIILAB/SCUT-FBP5500-Database-Release)
is a public dataset of 5500 frontal-face photos rated 1-5 by panels of
~60 raters. Multiply by 20 to convert to our 0-100 scale.

1. Clone the dataset (~600MB):
   ```
   git clone https://github.com/HCIILAB/SCUT-FBP5500-Database-Release.git
   ```
2. The label file `train_test_files/All_labels.txt` has rows of
   `<filename> <score>` (score 1-5). Pick three **male** frontal
   photos roughly at the score targets:
   - **low**: photo near score 1.9 (× 20 = 38/100)
   - **mid**: photo near score 2.75 (× 20 = 55/100)
   - **high**: photo near score 3.9 (× 20 = 78/100)
3. Copy them into this directory as:
   ```
   anchor_low.jpg
   anchor_mid.jpg
   anchor_high.jpg
   ```
4. If you used different score targets, edit `anchor_scores.json` to
   match. The keys are `overall_low`, `overall_mid`, `overall_high`,
   all 0-100 integers.
5. Set `BODY_IMAGE_USE_CALIBRATION_ANCHORS=true` in `.env` and
   restart the `mcp` container.

## Verification

The next upload's response should still come back with structured
ratings (anchors don't change the response shape, only the prompt). To
verify anchors are reaching the model, check the mcp logs for
`body_image.rater.anchors=enabled` on the upload.

## Cost impact

Each LLM call now sends 4 images instead of 1 (~3× input vision
tokens). With defaults (1 run per rater, 2 specialists, 3 raters),
that's ~12 images per upload instead of ~3. Claude vision is ~$0.005
per image; expect ~$0.06/photo with anchors on instead of ~$0.015.
