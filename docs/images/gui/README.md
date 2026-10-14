# GUI screenshots

The README gallery uses four 1080 × 720 PNGs: `ready.png`, `mode-wifi.png`,
`review.png` and `progress.png`.

Regenerate them from the toolkit root with Tk, Pillow, ImageMagick and Xvfb:

```sh
xvfb-run -a -s '-screen 0 1400x1000x24' python3 scripts/docs_screenshots.py
```

The docs-only script uses example data and removes PNG metadata.

The optional Phoenix path has three companion images: `phoenix-entry.png`,
`phoenix-guidance.png` and `phoenix-complete.png`. See
[the Phoenix connection guide](../../phoenix-nudge.md) for the exact wording and behavior.
Capture these along with the four existing screens by adding `--phoenix`:

```sh
xvfb-run -a -s '-screen 0 1400x1000x24' python3 scripts/docs_screenshots.py --phoenix
```

The flash choices have two companion images: `flash-calibration.png` and
`flash-without-calibration-confirm.png`. The separate capture script leaves
the published setup-card fixtures unchanged and verifies that declining the
unsafe confirmation makes no backend call:

```sh
xvfb-run -a -s '-screen 0 1400x1000x24' python3 scripts/flash_docs_screenshots.py
```
