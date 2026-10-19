/** Small Markdown renderer for the bundled help guides.
 *
 * Supports headings (#..####), paragraphs, bullet and numbered lists, block quotes, fenced code,
 * tables, horizontal rules, **bold**, *italic*, `code` and [links](url). Everything is HTML-escaped
 * first and the result goes through DOMPurify with a short allow-list, so even a hostile guide
 * cannot inject markup. Links must be http(s), mailto or an in-app "#help/..." anchor.
 */
import DOMPurify from "dompurify";

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => s.replace(/[&<>"']/g, (c) => ESC[c]);

function slugify(text) {
  // Split-and-join is linear; trimming dashes with /-+$/ backtracks quadratically on long runs.
  return text.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean).join("-");
}

function safeHref(href) {
  const h = href.trim();
  return /^(https?:\/\/|mailto:|#)/i.test(h) ? h : "";
}

/** Inline formatting on text that is already escaped. */
function inline(text) {
  const codes = [];
  let out = text.replace(/`([^`]+)`/g, (_, c) => {
    codes.push(c);
    return `${codes.length - 1}`;
  });
  // The {n,m} bounds keep the scans linear on input with many unclosed delimiters.
  out = out
    .replace(/\*\*([^*]{1,1000})\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]{0,1000})\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]{1,500})\]\(([^)\s]{1,2000})\)/g, (_, label, href) => {
      const url = safeHref(href.replaceAll("&amp;", "&"));
      if (!url) return label;
      const ext = /^https?:/i.test(url);
      return `<a href="${esc(url)}"${ext ? ' target="_blank" rel="noopener noreferrer"' : ""}>${label}</a>`;
    });
  return out.replace(/(\d+)/g, (_, i) => `<code>${codes[Number(i)]}</code>`);
}

// U+E000 (private use) delimits the inline-code placeholders, so it is stripped from the input first.
const cleanInput = (md) => String(md ?? "").replaceAll("", "").replace(/\r\n?/g, "\n");

function splitRow(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
}

const isTableSep = (l) => l.includes("-") && splitRow(l).every((c) => /^:?-{2,}:?$/.test(c));
const LIST_ITEM = /^\s*([-*]|\d+\.)\s+/;
const isBlockStart = (l) => /^(#{1,4}\s|>\s?|\s*[-*]\s|\s*\d+\.\s|```|---+\s*$)/.test(l);

// Each block reader looks at lines[i]; it returns the next index and pushes its HTML, or -1 if
// the line does not start that kind of block.
function readFence(lines, i, html) {
  if (!lines[i].startsWith("```")) return -1;
  const buf = [];
  let j = i + 1;
  while (j < lines.length && !lines[j].startsWith("```")) buf.push(lines[j++]);
  html.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`);
  return j + 1;
}

function readHeading(lines, i, html) {
  const h = /^(#{1,4})\s+(\S.*)$/.exec(lines[i]);
  if (!h) return -1;
  const level = h[1].length;
  html.push(`<h${level} id="g-${slugify(h[2])}">${inline(esc(h[2]))}</h${level}>`);
  return i + 1;
}

function readRule(lines, i, html) {
  if (!/^---+\s*$/.test(lines[i])) return -1;
  html.push("<hr>");
  return i + 1;
}

function readQuote(lines, i, html) {
  if (!lines[i].startsWith(">")) return -1;
  const buf = [];
  let j = i;
  while (j < lines.length && lines[j].startsWith(">")) buf.push(lines[j++].replace(/^>\s?/, ""));
  html.push(`<blockquote>${inline(esc(buf.join(" ")))}</blockquote>`);
  return j;
}

function readTable(lines, i, html) {
  if (!lines[i].includes("|") || i + 1 >= lines.length || !isTableSep(lines[i + 1])) return -1;
  const head = splitRow(lines[i]);
  let j = i + 2;
  const rows = [];
  while (j < lines.length && lines[j].includes("|") && lines[j].trim()) rows.push(splitRow(lines[j++]));
  const cell = (tag, c) => `<${tag}>${inline(esc(c))}</${tag}>`;
  html.push(
    `<table><thead><tr>${head.map((c) => cell("th", c)).join("")}</tr></thead><tbody>` +
    rows.map((r) => `<tr>${r.map((c) => cell("td", c)).join("")}</tr>`).join("") +
    "</tbody></table>",
  );
  return j;
}

function readList(lines, i, html) {
  const listMatch = LIST_ITEM.exec(lines[i]);
  if (!listMatch) return -1;
  const tag = /\d/.test(listMatch[1]) ? "ol" : "ul";
  const items = [];
  let j = i;
  while (j < lines.length && LIST_ITEM.test(lines[j])) {
    let text = lines[j++].replace(LIST_ITEM, "");
    // Indented follow-on lines continue the same item.
    while (j < lines.length && lines[j].trim() && /^\s{2,}\S/.test(lines[j]) && !LIST_ITEM.test(lines[j])) {
      text += ` ${lines[j++].trim()}`;
    }
    items.push(`<li>${inline(esc(text))}</li>`);
  }
  html.push(`<${tag}>${items.join("")}</${tag}>`);
  return j;
}

function readParagraph(lines, i, html) {
  const buf = [lines[i].trim()];
  let j = i + 1;
  while (j < lines.length && lines[j].trim() && !isBlockStart(lines[j])) buf.push(lines[j++].trim());
  html.push(`<p>${inline(esc(buf.join(" ")))}</p>`);
  return j;
}

const BLOCK_READERS = [readFence, readHeading, readRule, readQuote, readTable, readList, readParagraph];

export function markdownToHtml(md) {
  const lines = cleanInput(md).split("\n");
  const html = [];
  let i = 0;
  while (i < lines.length) {
    if (!lines[i].trim()) { i++; continue; }
    for (const read of BLOCK_READERS) {
      const next = read(lines, i, html);
      if (next >= 0) { i = next; break; }
    }
  }
  return sanitizeGuide(html.join("\n"));
}

function sanitizeGuide(html) {
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS: ["h1", "h2", "h3", "h4", "p", "ul", "ol", "li", "strong", "em", "code", "pre", "a",
      "blockquote", "hr", "table", "thead", "tbody", "tr", "th", "td", "br"],
    ALLOWED_ATTR: ["href", "rel", "id"],
    ADD_ATTR: ["target"],
    ALLOW_DATA_ATTR: false,
  });
}

/** Plain-text outline of headings (level 2 and 3) for a guide's table of contents. */
export function headingsOf(md) {
  const out = [];
  let inFence = false;
  for (const line of cleanInput(md).split("\n")) {
    if (line.startsWith("```")) inFence = !inFence;
    const m = !inFence && /^(#{2,3})\s+(\S.*)$/.exec(line);
    if (m) out.push({ level: m[1].length, text: m[2], id: `g-${slugify(m[2])}` });
  }
  return out;
}
