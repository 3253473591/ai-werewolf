/** 后端接口 + 轻提示。所有请求都走这里，方便单测时整体替换。 */

let toastTimer = null;

export function toast(msg, kind) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.className = 'show ' + (kind || '');
  el.textContent = msg;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ''; }, 2600);
}

export async function api(path, body) {
  const opt = body === undefined ? {} :
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  try {
    const r = await fetch(path, opt);
    return { ok: r.ok, data: await r.json().catch(() => ({})) };
  } catch (e) {
    return { ok: false, data: { error: String(e) } };
  }
}
