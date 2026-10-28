# Scoring Rubric

## Overall Rule

Start each dimension at 100, subtract for visible mismatch, then calculate the weighted overall score:

```text
overallScore =
  layoutStructure * 0.40 +
  visualStyle * 0.25 +
  contentAssets * 0.15 +
  perceptualSimilarity * 0.20
```

Use deductions consistently:

- Critical: large section missing, major structural failure, or dominant visual mismatch. Usually 15-30 dimension points.
- Major: clearly visible mismatch that affects the page's fidelity. Usually 6-14 dimension points.
- Minor: small mismatch visible on inspection but not dominant. Usually 1-5 dimension points.

Finding-level `deduction` should represent estimated overall score points lost, after dimension weighting. `dimensionDeduction` should represent points lost inside that dimension.

## Layout Structure - 40%

Primary evidence order for layout:

1. Overlay screenshot, when available.
2. Design screenshot and rendered screenshot side by side.
3. Design DSL bounds, hierarchy, and style geometry.
4. Implementation code and CSS.

The overlay is the first-class evidence for layout scoring. Use it before reading code-level intent: if the overlay shows edge drift, cluster shift, element-size mismatch, baseline mismatch, or accumulated vertical/horizontal offset, treat that visual mismatch as real unless the raw screenshots clearly contradict it. Code and DSL should explain why the mismatch happened and how to fix it; they should not override the overlay's visual evidence.

Host chrome boundary: system status bars, WebView title bars, mini-program capsule controls, native navigation controls, and bottom gesture bars are outside the page fidelity target. If these regions were cropped from the design side before overlay generation, exclude them from layout, visual, content, and perceptual deductions. If they were not cropped and cause an apparent whole-page vertical shift, identify the issue as material alignment first; do not suggest implementing fake host chrome in page code.

Evaluate geometry and structure:

- Element presence and hierarchy.
- Position, width, height, and bounding boxes.
- Padding, margin, gap, alignment, and grid/list rhythm.
- Layering, z-index, overlap, and clipping.
- Relative relationships between key elements.

Look for shifted clusters, misaligned edges, different baselines, inconsistent spacing, and regions where design and render outlines do not coincide. When an overlay is provided, mention overlay evidence in every major or critical layout finding.

Typical deductions:

- Critical: key section missing or completely reflowed, -20 to -30.
- Major: primary content block shifted by more than roughly 12px, major size mismatch, or repeated spacing error, -8 to -16.
- Minor: small edge, gap, or alignment drift, -1 to -6.

Actionable layout findings should name the region, expected geometry, actual geometry, overlay evidence, and likely CSS/layout property to change.

## Visual Style - 25%

Evaluate the styling of visible elements:

- Colors and gradients.
- Typography: font family, size, weight, line height, letter spacing, text color.
- Border, radius, shadow, opacity, blur, stroke, and elevation.
- Background treatments and surface contrast.
- Component state appearance when visible.

Use the design and rendered screenshots as primary evidence. Use DSL style data to confirm values when available. Avoid color judgments from the overlay because it blends pixels.

Typical deductions:

- Critical: dominant color/theme is wrong or typography system is broadly mismatched, -18 to -28.
- Major: important component has wrong color, font size, radius, shadow, or contrast, -7 to -15.
- Minor: slight color, weight, radius, or shadow mismatch, -1 to -6.

Actionable visual findings should include target CSS properties such as `color`, `background`, `font-size`, `font-weight`, `line-height`, `border-radius`, `box-shadow`, or opacity.

## Content And Assets - 15%

Evaluate whether the right content and assets appear:

- Text content, labels, numbers, and formatting.
- Images, icons, logos, illustrations, and placeholders.
- Cropping, aspect ratio, object fit, and asset scale.
- Missing, duplicated, or extra elements.
- Correct ordering of repeated items.

Use DSL and screenshots together. DSL is useful for confirming expected text and asset nodes; screenshots are useful for detecting visible omissions and wrong crops.

Typical deductions:

- Critical: primary image, logo, CTA, or content group missing, -18 to -30.
- Major: wrong image/icon, obvious text mismatch, incorrect crop, or missing repeated item, -7 to -16.
- Minor: small text formatting issue, slight crop mismatch, or minor asset scale issue, -1 to -6.

Actionable content findings should identify whether the fix is likely text replacement, asset substitution, `object-fit`/crop adjustment, or component rendering logic.

## Perceptual Similarity - 20%

Evaluate the whole-page impression:

- Overall "at a glance" resemblance.
- Visual density and balance.
- Distribution of whitespace and contrast.
- Local diff concentration around important areas.
- Whether the page feels like the same design even if some details differ.

Use all images together. The overlay is especially useful for seeing where differences cluster, but the raw design/rendered screenshots should determine final judgment.

Typical deductions:

- Critical: page reads as a different layout or product surface, -20 to -30.
- Major: several visible mismatches combine into a clearly lower-fidelity result, -8 to -18.
- Minor: page is generally faithful with a few visible local differences, -1 to -7.

Perceptual findings may summarize cross-cutting issues, but they still need concrete evidence and an optimization direction.

## Prioritization

Sort optimization work by expected score gain and repair clarity:

1. Fix missing or wrong primary content.
2. Fix large layout shifts and section-level geometry.
3. Fix repeated spacing, alignment, and sizing errors.
4. Fix dominant typography and color mismatches.
5. Fix local polish issues such as subtle shadows, radius, and small icon scale.

`expectedGain` should estimate recoverable overall score points, not dimension points. It should usually be lower than or equal to `deduction`, because one fix may only partially recover the observed loss.
