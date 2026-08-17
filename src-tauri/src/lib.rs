use serde::Serialize;
use std::io::{BufRead, BufReader};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter, Manager, State};
use walkdir::WalkDir;

#[derive(Clone, Default)]
struct TaskRuntime {
    running: Arc<AtomicBool>,
    stop_requested: Arc<AtomicBool>,
    current_pid: Arc<Mutex<Option<u32>>>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct MayaEnvironment {
    mayapy_path: String,
    available: bool,
    message: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct MayaSceneFile {
    path: String,
    name: String,
    size: u64,
}

#[derive(serde::Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct BatchOptions {
    mayapy_path: String,
    output_dir: String,
    copy_textures: bool,
    invert_opacity: bool,
    embedded_textures: bool,
    overwrite: String,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct BatchEvent {
    kind: String,
    file: Option<String>,
    index: usize,
    total: usize,
    message: String,
    output: Option<String>,
}

fn default_mayapy_path() -> PathBuf {
    let mut candidates = Vec::new();
    if let Some(location) = std::env::var_os("MAYA_LOCATION") {
        candidates.push(PathBuf::from(location).join("bin").join("mayapy.exe"));
    }
    for drive in 'C'..='Z' {
        candidates.push(PathBuf::from(format!(
            r"{drive}:\Program Files\Autodesk\Maya2020\bin\mayapy.exe"
        )));
        candidates.push(PathBuf::from(format!(
            r"{drive}:\Autodesk\MAYA2020\Maya2020\bin\mayapy.exe"
        )));
        candidates.push(PathBuf::from(format!(
            r"{drive}:\Autodesk\Maya2020\bin\mayapy.exe"
        )));
    }
    candidates
        .into_iter()
        .find(|path| path.is_file())
        .unwrap_or_else(|| PathBuf::from(r"C:\Program Files\Autodesk\Maya2020\bin\mayapy.exe"))
}

#[tauri::command]
fn detect_maya(path: Option<String>) -> MayaEnvironment {
    let candidate = path
        .filter(|value| !value.trim().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(default_mayapy_path);
    let available = candidate.is_file();
    let message = if available {
        "Maya 2020 mayapy 已就绪".to_string()
    } else {
        "未找到 Maya 2020 mayapy.exe".to_string()
    };

    MayaEnvironment {
        mayapy_path: candidate.to_string_lossy().into_owned(),
        available,
        message,
    }
}

#[tauri::command]
fn scan_maya_scenes(input_paths: Vec<String>) -> Result<Vec<MayaSceneFile>, String> {
    let mut paths = Vec::new();

    for input in input_paths {
        let path = PathBuf::from(input);
        if path.is_file() {
            if is_maya_scene_file(&path) {
                paths.push(path);
            }
            continue;
        }
        if path.is_dir() {
            for entry in WalkDir::new(path)
                .follow_links(false)
                .into_iter()
                .filter_map(Result::ok)
            {
                if entry.file_type().is_file() && is_maya_scene_file(entry.path()) {
                    paths.push(entry.into_path());
                }
            }
        }
    }

    paths.sort_by_key(|path| path.to_string_lossy().to_lowercase());
    paths.dedup();
    paths
        .into_iter()
        .map(|path| {
            let metadata = std::fs::metadata(&path).map_err(|error| error.to_string())?;
            Ok(MayaSceneFile {
                name: path
                    .file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .into_owned(),
                path: path.to_string_lossy().into_owned(),
                size: metadata.len(),
            })
        })
        .collect()
}

fn is_maya_scene_file(path: &Path) -> bool {
    path.extension()
        .map(|extension| {
            let extension = extension.to_string_lossy();
            extension.eq_ignore_ascii_case("mb") || extension.eq_ignore_ascii_case("ma")
        })
        .unwrap_or(false)
}

fn worker_path(app: &AppHandle) -> Result<PathBuf, String> {
    let bundled = app
        .path()
        .resource_dir()
        .map_err(|error| error.to_string())?
        .join("maya_backend")
        .join("maya_worker.py");
    if bundled.is_file() {
        return Ok(bundled);
    }

    let development = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join("maya_backend")
        .join("maya_worker.py");
    if development.is_file() {
        Ok(development)
    } else {
        Err("找不到 Maya Worker 脚本".to_string())
    }
}

#[tauri::command]
fn blender_addon_path(app: AppHandle) -> Result<String, String> {
    let bundled = app
        .path()
        .resource_dir()
        .map_err(|error| error.to_string())?
        .join("blender_addon")
        .join("m2b_importer.py");
    if bundled.is_file() {
        return Ok(bundled.to_string_lossy().into_owned());
    }
    let development = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join("blender_addon")
        .join("m2b_importer.py");
    if development.is_file() {
        Ok(development.to_string_lossy().into_owned())
    } else {
        Err("找不到 Blender 助手".to_string())
    }
}

fn emit_batch(app: &AppHandle, event: BatchEvent) {
    let _ = app.emit("batch-event", event);
}

fn read_lossy_lines<R, F>(reader: R, mut callback: F)
where
    R: std::io::Read,
    F: FnMut(String),
{
    let mut reader = BufReader::new(reader);
    let mut buffer = Vec::new();
    loop {
        buffer.clear();
        match reader.read_until(b'\n', &mut buffer) {
            Ok(0) | Err(_) => break,
            Ok(_) => {
                let line = String::from_utf8_lossy(&buffer)
                    .trim_end_matches(['\r', '\n'])
                    .to_string();
                callback(line);
            }
        }
    }
}

#[tauri::command]
fn start_batch(
    app: AppHandle,
    runtime: State<TaskRuntime>,
    files: Vec<String>,
    options: BatchOptions,
) -> Result<(), String> {
    if files.is_empty() {
        return Err("任务列表为空".to_string());
    }
    if !Path::new(&options.mayapy_path).is_file() {
        return Err("mayapy.exe 路径无效".to_string());
    }
    let worker = worker_path(&app)?;
    if runtime
        .running
        .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
        .is_err()
    {
        return Err("已有任务正在运行".to_string());
    }

    runtime.stop_requested.store(false, Ordering::SeqCst);
    let runtime = runtime.inner().clone();

    std::thread::spawn(move || {
        let total = files.len();
        let mut success_count = 0usize;

        for (position, file) in files.iter().enumerate() {
            let index = position + 1;
            if runtime.stop_requested.load(Ordering::SeqCst) {
                break;
            }

            emit_batch(
                &app,
                BatchEvent {
                    kind: "started".into(),
                    file: Some(file.clone()),
                    index,
                    total,
                    message: "正在启动 Maya Worker".into(),
                    output: None,
                },
            );

            let mut command = Command::new(&options.mayapy_path);
            command
                .arg(&worker)
                .arg("--input")
                .arg(file)
                .arg("--output-dir")
                .arg(&options.output_dir)
                .arg("--copy-textures")
                .arg(if options.copy_textures {
                    "true"
                } else {
                    "false"
                })
                .arg("--embedded-textures")
                .arg(if options.embedded_textures {
                    "true"
                } else {
                    "false"
                })
                .arg("--invert-opacity")
                .arg(if options.invert_opacity {
                    "true"
                } else {
                    "false"
                })
                .arg("--overwrite")
                .arg(&options.overwrite)
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .creation_flags(0x08000000);

            let mut child = match command.spawn() {
                Ok(child) => child,
                Err(error) => {
                    emit_batch(
                        &app,
                        BatchEvent {
                            kind: "failed".into(),
                            file: Some(file.clone()),
                            index,
                            total,
                            message: format!("无法启动 mayapy: {error}"),
                            output: None,
                        },
                    );
                    continue;
                }
            };

            *runtime.current_pid.lock().unwrap() = Some(child.id());
            let stdout = child.stdout.take();
            let stderr = child.stderr.take();
            let output_path = Arc::new(Mutex::new(None::<String>));
            let diagnostic_failure = Arc::new(AtomicBool::new(false));

            let stdout_thread = stdout.map(|stdout| {
                let app = app.clone();
                let output_path = output_path.clone();
                let diagnostic_failure = diagnostic_failure.clone();
                std::thread::spawn(move || {
                    read_lossy_lines(stdout, |line| {
                        match serde_json::from_str::<serde_json::Value>(&line) {
                            Ok(value) => {
                                if value.get("event").and_then(|item| item.as_str())
                                    == Some("completed")
                                {
                                    if value.get("success").and_then(|item| item.as_bool())
                                        == Some(false)
                                    {
                                        diagnostic_failure.store(true, Ordering::SeqCst);
                                    }
                                    if let Some(output) =
                                        value.get("output").and_then(|item| item.as_str())
                                    {
                                        *output_path.lock().unwrap() = Some(output.to_string());
                                    }
                                }
                                let _ = app.emit("worker-event", value);
                            }
                            Err(_) => {}
                        }
                    });
                })
            });

            let stderr_thread = stderr.map(|stderr| {
                let app = app.clone();
                std::thread::spawn(move || {
                    read_lossy_lines(stderr, |_line| {});
                    drop(app);
                })
            });

            let status = child.wait();
            if let Some(thread) = stdout_thread {
                let _ = thread.join();
            }
            if let Some(thread) = stderr_thread {
                let _ = thread.join();
            }
            *runtime.current_pid.lock().unwrap() = None;

            if runtime.stop_requested.load(Ordering::SeqCst) {
                emit_batch(
                    &app,
                    BatchEvent {
                        kind: "stopped".into(),
                        file: Some(file.clone()),
                        index,
                        total,
                        message: "任务已停止".into(),
                        output: None,
                    },
                );
                break;
            }

            let succeeded = status.map(|value| value.success()).unwrap_or(false);
            let output = output_path.lock().unwrap().clone();
            if succeeded {
                success_count += 1;
            }
            emit_batch(
                &app,
                BatchEvent {
                    kind: if succeeded { "completed" } else { "failed" }.into(),
                    file: Some(file.clone()),
                    index,
                    total,
                    message: if succeeded {
                        "处理完成".into()
                    } else if diagnostic_failure.load(Ordering::SeqCst) {
                        "已生成诊断资产包，但存在材质转换错误".into()
                    } else {
                        "Maya Worker 执行失败".into()
                    },
                    output,
                },
            );
        }

        runtime.running.store(false, Ordering::SeqCst);
        emit_batch(
            &app,
            BatchEvent {
                kind: "batch-finished".into(),
                file: None,
                index: success_count,
                total,
                message: format!("成功处理 {success_count}/{total} 个文件"),
                output: None,
            },
        );
    });

    Ok(())
}

#[tauri::command]
fn stop_batch(runtime: State<TaskRuntime>) -> Result<(), String> {
    runtime.stop_requested.store(true, Ordering::SeqCst);
    if let Some(pid) = *runtime.current_pid.lock().unwrap() {
        let pid = pid.to_string();
        Command::new("taskkill")
            .args(["/PID", &pid, "/T", "/F"])
            .creation_flags(0x08000000)
            .status()
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(TaskRuntime::default())
        .invoke_handler(tauri::generate_handler![
            detect_maya,
            blender_addon_path,
            scan_maya_scenes,
            start_batch,
            stop_batch
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

#[cfg(test)]
mod tests {
    use super::{is_maya_scene_file, scan_maya_scenes};
    use std::fs;
    use std::path::Path;

    #[test]
    fn recognizes_maya_scene_extensions_case_insensitively() {
        assert!(is_maya_scene_file(Path::new("asset.mb")));
        assert!(is_maya_scene_file(Path::new("asset.MB")));
        assert!(is_maya_scene_file(Path::new("asset.ma")));
        assert!(is_maya_scene_file(Path::new("asset.MA")));
    }

    #[test]
    fn rejects_non_maya_scene_extensions() {
        assert!(!is_maya_scene_file(Path::new("asset.fbx")));
        assert!(!is_maya_scene_file(Path::new("asset.mb.bak")));
        assert!(!is_maya_scene_file(Path::new("asset")));
    }

    #[test]
    fn scans_mixed_files_and_folders_without_duplicates() {
        let root = std::env::temp_dir().join(format!("m2b-scan-test-{}", std::process::id()));
        let nested = root.join("nested");
        let direct = root.join("direct.MA");
        fs::create_dir_all(&nested).unwrap();
        fs::write(&direct, b"").unwrap();
        fs::write(nested.join("inside.mb"), b"").unwrap();
        fs::write(nested.join("ignored.fbx"), b"").unwrap();

        let scenes = scan_maya_scenes(vec![
            direct.to_string_lossy().into_owned(),
            root.to_string_lossy().into_owned(),
        ])
        .unwrap();

        assert_eq!(scenes.len(), 2);
        assert!(scenes
            .iter()
            .all(|scene| scene.path.ends_with(".MA") || scene.path.ends_with(".mb")));
        fs::remove_dir_all(root).unwrap();
    }
}
