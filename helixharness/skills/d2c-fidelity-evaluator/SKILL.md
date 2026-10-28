---
user-invocable: false
name: d2c-fidelity-evaluator
description: Evaluate design-to-code fidelity for a generated page by comparing design inputs and implementation outputs. Use when the user provides or references a design screenshot, design DSL, source code, rendered page screenshot, and optionally a transparent design-over-render overlay image, and wants a restoration/fidelity percentage plus an actionable report that can drive the next D2C optimization pass.
---

# D2C Fidelity Evaluator

## Purpose

Score how faithfully a code-generated page restores its design source, then produce an optimization-oriented report that another D2C skill can use to improve the implementation in the next pass.

This skill evaluates visible output fidelity. Source code is used to locate likely causes and suggest implementation changes; source quality is not scored unless it creates visible design mismatch.

## Inputs

Expect these inputs from the user or surrounding task context:

- Design screenshot: the original design image.
- Design DSL: structured design data for the same page or frame.
- Implementation source: code files, snippets, or repo paths for the generated page.
- Rendered screenshot: screenshot of the implementation result.
- Overlay screenshot: optional but preferred; a transparent design-over-render overlay image.

If the overlay is available, use it primarily to diagnose layout, size, edge, and alignment drift. Do not use the overlay as the main evidence for color or typography, because the colors are blended.

For v1, assume the screenshots are comparable and the page has rendered successfully. Do not perform viewport, DPR, asset-loading, or blank-page comparability checks unless the user explicitly asks for that.

## Host Chrome Boundary

App, WebView, and mini-program container chrome is not part of the page fidelity target. Do not score system status bars, WebView title bars, mini-program capsule controls, native back controls, or bottom gesture bars as implementation omissions or extra elements.

If the design screenshot includes host chrome but the rendered screenshot represents only the H5/business page, evaluate the business page content boundary. When the surrounding workflow provides `design_crop_top`, treat that top crop as the intended material alignment and do not deduct for the removed host chrome.

If the overlay appears vertically shifted because host chrome was not cropped from the design side, report this as a material alignment issue before assigning implementation deductions. In that case, avoid turning the whole-page offset caused by host chrome into code optimization advice such as adding fake status bars or drawing mini-program capsules.

## DSL-Only Mode

When no design screenshot (`image-source.png`) is available, the evaluator operates in **dsl-only** mode:

- **Activation:** Set `D2C_FIDELITY_MODE=dsl-only` or pass `--mode dsl-only`
- **What changes:**
  - Pixel-level overlay comparison is **skipped**
  - Scoring based on geometry-diff.json (40%) + render-diff.json (40%) + vertical-diff.json (20%)
  - Report marked with "基于 DSL 评测（无设计原图）"
- **What stays:** All DSL-based checks (text, font, spacing, color, layout) still run

## Workflow

1. Inventory the provided inputs and identify the design frame/page being evaluated.
2. Read the design DSL enough to understand the intended element hierarchy, text, bounds, style tokens, and key node IDs.
3. Inspect the design screenshot, rendered screenshot, and overlay screenshot if present.
4. Compare the implementation against the design across the four scoring dimensions in `references/scoring-rubric.md`.
5. Calculate the overall fidelity score as a weighted average.
6. Produce a report using `references/report-schema.md`.
7. Write the complete Markdown report to the target workspace root's `d2c/temp/d2c-fidelity-evaluator-report/` folder. Create the folder if needed.
8. Sort findings by optimization value, not by visual narration order. The highest-priority items should be the ones most likely to raise the next score.

## Report File

Every evaluation must create a new Markdown report file on disk.

Determine the target workspace root as follows:

1. If the user provides a code file path inside a project, use that project's repository root when it can be detected.
2. Otherwise use the current working directory for the evaluation task.

Save reports under:

```text
<target-workspace-root>/d2c/temp/d2c-fidelity-evaluator-report/
```

Use this filename pattern:

```text
d2c-fidelity-evaluator-r{round}.md
```

Round selection:

- If the user provides an explicit round number, use it.
- Otherwise, if a screenshot or branch/file name contains a clear round marker such as `r6`, use that number.
- Otherwise, inspect existing `d2c/temp/d2c-fidelity-evaluator-report/d2c-fidelity-evaluator-r*.md` files and use the next integer round.
- Never overwrite an existing report. If the selected round file already exists, append `-run-{n}` before `.md`, where `n` is the next available integer.

The chat response should include the report path and a concise score summary. The full Markdown report file should include both the human-readable report and the JSON report block.

## Scoring Dimensions

Use these weights:

- Layout structure: 40%
- Visual style: 25%
- Content and assets: 15%
- Perceptual similarity: 20%

Overall score:

```text
overallScore =
  layoutStructure * 0.40 +
  visualStyle * 0.25 +
  contentAssets * 0.15 +
  perceptualSimilarity * 0.20
```

Scores are percentages from 0 to 100. Round the final score to the nearest whole number unless precise measurements justify one decimal place.

## Report Requirements

The report must be useful as input to a subsequent D2C optimization skill. Avoid generic comments such as "spacing is off" or "style differs". Each important finding must include:

- The affected target element or region.
- The scoring dimension.
- Severity and deduction.
- Expected design behavior.
- Actual rendered behavior.
- Evidence from screenshot, overlay, or DSL.
- A concrete suggested change.
- Acceptance criteria for the next evaluation.
- DSL node ID or code hints when they can be inferred.
- Expected gain, estimating how many overall score points could be recovered.

Prefer fewer high-signal findings over a long list of vague observations. Findings should be actionable, localizable, and re-checkable.

## Output

Return both:

1. A concise Markdown report for humans.
2. A valid JSON report following `references/report-schema.md` for downstream automation.
3. The filesystem path of the generated Markdown report.

The Markdown should summarize the score, dimension breakdown, top optimization plan, and key findings. The JSON should preserve the detailed structure for machine consumption.

When the user or surrounding workflow is Chinese, write the human-readable Markdown report in Chinese. Keep JSON field names in English for automation, but prefer Chinese for descriptive JSON string values such as `summary`, `notes`, `action`, `rationale`, `evidence`, `suggestedChange`, and `acceptanceCriteria`.

## Boundaries

- Do not include a separate comparability check in v1.
- Do not score code architecture, framework choice, naming, or maintainability unless it visibly affects fidelity.
- Do not overclaim exact pixel values unless they were measured. Use "about", "roughly", or ranges when estimating visually.
- If DSL and screenshots disagree, treat screenshots as the visual truth and use the DSL as structural evidence.
- If a suggested fix is uncertain, label it as a likely code hint rather than a confirmed cause.
