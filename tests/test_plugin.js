/**
 * 用 mock 的 pi API 加载插件，验证新增的 compact / create 动作。
 *
 * 重点验证：
 *  - compact 会调用 agent/compact，且带上正确的 sessionId
 *  - create 会调用 session/create，参数按预期裁剪（空字段不传）
 *  - create 会把 content 作为首条消息投给「新会话」（不是原会话）
 *  - create 拿不到新会话 id 时必须报错，而不是静默成功
 *  - 首条消息投递失败时，仍然返回会话已创建（不回滚、不假装失败）
 *  - 未知 action 仍然报错
 *
 * 为了让测试能直接调用内部函数，这里在读取源码后追加一段导出，
 * 而不是在生产代码里留下测试钩子。
 */
const fsSync = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const Module = require("node:module");

const calls = [];
let createResult = { session: { id: "new-sess-id" } };
let promptShouldFail = false;
let settingsShouldFail = false;
// 插件设置的模拟存储：与真实 Pi 一致 —— 值存在插件数据目录的 settings.json 里，
// 读的时候用 manifest 默认值兜底。测试「合并写」必须能读回刚写的值。
const SETTING_DEFAULTS = {
  enabled: true, defaultMessage: "继续", shutdownWhenAllDone: false,
  allDoneConfirmSeconds: 120, shutdownRequiresAllSources: true,
  shutdownWhenAll503: false, all503WindowMinutes: 60,
  contextContinue: true, contextContinueThreshold: 3,
  contextContinueMode: "compact", openNewOn400: true,
};

function settingsFilePath() {
  return path.join(os.tmpdir(), "pi-session-keeper-test", "mockdata",
    "plugins", "data", "local.session-keeper", "settings.json");
}

const pi = {
  plugin: {
    getId: () => "local.session-keeper",
    getDataPath: async () => {
      if (settingsShouldFail) {
        throw Object.assign(new Error("getDataPath failed"), { code: "NOT_FOUND" });
      }
      const dir = path.join(os.tmpdir(), "pi-session-keeper-test", "mockdata",
        "plugins", "data", "local.session-keeper");
      fsSync.mkdirSync(dir, { recursive: true });   // 真实 API 也会保证目录存在
      return dir;
    },
    // 与真实 API 一致：读插件数据目录里的 settings.json，缺的用默认值兜底
    getSettings: async () => {
      let stored = {};
      try {
        stored = JSON.parse(fsSync.readFileSync(settingsFilePath(), "utf8"));
      } catch {}
      return { ...SETTING_DEFAULTS, ...stored };
    },
    // 官方通道：真实实现就是往同一个 settings.json 里合并写
    setSettings: async (next) => {
      if (settingsShouldFail) {
        throw Object.assign(new Error("write failed: EACCES"), { code: "EACCES" });
      }
      fsSync.mkdirSync(path.dirname(settingsFilePath()), { recursive: true });
      fsSync.writeFileSync(settingsFilePath(), JSON.stringify(next, null, 2), "utf8");
    },
  },
  desktop: {
    invoke: async ({ operation, args }) => {
      calls.push({ operation, args });
      if (operation === "session/create") return createResult;
      if (operation === "agent/prompt") {
        if (promptShouldFail && args[0].sessionId === "new-sess-id") {
          throw Object.assign(new Error("prompt rejected"), { code: "AGENT_BUSY" });
        }
        return { accepted: true };
      }
      if (operation === "agent/compact") return { accepted: true };
      if (operation === "agent/abort") return { aborted: true };
      return {};
    },
    listOperations: async () => [
      { id: "agent/prompt", risk: "write", description: "p" },
      { id: "agent/abort", risk: "write", description: "a" },
      { id: "agent/getStatus", risk: "read", description: "g" },
      { id: "agent/compact", risk: "write", description: "c" },
      { id: "session/list", risk: "read", description: "l" },
      { id: "session/create", risk: "write", description: "n" },
      { id: "session/delete", risk: "dangerous", description: "d" },
    ],
  },
  session: { list: async () => ({ sessions: [] }) },
  commands: { register: async () => {}, unregister: async () => {} },
  services: { register: () => {}, unregister: async () => {} },
  ui: { openPanel: async () => {}, showToast: async () => {} },
};

global.pi = pi;

const FAIL = [];
function check(label, cond, extra = "") {
  console.log((cond ? "  \u2713 " : "  \u2717 ") + label + (extra ? "  " + extra : ""));
  if (!cond) FAIL.push(label);
}

// 读源码 + 追加内部导出（生产文件本身不动）
const srcPath = path.resolve("pi-session-keeper", "main.js");
const src = fsSync.readFileSync(srcPath, "utf8");
const instrumented = src.replace(
  /module\.exports\s*=\s*\{[^}]*\};?\s*$/,
  "module.exports.__test = { runAction, listOperations, PLUGIN_VERSION, readSettings, " +
    "writeHeartbeat, ensureDirs, onPanelInvoke, heartbeatPath: () => dirs.heartbeat };\n"
);
const mod = new Module(srcPath, null);
mod.filename = srcPath;
mod.paths = Module._nodeModulePaths(path.dirname(srcPath));
mod._compile(instrumented, srcPath);
const plugin = mod.exports;

// 每次都从干净状态开始：设置文件残留会让「默认值」断言误判（上一轮跑测试写下的值）
try { fsSync.rmSync(settingsFilePath(), { force: true }); } catch {}

async function run(action, extra = {}) {
  calls.length = 0;
  const cmd = { id: "t" + Date.now(), action, ...extra };
  return plugin.__test.runAction(cmd, { enabled: true, defaultMessage: "继续" });
}

(async () => {
  console.log("=".repeat(70));
  console.log("1. compact 动作");
  let r = await run("compact", { sessionId: "sess-1" });
  check("返回 ok", r.ok === true);
  check("调用了 agent/compact", calls.some((c) => c.operation === "agent/compact"));
  const cc = calls.find((c) => c.operation === "agent/compact");
  check("带上了 sessionId", cc && cc.args[0].sessionId === "sess-1", JSON.stringify(cc && cc.args));
  check("没有多余调用", calls.length === 1, JSON.stringify(calls.map((c) => c.operation)));

  console.log();
  console.log("=".repeat(70));
  console.log("2. compact 缺 sessionId 必须报错");
  let threw = false;
  try { await run("compact", {}); } catch (e) { threw = true; }
  check("抛错", threw);

  console.log();
  console.log("=".repeat(70));
  console.log("3. create 动作：参数裁剪与首条消息投递");
  promptShouldFail = false;
  r = await run("create", {
    projectPath: "D:/proj/alpha",
    title: "续接：原会话",
    content: "请继续这个会话未完成的任务：pi-desktop://session/sess-old",
    providerId: "prov1",
    modelId: "model1",
    mode: "agent",
    thinkingLevel: "high",
    sessionId: "ignored-should-not-matter",
  });
  check("返回 ok", r.ok === true);
  check("返回新会话 id", r.sessionId === "new-sess-id", String(r.sessionId));
  const createCall = calls.find((c) => c.operation === "session/create");
  check("调用了 session/create", Boolean(createCall));
  const input = createCall.args[0];
  check("传了 projectPath", input.projectPath === "D:/proj/alpha");
  check("传了 title", input.title === "续接：原会话");
  check("传了 providerId", input.providerId === "prov1");
  check("传了 modelId", input.modelId === "model1");
  check("传了 mode", input.mode === "agent");
  check("传了 thinkingLevel", input.thinkingLevel === "high");
  check("没有把 sessionId 塞进 create 参数", !("sessionId" in input), JSON.stringify(input));

  const promptCall = calls.find((c) => c.operation === "agent/prompt");
  check("投递了首条消息", Boolean(promptCall));
  check("首条消息发给「新会话」而非原会话",
    promptCall && promptCall.args[0].sessionId === "new-sess-id",
    promptCall && promptCall.args[0].sessionId);
  check("首条消息内容是深链交接说明",
    promptCall && promptCall.args[0].content.includes("pi-desktop://session/sess-old"));
  check("标记 prompted=true", r.prompted === true);

  console.log();
  console.log("=".repeat(70));
  console.log("4. create 缺 projectPath 时不应传该字段（让 Pi 用默认）");
  r = await run("create", { title: "t", content: "c" });
  const input2 = calls.find((c) => c.operation === "session/create").args[0];
  check("未传 projectPath", !("projectPath" in input2), JSON.stringify(input2));
  check("空 mode 不传", !("mode" in input2));

  console.log();
  console.log("=".repeat(70));
  console.log("5. create 拿不到会话 id 时必须报错（不能假装成功）");
  createResult = {};
  threw = false;
  let msg = "";
  try { await run("create", { projectPath: "D:/p", content: "x" }); }
  catch (e) { threw = true; msg = e.message; }
  check("抛错", threw);
  check("错误信息说明未返回 id", msg.includes("未返回会话 id"), msg);
  createResult = { session: { id: "new-sess-id" } };

  console.log();
  console.log("=".repeat(70));
  console.log("6. 首条消息投递失败：会话仍算创建成功，但要如实回报失败");
  promptShouldFail = true;
  r = await run("create", { projectPath: "D:/p", content: "hello" });
  check("仍返回 ok（会话已建好）", r.ok === true);
  check("返回了新会话 id", r.sessionId === "new-sess-id");
  check("prompted=false", r.prompted === false);
  check("带上了 promptError", Boolean(r.promptError), String(r.promptError));
  promptShouldFail = false;

  console.log();
  console.log("=".repeat(70));
  console.log("7. 不传 content 时不应投递首条消息");
  r = await run("create", { projectPath: "D:/p" });
  check("没有 agent/prompt 调用", !calls.some((c) => c.operation === "agent/prompt"));
  check("prompted=false", r.prompted === false);

  console.log();
  console.log("=".repeat(70));
  console.log("8. 原有动作不受影响，未知动作仍报错");
  r = await run("prompt", { sessionId: "sess-9", content: "继续" });
  check("prompt 仍可用", r.ok === true && calls.some((c) => c.operation === "agent/prompt"));
  r = await run("abort", { sessionId: "sess-9" });
  check("abort 仍可用", r.ok === true && calls.some((c) => c.operation === "agent/abort"));
  threw = false;
  try { await run("nonsense", {}); } catch (e) { threw = true; }
  check("未知动作抛错", threw);

  console.log();
  console.log("=".repeat(70));
  console.log("9. 面板能力核对要包含新增操作");
  const ops = await plugin.__test.listOperations();
  const ids = ops.map((o) => o.id);
  check("含 agent/compact", ids.includes("agent/compact"), ids.join(","));
  check("含 session/create", ids.includes("session/create"));
  check("不含危险的 session/delete", !ids.includes("session/delete"));

  console.log();
  console.log("=".repeat(70));
  console.log("10. 版本号已升到 1.9.2");
  check("PLUGIN_VERSION=1.9.2", plugin.__test.PLUGIN_VERSION === "1.9.2",
    plugin.__test.PLUGIN_VERSION);

  console.log();
  console.log("=".repeat(70));
  console.log("11. 关机设置：读默认值、写入心跳、保存时不被覆盖");
  // 默认值：未设置时开关关闭、观察 120 秒、要求跨来源
  let s = await plugin.__test.readSettings();
  check("默认不开关机", s.shutdownWhenAllDone === false, String(s.shutdownWhenAllDone));
  check("默认观察 120 秒", s.allDoneConfirmSeconds === 120,
    String(s.allDoneConfirmSeconds));
  check("默认要求跨来源", s.shutdownRequiresAllSources === true);

  // 保存后要能读回来（setSettings 是合并写，不能被别的键覆盖掉）
  await plugin.__test.onPanelInvoke("setSettings", {
    enabled: true, defaultMessage: "继续",
    shutdownWhenAllDone: true, allDoneConfirmSeconds: 45,
    shutdownRequiresAllSources: false,
  });
  s = await plugin.__test.readSettings();
  check("开关已保存", s.shutdownWhenAllDone === true);
  check("秒数已保存", s.allDoneConfirmSeconds === 45, String(s.allDoneConfirmSeconds));
  check("跨来源开关已保存", s.shutdownRequiresAllSources === false);

  // 只传部分键时，其余键必须保留（合并写）
  await plugin.__test.onPanelInvoke("setSettings", { defaultMessage: "继续干" });
  s = await plugin.__test.readSettings();
  check("部分保存不会清掉关机开关", s.shutdownWhenAllDone === true);
  check("部分保存不会清掉秒数", s.allDoneConfirmSeconds === 45);
  check("部分保存生效", s.defaultMessage === "继续干", s.defaultMessage);

  // 非法秒数要退回默认，而不是写进 NaN
  await plugin.__test.onPanelInvoke("setSettings", { allDoneConfirmSeconds: "abc" });
  s = await plugin.__test.readSettings();
  check("非法秒数退回 120", s.allDoneConfirmSeconds === 120,
    String(s.allDoneConfirmSeconds));
  await plugin.__test.onPanelInvoke("setSettings", { allDoneConfirmSeconds: -5 });
  s = await plugin.__test.readSettings();
  check("负数秒数退回 120", s.allDoneConfirmSeconds === 120,
    String(s.allDoneConfirmSeconds));
  await plugin.__test.ensureDirs();
  await plugin.__test.writeHeartbeat();

  console.log();
  console.log("=".repeat(70));
  console.log("12. 心跳要暴露关机设置（外部脚本靠它读配置）");
  await plugin.__test.writeHeartbeat();
  const hb = JSON.parse(
    fsSync.readFileSync(plugin.__test.heartbeatPath(), "utf8"));
  check("心跳含 shutdownWhenAllDone", "shutdownWhenAllDone" in hb,
    JSON.stringify(Object.keys(hb)));
  check("心跳含 allDoneConfirmSeconds", "allDoneConfirmSeconds" in hb);
  check("心跳含 shutdownRequiresAllSources", "shutdownRequiresAllSources" in hb);
  check("心跳值正确", hb.shutdownWhenAllDone === true && hb.allDoneConfirmSeconds === 120,
    JSON.stringify({ a: hb.shutdownWhenAllDone, b: hb.allDoneConfirmSeconds }));

  console.log();
  console.log("=".repeat(70));
  console.log("13. 报错/上下文超限续接设置：默认值、保存、心跳暴露");
  s = await plugin.__test.readSettings();
  check("默认启用续接", s.contextContinue === true, String(s.contextContinue));
  check("默认连续 3 次", s.contextContinueThreshold === 3,
    String(s.contextContinueThreshold));
  check("默认先压缩", s.contextContinueMode === "compact", s.contextContinueMode);
  check("默认报 400 直接新建会话", s.openNewOn400 === true, String(s.openNewOn400));

  await plugin.__test.onPanelInvoke("setSettings", {
    contextContinue: false, contextContinueThreshold: 5, contextContinueMode: "new",
    openNewOn400: false,
  });
  s = await plugin.__test.readSettings();
  check("续接开关已保存（关）", s.contextContinue === false);
  check("次数已保存", s.contextContinueThreshold === 5,
    String(s.contextContinueThreshold));
  check("模式已保存", s.contextContinueMode === "new", s.contextContinueMode);
  check("400 直开开关已保存（关）", s.openNewOn400 === false, String(s.openNewOn400));

  // 非法值必须退回默认，而不是写进 NaN / 未知模式
  await plugin.__test.onPanelInvoke("setSettings", {
    contextContinueThreshold: "abc", contextContinueMode: "bogus",
  });
  s = await plugin.__test.readSettings();
  check("非法次数退回 3", s.contextContinueThreshold === 3,
    String(s.contextContinueThreshold));
  check("未知模式退回 compact", s.contextContinueMode === "compact",
    s.contextContinueMode);

  await plugin.__test.writeHeartbeat();
  const hb2 = JSON.parse(fsSync.readFileSync(plugin.__test.heartbeatPath(), "utf8"));
  check("心跳含 contextContinue", "contextContinue" in hb2,
    JSON.stringify(Object.keys(hb2)));
  check("心跳含 contextContinueThreshold", "contextContinueThreshold" in hb2);
  check("心跳含 contextContinueMode", "contextContinueMode" in hb2);
  check("心跳含 openNewOn400", "openNewOn400" in hb2);
  check("心跳值正确",
    hb2.contextContinue === false && hb2.contextContinueThreshold === 3 &&
      hb2.contextContinueMode === "compact" && hb2.openNewOn400 === false,
    JSON.stringify({ a: hb2.contextContinue, b: hb2.contextContinueThreshold,
                     c: hb2.contextContinueMode, d: hb2.openNewOn400 }));

  console.log();
  console.log("=".repeat(70));
  console.log("13b. 设置写进插件自己的 settings.json（不再依赖 Pi 的保存通道）");
  const settingsFile = path.join(await pi.plugin.getDataPath(), "settings.json");
  try { fsSync.rmSync(settingsFile, { force: true }); } catch {}
  const saveOk0 = await plugin.__test.onPanelInvoke("setSettings", {
    contextContinue: true, contextContinueThreshold: 4, openNewOn400: false,
  });
  check("保存成功", saveOk0 && saveOk0.ok === true, JSON.stringify(saveOk0));
  check("插件自己的 settings.json 被写出来", fsSync.existsSync(settingsFile), settingsFile);
  if (fsSync.existsSync(settingsFile)) {
    const onDisk = JSON.parse(fsSync.readFileSync(settingsFile, "utf8"));
    check("写进去的是合并后的值", onDisk.contextContinueThreshold === 4
      && onDisk.openNewOn400 === false, JSON.stringify(onDisk).slice(0, 160));
    check("合并保留了原有键", onDisk.enabled === true, String(onDisk.enabled));
  }
  check("保存成功后心跳已刷新", JSON.parse(
    fsSync.readFileSync(plugin.__test.heartbeatPath(), "utf8")).openNewOn400 === false);

  console.log();
  console.log("13c. 两条通道都写不进去时，必须把原因说出来（面板能看到）");
  settingsShouldFail = true;
  const saveFail = await plugin.__test.onPanelInvoke("setSettings", { contextContinue: true });
  check("返回 ok=false", saveFail && saveFail.ok === false, JSON.stringify(saveFail));
  check("带上了失败原因", String(saveFail?.error ?? "").length > 0, String(saveFail?.error));
  await plugin.__test.writeHeartbeat();
  const hbFail = JSON.parse(fsSync.readFileSync(plugin.__test.heartbeatPath(), "utf8"));
  check("心跳里记下了这次失败",
    String(hbFail.lastError ?? "").includes("保存设置失败"), String(hbFail.lastError));
  settingsShouldFail = false;
  const saveOk = await plugin.__test.onPanelInvoke("setSettings", { contextContinue: true });
  check("恢复正常后能保存成功", saveOk && saveOk.ok === true, JSON.stringify(saveOk));
  console.log();
  console.log("=".repeat(70));
  console.log("14. 面板要能看到「监控脚本是否在跑」");
  const baseDir = path.dirname(plugin.__test.heartbeatPath());
  const piMon = path.join(baseDir, "monitor-pi.json");
  const cxMon = path.join(baseDir, "monitor-codex.json");

  // 没有任何心跳 → 两个来源都算未运行
  fsSync.rmSync(piMon, { force: true });
  fsSync.rmSync(cxMon, { force: true });
  let st2 = await plugin.__test.onPanelInvoke("status", {});
  check("无心跳时 Pi 未运行", st2.monitors?.pi?.running === false,
    JSON.stringify(st2.monitors?.pi));
  check("无心跳时 Codex 未运行", st2.monitors?.codex?.running === false);

  // 新鲜心跳 → 运行中，并透出「模拟运行」标记（这是最容易踩的坑）
  fsSync.writeFileSync(piMon, JSON.stringify({
    source: "pi", at: Date.now(), pid: 1234, dryRun: true, pollSeconds: 10,
  }));
  st2 = await plugin.__test.onPanelInvoke("status", {});
  check("新鲜心跳 → Pi 运行中", st2.monitors?.pi?.running === true);
  check("透出 dry-run 标记", st2.monitors?.pi?.dryRun === true);
  check("透出轮询间隔", st2.monitors?.pi?.ageSeconds !== undefined);

  // 过期心跳 → 未运行（监控脚本已退出）
  fsSync.writeFileSync(piMon, JSON.stringify({
    source: "pi", at: Date.now() - 600000, pid: 1234, dryRun: false, pollSeconds: 10,
  }));
  st2 = await plugin.__test.onPanelInvoke("status", {});
  check("过期心跳 → Pi 未运行", st2.monitors?.pi?.running === false,
    JSON.stringify(st2.monitors?.pi));

  // 轮询间隔调得很长时，不该误判为「未运行」
  fsSync.writeFileSync(piMon, JSON.stringify({
    source: "pi", at: Date.now() - 60000, pid: 1234, dryRun: false, pollSeconds: 120,
  }));
  st2 = await plugin.__test.onPanelInvoke("status", {});
  check("长轮询间隔不误判", st2.monitors?.pi?.running === true,
    JSON.stringify(st2.monitors?.pi));

  console.log();
  console.log("=".repeat(70));
  console.log("15. 全 503 关机设置：默认值、保存、心跳暴露");
  s = await plugin.__test.readSettings();
  check("默认关闭", s.shutdownWhenAll503 === false, String(s.shutdownWhenAll503));
  check("默认窗口 60", s.all503WindowMinutes === 60, String(s.all503WindowMinutes));

  await plugin.__test.onPanelInvoke("setSettings", {
    shutdownWhenAll503: true, all503WindowMinutes: 45,
  });
  s = await plugin.__test.readSettings();
  check("开关已保存", s.shutdownWhenAll503 === true);
  check("窗口已保存", s.all503WindowMinutes === 45, String(s.all503WindowMinutes));

  // 非法窗口必须退回默认，而不是写进负数/NaN
  await plugin.__test.onPanelInvoke("setSettings", { all503WindowMinutes: -3 });
  s = await plugin.__test.readSettings();
  check("非法窗口退回 60", s.all503WindowMinutes === 60,
    String(s.all503WindowMinutes));

  await plugin.__test.writeHeartbeat();
  const hb3 = JSON.parse(fsSync.readFileSync(plugin.__test.heartbeatPath(), "utf8"));
  check("心跳含 shutdownWhenAll503", "shutdownWhenAll503" in hb3,
    JSON.stringify(Object.keys(hb3)));
  check("心跳含 all503WindowMinutes", "all503WindowMinutes" in hb3);
  check("心跳值正确",
    hb3.shutdownWhenAll503 === true && hb3.all503WindowMinutes === 60,
    JSON.stringify({ a: hb3.shutdownWhenAll503, b: hb3.all503WindowMinutes }));

  console.log();
  console.log("=".repeat(70));
  if (FAIL.length) {
    console.log(`失败 ${FAIL.length} 项：`);
    FAIL.forEach((x) => console.log("   - " + x));
    process.exit(1);
  }
  console.log("全部通过");
})().catch((e) => {
  console.error("测试自身出错：", e);
  process.exit(2);
});
