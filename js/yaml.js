// Tiny YAML subset for ForgePanel config export/import (no dependencies).
// Emits block maps/lists with JSON-quoted strings; parses that output plus the
// common hand-edited forms (plain scalars, # comments, "- key: value" list items).
(function (root) {
  const needsQuote = s => s === "" || /^[\s\-?:,\[\]{}#&*!|>'"%@`]|[:#]\s|\s$|^(true|false|null|yes|no|on|off|~)$/i.test(s) || /^[-+]?(\d|\.\d)/.test(s) || /[\n\t"\\]/.test(s);
  function scalar(v) {
    if (v === null || v === undefined) return "null";
    if (typeof v === "boolean" || typeof v === "number") return String(v);
    const s = String(v);
    return needsQuote(s) ? JSON.stringify(s) : s;
  }
  const keyOut = k => /^[A-Za-z_][A-Za-z0-9_.\-]*$/.test(k) ? k : JSON.stringify(k);
  const isObj = v => v && typeof v === "object" && !Array.isArray(v);
  function dump(v, ind) {
    ind = ind || "";
    if (Array.isArray(v)) {
      if (!v.length) return " []";
      return v.map(x => {
        if (isObj(x) && Object.keys(x).length) {
          const body = dump(x, ind + "  ").replace(/^\n/, "");
          return "\n" + ind + "- " + body.slice(ind.length + 2);
        }
        if (Array.isArray(x) && x.length) return "\n" + ind + "-" + dump(x, ind + "  ");
        return "\n" + ind + "- " + (isObj(x) ? "{}" : Array.isArray(x) ? "[]" : scalar(x));
      }).join("");
    }
    if (isObj(v)) {
      const ks = Object.keys(v);
      if (!ks.length) return " {}";
      return ks.map(k => {
        const x = v[k];
        if ((isObj(x) && Object.keys(x).length) || (Array.isArray(x) && x.length)) return "\n" + ind + keyOut(k) + ":" + dump(x, ind + "  ");
        return "\n" + ind + keyOut(k) + ": " + (isObj(x) ? "{}" : Array.isArray(x) ? "[]" : scalar(x));
      }).join("");
    }
    return " " + scalar(v);
  }
  function stringify(v, header) { return (header ? header.split("\n").map(l => "# " + l).join("\n") + "\n" : "") + dump(v, "").replace(/^\n/, "") + "\n"; }

  function parseScalar(t, ln) {
    t = t.trim();
    if (t === "" || t === "~" || t === "null") return null;
    if (t === "{}") return {};
    if (t === "[]") return [];
    if (t[0] === '"') { try { return JSON.parse(t); } catch (e) { throw new Error("line " + ln + ": bad quoted string"); } }
    if (t[0] === "'") { if (t.slice(-1) !== "'") throw new Error("line " + ln + ": bad quoted string"); return t.slice(1, -1).replace(/''/g, "'"); }
    if (/^(true|yes|on)$/i.test(t)) return true;
    if (/^(false|no|off)$/i.test(t)) return false;
    if (/^[-+]?\d+(\.\d+)?$/.test(t)) return Number(t);
    if (t[0] === "[" || t[0] === "{") { try { return JSON.parse(t); } catch (e) { throw new Error("line " + ln + ": flow collections must be JSON"); } }
    return t;
  }
  function stripComment(s) {
    let q = null;
    for (let i = 0; i < s.length; i++) {
      const c = s[i];
      if (q) { if (c === "\\" && q === '"') i++; else if (c === q) q = null; }
      else if (c === '"' || c === "'") q = c;
      else if (c === "#" && (i === 0 || /\s/.test(s[i - 1]))) return s.slice(0, i);
    }
    return s;
  }
  function splitKey(t, ln) {   // "key: value" -> [key, value|undefined]
    let k, rest;
    if (t[0] === '"') { const m = t.match(/^("(?:[^"\\]|\\.)*")\s*:(\s|$)/); if (!m) return null; k = JSON.parse(m[1]); rest = t.slice(m[0].length); }
    else { const m = t.match(/^([^:\s][^:]*?)\s*:(\s|$)/); if (!m) return null; k = m[1]; rest = t.slice(m[0].length); }
    return [k, rest.trim()];
  }
  function parse(text) {
    const lines = [];
    String(text).replace(/\r/g, "").split("\n").forEach((raw, n) => {
      if (/\t/.test(raw.match(/^\s*/)[0])) throw new Error("line " + (n + 1) + ": tabs are not allowed for indentation");
      const s = stripComment(raw).replace(/\s+$/, "");
      if (!s.trim() || s.trim() === "---") return;
      lines.push({ ind: s.match(/^ */)[0].length, t: s.trim(), ln: n + 1 });
    });
    let p = 0;
    function block(ind) {
      const L = lines[p];
      if (!L || L.ind < ind) return null;
      return L.t.startsWith("- ") || L.t === "-" ? list(L.ind) : map(L.ind);
    }
    function value(rest, ind, ln) {
      if (rest === "" ) { const L = lines[p]; return L && (L.ind > ind || (L.ind === ind && (L.t.startsWith("- ") || L.t === "-"))) ? block(L.ind) : null; }
      if (rest === "|" || rest === ">") throw new Error("line " + ln + ": block scalars are not supported");
      return parseScalar(rest, ln);
    }
    function map(ind) {
      const o = {};
      while (p < lines.length && lines[p].ind === ind && !lines[p].t.startsWith("- ")) {
        const L = lines[p++], kv = splitKey(L.t, L.ln);
        if (!kv) throw new Error("line " + L.ln + ": expected key: value");
        o[kv[0]] = value(kv[1], ind, L.ln);
      }
      if (p < lines.length && lines[p].ind > ind) throw new Error("line " + lines[p].ln + ": unexpected indentation");
      return o;
    }
    function list(ind) {
      const a = [];
      while (p < lines.length && lines[p].ind === ind && (lines[p].t.startsWith("- ") || lines[p].t === "-")) {
        const L = lines[p], rest = L.t === "-" ? "" : L.t.slice(2).trim();
        if (rest === "") { p++; a.push(value("", ind, L.ln)); continue; }
        const kv = !/^["'\[{]/.test(rest) || rest[0] === '"' ? splitKey(rest, L.ln) : null;
        if (kv) {   // "- key: v" starts a map whose keys are indented to the dash content
          const inner = ind + 2 + (L.t.slice(1).match(/^ */)[0].length - 1);
          lines[p] = { ind: inner, t: rest, ln: L.ln };
          a.push(map(inner));
        } else { p++; a.push(parseScalar(rest, L.ln)); }
      }
      return a;
    }
    const out = block(lines.length ? lines[0].ind : 0);
    if (p < lines.length) throw new Error("line " + lines[p].ln + ": could not parse");
    return out === null ? {} : out;
  }
  root.FPYaml = { stringify, parse };
  if (typeof module !== "undefined") module.exports = root.FPYaml;
})(typeof window !== "undefined" ? window : globalThis);
