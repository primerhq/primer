/* global React */
//
// Minimal markdown-to-React renderer used by the chat surface to make
// LLM responses readable. Supports the subset of markdown that chat
// models actually emit:
//
//   * Headings (# .. ######)
//   * Bold (**x** or __x__) and italic (*x* or _x_)
//   * Inline code (`x`) and fenced code blocks (```lang\n...\n```)
//   * Unordered lists (-, *, +)  and ordered lists (1. 2. ...)
//   * Links ([text](url)) — http(s)/mailto only, dropped otherwise
//   * Horizontal rules (---)
//   * Paragraphs separated by blank lines; soft newlines become <br/>
//   * Blockquotes (> line)
//
// Designed to be safe against XSS — output is composed of React
// elements, never dangerouslySetInnerHTML. URLs in links are
// whitelisted to http/https/mailto so 'javascript:' / 'data:' can't
// sneak through. Untrusted source is the LLM stream.
//
// Tolerates partial input (streaming): an unclosed `**` mid-response
// degrades to literal asterisks until the closer arrives, then snaps
// into bold on the next re-render. Acceptable jitter.
//
// Exposes window.renderMarkdown(text) -> React children.

(function () {
  const SAFE_PROTOCOL = /^(https?:|mailto:)/i;

  // --------------------------------------------------------------------
  // Inline parser — bold / italic / code / link, applied to a single
  // line of text. Returns an array of React nodes (strings + spans).
  // --------------------------------------------------------------------
  function renderInline(text, keyPrefix) {
    const out = [];
    let i = 0;
    let buf = "";
    let nodeKey = 0;

    const flushBuf = () => {
      if (buf) {
        out.push(buf);
        buf = "";
      }
    };
    const push = (node) => {
      flushBuf();
      out.push(React.cloneElement(node, { key: `${keyPrefix}-${nodeKey++}` }));
    };

    while (i < text.length) {
      const ch = text[i];

      // Inline code: `…` — content is rendered verbatim.
      if (ch === "`") {
        const end = text.indexOf("`", i + 1);
        if (end > i) {
          push(<code className="md-code">{text.slice(i + 1, end)}</code>);
          i = end + 1;
          continue;
        }
      }

      // Bold: **…** or __…__
      if ((ch === "*" || ch === "_") && text[i + 1] === ch) {
        const marker = ch + ch;
        const end = text.indexOf(marker, i + 2);
        if (end > i + 1) {
          push(<strong>{renderInline(text.slice(i + 2, end), `${keyPrefix}-b${nodeKey}`)}</strong>);
          i = end + 2;
          continue;
        }
      }

      // Italic: *…* or _…_  (single-char marker; require non-space after)
      if ((ch === "*" || ch === "_") && text[i + 1] && text[i + 1] !== ch && !/\s/.test(text[i + 1])) {
        // Find a matching unescaped closer that isn't followed by the
        // same char (so '**' bold doesn't match as opening italic).
        let j = i + 1;
        while (j < text.length) {
          if (text[j] === ch && text[j - 1] !== "\\" && text[j + 1] !== ch && text[j - 1] !== ch) {
            break;
          }
          j++;
        }
        if (j < text.length && j > i + 1) {
          push(<em>{renderInline(text.slice(i + 1, j), `${keyPrefix}-i${nodeKey}`)}</em>);
          i = j + 1;
          continue;
        }
      }

      // Link: [text](url)
      if (ch === "[") {
        const closeText = text.indexOf("]", i + 1);
        if (closeText > i && text[closeText + 1] === "(") {
          const closeUrl = text.indexOf(")", closeText + 2);
          if (closeUrl > closeText + 1) {
            const linkText = text.slice(i + 1, closeText);
            const url = text.slice(closeText + 2, closeUrl).trim();
            if (SAFE_PROTOCOL.test(url)) {
              push(
                <a href={url} target="_blank" rel="noreferrer noopener">
                  {renderInline(linkText, `${keyPrefix}-l${nodeKey}`)}
                </a>
              );
            } else {
              // Drop the URL but keep the visible text — safer than
              // rendering 'javascript:' links.
              push(<span>{renderInline(linkText, `${keyPrefix}-lt${nodeKey}`)}</span>);
            }
            i = closeUrl + 1;
            continue;
          }
        }
      }

      buf += ch;
      i++;
    }

    flushBuf();
    return out;
  }

  // --------------------------------------------------------------------
  // Block parser — group lines into paragraphs, headings, lists, code
  // blocks, blockquotes, hrs. Each block renders as a React element.
  // --------------------------------------------------------------------
  function renderMarkdown(input) {
    if (typeof input !== "string" || !input) return null;
    const lines = input.replace(/\r\n?/g, "\n").split("\n");
    const blocks = [];
    let i = 0;
    let blockKey = 0;

    const pushBlock = (node) => {
      blocks.push(React.cloneElement(node, { key: `md-b${blockKey++}` }));
    };

    while (i < lines.length) {
      const line = lines[i];

      // Fenced code block: ```lang ... ```
      const fence = line.match(/^```(\w*)\s*$/);
      if (fence) {
        const lang = fence[1] || "";
        const buf = [];
        i++;
        while (i < lines.length && !/^```\s*$/.test(lines[i])) {
          buf.push(lines[i]);
          i++;
        }
        if (i < lines.length) i++; // consume closing fence
        pushBlock(
          <pre className={`md-pre lang-${lang || "plain"}`}>
            <code>{buf.join("\n")}</code>
          </pre>
        );
        continue;
      }

      // Horizontal rule
      if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) {
        pushBlock(<hr className="md-hr" />);
        i++;
        continue;
      }

      // Heading
      const h = line.match(/^(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (h) {
        const level = h[1].length;
        const text = h[2];
        const Tag = `h${level}`;
        pushBlock(
          <Tag className={`md-h md-h${level}`}>
            {renderInline(text, `md-h${level}`)}
          </Tag>
        );
        i++;
        continue;
      }

      // Blockquote — collapse consecutive '>' lines into one block.
      if (/^\s*>\s?/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          buf.push(lines[i].replace(/^\s*>\s?/, ""));
          i++;
        }
        pushBlock(
          <blockquote className="md-quote">
            {renderInline(buf.join(" "), "md-q")}
          </blockquote>
        );
        continue;
      }

      // Unordered list — accept *, -, +; stop at blank line or block change.
      if (/^\s*[*\-+]\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*[*\-+]\s+/.test(lines[i])) {
          const itemText = lines[i].replace(/^\s*[*\-+]\s+/, "");
          items.push(itemText);
          i++;
        }
        pushBlock(
          <ul className="md-ul">
            {items.map((it, idx) => (
              <li key={`md-uli${idx}`}>{renderInline(it, `md-uli${idx}`)}</li>
            ))}
          </ul>
        );
        continue;
      }

      // Ordered list — 1. 2. ...
      if (/^\s*\d+\.\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
          const itemText = lines[i].replace(/^\s*\d+\.\s+/, "");
          items.push(itemText);
          i++;
        }
        pushBlock(
          <ol className="md-ol">
            {items.map((it, idx) => (
              <li key={`md-oli${idx}`}>{renderInline(it, `md-oli${idx}`)}</li>
            ))}
          </ol>
        );
        continue;
      }

      // Blank line — paragraph break.
      if (/^\s*$/.test(line)) {
        i++;
        continue;
      }

      // Paragraph — accumulate consecutive non-blank, non-block lines.
      const buf = [line];
      i++;
      while (
        i < lines.length &&
        !/^\s*$/.test(lines[i]) &&
        !/^```/.test(lines[i]) &&
        !/^(#{1,6})\s+/.test(lines[i]) &&
        !/^\s*[*\-+]\s+/.test(lines[i]) &&
        !/^\s*\d+\.\s+/.test(lines[i]) &&
        !/^\s*>\s?/.test(lines[i])
      ) {
        buf.push(lines[i]);
        i++;
      }
      // Soft newlines become <br/>; preserve hard breaks.
      const children = [];
      buf.forEach((bl, idx) => {
        if (idx > 0) children.push(<br key={`br-${idx}`} />);
        children.push(...renderInline(bl, `p${blockKey}-${idx}`));
      });
      pushBlock(<p className="md-p">{children}</p>);
    }

    return blocks;
  }

  window.renderMarkdown = renderMarkdown;
})();
