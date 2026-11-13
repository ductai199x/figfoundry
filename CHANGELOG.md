# Changelog

All notable changes to Fig Foundry. Versions follow [semantic versioning](https://semver.org/).

## 0.4.0 — 2026-11-13

Existing specs render pixel-identically to 0.3.1.

### Fixed

- **`aspect` on contour, contourf and pcolormesh.** These now get `ax.set_aspect(...)` (they
  reject an `aspect=` kwarg), and only when the spec sets `aspect`, so their default stays
  matplotlib's `auto`.
- **Export dpi equals script dpi.** `GET /figure`, `figfoundry render` and the new
  headless export render at the canvas `dpi`, which is also the generated script's default
  `--dpi`. The server export and a script run are now the same file. They used to differ
  for rasterized content such as `imshow`, because the server exported at 150 dpi. The
  interactive preview still renders at 150 dpi.
- **Portable generated scripts.** The `.py` no longer hardcodes an absolute,
  user-specific dataset path. By default it reads the dataset file next to itself, and
  `--data` overrides that path. It also takes `--out`.
- **Nothing is dropped silently.** Unknown plot types, artist types and keys, invalid
  values, and malformed elements are now reported. They appear as `warnings` in the render
  and `/code` responses, in the `X-Figfoundry-Warnings` header of `/figure`, on stderr from
  the CLI, and as comments at the top of the generated code.
- **An empty tick list hides the ticks.** `"xticks": []` emits `ax.set_xticks([])`.
  Before, it meant "auto". The editor accepts `none` for this.
- Canvas and code files are read and written as UTF-8 (non-ASCII text such as ✓).
- The test suite no longer writes into the real `~/.figfoundry`.

### Added

- Spec `schema_version` (now 2) and an optional `min_figfoundry`. A spec made for a newer
  version produces a warning.
- `vmin` / `vmax` / `norm` (`linear`, `log`, `symlog`) on imshow, pcolormesh, contourf,
  contour, hexbin, hist2d and grouped scatter.
- An optional `id` / `tag` on any plot, artist or figure artist. Each is kept through
  editor round-trips and written as a comment in the code.
- `"coords": "data"` on `box` and `text` artists, so heatmap cell outlines follow the grid.
- A `cell_marks` artist that marks many heatmap cells with one box, x or dot marker, with
  `color`, `lw`, `inset` and `alpha`.
- The render response returns `plot_bboxes`, each plot's drawn axes box and tight box in
  figure fractions. These differ from the spec `rect` under `aspect: equal`.
- Layout warnings for overlapping tick labels, and for plots or texts that extend past the
  canvas edge.
- `axes.tick_params` controls the tick marks: visibility, `length`, `width`, `pad`
  and `direction`.
- Canvas history. Every save snapshots the previous version (the newest 100 are kept).
  `GET /api/canvases/{slug}/history`, `GET …/history/{id}` and `POST …/restore` read and
  restore these snapshots.
- Headless export: `figfoundry export <slug|spec.json> --pdf --py --png --svg`, plus
  `--out-dir` for a bundle (script, figure, spec and a dataset copy), `--data` and `--dpi`.
  `figfoundry render` also accepts a spec `.json`.
- Signal slices in data refs: `"@M[0:8]"`, `"@M[:, 2]"`.
- A text outline: `"stroke": {"width": 2, "color": "#000"}` on text and figure text.
- Editor: a figure-level line artist with draggable endpoints, image aspect, axis-label
  font size, and a canvas size that is independent of the plot size (plots keep their size
  in inches). It also has controls for the color range and scale, tick marks, text and box
  coordinates, and cell markers, and it shows render warnings in the status line.
