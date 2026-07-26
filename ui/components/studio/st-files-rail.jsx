/* global React, FilesTree */
// Studio revamp - the Files rail (ui/studio/STUDIO-WIRING.md §5).
//
// FilesTree moves over UNCHANGED. It is ~1000 lines carrying the lazy one-level
// tree fetch, drag-move, upload, the 9-action context menu, collection-origin
// marking with its dirty dot, and the mount/apply modals - all of which already
// work and all of which are already covered by tests. Re-hosting it means
// wrapping it, not copying it: it is a bundle global, so this file is the seam
// where the rail's chrome goes, not a second implementation.
//
// WIRING also specifies a "Changed by agents" lens here, which filters the tree
// to paths in the turn trail and annotates each with its +/-. That trail does
// not exist yet: CommitInfo carries no file list and there is no diff route (see
// the plan's §0.2), so the lens would have nothing to filter on. It lands with
// Task 7, together with the endpoint it depends on - not as a chip that lies.

function ST2_FilesRail({ wid, studio }) {
  // FilesTree still gates its body on the persisted `filesOpen` from the v1
  // two-section sidebar. In the rail, Files IS the mode - so an operator who
  // had collapsed the section in v1 would switch modes and land on an empty
  // panel. Open it once on mount; the collapse chevron still works after that.
  var opened = React.useRef(false);
  React.useEffect(function () {
    if (opened.current) return;
    opened.current = true;
    if (!studio.state.filesOpen && studio.toggleFiles) studio.toggleFiles();
  }, []);

  return (
    <div className="col" data-testid="rail-files" style={{ flex: 1, minHeight: 0, gap: 0 }}>
      <FilesTree wid={wid} studio={studio} />
    </div>
  );
}

window.ST2_FilesRail = ST2_FilesRail;
