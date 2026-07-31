/* Front-end tests — run with:  node tests/test_frontend.js
 *
 * index.html has no build step and no module system, so these lift the
 * functions straight out of the file by source markers and evaluate them
 * against stubs. Three things are worth locking down:
 *
 *   1. the LaTeX guards — "$5 to $10" and "$HOME/src" must never typeset;
 *   2. pipe tables, including the ragged ones Claude actually emits;
 *   3. the file explorer's follow/remember rule across terminal tabs.
 *
 * Node is only needed for the tests. The dashboard itself stays dependency-free.
 */
"use strict";
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");

function slice(from, to) {
  const a = html.indexOf(from), b = html.indexOf(to);
  if (a < 0 || b < 0 || b < a) throw new Error(`source marker moved: ${from}`);
  return html.slice(a, b);
}

let fails = 0;
function report(name, ok, detail) {
  if (ok) { console.log("ok   " + name); return; }
  fails++;
  console.log("FAIL " + name + (detail ? "\n     " + detail : ""));
}

/* ---------------- markdown + KaTeX ---------------- */

global.katex = require(path.join(ROOT, "vendor", "katex.min.js"));
const { md } = (0, eval)(
  slice("const esc = s =>", "/* ---------- sidebar ----------") +
  "\n({md, mathify, inlineDollarOk});");

const check = (name, input, pred) => {
  const out = md(input);
  report(name, pred(out), "out: " + out.slice(0, 200));
};
const isMath = o => o.includes('class="katex');
const noMath = o => !o.includes("katex");

// renders
check("display $$", "$$\\hat\\beta = (X'X)^{-1}X'y$$", isMath);
check("display \\[ \\]", "\\[ \\sum_{i=1}^N w_i \\]", isMath);
check("inline \\( \\)", "where \\(\\alpha > 0\\) holds", isMath);
check("inline $ with a command", "assume $\\varepsilon_i \\sim N(0,\\sigma^2)$", isMath);
check("inline $ with a subscript", "the term $x_{it}$ enters", isMath);
check("multiline display", "$$\n\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}\n$$", isMath);
check("two display blocks", "$$a=1$$ and $$b=2$$",
      o => (o.match(/katex-display/g) || []).length === 2);
check("< inside math is not an entity", "$a < b$", isMath);

// must NOT render — transcripts are full of these
check("price range", "costs $5 to $10 per unit", noMath);
check("env vars", "export $PATH and $HOME are set", noMath);
check("var followed by a path", "check $HOME/src and $PATH now", noMath);
check("escaped dollar", "it costs \\$9.99 total", noMath);
check("bare identifier", "the $foo$ variable", noMath);
check("inside inline code", "run `echo $HOME$USER` now", noMath);
check("inside a fenced block", "```sh\nawk '{print $1}' f\necho $$x^2$$\n```", noMath);

// escaping and markdown must survive the math pass
check("no raw script tag", "$$\\text{a}$$ <script>alert(1)</script>",
      o => !o.includes("<script>"));
check("unparseable tex does not throw", "$$\\frobnicate{x}$$",
      o => o.length > 0 && !o.includes("<script>"));
check("code is still escaped", "`a < b`", o => o.includes("&lt;") && o.includes("<code>"));
check("bold/italic/code intact", "**b** and *i* and `c`",
      o => o.includes("<b>b</b>") && o.includes("<i>i</i>") && o.includes("<code>c</code>"));
check("bullets intact", "- one\n- two", o => o.includes("•"));
check("fences intact", "```py\nx=1\n```", o => o.includes("<pre><code>"));
check("links intact", "[x](https://a.b)", o => o.includes('href="https://a.b"'));

/* ---------------- pipe tables ---------------- */

const T3 =
  "| Reading | What moves | Source of the extra adaptation $ |\n" +
  "|---|---|\n" +
  "| Pure within-country composition (strict H1) | $O\\downarrow$, $S$ flat | $i$ converts its own other-aid |\n" +
  "| Reallocation into $i$ / scale (H2-flavored) | $S\\uparrow$, $O$ flat | money drawn from the global pool |\n";

check("table renders as a table", T3, o => o.includes("<table>") && o.includes("<tbody>"));
check("no raw pipes leak into the text", T3, o => !/\|/.test(o.replace(/<[^>]*>/g, "")));
check("short separator still yields 3 columns", T3,
      o => (o.match(/<th\b/g) || []).length === 3 &&
           (o.match(/<\/tr>/g) || []).length === 3);
check("math inside cells is typeset", T3, o => o.includes('class="katex'));
check("no leftover placeholders", T3, o => !o.includes("\x00"));
check("cell text survives", T3, o => o.includes("Pure within-country composition"));
check("separator row is not a body row", T3, o => !o.includes("<td>---"));

check("alignment from the separator",
      "| a | b | c |\n|:--|:-:|--:|\n| 1 | 2 | 3 |\n",
      o => o.includes('text-align:center') && o.includes('text-align:right'));
check("ragged rows are padded, not dropped",
      "| a | b | c |\n|---|---|---|\n| 1 |\n",
      o => (o.match(/<td\b/g) || []).length === 3);
check("escaped pipe stays a pipe",
      "| a | b |\n|---|---|\n| x \\| y | z |\n",
      o => o.includes("x | y"));
check("bold inside a cell", "| a |\n|---|\n| **hi** |\n", o => o.includes("<b>hi</b>"));
check("table inside a fence is left alone",
      "```\n| a |\n|---|\n| 1 |\n```", o => !o.includes("<table>"));
check("prose after a table resumes",
      "| a |\n|---|\n| 1 |\n\nAfter the table.",
      o => o.includes("<table>") && o.includes("After the table."));
check("a lone pipe line is not a table", "a | b\nnot a table", o => !o.includes("<table>"));
check("a horizontal rule is not a separator", "text\n---\nmore", o => !o.includes("<table>"));

/* ---------------- file explorer follows the active terminal ---------------- */

const TM = { terms: new Map(), active: null };
const app = (0, eval)("(TM)=>{" +
  "let fsPath = null; const loaded = [];" +
  "function loadFs(p){ loaded.push(p); fsPath = p; }" +
  "function renderTabs(){} function fitActive(){}" +
  "function $(s){ return {style:{display:'flex'}}; }" +   // Files tab visible
  slice("/* Switching terminal tabs moves the file explorer",
        "function fitActive(force)") +
  "\nreturn {activateTerm, loadFs, loaded, get fsPath(){return fsPath;}};}")(TM);

const mkTab = (id, cwd) => TM.terms.set(id,
  { id, cwd, fsPath: cwd, container: { style: {} }, term: { focus() {} } });

const eq = (name, got, want) =>
  report(name, got === want, `got ${got}, want ${want}`);

const A = "/Users/x/proj-a", B = "/Users/x/proj-b";
mkTab("a", A); mkTab("b", B);

app.activateTerm("a");
eq("opening a session points the explorer at its root", app.fsPath, A);
app.activateTerm("b");
eq("switching sessions jumps to the other root", app.fsPath, B);
app.activateTerm("a");
eq("switching back returns to the first root", app.fsPath, A);

app.loadFs(A + "/scripts/estimation");     // browse deeper, then leave and return
app.activateTerm("b");
eq("leaving goes to the other root, not the subdir", app.fsPath, B);
app.activateTerm("a");
eq("returning remembers where you had browsed", app.fsPath, A + "/scripts/estimation");

mkTab("c", null);
app.activateTerm("c");
eq("a tab with no cwd leaves the explorer alone", app.fsPath, A + "/scripts/estimation");

const before = app.loaded.length;
app.activateTerm("c");
eq("re-activating the same tab does not reload", app.loaded.length, before);

console.log(fails ? `\n${fails} failed` : `\nall passed`);
process.exit(fails ? 1 : 0);
