# Singapore Government Design System — Design Style Reference

```yaml
# How this file was made. Sources are first-party; nothing here is
# read from a rendered page. Full breakdown at source_page below.
method: restated-from-official-docs
confidence: medium
verified_at: unknown
source_page: https://www.designsystems.one/design-systems/singapore-design
sources:
  - https://www.designsystem.tech.gov.sg/
  - https://www.designsystem.tech.gov.sg/foundations/color/
```

> **Unofficial, community-authored reference.** Compiled by [designsystems.one](https://www.designsystems.one). Not affiliated with, endorsed by, or sponsored by Singapore Government. "Singapore Government Design System" and related marks belong to their respective owners. This document describes publicly observable style attributes in original words — it contains no copied documentation, logos, icon artwork, or original token files. Always defer to the official source for canonical, current values.

**System:** Singapore Government Design System
**Owner:** Singapore Government
**Official documentation:** https://www.designsystem.tech.gov.sg/
**Visual character:** Smart-nation efficiency: Inter throughout, one civic blue, and Bootstrap-derived components hardened for government scale.

---

## Summary

Design system for Singapore Government digital services.

_Editorial confidence: medium. Most values are publicly documented; a few are best-effort readings of the live product. Field-level evidence: 0/60 claims source-linked, 0/60 dated. Confidence is not verification; validate canonical values before implementation._

[Inspect every field and its evidence](https://www.designsystems.one/api/style-spec?system=singapore-design)

## Color tokens

| Color | Value | CSS variable | Role |
| --- | --- | --- | --- |
| Brand Blue | `#0f71bb` | `--sgds-primary` | Primary actions and links |
| Text | `#1d2939` | `--sgds-body-color` | Primary text |
| Secondary Text | `#667085` | `--sgds-secondary-text` | Secondary text |
| White | `#ffffff` | `--sgds-white` | Surfaces |
| Gray 100 | `#f7f7f9` | `--sgds-gray-100` | Subtle background |
| Border | `#d0d5dd` | `--sgds-border-color` | Borders |
| Danger | `#d7260f` | `--sgds-danger` | Errors |
| Success | `#0a8217` | `--sgds-success` | Success |

## Typography

- **Body:** Inter — weights 400, 500, 600, 700 — `Inter, system-ui, -apple-system, sans-serif`

## Type scale

| Step | Size | Line height |
| --- | --- | --- |
| h1 | `44px` | `57px` |
| h2 | `36px` | `47px` |
| h3 | `28px` | `36px` |
| body | `16px` | `24px` |

## Spacing

Base unit: `8px`

Scale: `4px` · `8px` · `16px` · `24px` · `32px` · `48px`

## Corner radius

| Name | Value |
| --- | --- |
| default | `5px` |

## Elevation / shadows

| Name | Value |
| --- | --- |
| default | `0 1px 2px rgba(16,24,40,0.05)` |

## Quick start (CSS)

```css
/* Singapore Government Design System — illustrative tokens, restated as CSS custom properties.
   Unofficial reference by designsystems.one; values are indicative, not
   canonical. Native dp/sp values are omitted, never silently converted.
   Official reference: https://www.designsystem.tech.gov.sg/
   Field evidence: https://www.designsystems.one/api/style-spec?system=singapore-design */

:root {
  /* Color */
  --sgds-primary: #0f71bb; /* Primary actions and links */
  --sgds-body-color: #1d2939; /* Primary text */
  --sgds-secondary-text: #667085; /* Secondary text */
  --sgds-white: #ffffff; /* Surfaces */
  --sgds-gray-100: #f7f7f9; /* Subtle background */
  --sgds-border-color: #d0d5dd; /* Borders */
  --sgds-danger: #d7260f; /* Errors */
  --sgds-success: #0a8217; /* Success */

  /* Typography */
  --font-body: Inter, system-ui, -apple-system, sans-serif;

  /* Type scale */
  --text-h1: 44px; /* line-height 57px */
  --text-h2: 36px; /* line-height 47px */
  --text-h3: 28px; /* line-height 36px */
  --text-body: 16px; /* line-height 24px */

  /* Spacing */
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 16px;
  --space-4: 24px;
  --space-5: 32px;
  --space-6: 48px;

  /* Radius */
  --radius-default: 5px;

  /* Elevation */
  --shadow-default: 0 1px 2px rgba(16,24,40,0.05);
}
```

## Engineering profile

- **Ships for:** CSS-only
- **Token pipeline:** Custom
- **Source model:** Open source

## Apply this style with an AI tool

Paste this into your AI coding assistant as a direction, then iterate:

> Design in the spirit of Singapore Government Design System by Singapore Government — Design system for Singapore Government digital services. Character: Smart-nation efficiency: Inter throughout, one civic blue, and Bootstrap-derived components hardened for government scale. Use a primary colour near #0f71bb, the typeface "Inter" (or a close equivalent), corner radius around 5px, a spacing rhythm on a 8px base. Match the intent, not the pixels; adapt it to my product rather than cloning it.

## Official reference

- [Singapore Government Design System documentation](https://www.designsystem.tech.gov.sg/)

---

_Generated by [designsystems.one](https://www.designsystems.one/design-systems/singapore-design) — a catalogue of public design systems. Style facts are reported for reference and education; adapt them to your own product. Report issues or takedown requests via the site._
