/* global React */
// Shared icons + small components

const Icon = ({ name, size = 14, ...rest }) => {
  const props = { width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.75, strokeLinecap: "round", strokeLinejoin: "round", ...rest };
  switch (name) {
    case "home": return <svg {...props}><path d="M3 11l9-8 9 8" /><path d="M5 10v10h14V10" /></svg>;
    case "zap": return <svg {...props}><path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z" /></svg>;
    case "box": return <svg {...props}><path d="M3 7l9-4 9 4v10l-9 4-9-4V7z" /><path d="M3 7l9 4 9-4" /><path d="M12 11v10" /></svg>;
    case "agent": return <svg {...props}><circle cx="12" cy="9" r="3.5" /><path d="M5 20c0-3.5 3-6 7-6s7 2.5 7 6" /></svg>;
    case "graph": return <svg {...props}><circle cx="6" cy="6" r="2.5" /><circle cx="18" cy="6" r="2.5" /><circle cx="12" cy="18" r="2.5" /><path d="M7.5 7.5L11 16M16.5 7.5L13 16" /></svg>;
    case "collection": return <svg {...props}><ellipse cx="12" cy="6" rx="8" ry="2.5" /><path d="M4 6v6c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5V6" /><path d="M4 12v6c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5v-6" /></svg>;
    case "doc": return <svg {...props}><path d="M6 3h9l5 5v13H6z" /><path d="M15 3v5h5" /><path d="M9 13h7M9 17h7" /></svg>;
    case "search": return <svg {...props}><circle cx="11" cy="11" r="6.5" /><path d="M16 16l4 4" /></svg>;
    case "tools": return <svg {...props}><path d="M14 6l4-4 4 4-4 4M14 6L8 12M5 19l-3 3v-3h3l9-9 3 3-9 9z" /></svg>;
    case "llm": return <svg {...props}><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 9h2M7 13h6M7 17h4" /></svg>;
    case "emb": return <svg {...props}><circle cx="6" cy="12" r="2.5" /><circle cx="12" cy="6" r="2.5" /><circle cx="18" cy="18" r="2.5" /><path d="M7.8 10.5L10.2 7.5M13.5 8L17 16" /></svg>;
    case "subsystem": return <svg {...props}><path d="M12 2L4 6v6c0 5 3.5 8.5 8 10 4.5-1.5 8-5 8-10V6z" /></svg>;
    case "worker": return <svg {...props}><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2" /></svg>;
    case "heart": return <svg {...props}><path d="M12 21s-7-4.5-7-10a4 4 0 017-2.6A4 4 0 0119 11c0 5.5-7 10-7 10z" /></svg>;
    case "filter": return <svg {...props}><path d="M3 5h18l-7 9v6l-4-2v-4z" /></svg>;
    case "chevron-right": return <svg {...props}><path d="M9 6l6 6-6 6" /></svg>;
    case "chevron-down": return <svg {...props}><path d="M6 9l6 6 6-6" /></svg>;
    case "chevron-up": return <svg {...props}><path d="M6 15l6-6 6 6" /></svg>;
    case "chevron-left": return <svg {...props}><path d="M15 6l-6 6 6 6" /></svg>;
    case "plus": return <svg {...props}><path d="M12 5v14M5 12h14" /></svg>;
    case "minus": return <svg {...props}><path d="M5 12h14" /></svg>;
    case "x": return <svg {...props}><path d="M6 6l12 12M18 6l-12 12" /></svg>;
    case "play": return <svg {...props} fill="currentColor" stroke="none"><path d="M7 4v16l13-8z" /></svg>;
    case "pause": return <svg {...props} fill="currentColor" stroke="none"><rect x="6" y="4" width="4" height="16" /><rect x="14" y="4" width="4" height="16" /></svg>;
    case "stop": return <svg {...props} fill="currentColor" stroke="none"><rect x="5" y="5" width="14" height="14" rx="1" /></svg>;
    case "send": return <svg {...props}><path d="M22 2L11 13M22 2l-7 20-4-9-9-4z" /></svg>;
    case "copy": return <svg {...props}><rect x="8" y="8" width="13" height="13" rx="2" /><path d="M5 16V5a2 2 0 012-2h11" /></svg>;
    case "alert": return <svg {...props}><path d="M12 2l11 19H1z" /><path d="M12 9v5M12 18v.5" /></svg>;
    case "info": return <svg {...props}><circle cx="12" cy="12" r="9" /><path d="M12 11v6M12 8v.5" /></svg>;
    case "check": return <svg {...props}><path d="M4 12l5 5L20 6" /></svg>;
    case "check-circle": return <svg {...props}><circle cx="12" cy="12" r="9" /><path d="M8 12l3 3 5-6" /></svg>;
    case "x-circle": return <svg {...props}><circle cx="12" cy="12" r="9" /><path d="M9 9l6 6M15 9l-6 6" /></svg>;
    case "warn-circle": return <svg {...props}><circle cx="12" cy="12" r="9" /><path d="M12 7v6M12 16v.5" /></svg>;
    case "command": return <svg {...props}><path d="M9 6V3a3 3 0 110 6H3v0a3 3 0 116 0v12a3 3 0 11-6 0v0h6m6-12v-3a3 3 0 116 0 3 3 0 01-3 3h-3m0 0v12a3 3 0 113 3 3 3 0 01-3-3v-3" /></svg>;
    case "panel-left": return <svg {...props}><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16" /></svg>;
    case "settings": return <svg {...props}><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 01-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z" /></svg>;
    case "trash": return <svg {...props}><path d="M3 6h18M8 6V4a1 1 0 011-1h6a1 1 0 011 1v2M6 6l1 14a2 2 0 002 2h6a2 2 0 002-2l1-14" /></svg>;
    case "edit": return <svg {...props}><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7" /><path d="M18.5 2.5a2.12 2.12 0 013 3L12 15l-4 1 1-4 9.5-9.5z" /></svg>;
    case "paperclip": return <svg {...props}><path d="M21 11.5l-9.4 9.4a5 5 0 11-7.1-7.1l9.4-9.4a3.5 3.5 0 115 5L9.9 18.3a2 2 0 11-2.8-2.8l8.3-8.3" /></svg>;
    case "file": return <svg {...props}><path d="M14 3H6a2 2 0 00-2 2v14a2 2 0 002 2h12a2 2 0 002-2V9z" /><path d="M14 3v6h6" /></svg>;
    case "image": return <svg {...props}><rect x="3" y="3" width="18" height="18" rx="2" /><circle cx="9" cy="9" r="2" /><path d="M21 15l-5-5L5 21" /></svg>;
    case "refresh": return <svg {...props}><path d="M21 12a9 9 0 11-3-6.7L21 8M21 3v5h-5" /></svg>;
    case "external": return <svg {...props}><path d="M14 4h6v6M10 14L20 4M19 13v6a1 1 0 01-1 1H5a1 1 0 01-1-1V6a1 1 0 011-1h6" /></svg>;
    case "clock": return <svg {...props}><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></svg>;
    case "user": return <svg {...props}><circle cx="12" cy="8" r="4" /><path d="M4 21c0-4.4 3.6-8 8-8s8 3.6 8 8" /></svg>;
    case "bell": return <svg {...props}><path d="M6 8a6 6 0 1112 0c0 7 3 9 3 9H3s3-2 3-9zM10 21a2 2 0 004 0" /></svg>;
    case "code": return <svg {...props}><path d="M8 7l-5 5 5 5M16 7l5 5-5 5M14 4l-4 16" /></svg>;
    case "key": return <svg {...props}><circle cx="8" cy="15" r="4" /><path d="M11 13l9-9M16 8l3 3" /></svg>;
    case "git-commit": return <svg {...props}><circle cx="12" cy="12" r="3.5" /><path d="M2 12h6M16 12h6" /></svg>;
    case "fork": return <svg {...props}><circle cx="6" cy="6" r="2.5" /><circle cx="18" cy="6" r="2.5" /><circle cx="12" cy="18" r="2.5" /><path d="M6 8.5v3a3 3 0 003 3h6a3 3 0 003-3v-3M12 14.5v.5" /></svg>;
    case "compress": return <svg {...props}><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5M9 9l-5-5M15 9l5-5M9 15l-5 5M15 15l5 5" /></svg>;
    case "sun": return <svg {...props}><circle cx="12" cy="12" r="4" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2" /></svg>;
    case "moon": return <svg {...props}><path d="M20 14.5A8 8 0 019.5 4a8 8 0 1010.5 10.5z" /></svg>;
    default: return <svg {...props}><circle cx="12" cy="12" r="8" /></svg>;
  }
};

const StatusPill = ({ status, className = "", parked }) => {
  // Parked overrides the visible label/color per UI spec A.2 — pill reads WAITING,
  // amber, tooltip says "Parked on <tool_name>"
  if (parked) {
    return (
      <span className={`pill pill-paused ${className}`} title={`Parked on ${parked}`}>
        <span className="dot"></span>
        waiting
      </span>
    );
  }
  const labels = {
    created: "created",
    running: "running",
    paused: "paused",
    ended: "ended",
    completed: "ended",
    failed: "failed",
    cancelled: "cancelled",
    claimed: "claimed",
  };
  return (
    <span className={`pill pill-${status} ${className}`}>
      <span className="dot"></span>
      {labels[status] || status}
    </span>
  );
};

const Btn = ({ children, kind = "default", size, icon, iconRight, disabled, onClick, title, type = "button", ...rest }) => {
  const cls = ["btn"];
  if (kind === "primary") cls.push("btn-primary");
  if (kind === "danger") cls.push("btn-danger");
  if (kind === "ghost") cls.push("btn-ghost");
  if (size === "sm") cls.push("btn-sm");
  if (size === "lg") cls.push("btn-lg");
  // Forward arbitrary HTML attrs (data-testid, aria-*, etc.) so call
  // sites that set them — e.g. data-testid="approval-approve" in
  // approvals.jsx — actually surface them on the rendered <button>.
  return (
    <button type={type} className={cls.join(" ")} disabled={disabled} onClick={onClick} title={title} {...rest}>
      {icon && <Icon name={icon} size={13} />}
      {children}
      {iconRight && <Icon name={iconRight} size={13} />}
    </button>
  );
};

function relativeTime(secAgo) {
  if (secAgo < 5) return "just now";
  if (secAgo < 60) return `${Math.floor(secAgo)}s ago`;
  if (secAgo < 3600) return `${Math.floor(secAgo / 60)}m ago`;
  if (secAgo < 86400) return `${Math.floor(secAgo / 3600)}h ago`;
  return `${Math.floor(secAgo / 86400)}d ago`;
}

function fmtDate(d) {
  return d.toISOString().replace("T", " ").replace(/\..+$/, "");
}

// Modal — desktop: centered dialog. Mobile: bottom sheet via the
// same API. Consumers (every form modal in the app) get the mobile
// behavior automatically.
const Modal = ({ title, onClose, children, footer, danger }) => {
  const useViewport = (window.primerApi && window.primerApi.useViewport) || null;
  const vp = useViewport ? useViewport() : { isMobile: false };
  const isMobile = !!vp.isMobile;

  React.useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose && onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  React.useEffect(() => {
    if (!isMobile) return undefined;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = prev; };
  }, [isMobile]);

  if (isMobile) {
    return (
      <div className="sheet-overlay" onClick={onClose}>
        <div
          className="sheet"
          role="dialog"
          aria-modal="true"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="sheet-handle" />
          <div className="sheet-h">
            <span className="title" style={{ color: danger ? "var(--red)" : undefined }}>{title}</span>
            <button className="close touch-target" onClick={onClose} aria-label="Close"><Icon name="x" size={16} /></button>
          </div>
          <div className="sheet-b">{children}</div>
          {footer && <div className="sheet-f">{footer}</div>}
        </div>
      </div>
    );
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-h">
          <span className="title" style={{ color: danger ? "var(--red)" : undefined }}>{title}</span>
          <button className="close" onClick={onClose}><Icon name="x" size={14} /></button>
        </div>
        <div className="modal-b">{children}</div>
        {footer && <div className="modal-f">{footer}</div>}
      </div>
    </div>
  );
};

const Banner = ({ kind = "info", icon, title, detail, actions }) => (
  <div className={`banner banner-${kind}`}>
    <Icon name={icon || (kind === "warning" ? "alert" : kind === "error" ? "x-circle" : "info")} size={16} className="ico" />
    <div style={{ flex: 1, minWidth: 0 }}>
      <div className="title">{title}</div>
      {detail && <div className="detail">{detail}</div>}
    </div>
    {actions && <div className="actions">{actions}</div>}
  </div>
);

const Sparkline = ({ values, width = 80, height = 24 }) => {
  if (!values || values.length === 0) return null;
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const range = max - min || 1;
  const step = width / (values.length - 1 || 1);
  const pts = values.map((v, i) => {
    const x = i * step;
    const y = height - 2 - ((v - min) / range) * (height - 4);
    return [x, y];
  });
  const path = pts.map((p, i) => (i === 0 ? `M${p[0]},${p[1]}` : `L${p[0]},${p[1]}`)).join(" ");
  const area = `${path} L${width},${height} L0,${height} Z`;
  return (
    <svg className="spark" width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      <path d={area} className="area" />
      <path d={path} />
    </svg>
  );
};

Object.assign(window, { Icon, StatusPill, Btn, Modal, Banner, Sparkline, relativeTime, fmtDate });
