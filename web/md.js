// Small, dependency-free Markdown renderer (headings, lists, tables, code, quotes, bold/italic, links).
// Input is escaped first, so model output can never inject HTML.
(function () {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  function inline(s, opts) {
    s = esc(s);
    const codes = [];
    s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
         .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>")
         .replace(/(^|\W)_([^_\n]+)_(?=\W|$)/g, "$1<em>$2</em>")
         .replace(/\[([^\]]+)\]\((https?:[^)\s]+|#[^)\s]*|[\w./-]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    if (opts && opts.cites) {
      s = s.replace(/\[(\d{1,2})\]/g, '<span class="cite" data-cite="$1">$1</span>');
    }
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
  }

  function render(src, opts) {
    const lines = String(src || "").replace(/\r/g, "").split("\n");
    let out = "", i = 0;
    const para = [];
    const flush = () => { if (para.length) { out += `<p>${inline(para.join(" "), opts)}</p>`; para.length = 0; } };
    while (i < lines.length) {
      const l = lines[i];
      if (/^```/.test(l)) {
        flush(); const buf = []; i++;
        while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
        i++; out += `<pre><code>${esc(buf.join("\n"))}</code></pre>`; continue;
      }
      const h = l.match(/^(#{1,4})\s+(.*)$/);
      if (h) { flush(); out += `<h${h[1].length}>${inline(h[2], opts)}</h${h[1].length}>`; i++; continue; }
      if (/^---+\s*$/.test(l)) { flush(); out += "<hr>"; i++; continue; }
      if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        flush();
        const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
        const head = cells(l); i += 2;
        let t = "<table><thead><tr>" + head.map((c) => `<th>${inline(c, opts)}</th>`).join("") + "</tr></thead><tbody>";
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
          t += "<tr>" + cells(lines[i]).map((c) => `<td>${inline(c, opts)}</td>`).join("") + "</tr>"; i++;
        }
        out += t + "</tbody></table>"; continue;
      }
      if (/^\s*([-*•]|\d+[.)])\s+/.test(l)) {
        flush();
        const ordered = /^\s*\d+[.)]/.test(l);
        let items = [];
        while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) {
          let item = lines[i].replace(/^\s*([-*•]|\d+[.)])\s+/, ""); i++;
          while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) item += " " + lines[i++].trim();
          items.push(`<li>${inline(item, opts)}</li>`);
        }
        out += ordered ? `<ol>${items.join("")}</ol>` : `<ul>${items.join("")}</ul>`; continue;
      }
      if (/^>\s?/.test(l)) {
        flush(); const buf = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
        out += `<blockquote>${inline(buf.join(" "), opts)}</blockquote>`; continue;
      }
      if (!l.trim()) { flush(); i++; continue; }
      para.push(l.trim()); i++;
    }
    flush();
    return out;
  }
  window.MD = { render, esc };
})();
