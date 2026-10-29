/**
 * Foreground stack for tool windows.
 *
 * Rule (single, applied everywhere): only ONE window occupies the foreground.
 * Opening/restoring a window collapses whatever was in front of it; when that
 * window closes or is minimized, the one it displaced takes its place back.
 *
 * Deliberately DOM-free so the sequencing can be tested for real (the state
 * machine is where the bugs would live; touching elements is the easy part).
 * `modalManager.js` owns the DOM side and calls into this.
 *
 * Model
 * -----
 * `fg`      — ids of currently VISIBLE windows, most recent last.
 * `entries` — LIFO of { child, parent }: child was opened over parent, so
 *             child owes parent a restore when it leaves the foreground.
 * `suppressed` — ids whose next `left()` must not restore anything. Set when
 *             the manager minimizes a window *because another replaced it*:
 *             that minimize must not also unwind the window's own displaced
 *             grandparent (B over A over Z must not restore Z while B is up).
 *
 * Why `forget` exists
 * -------------------
 * A closed window can never come back, so pairs referencing it are dropped.
 * Without this, closing A while B is open leaves an entry that would restore
 * the closed A later — a window the user explicitly closed would reappear.
 */

export function createForegroundStack() {
  const fg = [];
  const entries = [];
  const suppressed = new Set();

  function removeFromForeground(id) {
    const at = fg.indexOf(id);
    if (at >= 0) fg.splice(at, 1);
  }

  return {
    /**
     * A window became visible / was restored.
     * Returns `{ minimize }`: the window that must collapse, or null.
     */
    opened(id) {
      if (!id) return { minimize: null };
      // Already in front — nothing to displace.
      if (fg[fg.length - 1] === id) return { minimize: null };
      // Re-surfacing a window that was minimized: drop its stale slot first so
      // it can displace whoever is in front now.
      removeFromForeground(id);

      const parent = fg.length ? fg[fg.length - 1] : null;
      if (parent) {
        entries.push({ child: id, parent });
        // The parent is about to be collapsed by the caller, so drop it from
        // the foreground now: `foreground()` then reflects what the user will
        // actually see, instead of waiting for the async hide transition.
        removeFromForeground(parent);
      }
      fg.push(id);
      return { minimize: parent };
    },

    /**
     * A window left the foreground (closed OR minimized).
     * Returns `{ restore }`: the window that should come back, or null.
     */
    left(id) {
      if (!id) return { restore: null };
      removeFromForeground(id);
      if (suppressed.has(id)) {
        // Programmatic minimize caused by a replacement: keep the entry so the
        // chain survives until the replacing window leaves.
        suppressed.delete(id);
        return { restore: null };
      }
      for (let i = entries.length - 1; i >= 0; i--) {
        if (entries[i].child === id) {
          const parent = entries[i].parent;
          entries.splice(i, 1);
          return { restore: parent };
        }
      }
      return { restore: null };
    },

    /** The next `left(id)` is a displacement, not a user action. */
    suppressNext(id) {
      if (id) suppressed.add(id);
    },

    /** A window was destroyed: drop every pair that references it. */
    forget(id) {
      if (!id) return;
      removeFromForeground(id);
      suppressed.delete(id);
      for (let i = entries.length - 1; i >= 0; i--) {
        if (entries[i].child === id || entries[i].parent === id) entries.splice(i, 1);
      }
    },

    /** Diagnostics/tests. */
    foreground() {
      return [...fg];
    },

    reset() {
      fg.length = 0;
      entries.length = 0;
      suppressed.clear();
    },
  };
}
