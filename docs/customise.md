# Customise: the owner's styling, previewed and baked

The owner of a framed card can change its text and restyle its frame before it is published. The browser shows the change live as SVG; argus-mint then **bakes** a PNG, and that PNG is what both reviewers see and what gets published. The preview and the bake use the same numbers, so what the owner sees is what they get.

## What the owner can change

| Field | |
|---|---|
| `name` | the card's name |
| `bio` | the bio text, if the card has one. An edited bio is marked `edited` and goes through the word filter, since no model screened it |
| `texts` | display text by frame slot. An empty string clears a slot |
| `style` | the frame's tint and each slot's look (below). It replaces the whole style; an empty style clears it |

Any colours are allowed: if the owner wants black on black, so be it. A frame with `style_locked: true` refuses styles. A style may only name slots the frame has.

### The style

```json
{
  "tint": {"colour": "#7a1f1f", "strength": 0.6},
  "slots": {
    "name": {"colour": "#fff4d6", "effect": "outline", "effect_colour": "#000000"},
    "bio":  {"background": {"colour": "#000000", "opacity": 0.5, "radius": 8, "padding": 4}}
  }
}
```

| Part | Fields |
|---|---|
| `tint` | `colour` (`#rrggbb`), `strength` 0–1 (default 0.6) |
| `slots.<slot>` | `colour` (unset: the frame's own), `effect` (`none \| shadow \| outline \| glow`), `effect_colour` (default black), `background` |
| `background` | `colour`, `opacity` 0–1 (default 0.6), `radius` (default 8), `padding` (default 4), in overlay pixels |

The style goes into the card's provenance, so the card can be redrawn exactly from its public metadata.

## The flow

1. **Lay out.** `POST /frames/{id}/{version}/layout` with `{"texts": {"name": "…", "bio": "…"}}` returns where every line goes, exactly as the bake will draw it.
2. **Load fonts.** `GET /frames/{id}/{version}/fonts/<path>` serves each font the layout names.
3. **Preview** in the browser as SVG: the art, the frame overlay (`GET /frames/{id}/{version}/overlay`) with the tint filter, slot backgrounds, then the text.
4. **Sign the edit.** `POST /drafts/{id}/challenge` with `{"action": "edit", "edit": {name?, bio?, texts?, style?}}`. The returned message covers the sha256 of exactly that edit. The owner's wallet signs it.
5. **Bake.** `POST /drafts/{id}/edit` with the same fields plus `message` and `signature`. Any difference from what was signed is refused. argus-mint redraws the card on the same art, with the exact frame the draft was made with (checked by hash), and saves the new image.
6. **Review again.** The new image needs both approvals ([review.md](review.md)).

## Layout

`/layout` answers with the overlay's `width` and `height` and, per slot with text:

| Field | |
|---|---|
| `font` | the font path for `/fonts/<path>`, or `null` for Pillow's built-in font |
| `size` | the chosen font size in pixels, after shrinking to fit |
| `ascent`, `descent` | the font's metrics at that size |
| `effects` | effect sizes in pixels: `shadow` offset, `outline` width, `glow` blur radius |
| `lines` | each line's `text`, left `x` and top `y` |

All positions are overlay pixels. `y` is the top of the line (the font's ascender line), not the baseline, so in SVG put the baseline at `y + ascent`.

**Fitting.** A slot draws its text at the frame's `size` if it fits, else at the largest size down to `min_size` that does, wrapping across lines if the slot has `wrap`. If it doesn't fit even at `min_size`, the last line that fits is cut back (at a word boundary when it can) and ends in an ellipsis. Spaces are collapsed first.

**Effect sizes** scale with the font size:

| Effect | Pixels |
|---|---|
| shadow offset | `max(1, round(size × 0.06))`, right and down |
| outline width | `max(1, round(size × 0.05))` |
| glow blur radius | `max(2, round(size × 0.18))` |

## Drawing order

The bake (`frames.compose`) draws, bottom to top:

1. the art, scaled to **cover** the frame's window, centred, overflow cropped;
2. the overlay, tinted if the style has a tint (its alpha is kept);
3. each slot's background: a rounded rectangle around the slot's box, grown by `padding`, at `opacity`;
4. for `glow`: the text in the effect colour, stroked at the outline width, blurred by the glow radius;
5. for `shadow`: the text in the effect colour, offset by the shadow size;
6. the text in its colour, stroked in the effect colour at the outline width for `outline`.

Slots are drawn in name order.

## The tint

The tint recolours the frame overlay: each pixel moves toward the tint colour, scaled by its own brightness, so the texture survives and the hue changes.

With tint colour `(tR, tG, tB)` in 0–1, strength `s`, and the pixel's luma `Y = 0.299·R + 0.587·G + 0.114·B`:

```
R' = (1 − s)·R + s·tR·Y
G' = (1 − s)·G + s·tG·Y
B' = (1 − s)·B + s·tB·Y
```

As a 3 × 4 matrix (row-major, offsets zero), which is what `frames.tint_matrix` returns and Pillow applies with `convert("RGB", matrix)`:

```
[ (1−s) + s·tR·0.299   s·tR·0.587           s·tR·0.114           0 ]
[ s·tG·0.299           (1−s) + s·tG·0.587   s·tG·0.114           0 ]
[ s·tB·0.299           s·tB·0.587           (1−s) + s·tB·0.114   0 ]
```

Values are rounded to 6 decimals. In the browser, use the same numbers in an SVG `feColorMatrix` with `color-interpolation-filters="sRGB"`, adding the alpha row `0 0 0 1 0` (and a zero alpha column in each colour row):

```html
<filter id="tint" color-interpolation-filters="sRGB">
  <feColorMatrix type="matrix" values="
    m00 m01 m02 0 0
    m10 m11 m12 0 0
    m20 m21 m22 0 0
    0   0   0   1 0"/>
</filter>
```

Example: `#7a1f1f` at strength 0.6 gives `tint_matrix` = `(0.485831, 0.168504, 0.032725, 0, 0.021809, 0.442816, 0.008315, 0, 0.021809, 0.042816, 0.408315, 0)`.

## Fonts

A frame's fonts ship with it, next to its YAML, and are part of the frame's hash. card-green v1 uses Cinzel and Crimson Text, both under the SIL Open Font License. `/fonts/<path>` serves only fonts the frame names, as `font/ttf` or `font/otf`, cacheable for a day.

Compositing is deterministic for a given Pillow version, so a style and the frame's hash fix the card's pixels.
