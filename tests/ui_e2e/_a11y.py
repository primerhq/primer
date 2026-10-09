"""One definition of "a visible control with no accessible name", for the journeys and the standing sweep that look at the REAL page.

A name is, in this order: ``aria-labelledby``, ``aria-label``, a ``<label>`` pointing at the control; a button (or an element with a button-like role) may also be named by its text, its ``title``,
an image's ``alt`` or an SVG ``<title>``. A ``placeholder`` is NOT a name: it vanishes when the user types and is not read as the field's name by every screen reader.
"""

from __future__ import annotations

from playwright.sync_api import Page

UNNAMED_JS = r"""(rootSel) => {
  const root = (rootSel && document.querySelector(rootSel)) || document;
  const rect = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const shown = e => { const s = getComputedStyle(e); return s.visibility !== 'hidden' && s.display !== 'none' && rect(e); };
  const textOf = id => { const x = document.getElementById(id); return x ? x.textContent.trim() : ''; };
  const BUTTONISH = ['button', 'tab', 'menuitem', 'checkbox', 'switch', 'radio', 'option', 'link'];
  const CONTROL_ROLES = ['button', 'textbox', 'combobox', 'checkbox', 'switch', 'radio', 'tab', 'menuitem', 'searchbox', 'spinbutton', 'slider'];
  const out = [];
  for (const e of root.querySelectorAll('input, select, textarea, button, [role]')) {
    const role = e.getAttribute('role');
    const tag = e.tagName;
    const native = tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA' || tag === 'BUTTON';
    if (!native && !CONTROL_ROLES.includes(role)) continue;
    if (tag === 'INPUT' && e.type === 'hidden') continue;
    if (!shown(e) || e.closest('[aria-hidden="true"], [inert]')) continue;
    const labelledby = (e.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean).map(textOf).join(' ').trim();
    const arialabel = (e.getAttribute('aria-label') || '').trim();
    const labels = Array.from(e.labels || []).map(l => l.textContent.trim()).join(' ').trim();
    let name = labelledby || arialabel || labels;
    const buttonish = tag === 'BUTTON' || BUTTONISH.includes(role) || (tag === 'INPUT' && ['button', 'submit', 'reset', 'image'].includes(e.type));
    if (!name && buttonish) {
      const img = e.querySelector('img[alt]');
      const svgTitle = e.querySelector('svg title');
      name = (e.textContent || '').trim() || (e.getAttribute('title') || '').trim() || (e.value || '').trim()
        || (img ? (img.getAttribute('alt') || '').trim() : '') || (svgTitle ? svgTitle.textContent.trim() : '');
    }
    if (!name) out.push({ html: e.outerHTML.replace(/\s+/g, ' ').slice(0, 220), testid: e.getAttribute('data-testid') || '' });
  }
  return out;
}"""


def unnamed_controls(page: Page, root: str | None = None) -> list[str]:
    """The outer HTML (first 220 characters) of every visible control under ``root`` (a CSS selector; the whole page when none) that has no accessible name."""
    return [item["html"] for item in page.evaluate(UNNAMED_JS, root)]
