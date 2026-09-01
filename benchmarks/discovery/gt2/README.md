# Orange/Fibre GT2 evidence

This directory contains the fail-closed consolidation, final completeness
control, and frozen evidence for `orange-fibre-cross-domain-v1`.

- `raw/` preserves all four human-review workbooks byte-for-byte, including pass 3.
- `*-human-review-source.json` is a normalized export of human-owned columns only.
- `*-consolidated-qrels-draft.{json,csv}` consolidates 337 human document judgments.
  It remains preserved as the pre-pass-3 draft.
- `*-pass-3-review-import.json` contains only the 13 reviewed `human_label` and
  `review_notes` values plus their candidate identities.
- `*-consolidated-qrels-frozen.{json,csv}` consolidates 350 human judgments.
- `*-completeness-candidates.json` preserves the now-completed pass-3 selection.
- `*-completeness-control-final.json` records the post-pass-3 full-universe rerun.
- `*-negative-control.json` records the deterministic 60-document control and its
  human labels.
- `*-freeze-gate.json` records the passed, fail-closed freeze decision and digest.
- `../ground_truth/orange-fibre-cross-domain-v1.json` is the frozen GT2 definition.
- `*-freeze-report.md` is the A–T freeze and horizon-campaign report.

Unjudged documents remain excluded from qrels and judged-only metrics; they are
never defaulted to `NOT_RELEVANT`.
