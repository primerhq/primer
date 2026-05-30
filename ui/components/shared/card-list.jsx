/* global React */

function CardList({ items, renderCard, empty }) {
  if (!items || items.length === 0) {
    return (
      <div className="card-list-empty">
        {empty || "No items"}
      </div>
    );
  }
  return (
    <div className="card-list">
      {items.map((it, i) => (
        <React.Fragment key={it.id != null ? it.id : i}>
          {renderCard(it, i)}
        </React.Fragment>
      ))}
    </div>
  );
}

function Card({ title, subtitle, pill, meta, onClick, children }) {
  const interactive = typeof onClick === "function";
  return (
    <div
      className={`card ${interactive ? "card-interactive touch-target" : ""}`}
      onClick={onClick}
      role={interactive ? "button" : undefined}
      tabIndex={interactive ? 0 : undefined}
      onKeyDown={
        interactive
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onClick(e);
              }
            }
          : undefined
      }
    >
      <div className="card-row">
        <div className="card-title-wrap">
          {title && <div className="card-title">{title}</div>}
          {subtitle && <div className="card-subtitle">{subtitle}</div>}
        </div>
        {pill && <div className="card-pill">{pill}</div>}
      </div>
      {children && <div className="card-body">{children}</div>}
      {meta && <div className="card-meta">{meta}</div>}
    </div>
  );
}

window.CardList = CardList;
window.Card = Card;
