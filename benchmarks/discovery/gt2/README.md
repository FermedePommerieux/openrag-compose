# Orange/Fibre GT2 evidence

This directory contains the fail-closed consolidation and completeness-control
evidence for `orange-fibre-cross-domain-v1`.

- `raw/` preserves the three human-review workbooks byte-for-byte.
- `*-human-review-source.json` is a normalized export of human-owned columns only.
- `*-consolidated-qrels-draft.{json,csv}` consolidates 337 human document judgments.
  It is explicitly a draft and is not a frozen ground truth.
- `*-completeness-candidates.json` contains 13 unlabeled candidates selected for a
  new human pass. Retrieval and title-family signals are diagnostic only.
- `*-negative-control.json` records the deterministic 60-document control and its
  human labels.
- `*-freeze-gate.json` records the blocked, fail-closed freeze decision.
- `*-freeze-report.md` is the A–T campaign report.

The corresponding editable review workbook is
`outputs/gt2-completeness-control/orange-fibre-GT2-completeness-review-pass-3.xlsx`.
No `benchmarks/discovery/ground_truth/orange-fibre-cross-domain-v1.json` exists
because the selected completeness candidates are not yet human-labeled.
