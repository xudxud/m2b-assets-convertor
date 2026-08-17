import { useEffect, useState } from "react";
import { getVersion } from "@tauri-apps/api/app";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import { open } from "@tauri-apps/plugin-dialog";
import { revealItemInDir } from "@tauri-apps/plugin-opener";
import {
  Blocks,
  Box,
  Check,
  ChevronRight,
  CircleAlert,
  CircleStop,
  FileBox,
  FolderOpen,
  Layers3,
  LoaderCircle,
  Play,
  RotateCcw,
  Settings2,
  Sparkles,
  Trash2,
} from "lucide-react";
import "./App.css";

type JobStatus = "queued" | "processing" | "completed" | "failed" | "stopped";

interface MayaSceneFile {
  path: string;
  name: string;
  size: number;
}

interface Job extends MayaSceneFile {
  selected: boolean;
  status: JobStatus;
  detail: string;
  output?: string;
}

interface MayaEnvironment {
  mayapyPath: string;
  available: boolean;
  message: string;
}

interface BatchEvent {
  kind: string;
  file?: string;
  index: number;
  total: number;
  message: string;
  output?: string;
}

interface WorkerEvent {
  event: string;
  level?: string;
  message?: string;
  current?: number;
  total?: number;
  material?: string;
  output?: string;
  elapsed?: number;
  converted?: number;
  materialFailed?: number;
  meshes?: number;
  success?: boolean;
  warnings?: number;
  manifest?: string;
  plugins?: string[];
}

const statusLabel: Record<JobStatus, string> = {
  queued: "等待",
  processing: "处理中",
  completed: "完成",
  failed: "失败",
  stopped: "已停止",
};

function formatBytes(bytes: number) {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function shortPath(path: string) {
  if (path.length <= 64) return path;
  return `${path.slice(0, 26)}...${path.slice(-34)}`;
}

function App() {
  const [appVersion, setAppVersion] = useState("");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [mayapyPath, setMayapyPath] = useState("");
  const [environment, setEnvironment] = useState<MayaEnvironment | null>(null);
  const [outputDir, setOutputDir] = useState("");
  const [copyTextures, setCopyTextures] = useState(true);
  const [invertOpacity, setInvertOpacity] = useState(false);
  const [embeddedTextures, setEmbeddedTextures] = useState(false);
  const [overwrite, setOverwrite] = useState("rename");
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState(0);
  const [logs, setLogs] = useState<string[]>(["M2B 已启动，等待选择资产文件夹。"]);

  const appendLog = (message: string) => {
    const time = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    setLogs((current) => [...current.slice(-299), `[${time}] ${message}`]);
  };

  const scanPaths = async (paths: string[]) => {
    if (!paths.length) return;
    try {
      const files = await invoke<MayaSceneFile[]>("scan_maya_scenes", { inputPaths: paths });
      appendLog(`扫描完成，共发现 ${files.length} 个 Maya 场景。`);
      setJobs((current) => {
        const known = new Set(current.map((item) => item.path.toLowerCase()));
        const additions = files
          .filter((item) => !known.has(item.path.toLowerCase()))
          .map((item) => ({
            ...item,
            selected: true,
            status: "queued" as const,
            detail: "等待处理",
          }));
        return [...current, ...additions];
      });
    } catch (error) {
      appendLog(`扫描失败：${String(error)}`);
    }
  };

  const checkMaya = async (path?: string) => {
    try {
      const result = await invoke<MayaEnvironment>("detect_maya", { path: path || null });
      setEnvironment(result);
      setMayapyPath(result.mayapyPath);
    } catch (error) {
      appendLog(`Maya 检测失败：${String(error)}`);
    }
  };

  useEffect(() => {
    void getVersion().then(setAppVersion);
    checkMaya();

    const subscriptions = Promise.all([
      listen<BatchEvent>("batch-event", ({ payload }) => {
        if (payload.kind === "started" && payload.file) {
          setJobs((current) => current.map((job) =>
            job.path === payload.file
              ? { ...job, status: "processing", detail: payload.message }
              : job,
          ));
          appendLog(`${payload.index}/${payload.total} 开始处理：${payload.file}`);
        }
        if (["completed", "failed", "stopped"].includes(payload.kind) && payload.file) {
          const status = payload.kind === "completed" ? "completed" : payload.kind as JobStatus;
          setJobs((current) => current.map((job) =>
            job.path === payload.file
              ? { ...job, status, detail: payload.message, output: payload.output }
              : job,
          ));
          setProgress(Math.round((payload.index / payload.total) * 100));
          appendLog(`${payload.file}：${payload.message}`);
        }
        if (payload.kind === "batch-finished") {
          setRunning(false);
          appendLog(payload.message);
        }
      }),
      listen<WorkerEvent>("worker-event", ({ payload }) => {
        if (payload.event === "material-progress") {
          setJobs((current) => current.map((job) => job.status === "processing"
            ? { ...job, detail: `材质 ${payload.current}/${payload.total} · ${payload.material}` }
            : job));
        }
        if (payload.event === "export-started") {
          setJobs((current) => current.map((job) => job.status === "processing"
            ? { ...job, detail: `正在导出 FBX · ${payload.meshes ?? 0} meshes` }
            : job));
        }
        if (payload.event === "log" && payload.message) appendLog(payload.message);
        if (payload.event === "scene-warning" && payload.plugins?.length) {
          const preview = payload.plugins.slice(0, 4).join(", ");
          const more = payload.plugins.length > 4 ? ` 等 ${payload.plugins.length} 个` : "";
          appendLog(`场景引用了未安装的历史插件：${preview}${more}。已作为警告继续处理。`);
        }
        if (payload.event === "failed" && payload.message) appendLog(`Worker：${payload.message}`);
        if (payload.event === "completed") {
          const result = payload.success === false ? "诊断导出完成，但存在材质错误" : "导出完成";
          appendLog(`${result}：${payload.output}，耗时 ${payload.elapsed ?? 0}s`);
        }
      }),
      listen<string>("worker-log", ({ payload }) => appendLog(payload)),
      getCurrentWebview().onDragDropEvent((event) => {
        if (event.payload.type === "drop") scanPaths(event.payload.paths);
      }),
    ]);

    return () => {
      subscriptions.then((unlisten) => unlisten.forEach((fn) => fn()));
    };
  }, []);

  const chooseFolder = async () => {
    const selected = await open({ directory: true, multiple: true });
    if (selected) scanPaths(Array.isArray(selected) ? selected : [selected]);
  };

  const chooseOutput = async () => {
    const selected = await open({ directory: true });
    if (typeof selected === "string") setOutputDir(selected);
  };

  const chooseMayapy = async () => {
    const selected = await open({
      filters: [{ name: "Maya Python", extensions: ["exe"] }],
    });
    if (typeof selected === "string") checkMaya(selected);
  };

  const startBatch = async () => {
    const targets = jobs.filter((job) => job.selected && job.status !== "completed");
    if (!environment?.available) {
      appendLog("请先设置有效的 Maya 2020 mayapy.exe。");
      return;
    }
    if (!targets.length) {
      appendLog("没有可执行的任务。");
      return;
    }
    setJobs((current) => current.map((job) =>
      !job.selected || job.status === "completed"
        ? job
        : { ...job, status: "queued", detail: "等待处理" },
    ));
    setProgress(0);
    setRunning(true);
    try {
      await invoke("start_batch", {
        files: targets.map((job) => job.path),
        options: {
          mayapyPath,
          outputDir,
          copyTextures,
          invertOpacity,
          embeddedTextures,
          overwrite,
        },
      });
    } catch (error) {
      setRunning(false);
      appendLog(`无法启动任务：${String(error)}`);
    }
  };

  const stopBatch = async () => {
    try {
      await invoke("stop_batch");
      appendLog("正在停止当前 Maya Worker...");
    } catch (error) {
      appendLog(`停止失败：${String(error)}`);
    }
  };

  const revealBlenderAddon = async () => {
    try {
      const path = await invoke<string>("blender_addon_path");
      await revealItemInDir(path);
      appendLog(`Blender 助手位置：${path}`);
    } catch (error) {
      appendLog(`无法定位 Blender 助手：${String(error)}`);
    }
  };

  const completedCount = jobs.filter((job) => job.status === "completed").length;
  const failedCount = jobs.filter((job) => job.status === "failed").length;
  const selectedCount = jobs.filter((job) => job.selected).length;
  const runnableCount = jobs.filter(
    (job) => job.selected && job.status !== "completed",
  ).length;

  const selectAll = (selected: boolean) => {
    setJobs((current) => current.map((job) => ({ ...job, selected })));
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-mark"><Layers3 size={20} /></div>
        <div className="brand-copy">
          <strong>M2B</strong>
        </div>
        <div className="topbar-spacer" />
        <div className={`environment-pill ${environment?.available ? "ok" : "error"}`}>
          <span className="status-dot" />
          {environment?.message ?? "正在检测 Maya"}
        </div>
        <button className="icon-button" title="重新检测" onClick={() => checkMaya(mayapyPath)}>
          <RotateCcw size={16} />
        </button>
      </header>

      <div className="workspace">
        <aside className="sidebar">
          <section className="side-section">
            <div className="section-kicker">01 / SOURCE</div>
            <h2>输入场景</h2>
            <button className="drop-zone" onClick={chooseFolder} disabled={running}>
              <FolderOpen size={24} />
              <span>选择资产文件夹</span>
              <small>递归查找 .mb / .ma，也可拖入文件或文件夹</small>
            </button>
          </section>

          <section className="side-section">
            <div className="section-kicker">02 / OUTPUT</div>
            <h2>输出位置</h2>
            <button className="path-field" onClick={chooseOutput} disabled={running}>
              <span>{outputDir || "与源文件相同"}</span>
              <FolderOpen size={15} />
            </button>
            <label className="field-label" htmlFor="overwrite">同名文件</label>
            <select id="overwrite" value={overwrite} onChange={(event) => setOverwrite(event.target.value)} disabled={running}>
              <option value="rename">自动编号</option>
              <option value="skip">跳过已有文件</option>
              <option value="overwrite">直接覆盖</option>
            </select>
          </section>

          <section className="side-section options-section">
            <div className="section-kicker">03 / PROCESS</div>
            <h2>处理选项</h2>
            <label className="toggle-row">
              <span><b>复制贴图到资产包</b><small>生成相对路径与 M2B 清单</small></span>
              <input type="checkbox" checked={copyTextures} onChange={(event) => setCopyTextures(event.target.checked)} disabled={running} />
            </label>
            <label className="toggle-row">
              <span><b>反转透明贴图</b><small>白色透明、黑色不透明时开启</small></span>
              <input type="checkbox" checked={invertOpacity} onChange={(event) => setInvertOpacity(event.target.checked)} disabled={running} />
            </label>
            <label className="toggle-row">
              <span><b>嵌入纹理</b><small>增加 FBX 文件体积</small></span>
              <input type="checkbox" checked={embeddedTextures} onChange={(event) => setEmbeddedTextures(event.target.checked)} disabled={running} />
            </label>
          </section>

          <section className="maya-path">
            <Settings2 size={15} />
            <div><small>MAYAPY 路径</small><span title={mayapyPath}>{shortPath(mayapyPath || "未设置")}</span></div>
            <button onClick={chooseMayapy} disabled={running}>更改</button>
          </section>
          <button className="addon-link" onClick={revealBlenderAddon}>
            <Blocks size={15} />
            <span><b>Blender 导入助手</b><small>支持 Blender 4.2+</small></span>
            <ChevronRight size={14} />
          </button>
        </aside>

        <main className="main-panel">
          <div className="queue-header">
            <div>
              <div className="section-kicker">BATCH QUEUE</div>
              <h1>转换任务</h1>
            </div>
            <div className="queue-stats">
              <span><b>{selectedCount}</b> 已选</span>
              <span><b>{jobs.length}</b> 总计</span>
              <span><b>{completedCount}</b> 完成</span>
              <span className={failedCount ? "danger" : ""}><b>{failedCount}</b> 失败</span>
            </div>
            <button className="icon-button" title="清空列表" disabled={running || !jobs.length} onClick={() => setJobs([])}>
              <Trash2 size={17} />
            </button>
          </div>

          <div className="queue-table">
            <div className="table-head">
              <label className="check-cell" title="全选/取消全选">
                <input
                  type="checkbox"
                  checked={jobs.length > 0 && selectedCount === jobs.length}
                  disabled={running || !jobs.length}
                  onChange={(event) => selectAll(event.target.checked)}
                />
              </label>
              <span>场景文件</span><span>大小</span><span>状态</span><span />
            </div>
            <div className="table-body">
              {!jobs.length && (
                <div className="empty-state">
                  <div className="empty-orbit"><FileBox size={34} /></div>
                  <h3>队列还是空的</h3>
                  <p>选择文件夹，或拖入 `.mb`、`.ma` 文件及文件夹</p>
                </div>
              )}
              {jobs.map((job) => (
                <div className={`job-row ${job.status} ${job.selected ? "" : "unselected"}`} key={job.path}>
                  <label className="check-cell">
                    <input
                      type="checkbox"
                      checked={job.selected}
                      disabled={running}
                      onChange={(event) => setJobs((current) => current.map((item) =>
                        item.path === job.path
                          ? { ...item, selected: event.target.checked }
                          : item,
                      ))}
                    />
                  </label>
                  <div className="file-cell">
                    <div className="file-icon"><Box size={18} /></div>
                    <div><b>{job.name}</b><small title={job.path}>{shortPath(job.path)}</small></div>
                  </div>
                  <span className="size-cell">{formatBytes(job.size)}</span>
                  <div className={`status-cell ${job.status}`}>
                    {job.status === "processing" && <LoaderCircle size={14} className="spin" />}
                    {job.status === "completed" && <Check size={14} />}
                    {job.status === "failed" && <CircleAlert size={14} />}
                    <span><b>{statusLabel[job.status]}</b><small>{job.detail}</small></span>
                  </div>
                  <button className="row-remove" disabled={running} onClick={() => setJobs((current) => current.filter((item) => item.path !== job.path))}>
                    <Trash2 size={15} />
                  </button>
                </div>
              ))}
            </div>
          </div>

          <section className="console-panel">
            <div className="console-title"><span className="status-dot" /> PROCESS LOG <small>{logs.length} lines</small></div>
            <div className="console-lines">
              {logs.slice(-7).map((line, index) => <div key={`${index}-${line}`}>{line}</div>)}
            </div>
          </section>

          <footer className="action-bar">
            <div className="progress-copy">
              <span>{running ? "批处理进行中" : jobs.length ? "任务已就绪" : "等待任务"}</span>
              <small>{progress}%</small>
            </div>
            <div className="progress-track"><span style={{ width: `${progress}%` }} /></div>
            {running ? (
              <button className="stop-button" onClick={stopBatch}><CircleStop size={18} /> 停止任务</button>
            ) : (
              <button className="run-button" onClick={startBatch} disabled={!runnableCount || !environment?.available}>
                <Play size={18} fill="currentColor" /> 开始转换 <ChevronRight size={17} />
              </button>
            )}
          </footer>
        </main>
      </div>
      <div className="build-tag"><Sparkles size={12} /> M2B{appVersion ? ` · v${appVersion}` : ""}</div>
    </div>
  );
}

export default App;
