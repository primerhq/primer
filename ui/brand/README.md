# primer · brand kit

Five-poly rotated-quad mark. One SVG, two colors max. Designed to be legible at 12 px.

## Files

| File | Use |
|---|---|
| `logo.svg` | **Recommended for web/code.** Themeable — ink via CSS `color`, accent baked at `#61d46a` |
| `logo-light.svg` | Dark ink on light backgrounds, all colors baked |
| `logo-dark.svg` | Light ink on dark backgrounds, all colors baked |
| `logo-mono.svg` | Single-color (no accent), uses `currentColor` |
| `wordmark-light.svg` | Mark + "primer" wordmark for light bg |
| `wordmark-dark.svg` | Mark + "primer" wordmark for dark bg |
| `favicon-{16,32,48,64,128,256,512}.png` | Light-mode PNG raster, transparent bg |
| `social-{256,512}.png` | Dark-bg square for OG / app icons |

## Colors

| Role | Value | Notes |
|---|---|---|
| Ink | `#0d100f` | Default foreground |
| Paper | `#f7f6f3` | Default background |
| Accent | `#61d46a` | Primer green — equivalent to `oklch(0.78 0.18 145)` |

## Usage

### Web / HTML

```html
<img src="brand/logo.svg" alt="primer" width="24" height="24" />
```

For dynamic theming, inline the file's contents and control color via CSS:

```html
<span style="color: #0d100f; display: inline-flex; align-items: center; gap: 8px;">
  <!-- paste contents of logo.svg here -->
  <strong>primer</strong>
</span>
```

### As favicon

```html
<link rel="icon" type="image/svg+xml" href="brand/logo.svg" />
<link rel="icon" type="image/png" sizes="32x32" href="brand/favicon-32.png" />
<link rel="apple-touch-icon" href="brand/favicon-128.png" />
```

### In a GitHub README (auto-switches light/dark)

```md
![primer](brand/wordmark-light.svg#gh-light-mode-only)
![primer](brand/wordmark-dark.svg#gh-dark-mode-only)
```

### Minimum sizes

- Mark only: **12 px** (24 px viewBox; scales cleanly to 16/24/32/48/64)
- Wordmark lockup: **20 px tall**
- Preserve at least 1 mark-unit of clearspace on every side
