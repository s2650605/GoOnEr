/**
 * 会话续跑 —— 外部指令执行器（PI-Desktop 插件）
 *
 * 为什么需要这个插件：
 *   PI-Desktop 没有命令行入口，host-core 只接受 Electron 主进程的 stdio 握手，
 *   外部进程无法直接给某个会话投递消息。插件是官方允许的、能调用
 *   `agent/prompt`（"Send a prompt to a session's Agent"）的通道。
 *
 * 分工（避免同一个会话被重复发送）：
 *   判定与节奏全部由外部 Python 监控脚本负责；本插件只做「执行器」，
 *   收到指令就调用官方 host 操作，并把结果写回。插件自己不轮询、不判定、
 *   不决定节奏，因此不会和外部脚本抢着发送。
 *
 * 通信方式（纯文件，无需端口/令牌）：
 *   指令目录  <数据目录>/session-keeper/commands/<id>.json   外部脚本写入
 *   结果目录  <数据目录>/session-keeper/results/<id>.json    插件执行后写入
 *   心跳文件  <数据目录>/session-keeper/plugin.json          插件持续更新
 *
 * 指令格式：
 *   {"id":"...","action":"prompt","sessionId":"<uuid>","content":"继续"}
 *   {"id":"...","action":"abort","sessionId":"<uuid>"}
 *   {"id":"...","action":"compact","sessionId":"<uuid>"}
 *   {"id":"...","action":"create","projectPath":"<dir>","title":"...",
 *    "providerId":"...","modelId":"...","mode":"agent",
 *    "content":"可选：创建后立即投递的首条消息"}
 *   {"id":"...","action":"list"}
 *
 * compact / create 是给「上下文超限自动续接」用的：
 *   compact  压缩某会话的上下文（agent/compact），让爆掉的会话能继续；
 *   create   在指定项目下新建会话（session/create），并可顺带投递首条消息，
 *            用于把爆掉的原会话交接给一个干净的新会话。
 * 两者都由外部监控脚本决定何时调用，插件自己不判定、不轮询。 */

const path = require("node:path");
const fs = require("node:fs/promises");

/** 指令轮询间隔：外部脚本写入后最迟这么久被执行。 */
const POLL_MS = 1000;
/** 心跳写入间隔。 */
const HEARTBEAT_MS = 3000;
/** 结果文件保留上限，避免目录无限增长。 */
const MAX_RESULTS = 200;
/** 最近执行记录在面板里保留的条数。 */
const MAX_EVENTS = 40;
/** 单条指令最长处理时间，超时按失败写回，避免卡住轮询。 */
const ACTION_TIMEOUT_MS = 60000;
/** 压缩上下文可能要等模型跑一轮，给更长的超时。 */
const COMPACT_TIMEOUT_MS = 180000;
/** 新建会话（含首条消息投递）的超时。 */
const CREATE_TIMEOUT_MS = 120000;

/**
 * 监控脚本心跳的「最短过期时间」。
 * 实际阈值取 max(它自己的轮询间隔 × 3, 这个值)，避免用户把轮询调长后误判为未运行。
 */
const MONITOR_MIN_STALE_MS = 30000;

/** 插件版本号，写进心跳与面板，便于外部脚本核对。 */
const PLUGIN_VERSION = "1.9.2";

let hostRoot = null;
let dirs = null;
let timer = null;
let heartbeatTimer = null;
let running = false;
let draining = false;
const events = [];
let lastError = "";

/** 面板/日志共用的一条记录。 */
function record(action, sessionId, ok, detail) {
  const entry = {
    at: Date.now(),
    action,
    sessionId: sessionId ? String(sessionId).slice(0, 8) : "",
    ok: Boolean(ok),
    detail: detail ? String(detail).slice(0, 300) : "",
  };
  events.unshift(entry);
  if (events.length > MAX_EVENTS) events.length = MAX_EVENTS;
  return entry;
}

async function ensureDirs() {
  if (dirs) return dirs;
  const dataPath = await pi.plugin.getDataPath();
  // <宿主数据目录>/plugins/data/<pluginId> → <宿主数据目录>
  hostRoot = path.resolve(String(dataPath || ""), "..", "..", "..");
  const base = path.join(hostRoot, "session-keeper");
  dirs = {
    base,
    commands: path.join(base, "commands"),
    results: path.join(base, "results"),
    heartbeat: path.join(base, "plugin.json"),
  };
  await fs.mkdir(dirs.commands, { recursive: true });
  await fs.mkdir(dirs.results, { recursive: true });
  return dirs;
}

async function readJson(file) {
  try {
    return JSON.parse(await fs.readFile(file, "utf8"));
  } catch {
    return null;
  }
}

async function writeJson(file, value) {
  const tmp = `${file}.tmp`;
  await fs.writeFile(tmp, `${JSON.stringify(value, null, 2)}\n`, "utf8");
  await fs.rename(tmp, file);
}

/** 读取插件自身设置，失败时退回安全默认值。 */
async function readSettings() {
  let raw = {};
  try {
    raw = (await pi.plugin.getSettings()) ?? {};
  } catch {
    raw = {};
  }
  return {
    enabled: raw.enabled !== false,
    defaultMessage:
      typeof raw.defaultMessage === "string" && raw.defaultMessage.trim()
        ? raw.defaultMessage.trim()
        : "继续",
    // 关机策略：外部监控脚本读这三项决定何时关机。
    // 放在插件里配置的好处是「睡前在这里勾一次」，不用再去改监控脚本的命令行。
    shutdownWhenAllDone: raw.shutdownWhenAllDone === true,
    allDoneConfirmSeconds:
      Number.isFinite(Number(raw.allDoneConfirmSeconds)) &&
      Number(raw.allDoneConfirmSeconds) >= 0
        ? Number(raw.allDoneConfirmSeconds)
        : 120,
    shutdownRequiresAllSources: raw.shutdownRequiresAllSources !== false,
    // 「近期活跃会话全部报 503 → 关机」：同样由外部监控脚本执行，
    // 这里只保存设置并写进心跳。
    shutdownWhenAll503: raw.shutdownWhenAll503 === true,
    all503WindowMinutes:
      Number.isFinite(Number(raw.all503WindowMinutes)) &&
      Number(raw.all503WindowMinutes) > 0
        ? Number(raw.all503WindowMinutes)
        : 60,
    // 报错/上下文超限续接（Pi 的「400」）：同样由外部监控脚本执行，
    // 这里只负责保存设置并写进心跳。默认开、连续 3 次、先压缩。
    contextContinue: raw.contextContinue !== false,
    contextContinueThreshold:
      Number.isFinite(Number(raw.contextContinueThreshold)) &&
      Number(raw.contextContinueThreshold) > 0
        ? Math.floor(Number(raw.contextContinueThreshold))
        : 3,
    contextContinueMode: ["compact", "new"].includes(
      String(raw.contextContinueMode ?? "").trim()
    )
      ? String(raw.contextContinueMode).trim()
      : "compact",
    // 真 400 → 直接新建会话：同样只保存设置 + 写心跳，判定在执行器（监控脚本）里。
    // 默认开；关掉后 400 仍按上面的连续次数规则处理。
    openNewOn400: raw.openNewOn400 !== false,
  };
}

/**
 * 读监控脚本的心跳，判断「现在有没有人在监控」。
 *
 * 为什么需要：插件只是执行器，自己看不出有没有监控脚本在跑。
 * 没有这个提示时，用户会以为「插件装了就等于生效了」，
 * 结果会话报错后没人处理，事后还查不出原因。
 */
async function readMonitorStatus(source) {
  if (!dirs) return { running: false, source, reason: "插件服务未就绪" };
  const data = await readJson(path.join(dirs.base, `monitor-${source}.json`));
  if (!data || !data.at) {
    return { running: false, source, reason: "没有心跳（监控脚本没在跑）" };
  }
  const poll = Number(data.pollSeconds) > 0 ? Number(data.pollSeconds) : 10;
  const staleMs = Math.max(MONITOR_MIN_STALE_MS, poll * 3000);
  const ageMs = Date.now() - Number(data.at);
  return {
    running: ageMs <= staleMs,
    source,
    ageSeconds: Math.max(0, Math.round(ageMs / 1000)),
    pid: data.pid ?? null,
    dryRun: data.dryRun === true,
  };
}

function withTimeout(promise, ms, label) {
  let timerId;
  const timeout = new Promise((_, reject) => {
    timerId = setTimeout(() => reject(new Error(`${label} 超时(${ms}ms)`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timerId));
}

/**
 * 调用官方 host 操作。`desktop.control` 权限已在 manifest 声明，
 * 这里只做最小封装，不做任何隐式重试（节奏由外部脚本控制）。
 */
async function invokeOperation(operation, args) {
  const result = await pi.desktop.invoke({ operation, args });
  return result ?? null;
}

/** 执行一条指令，返回写回的结果对象。 */
async function runAction(cmd, settings) {
  const action = String(cmd?.action ?? "").trim();
  const sessionId = String(cmd?.sessionId ?? "").trim();

  if (action === "prompt") {
    if (!sessionId) throw new Error("sessionId 不能为空");
    const content =
      typeof cmd?.content === "string" && cmd.content.length
        ? cmd.content
        : settings.defaultMessage;
    const result = await withTimeout(
      invokeOperation("agent/prompt", [{ sessionId, content }]),
      ACTION_TIMEOUT_MS,
      "agent/prompt"
    );
    return { ok: true, action, sessionId, content, result };
  }

  if (action === "abort") {
    if (!sessionId) throw new Error("sessionId 不能为空");
    const result = await withTimeout(
      invokeOperation("agent/abort", [{ sessionId }]),
      ACTION_TIMEOUT_MS,
      "agent/abort"
    );
    return { ok: true, action, sessionId, result };
  }

  if (action === "compact") {
    // 压缩会话上下文：让「上下文超限」的会话重新变得可继续。
    // 官方操作 agent/compact（会话有正在跑的 turn 时宿主会返回 AGENT_BUSY）。
    if (!sessionId) throw new Error("sessionId 不能为空");
    const result = await withTimeout(
      invokeOperation("agent/compact", [{ sessionId }]),
      COMPACT_TIMEOUT_MS,
      "agent/compact"
    );
    return { ok: true, action, sessionId, result };
  }

  if (action === "create") {
    // 新建会话（可选顺带投递首条消息），用于把爆掉的原会话交接给干净的新会话。
    //
    // 为什么走 session/create 而不是 session/fork：fork 会连历史一起复制，
    // 原会话本身就是「上下文过大」才出问题的，复制过去只会立刻再爆一次。
    // 交接靠首条消息里的 pi-desktop://session/<原id> 深链完成 —— Pi 在发送
    // prompt 时会自动把该链接解析成原会话最近若干条消息的摘录附进去。
    const input = {};
    const projectPath = String(cmd?.projectPath ?? "").trim();
    if (projectPath) input.projectPath = projectPath;
    const title = String(cmd?.title ?? "").trim();
    if (title) input.title = title;
    for (const key of ["providerId", "modelId", "thinkingLevel"]) {
      const value = String(cmd?.[key] ?? "").trim();
      if (value) input[key] = value;
    }
    const mode = String(cmd?.mode ?? "").trim();
    if (["agent", "plan", "goal"].includes(mode)) input.mode = mode;

    const created = await withTimeout(
      invokeOperation("session/create", [input]),
      CREATE_TIMEOUT_MS,
      "session/create"
    );
    const newSessionId = String(
      created?.session?.id ?? created?.id ?? ""
    ).trim();
    if (!newSessionId) {
      throw new Error(`session/create 未返回会话 id: ${JSON.stringify(created).slice(0, 200)}`);
    }

    // 可选：把首条消息（含深链的交接说明）投递给新会话。
    // 投递失败不回滚会话 —— 会话已经建好，由外部脚本决定后续怎么处理。
    let promptResult = null;
    let promptError = "";
    const content = typeof cmd?.content === "string" ? cmd.content.trim() : "";
    if (content) {
      try {
        promptResult = await withTimeout(
          invokeOperation("agent/prompt", [{ sessionId: newSessionId, content }]),
          ACTION_TIMEOUT_MS,
          "agent/prompt"
        );
      } catch (error) {
        promptError = String(error?.message ?? error);
      }
    }
    return {
      ok: true,
      action,
      sessionId: newSessionId,
      projectPath: input.projectPath ?? "",
      title: input.title ?? "",
      prompted: Boolean(content) && !promptError,
      promptError: promptError || undefined,
      result: created,
      promptResult,
    };
  }

  if (action === "list") {
    const result = await pi.session.list({});
    return { ok: true, action, result };
  }

  if (action === "ping") {
    return { ok: true, action, version: PLUGIN_VERSION, pid: process.pid };
  }

  throw new Error(`未知指令: ${action || "(空)"}`);
}

/** 处理一条指令文件：执行 → 写结果 → 删除指令。 */
async function handleCommandFile(file, settings) {
  const cmd = await readJson(file);
  const id = path.basename(file, ".json");
  if (!cmd || typeof cmd !== "object") {
    await writeJson(path.join(dirs.results, `${id}.json`), {
      id,
      ok: false,
      error: "指令文件不是合法 JSON 对象",
      at: Date.now(),
    });
    await fs.rm(file, { force: true });
    return;
  }

  let payload;
  if (!settings.enabled) {
    payload = { id, ok: false, error: "执行器已在插件设置中关闭", at: Date.now() };
  } else {
    try {
      payload = { id, at: Date.now(), ...(await runAction(cmd, settings)) };
    } catch (error) {
      payload = {
        id,
        ok: false,
        action: String(cmd.action ?? ""),
        sessionId: String(cmd.sessionId ?? ""),
        error: String(error?.message ?? error),
        code: error?.code ?? undefined,
        at: Date.now(),
      };
    }
  }

  record(
    String(cmd.action ?? "?"),
    cmd.sessionId,
    payload.ok,
    payload.ok ? "已投递" : payload.error
  );
  if (!payload.ok) lastError = String(payload.error ?? "");
  try {
    await writeJson(path.join(dirs.results, `${id}.json`), payload);
  } catch (error) {
    lastError = `写结果失败: ${String(error?.message ?? error)}`;
  }
  await fs.rm(file, { force: true }).catch(() => void 0);
  await trimResults();
}

/** 结果目录只保留最近 MAX_RESULTS 个文件。 */
async function trimResults() {
  try {
    const names = (await fs.readdir(dirs.results)).filter((n) => n.endsWith(".json"));
    if (names.length <= MAX_RESULTS) return;
    const withTime = [];
    for (const name of names) {
      const full = path.join(dirs.results, name);
      try {
        const stat = await fs.stat(full);
        withTime.push({ full, mtime: stat.mtimeMs });
      } catch {
        /* 已被外部删除，忽略 */
      }
    }
    withTime.sort((a, b) => a.mtime - b.mtime);
    for (const item of withTime.slice(0, withTime.length - MAX_RESULTS)) {
      await fs.rm(item.full, { force: true }).catch(() => void 0);
    }
  } catch {
    /* 清理失败不影响执行 */
  }
}

/** 一轮：把待处理指令按文件名顺序依次执行完。 */
async function drain() {
  if (draining) return;
  draining = true;
  try {
    const settings = await readSettings();
    const names = (await fs.readdir(dirs.commands))
      .filter((name) => name.endsWith(".json"))
      .sort();
    for (const name of names) {
      await handleCommandFile(path.join(dirs.commands, name), settings);
    }
  } catch (error) {
    lastError = `轮询失败: ${String(error?.message ?? error)}`;
  } finally {
    draining = false;
  }
}

/** 心跳：外部脚本据此确认插件在跑、以及当前设置。 */
async function writeHeartbeat() {
  try {
    const settings = await readSettings();
    await writeJson(dirs.heartbeat, {
      ok: true,
      pluginId: pi.plugin.getId(),
      version: PLUGIN_VERSION,
      pid: process.pid,
      at: Date.now(),
      enabled: settings.enabled,
      defaultMessage: settings.defaultMessage,
      // 关机策略也写进心跳：外部监控脚本读它决定何时关机，
      // 这样「睡前在插件面板勾一次」就够了，不必再去改命令行参数。
      shutdownWhenAllDone: settings.shutdownWhenAllDone,
      allDoneConfirmSeconds: settings.allDoneConfirmSeconds,
      shutdownRequiresAllSources: settings.shutdownRequiresAllSources,
      shutdownWhenAll503: settings.shutdownWhenAll503,
      all503WindowMinutes: settings.all503WindowMinutes,
      // 报错/上下文超限续接：外部监控脚本读这三项决定何时续接、怎么续接。
      contextContinue: settings.contextContinue,
      contextContinueThreshold: settings.contextContinueThreshold,
      contextContinueMode: settings.contextContinueMode,
      openNewOn400: settings.openNewOn400,
      lastError: lastError || null,
      events: events.slice(0, 10),
    });
  } catch (error) {
    lastError = `写心跳失败: ${String(error?.message ?? error)}`;
  }
}

async function startService({ log } = {}) {
  if (running) return;
  await ensureDirs();
  running = true;
  lastError = "";
  log?.(`会话续跑执行器已启动，指令目录 ${dirs.commands}`);
  record("service", "", true, "执行器已启动");
  await writeHeartbeat();
  timer = setInterval(() => void drain(), POLL_MS);
  heartbeatTimer = setInterval(() => void writeHeartbeat(), HEARTBEAT_MS);
  // 启动瞬间先跑一轮，避免外部脚本已经写入的指令要等一个轮询周期
  void drain();
}

async function stopService() {
  running = false;
  if (timer) clearInterval(timer);
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  timer = null;
  heartbeatTimer = null;
  try {
    if (dirs) {
      await writeJson(dirs.heartbeat, {
        ok: true,
        pluginId: pi.plugin.getId(),
        version: PLUGIN_VERSION,
        pid: process.pid,
        at: Date.now(),
        enabled: false,
        running: false,
        lastError: lastError || null,
        events: events.slice(0, 10),
      });
    }
  } catch {
    /* 停服时心跳写不进去不影响退出 */
  }
}

/**
 * 列出会话，并逐个查运行状态。
 *
 * 为什么要这么做：插件是后台服务形态，宿主不会告诉它"当前是哪个会话"
 * （service.start 只收到 { log }，命令/面板也没有会话上下文），
 * Pi 也没有"当前会话"这种查询。所以这里用两个公开的 read 操作自己拼：
 *   session/list      → 会话清单（id/标题/项目/模式/时间）
 *   agent/getStatus   → 每个会话是否正在跑（isRunning / currentTurnId）
 * 这样面板里就能直接选会话，不用手填 ID。
 */
async function listSessionsWithStatus(limit = 40) {
  const listed = await invokeOperation("session/list", []);
  const all = Array.isArray(listed?.sessions) ? listed.sessions : [];
  const picked = all.slice(0, limit);

  // 并发查运行状态；单个失败不影响整体列表
  const statuses = await Promise.all(
    picked.map((s) =>
      invokeOperation("agent/getStatus", [String(s?.id ?? "")])
        .then((r) => r?.status ?? null)
        .catch(() => null)
    )
  );

  const now = Date.now();
  return picked.map((s, i) => {
    const st = statuses[i] ?? {};
    const updated = Date.parse(String(s?.updatedAt ?? "")) || 0;
    return {
      id: String(s?.id ?? ""),
      title: String(s?.title ?? "") || "（无标题）",
      project: String(s?.workspaceLabel ?? "") || "（无项目）",
      mode: String(s?.mode ?? ""),
      permissionMode: String(s?.permissionMode ?? ""),
      isRunning: st.isRunning === true,
      currentTurnId: st.currentTurnId ? String(st.currentTurnId) : null,
      pendingToolConfirmations: Number(st.pendingToolConfirmations ?? 0),
      updatedAt: updated,
      idleSeconds: updated ? Math.max(0, Math.round((now - updated) / 1000)) : null,
    };
  });
}

/** 面板调用的接口。 */
async function onPanelInvoke(channel, payload) {
  const name = String(channel ?? "");
  if (name === "status") {
    const settings = await readSettings();
    return {
      ok: true,
      running,
      version: PLUGIN_VERSION,
      pluginId: pi.plugin.getId(),
      hostRoot,
      commandDir: dirs?.commands ?? null,
      settings,
      lastError: lastError || null,
      events: events.slice(0, MAX_EVENTS),
      // 面板展示：确认宿主确实暴露了我们要用的操作
      operations: await listOperations().catch(() => []),
      // 监控脚本是否在跑：插件只是执行器，看不出这一点，
      // 但用户最需要知道的就是「现在到底有没有人在监控」。
      monitors: {
        codex: await readMonitorStatus("codex"),
        pi: await readMonitorStatus("pi"),
      },
    };
  }
  if (name === "listSessions") {
    try {
      const sessions = await listSessionsWithStatus();
      return { ok: true, sessions };
    } catch (error) {
      return { ok: false, error: String(error?.message ?? error), code: error?.code };
    }
  }
  if (name === "setAutoMode") {
    // 把会话权限模式切成「全自动」，这是无人值守的前提：
    // 非 auto 模式下，半夜遇到需要确认的工具操作会一直卡到早上。
    // 注意：session/configure 是 dangerous 操作，宿主会弹一次原生确认框
    // （这是 Pi 的安全设计，插件无法绕过），所以做成"一次点一个会话"，
    // 你睡前确认几次即可，比半夜被卡住强。
    const ids = Array.isArray(payload?.sessionIds)
      ? payload.sessionIds.map((x) => String(x ?? "").trim()).filter(Boolean)
      : [];
    if (!ids.length) return { ok: false, error: "未指定会话" };

    const results = [];
    for (const sessionId of ids) {
      try {
        await invokeOperation("session/configure", [
          { id: sessionId, permissionMode: "auto", confirm: true },
        ]);
        results.push({ sessionId, ok: true });
        record("setAuto", sessionId, true, "已切到全自动");
      } catch (error) {
        results.push({
          sessionId,
          ok: false,
          error: String(error?.message ?? error),
          code: error?.code,
        });
        record("setAuto", sessionId, false, String(error?.message ?? error));
      }
    }
    const okCount = results.filter((r) => r.ok).length;
    return { ok: okCount > 0, results, okCount, total: ids.length };
  }
  if (name === "setSettings") {
    // 与现有设置合并后再写：面板只传它自己那几个键，
    // 直接整体替换会把别的键（以及将来新增的键）悄悄清掉。
    const incoming = payload && typeof payload === "object" ? payload : {};
    let current = {};
    try {
      current = (await pi.plugin.getSettings()) ?? {};
    } catch {
      current = {};
    }
    const merged = { ...current, ...incoming };
    // 保存走「插件自己的 settings.json」，不再依赖 pi.plugin.setSettings：
    // 实测那条通道保存不了（plugins/data/<id>/settings.json 从装上起就没被写出来过），
    // 而插件本来就有自己数据目录的读写权限，写这里最可靠。
    // 位置与 Pi 读的是同一个文件，所以 Pi 的设置页也能看到同一份值。
    // 顺序：先自己写 → 自己写不了退回官方通道 → 都失败才报错。
    let saved = false;
    let reason = "";
    try {
      const dir = await pi.plugin.getDataPath();
      await fs.mkdir(dir, { recursive: true });   // 目录不存在也要能自己建出来
      const file = path.join(dir, "settings.json");
      const tmp = `${file}.tmp`;
      await fs.writeFile(tmp, `${JSON.stringify(merged, null, 2)}\n`, "utf8");
      await fs.rename(tmp, file);
      saved = true;
    } catch (error) {
      reason = String(error?.message ?? error);
    }
    if (!saved) {
      try {
        await pi.plugin.setSettings(merged);
        saved = true;
      } catch (error) {
        reason = reason || String(error?.message ?? error);
      }
    }
    if (!saved) {
      // 保存失败必须说出来：面板上直接显示原因，心跳里也留一条，
      // 否则用户只看到「点了报错」，事后完全查不出哪一步失败。
      const message = reason || "未知原因";
      // lastError 要显式写：record() 只记事件，不会自动进「最近错误」与心跳。
      lastError = `保存设置失败: ${message}`;
      record("settings", "", false, message);
      return { ok: false, error: message };
    }
    if (String(lastError).startsWith("保存设置失败")) lastError = "";
    // 立刻写一次心跳：外部监控脚本读的是心跳，保存后马上生效。
    await writeHeartbeat();
    record("settings", "", true, "设置已更新");
    return { ok: true };
  }
  if (name === "selftest") {
    // 自检：对当前会话发一条 ping，验证 agent/prompt 通道是否可用
    const sessionId = String(payload?.sessionId ?? "").trim();
    if (!sessionId) return { ok: false, error: "缺少 sessionId" };
    try {
      const result = await invokeOperation("agent/prompt", [
        { sessionId, content: "（会话续跑插件自检，可忽略）" },
      ]);
      record("selftest", sessionId, true, "自检已投递");
      return { ok: true, result };
    } catch (error) {
      record("selftest", sessionId, false, String(error?.message ?? error));
      return { ok: false, error: String(error?.message ?? error), code: error?.code };
    }
  }
  if (name === "clearEvents") {
    events.length = 0;
    lastError = "";
    return { ok: true };
  }
  return { ok: false, error: `未知面板接口: ${name}` };
}

/** 列出宿主暴露的操作，供面板核对能力。 */
async function listOperations() {
  const ops = await pi.desktop.listOperations();
  if (!Array.isArray(ops)) return [];
  return ops
    .filter((op) =>
      [
        "agent/prompt",
        "agent/abort",
        "agent/getStatus",
        "agent/compact",
        "session/list",
        "session/create",
      ].includes(op?.id)
    )
    .map((op) => ({ id: op.id, risk: op.risk, description: op.description }));
}

async function onLoad() {
  await pi.commands.register({
    id: "local.session-keeper.panel",
    title: "会话续跑：打开面板",
    category: "会话",
    run: async () => {
      await pi.ui.openPanel();
    },
  });

  await pi.commands.register({
    id: "local.session-keeper.test-send",
    title: "会话续跑：自检发送（发给当前会话）",
    category: "会话",
    run: async () => {
      // 命令没有会话上下文，只能提示用户到面板里选会话自检
      await pi.ui.showToast("请在「会话续跑」面板里选择会话后点自检", "info");
    },
  });

  // 常驻服务：manifest 已声明 contributes.services，权限授予后由宿主启动
  pi.services.register({
    id: "watcher",
    start: startService,
    stop: stopService,
  });
}

async function onUnload() {
  await stopService();
  await pi.commands.unregister("local.session-keeper.panel").catch(() => void 0);
  await pi.commands.unregister("local.session-keeper.test-send").catch(() => void 0);
  pi.services.unregister("watcher").catch(() => void 0);
}

module.exports = { onLoad, onUnload, onPanelInvoke };
