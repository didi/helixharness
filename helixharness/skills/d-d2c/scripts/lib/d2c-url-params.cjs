'use strict';

/**
 * Shared URL-params helper for d-d2c capture/geometry/vertical/render scripts.
 *
 * Some target projects refuse to render without an auth token. The loop (or a
 * caller) supplies extra query params — typically a login token — via the
 * `--url-params` CLI arg or the `D2C_URL_PARAMS` env var (form: `token=xxx&a=b`,
 * values already URL-encoded). We append them to the page URL right before
 * `page.goto`, so the screenshot/self-check actually lands on the target page
 * instead of a login wall.
 */

/** Read extra query params from --url-params arg or D2C_URL_PARAMS env. */
function readExtraParams(args) {
  const fromArg = args && args['url-params'] && args['url-params'] !== true ? args['url-params'] : null;
  const raw = fromArg || process.env.D2C_URL_PARAMS || '';
  return String(raw).replace(/^[?&]+/, '').trim();
}

/** Append `extra` (form `k=v&k2=v2`) to url, picking ? or & correctly. */
function appendUrlParams(url, args) {
  const extra = readExtraParams(args);
  if (!url || !extra) return url;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}${extra}`;
}

/** Keys whose query values are sensitive and must be masked in logs. */
const SENSITIVE_KEY = /^(token|auth|authorization|access[_-]?token|ticket|sid|session|secret|password|pwd)$/i;

/** Mask sensitive query values in a URL for safe logging. */
function maskUrl(url) {
  if (!url || typeof url !== 'string') return url;
  const qi = url.indexOf('?');
  if (qi < 0) return url;
  const base = url.slice(0, qi);
  const query = url.slice(qi + 1);
  const masked = query.split('&').map((pair) => {
    const eq = pair.indexOf('=');
    if (eq < 0) return pair;
    const key = pair.slice(0, eq);
    return SENSITIVE_KEY.test(key) ? `${key}=***` : pair;
  }).join('&');
  return `${base}?${masked}`;
}

/**
 * Session-state injection for flow-gated SPAs (generic, page-agnostic).
 *
 * Some SPAs guard routes behind an auth *session* held in cookie / localStorage
 * and do NOT accept a login token passed as a URL query param — their router
 * guard / flow chain redirects the target route to home/login (URL token is not
 * recognised as a valid session). For these, the caller supplies a real session
 * snapshot and we put it into the browser *before* navigation, so the guard runs
 * normally, verifies a valid login state, and admits the page — equivalent to a
 * real user who already logged in. This is NOT bypassing the guard.
 *
 * It stays generic: the skill only injects whatever key/values the caller passes
 * in; it NEVER hardcodes cookie names, domains, or page-specific logic. What to
 * inject is external input, supplied via `--session-state` CLI or the
 * `D2C_SESSION_STATE` env var — the same pattern as `D2C_URL_PARAMS`.
 *
 * Structure (JSON; values may be pre-encoded strings). Empty/absent → skipped:
 *   { "cookies": [{ "name", "value", "domain?", "path?" }],
 *     "localStorage": { "k": "v" },
 *     "origin?": "http://127.0.0.1:PORT" }
 */
function readSessionState(args) {
  const fromArg =
    args && args['session-state'] && args['session-state'] !== true ? args['session-state'] : null;
  const raw = fromArg || process.env.D2C_SESSION_STATE || '';
  const s = String(raw).trim();
  if (!s) return null;
  let parsed;
  try {
    parsed = JSON.parse(s);
  } catch (e) {
    // 解析失败不阻断截图，仅跳过会话注入并告警
    console.error(
      `⚠️ --session-state / D2C_SESSION_STATE 不是合法 JSON，已跳过会话注入：${e && e.message ? e.message : e}`
    );
    return null;
  }
  if (!parsed || typeof parsed !== 'object') return null;
  const cookies = Array.isArray(parsed.cookies)
    ? parsed.cookies.filter((c) => c && c.name)
    : [];
  const localStorage =
    parsed.localStorage && typeof parsed.localStorage === 'object' ? parsed.localStorage : {};
  if (!cookies.length && !Object.keys(localStorage).length) return null;
  return { cookies, localStorage, origin: parsed.origin || null };
}

/**
 * Put the caller-provided session into `page` BEFORE `page.goto`.
 * - cookies: `page.setCookie`；缺 domain/path 时按 targetUrl 的 origin 推断。
 * - localStorage: 绑定 origin，goto 前直接 setItem 会失败 → 用 `evaluateOnNewDocument`
 *   在文档创建时（早于 app JS 读取）注入。
 * 任一步失败都只告警、不阻断截图。返回是否注入的摘要。
 */
async function applySessionState(page, targetUrl, args) {
  const state = readSessionState(args);
  if (!state) return { applied: false };

  let origin = state.origin;
  try {
    if (!origin && targetUrl) origin = new URL(targetUrl).origin;
  } catch (_) {
    /* origin 解析失败：带 domain 的 cookie 仍可注入 */
  }

  const cookies = state.cookies.map((c) => {
    const cookie = { name: String(c.name), value: String(c.value == null ? '' : c.value) };
    if (c.domain) {
      cookie.domain = c.domain;
      cookie.path = c.path || '/';
    } else if (origin) {
      cookie.url = origin;
      if (c.path) cookie.path = c.path;
    }
    return cookie;
  });
  if (cookies.length) {
    try {
      await page.setCookie(...cookies);
    } catch (e) {
      console.error(`⚠️ 注入 cookie 失败（已跳过）：${e && e.message ? e.message : e}`);
    }
  }

  const lsKeys = Object.keys(state.localStorage);
  if (lsKeys.length) {
    const entries = lsKeys.map((k) => [
      String(k),
      String(state.localStorage[k] == null ? '' : state.localStorage[k]),
    ]);
    try {
      await page.evaluateOnNewDocument((pairs) => {
        try {
          for (let i = 0; i < pairs.length; i += 1) {
            window.localStorage.setItem(pairs[i][0], pairs[i][1]);
          }
        } catch (_) {
          /* about:blank 等无 storage 的文档忽略 */
        }
      }, entries);
    } catch (e) {
      console.error(`⚠️ 注入 localStorage 失败（已跳过）：${e && e.message ? e.message : e}`);
    }
  }

  console.log(`会话注入：${maskSessionState(state)}`);
  return { applied: true, cookieCount: cookies.length, localStorageKeys: lsKeys.length };
}

/** Mask a session snapshot for safe logging — never prints any value. */
function maskSessionState(state) {
  if (!state) return '(none)';
  const mark = (k) => (SENSITIVE_KEY.test(k) ? `${k}(masked)` : k);
  const cookieNames = (state.cookies || []).map((c) => mark(c.name));
  const lsKeys = Object.keys(state.localStorage || {}).map(mark);
  return `cookies=[${cookieNames.join(', ')}] localStorage=[${lsKeys.join(', ')}] (values not logged)`;
}

module.exports = {
  appendUrlParams,
  maskUrl,
  readExtraParams,
  readSessionState,
  applySessionState,
  maskSessionState,
};
