// Botkeep 免费机器抢购脚本 (Node.js 20+)
// 流程：登录 -> 启动TG通知 -> 轮询容量 -> 有容量立即创建 Node.js 满配服务器 -> 成功TG通知
const BASE = 'https://api.botkeep.cloud';
const SITE = 'https://botkeep.cloud';
const env = process.env;
const EMAIL = env.BOTKEEP_EMAIL;
const PASSWORD = env.BOTKEEP_PASSWORD;
const TG_TOKEN = env.TG_BOT_TOKEN;
const TG_CHAT = env.TG_CHAT_ID;
const NAME = env.SERVER_NAME || 'my-project';
const RUNTIME = env.RUNTIME || 'nodejs';
const RES = {
  memoryLimitMb: Number(env.RAM_MB || 512),
  cpuLimitPercent: Number(env.CPU_PERCENT || 50),
  storageLimitMb: Number(env.STORAGE_MB || 1024),
};
const EXCLUDE = (env.EXCLUDE_NODES || 'node2-it').split(',').map((x) => x.trim().toLowerCase()).filter(Boolean);
const MAX_REJECTS = Number(env.MAX_REJECTS || 3); // 单次运行最多删几次落到黑名单节点的机器
const RUN_SECONDS = Number(env.RUN_SECONDS || 240); // 单次运行轮询总时长
const INTERVAL_SECONDS = Number(env.INTERVAL_SECONDS || 20); // 轮询间隔，别太小以免触发风控

const jar = new Map();
const sleep = (s) => new Promise((r) => setTimeout(r, s * 1000));
const log = (...a) => console.log(new Date().toISOString(), ...a);

function saveCookies(res) {
  const list = res.headers.getSetCookie ? res.headers.getSetCookie() : [];
  for (const c of list) {
    const [pair] = c.split(';');
    const i = pair.indexOf('=');
    if (i > 0) jar.set(pair.slice(0, i).trim(), pair.slice(i + 1).trim());
  }
}
const cookieHeader = () => [...jar].map(([k, v]) => `${k}=${v}`).join('; ');

let csrf = null;
async function api(path, { method = 'GET', body, headers = {}, anonymous = false } = {}) {
  const h = {
    Accept: 'application/json',
    Origin: SITE,
    Referer: `${SITE}/app/create`,
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/129 Safari/537.36',
    ...headers,
  };
  if (jar.size) h.Cookie = cookieHeader();
  if (body !== undefined) h['Content-Type'] = 'application/json';
  if (method !== 'GET' && !anonymous) {
    if (!csrf) csrf = (await api('/api/v1/auth/session')).csrf_token;
    h['X-CSRF-Token'] = csrf;
  }
  const res = await fetch(BASE + path, { method, headers: h, body: body === undefined ? undefined : JSON.stringify(body) });
  saveCookies(res);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const msg = (data && (data.detail || data.message || data.error)) || text || res.statusText;
    const err = new Error(`${method} ${path} -> ${res.status}: ${typeof msg === 'string' ? msg : JSON.stringify(msg)}`);
    err.status = res.status;
    throw err;
  }
  return data && data.data !== undefined && Object.keys(data).length <= 2 ? data.data : data;
}

async function tg(text) {
  if (!TG_TOKEN || !TG_CHAT) return log('TG 未配置，跳过通知:', text);
  try {
    await fetch(`https://api.telegram.org/bot${TG_TOKEN}/sendMessage`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: TG_CHAT, text, disable_web_page_preview: true }),
    });
  } catch (e) { log('TG 发送失败', e.message); }
}

// 取服务器所在节点/地址，返回小写字符串（等待地址分配，最多 waitSec 秒）
async function botLocation(id, waitSec = 60) {
  const end = Date.now() + waitSec * 1000;
  let info = '';
  while (true) {
    try {
      const bot = await api(`/api/v1/panel/bots/${encodeURIComponent(id)}`);
      const net = await api(`/api/v1/panel/bots/${encodeURIComponent(id)}/network`).catch(() => null);
      const hosts = (net?.allocations || []).map((a) => a.host).filter(Boolean);
      info = (JSON.stringify(bot) + ' ' + hosts.join(' ')).toLowerCase();
      if (hosts.length || /node\d*-[a-z]+/.test(info)) return { info, hosts };
    } catch (e) { log('读取节点失败', e.message); }
    if (Date.now() > end) return { info, hosts: [] };
    await sleep(5);
  }
}
const badNode = (info) => EXCLUDE.find((n) => info.includes(n));
const nodeLabel = (loc) => loc.hosts[0] || (loc.info.match(/node\d*-[a-z]+[a-z0-9.-]*/) || ['未知节点'])[0];
// 删除服务器；创建/部署中会返回 409，等它完成再删（最多等 DELETE_WAIT 秒）
async function deleteBot(id) {
  const end = Date.now() + Number(env.DELETE_WAIT || 180) * 1000;
  while (true) {
    try {
      await api(`/api/v1/panel/bots/${encodeURIComponent(id)}/actions`, { method: 'POST', body: { action: 'delete' } });
      log('已提交删除', id);
      return true;
    } catch (e) {
      if (e.status === 404) return true;
      if (e.status !== 409 || Date.now() > end) throw e;
      log('有操作进行中，5 秒后重试删除');
      await sleep(5);
    }
  }
}

// 检查账号已有服务器：黑名单节点的删掉；有合格的返回 true（直接结束）
async function handleExisting(notify = true) {
  const dash = await api('/api/v1/panel/dashboard');
  const bots = Array.isArray(dash?.bots) ? dash.bots : [];
  const kept = [];
  for (const b of bots) {
    const loc = await botLocation(b.id, 5);
    const bad = badNode(loc.info);
    if (bad) {
      log(`已有服务器 ${b.name || b.id} 在黑名单节点 ${bad}，删除`);
      await deleteBot(b.id);
      await tg(`🗑 Botkeep：${b.name || b.id} 在 ${nodeLabel(loc)}（黑名单 ${bad}），已删除，继续抢。`);
    } else kept.push(`${b.name || b.id} @ ${nodeLabel(loc)}`);
  }
  if (kept.length) {
    log('账号已有合格服务器，退出');
    if (notify) await tg(`✅ Botkeep：账号里已有服务器（${kept.join(', ')}），无需再抢。`);
    return true;
  }
  return false;
}

async function main() {
  if (!EMAIL || !PASSWORD) throw new Error('缺少 BOTKEEP_EMAIL / BOTKEEP_PASSWORD');

  await api('/api/v1/auth/login', { method: 'POST', anonymous: true, body: { email: EMAIL, password: PASSWORD } });
  csrf = (await api('/api/v1/auth/session')).csrf_token;
  log('登录成功');

  // 已有服务器：黑名单节点删掉，合格的就结束
  if (await handleExisting()) return;

  // 取 Node.js 版本和默认启动命令
  const caps = await api('/api/v1/panel/capabilities');
  const versions = caps?.runtimes?.[RUNTIME] || [];
  const runtimeVersion = env.RUNTIME_VERSION || versions[0] || '';
  const startCommand = caps?.runtimeOptions?.[RUNTIME]?.[runtimeVersion]?.command || 'npm start';
  log('运行时', RUNTIME, runtimeVersion || '(未开放)', '启动命令', startCommand);

  await tg(`🚀 Botkeep 抢机器服务已开启\n配置：${RUNTIME} ${runtimeVersion} / ${RES.memoryLimitMb}MB / ${RES.cpuLimitPercent}% CPU / ${RES.storageLimitMb}MB\n本次轮询 ${RUN_SECONDS}s，每 ${INTERVAL_SECONDS}s 一次`);

  const qs = new URLSearchParams({
    runtime: RUNTIME,
    runtimeVersion,
    memoryLimitMb: String(RES.memoryLimitMb),
    cpuLimitPercent: String(RES.cpuLimitPercent),
    storageLimitMb: String(RES.storageLimitMb),
  }).toString();
  let idemKey = crypto.randomUUID();
  let rejects = 0;
  const deadline = Date.now() + RUN_SECONDS * 1000;
  let n = 0;

  while (Date.now() < deadline) {
    n++;
    try {
      const avail = await api('/api/v1/panel/creation-availability?' + qs);
      if (avail?.allowed) {
        log(`第${n}次：有容量，立即创建`);
        const bot = await api('/api/v1/panel/bots', {
          method: 'POST',
          headers: { 'Idempotency-Key': idemKey },
          body: {
            name: NAME,
            platform: 'general',
            runtime: RUNTIME,
            runtimeVersion,
            sourceType: 'blank',
            repository: '',
            archiveName: '',
            branch: 'main',
            startCommand,
            tokenProvided: false,
            resources: RES,
            seedFiles: [],
          },
        });
        const id = bot?.id || bot?.bot?.id || '';
        log('创建成功', JSON.stringify(bot).slice(0, 300));
        const loc = id ? await botLocation(id, 60) : { info: '', hosts: [] };
        const bad = badNode(loc.info + ' ' + JSON.stringify(bot).toLowerCase());
        if (bad) {
          rejects++;
          log(`落在黑名单节点 ${bad}，删除重抢 (${rejects}/${MAX_REJECTS})`);
          await deleteBot(id);
          await tg(`🗑 Botkeep：抢到的机器在 ${nodeLabel(loc)}（黑名单 ${bad}），已删除，继续抢。`);
          idemKey = crypto.randomUUID();
          if (rejects >= MAX_REJECTS) { log('本次删除次数已达上限，等下次触发'); return; }
          await sleep(15);
          continue;
        }
        await tg(`🎉 Botkeep 抢到了！\n服务器：${NAME} ${id}\n节点：${nodeLabel(loc)}\n${RUNTIME} ${runtimeVersion} / ${RES.memoryLimitMb}MB / ${RES.cpuLimitPercent}% / ${RES.storageLimitMb}MB\n${SITE}/app`);
        return;
      }
      log(`第${n}次：无容量 - ${avail?.message || ''}`);
      if (/slot/i.test(avail?.message || '')) {
        // 名额被占：可能是上次没删成的黑名单机器
        if (await handleExisting(false)) { await tg('✅ Botkeep：账号已有合格服务器，停止抢。'); return; }
      }
    } catch (e) {
      log(`第${n}次出错：${e.message}`);
      if (e.status === 401 || e.status === 403) { csrf = null; }
      if (e.status === 429) await sleep(INTERVAL_SECONDS * 2);
    }
    await sleep(INTERVAL_SECONDS);
  }
  log('本次未抢到，等下次触发');
}

main().catch(async (e) => {
  log('致命错误', e.message);
  await tg(`⚠️ Botkeep 抢机器脚本出错：${e.message}`);
  process.exit(1);
});
