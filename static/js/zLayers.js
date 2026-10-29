/**
 * Window stacking scale.
 *
 * Every layer that participates in the tool-window system reads its z-index
 * from here. CSS mirrors these values as `--z-*` custom properties in
 * `static/style.css` (`:root`); tests assert the two stay in sync.
 *
 * Ordering (low → high):
 *   windows (allocator: windowRaised → splitPane-1)
 *   dragGhost (tile preview)
 *   edgeHint (edge-dock target hint)
 *   splitPane / splitPaneRaised (doc ↔ email split)
 *   portalFloor (body-portaled dropdowns just above windows)
 *   dock (chip dock pad)
 *   chipFree (a chip displaced out of the dock)
 *   chipDrag (a chip being dragged)
 */
export const Z = Object.freeze({
  windowBase: 250,
  windowRaised: 300,
  tileStripe: 261,
  splitUnderlay: 260,
  dragGhost: 9000,
  edgeHint: 9998,
  splitPane: 10000,
  splitPaneRaised: 10010,
  portalFloor: 10030,
  dock: 10040,
  chipFree: 10050,
  chipDrag: 10060,
});

export default Z;
