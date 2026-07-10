/* views/dsl.js — structural DSL formula tokenizer (CP9-2).
 * Pure string → token list → esc()-safe HTML spans. The operator catalog is
 * server-side only (/catalog is token-gated), so tokenization is structural:
 * an identifier directly before '(' renders as a function name. Every input
 * character is emitted exactly once (unknown chars fall through as plain
 * escaped text), so join(tokens.text) === input always holds. */

import { esc } from '../metric.js';

const TOKEN_PATTERNS = [
  ['ws',    /^\s+/],
  ['str',   /^"(?:[^"\\]|\\.)*"|^'(?:[^'\\]|\\.)*'/],
  ['num',   /^\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/],
  ['ident', /^[A-Za-z_][A-Za-z0-9_]*/],
  ['punct', /^[()\[\]{},]/],
  ['op',    /^(?:<=|>=|==|!=|&&|\|\||[+\-*/%<>=!&|^~?:.])/]
];

export function tokenizeFormula(text) {
  const tokens = [];
  let rest = String(text === undefined || text === null ? '' : text);
  while (rest) {
    let token = null;
    for (const [type, pattern] of TOKEN_PATTERNS) {
      const hit = pattern.exec(rest);
      if (hit) { token = { type, text: hit[0] }; break; }
    }
    if (!token) token = { type: 'plain', text: rest[0] };
    tokens.push(token);
    rest = rest.slice(token.text.length);
  }
  tokens.forEach((token, index) => {
    if (token.type !== 'ident') return;
    let next = index + 1;
    while (next < tokens.length && tokens[next].type === 'ws') next += 1;
    if (tokens[next] && tokens[next].text === '(') token.type = 'fn';
  });
  return tokens;
}

const TOKEN_CLASSES = { fn: 'dsl-fn', ident: 'dsl-id', num: 'dsl-num', str: 'dsl-str', op: 'dsl-op', punct: 'dsl-punct' };

export function formulaHtml(text) {
  return tokenizeFormula(text).map(token => {
    const cls = TOKEN_CLASSES[token.type];
    return cls ? `<span class="${cls}">${esc(token.text)}</span>` : esc(token.text);
  }).join('');
}
