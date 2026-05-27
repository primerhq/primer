/* global React */

function HarnessesPage({ harnessId }) {
  return React.createElement(
    "div",
    { style: { padding: 24 } },
    React.createElement("h1", null, "Harnesses"),
    React.createElement(
      "p",
      { className: "muted" },
      harnessId ? `Detail: ${harnessId}` : "List page — Task 14 will fill this in.",
    ),
  );
}

window.HarnessesPage = HarnessesPage;
