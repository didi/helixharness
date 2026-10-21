# Report Schema

Create a Markdown report file, then return a brief chat summary with the generated path. The report file must contain a human-readable Markdown report followed by a machine-readable JSON block.

## Report File

Save each report to the target workspace root's `d2c/temp/d2c-fidelity-evaluator-report/` folder:

```text
<target-workspace-root>/d2c/temp/d2c-fidelity-evaluator-report/d2c-fidelity-evaluator-r{round}.md
```

Round selection:

- Prefer an explicit round number from the user.
- Otherwise infer from obvious filename markers such as `r6`.
- Otherwise use the next integer after existing `d2c/temp/d2c-fidelity-evaluator-report/d2c-fidelity-evaluator-r*.md` reports.
- Do not overwrite an existing report. Use `d2c-fidelity-evaluator-r{round}-run-{n}.md` when the base filename already exists.

The chat answer should include the generated report path and the overall score.

## Markdown Shape

Use this structure:

- `**Fidelity Score**`: overall percentage.
- `**Report File**`: absolute path to the generated report file.
- `**Dimension Breakdown**`: a table with dimension, weight, score, and notes.
- `**Optimization Plan**`: numbered list of the highest-gain fixes.
- `**Key Findings**`: concise human-readable findings.
- `**JSON Report**`: fenced `json` block containing the full structured report.

Keep the Markdown concise. Put complete structured detail in JSON.
When the user or surrounding workflow is Chinese, use equivalent Chinese headings and write the human-readable report in Chinese. Keep the JSON field names unchanged.

## JSON Shape

The JSON must be valid: no comments, no trailing commas, and no Markdown inside string values unless necessary.

```json
{
  "version": "1.0",
  "round": 6,
  "reportFile": "/absolute/path/to/workspace/d2c/temp/d2c-fidelity-evaluator-report/d2c-fidelity-evaluator-r6.md",
  "overallScore": 82,
  "scoreUnit": "percent",
  "summary": "The implementation is close overall, with the largest losses coming from layout drift in the hero area and typography differences in primary text.",
  "dimensions": {
    "layoutStructure": {
      "weight": 0.4,
      "score": 78,
      "deduction": 22,
      "notes": "Primary content is present but several key edges are shifted relative to the design."
    },
    "visualStyle": {
      "weight": 0.25,
      "score": 84,
      "deduction": 16,
      "notes": "Colors are close, but heading weight and button radius differ."
    },
    "contentAssets": {
      "weight": 0.15,
      "score": 92,
      "deduction": 8,
      "notes": "Text and assets are mostly correct, with one icon scale mismatch."
    },
    "perceptualSimilarity": {
      "weight": 0.2,
      "score": 80,
      "deduction": 20,
      "notes": "The page reads as the same design, but the top section feels less balanced."
    }
  },
  "optimizationPlan": [
    {
      "priority": 1,
      "findingIds": ["F001"],
      "expectedGain": 4,
      "action": "Align the hero title block with the design by reducing top offset and left padding.",
      "rationale": "This is the largest visible layout drift and affects first-glance fidelity.",
      "acceptanceCriteria": "In the next overlay, the title block's left and top edges should be within roughly 4-6px of the design."
    }
  ],
  "findings": [
    {
      "id": "F001",
      "priority": 1,
      "dimension": "layoutStructure",
      "severity": "major",
      "target": "Hero title block",
      "dslNodeId": "node_123",
      "evidence": "The overlay shows the title block shifted down and right relative to the design.",
      "expected": "Title block starts around x=24px and y=96px, with the first baseline aligned to the design text.",
      "actual": "Title block appears roughly 10px farther right and 14px lower than the design.",
      "dimensionDeduction": 12,
      "deduction": 4.8,
      "expectedGain": 4,
      "suggestedChange": "Reduce the hero container's top padding and left padding, or adjust the title block margin values.",
      "codeHints": [
        {
          "file": "src/components/Hero.tsx",
          "hint": "Look for hero container padding, title margin, or gap values."
        }
      ],
      "acceptanceCriteria": "The next rendered screenshot should align the title block within roughly 4-6px of the design in the overlay."
    }
  ],
  "unresolvedQuestions": []
}
```

## Field Rules

- `overallScore`: weighted final score from 0 to 100.
- `round`: the evaluation round used in the report filename.
- `reportFile`: absolute path to the generated Markdown report.
- `dimensions.*.score`: dimension score from 0 to 100.
- `dimensions.*.deduction`: `100 - score` for that dimension.
- `findings[].dimension`: one of `layoutStructure`, `visualStyle`, `contentAssets`, `perceptualSimilarity`.
- `findings[].severity`: one of `critical`, `major`, `minor`.
- `findings[].dimensionDeduction`: score points deducted inside that dimension.
- `findings[].deduction`: estimated overall score points lost after applying the dimension weight.
- `findings[].expectedGain`: estimated overall score points recoverable in the next optimization pass.
- `findings[].dslNodeId`: use `null` when no reliable DSL node can be identified.
- `findings[].codeHints`: use an empty array when no likely code location can be inferred.
- `optimizationPlan`: should contain the top 3-7 actions most likely to improve the score.
- Major or critical `layoutStructure` findings must include overlay-based evidence when an overlay screenshot was provided.

## Finding Quality Bar

Every major or critical finding must be:

- Localizable: the next D2C pass can identify where to change.
- Actionable: the suggested change names a concrete layout, style, asset, or text adjustment.
- Re-checkable: the acceptance criteria explain how to verify improvement.
- Score-linked: the expected gain explains why it is worth prioritizing.
