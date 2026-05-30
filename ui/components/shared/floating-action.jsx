/* global React, Icon */

function Fab({ icon = "plus", label, onClick }) {
  return (
    <button
      className="fab touch-target"
      onClick={onClick}
      aria-label={label || "Action"}
      title={label}
    >
      <Icon name={icon} size={22} />
    </button>
  );
}

window.Fab = Fab;
