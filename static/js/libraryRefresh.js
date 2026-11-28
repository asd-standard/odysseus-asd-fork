// static/js/libraryRefresh.js
//
// Debounced cross-module notification that the document library changed.
// Agent tool results (main chat, compare, group) call scheduleLibraryRefresh();
// documentLibrary.js listens for the resulting `library-refresh` event and
// refetches its folder tree and current grid.

const LIBRARY_TOOLS = new Set([
  'make_library_file',
  'manage_library_folders',
  'create_document',
]);

const MUTATING_DOC_ACTIONS = new Set(['move', 'delete', 'tidy']);

const REFRESH_DEBOUNCE_MS = 600;

function _successfulToolOutput(json) {
  if (!json || json.ask_user) return false;
  // Some success paths omit exit_code; an explicit non-zero code means the
  // action failed and changed nothing.
  return json.exit_code === 0 || json.exit_code == null;
}

function _manageDocumentsChanged(json) {
  if (!json.command) return true; // no args available: refresh to be safe
  try {
    const args = JSON.parse(json.command);
    const action = String((args && args.action) || 'list').toLowerCase();
    return MUTATING_DOC_ACTIONS.has(action);
  } catch (_) {
    // Unknown payload shape: refresh to be safe rather than miss a change.
    return true;
  }
}

/** Whether this tool_output event changed the document library. */
export function libraryToolChanged(json) {
  if (!_successfulToolOutput(json)) return false;
  const tool = String((json && json.tool) || '');
  if (LIBRARY_TOOLS.has(tool)) return true;
  if (tool === 'manage_documents') return _manageDocumentsChanged(json);
  return false;
}

/** Debounce a library refresh after a successful library-mutating tool call. */
export function scheduleLibraryRefresh(json) {
  if (!libraryToolChanged(json)) return;
  if (window._libraryRefreshTimer) clearTimeout(window._libraryRefreshTimer);
  window._libraryRefreshTimer = setTimeout(() => {
    window._libraryRefreshTimer = null;
    window.dispatchEvent(new CustomEvent('library-refresh'));
  }, REFRESH_DEBOUNCE_MS);
}
