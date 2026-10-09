/* global React, Icon */

function BottomSheet({ open, onClose, title, footer, children }) {
  const sheetRef = React.useRef(null);
  // Focus moves into the open sheet, Tab cycles inside it and focus returns to the opener on close (foundation/focus-trap.js). The sheet
  // stays mounted while closed, so the trap is gated on `open`.
  window.primerApi.useFocusTrap(sheetRef, !!open);
  window.primerApi.useEscape(() => { if (onClose) onClose(); }, !!open);
  React.useEffect(() => {
    if (!open) return undefined;
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prevOverflow;
    };
  }, [open]);

  if (!open) return null;
  return (
    <div className="sheet-overlay" onClick={onClose}>
      <div
        className="sheet"
        role="dialog"
        aria-modal="true"
        aria-label={typeof title === "string" ? title : undefined}
        tabIndex={-1}
        ref={sheetRef}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sheet-handle" />
        {title && (
          <div className="sheet-h">
            <span className="title">{title}</span>
            <button className="close touch-target" onClick={onClose} aria-label="Close">
              <Icon name="x" size={16} />
            </button>
          </div>
        )}
        <div className="sheet-b">{children}</div>
        {footer && <div className="sheet-f">{footer}</div>}
      </div>
    </div>
  );
}

window.BottomSheet = BottomSheet;
