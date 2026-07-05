use serde::Serialize;
use std::collections::HashMap;
use std::env;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::process::{Child, Command as StdCommand, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::Duration;
use tauri::Manager;
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

const API_HOST: &str = "127.0.0.1";
const API_PORT: &str = "8000";
const SIDECAR_NAME: &str = "veyra-backend";

#[derive(Clone, Serialize)]
struct BackendLaunchStatus {
    status: String,
    mode: String,
    pid: Option<u32>,
    api_base_url: String,
    data_dir: String,
    state_root: String,
    agency_root: String,
    message: String,
}

impl Default for BackendLaunchStatus {
    fn default() -> Self {
        Self {
            status: "unknown".to_string(),
            mode: "unknown".to_string(),
            pid: None,
            api_base_url: api_base_url(),
            data_dir: String::new(),
            state_root: String::new(),
            agency_root: String::new(),
            message: "Backend status has not been initialized.".to_string(),
        }
    }
}

#[derive(Clone)]
struct BackendLaunchConfig {
    mode: String,
    cwd: PathBuf,
    data_dir: PathBuf,
    state_root: PathBuf,
    agency_root: PathBuf,
    env_file: PathBuf,
    log_dir: PathBuf,
}

struct BackendState {
    child: Mutex<Option<BackendChild>>,
    status: Mutex<BackendLaunchStatus>,
}

impl Default for BackendState {
    fn default() -> Self {
        Self {
            child: Mutex::new(None),
            status: Mutex::new(BackendLaunchStatus::default()),
        }
    }
}

impl Drop for BackendState {
    fn drop(&mut self) {
        if let Ok(slot) = self.child.get_mut() {
            if let Some(child) = slot.take() {
                child.kill();
            }
        }
    }
}

enum BackendChild {
    Dev(Child),
    Sidecar(CommandChild),
}

impl BackendChild {
    fn pid(&self) -> u32 {
        match self {
            BackendChild::Dev(child) => child.id(),
            BackendChild::Sidecar(child) => child.pid(),
        }
    }

    fn kill(self) {
        match self {
            BackendChild::Dev(mut child) => {
                let _ = child.kill();
                let _ = child.wait();
            }
            BackendChild::Sidecar(child) => {
                let _ = child.kill();
            }
        }
    }
}

#[tauri::command]
fn veyra_backend_status(state: tauri::State<'_, BackendState>) -> BackendLaunchStatus {
    state.status.lock().map(|status| status.clone()).unwrap_or_default()
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(BackendState::default())
        .invoke_handler(tauri::generate_handler![veyra_backend_status])
        .setup(|app| {
            if let Err(message) = bootstrap_backend(app) {
                let config = launch_config(app);
                set_backend_status(
                    app.handle(),
                    BackendLaunchStatus {
                        status: "error".to_string(),
                        mode: config.mode,
                        pid: None,
                        api_base_url: api_base_url(),
                        data_dir: path_string(&config.data_dir),
                        state_root: path_string(&config.state_root),
                        agency_root: path_string(&config.agency_root),
                        message,
                    },
                );
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to run Veyra desktop app");
}

fn bootstrap_backend(app: &mut tauri::App) -> Result<(), String> {
    let config = launch_config(app);
    ensure_runtime_dirs(&config)?;

    if api_is_veyra() {
        set_backend_status(
            app.handle(),
            BackendLaunchStatus {
                status: "running".to_string(),
                mode: "existing_api".to_string(),
                pid: None,
                api_base_url: api_base_url(),
                data_dir: path_string(&config.data_dir),
                state_root: path_string(&config.state_root),
                agency_root: path_string(&config.agency_root),
                message: "Found an existing Veyra API on 127.0.0.1:8000.".to_string(),
            },
        );
        return Ok(());
    }

    let envs = backend_env(&config);
    let child = if config.mode == "dev_source" {
        start_dev_backend(&config, &envs)?
    } else {
        start_sidecar_backend(app, &config, &envs)?
    };
    let pid = child.pid();
    {
        let state = app.state::<BackendState>();
        let mut slot = state.child.lock().map_err(|_| "Backend state lock failed.".to_string())?;
        *slot = Some(child);
    }

    set_backend_status(
        app.handle(),
        BackendLaunchStatus {
            status: "starting".to_string(),
            mode: config.mode.clone(),
            pid: Some(pid),
            api_base_url: api_base_url(),
            data_dir: path_string(&config.data_dir),
            state_root: path_string(&config.state_root),
            agency_root: path_string(&config.agency_root),
            message: "Veyra backend process started; waiting for API readiness.".to_string(),
        },
    );
    spawn_readiness_watcher(app.handle().clone(), config, pid);
    Ok(())
}

fn start_dev_backend(
    config: &BackendLaunchConfig,
    envs: &HashMap<String, String>,
) -> Result<BackendChild, String> {
    let python = dev_python_bin(&config.cwd);
    let stdout = log_file(&config.log_dir, "desktop_backend.out.log")?;
    let stderr = log_file(&config.log_dir, "desktop_backend.err.log")?;
    let mut command = StdCommand::new(&python);
    command
        .arg("-B")
        .arg("desktop_backend.py")
        .current_dir(&config.cwd)
        .envs(envs)
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));
    command
        .spawn()
        .map(BackendChild::Dev)
        .map_err(|err| format!("Failed to start development backend with {}: {err}", path_string(&python)))
}

fn start_sidecar_backend(
    app: &mut tauri::App,
    config: &BackendLaunchConfig,
    envs: &HashMap<String, String>,
) -> Result<BackendChild, String> {
    let mut command = app
        .shell()
        .sidecar(SIDECAR_NAME)
        .map_err(|err| format!("Failed to resolve Veyra backend sidecar: {err}"))?
        .current_dir(&config.cwd);
    for (key, value) in envs {
        command = command.env(key, value);
    }
    let (mut rx, child) = command
        .spawn()
        .map_err(|err| format!("Failed to start Veyra backend sidecar: {err}"))?;
    let handle = app.handle().clone();
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stderr(line) | CommandEvent::Stdout(line) => {
                    let text = String::from_utf8_lossy(&line);
                    if text.contains("Application startup complete") {
                        let status = handle.state::<BackendState>().status.lock().ok().map(|guard| guard.clone());
                        if let Some(mut current) = status {
                            current.status = "running".to_string();
                            current.message = "Veyra backend reported startup complete.".to_string();
                            set_backend_status(&handle, current);
                        }
                    }
                }
                CommandEvent::Terminated(payload) => {
                    let status = handle.state::<BackendState>().status.lock().ok().map(|guard| guard.clone());
                    if let Some(mut current) = status {
                        current.status = "stopped".to_string();
                        current.message = format!("Veyra backend exited with code {:?}.", payload.code);
                        set_backend_status(&handle, current);
                    }
                }
                CommandEvent::Error(message) => {
                    let status = handle.state::<BackendState>().status.lock().ok().map(|guard| guard.clone());
                    if let Some(mut current) = status {
                        current.status = "error".to_string();
                        current.message = message;
                        set_backend_status(&handle, current);
                    }
                }
                _ => {}
            }
        }
    });
    Ok(BackendChild::Sidecar(child))
}

fn spawn_readiness_watcher(handle: tauri::AppHandle, config: BackendLaunchConfig, pid: u32) {
    thread::spawn(move || {
        for _ in 0..80 {
            if api_is_veyra() {
                set_backend_status(
                    &handle,
                    BackendLaunchStatus {
                        status: "running".to_string(),
                        mode: config.mode.clone(),
                        pid: Some(pid),
                        api_base_url: api_base_url(),
                        data_dir: path_string(&config.data_dir),
                        state_root: path_string(&config.state_root),
                        agency_root: path_string(&config.agency_root),
                        message: "Veyra backend is ready.".to_string(),
                    },
                );
                return;
            }
            thread::sleep(Duration::from_millis(500));
        }
        set_backend_status(
            &handle,
            BackendLaunchStatus {
                status: "degraded".to_string(),
                mode: config.mode,
                pid: Some(pid),
                api_base_url: api_base_url(),
                data_dir: path_string(&config.data_dir),
                state_root: path_string(&config.state_root),
                agency_root: path_string(&config.agency_root),
                message: "Backend process started but /setup/status did not become ready within 40 seconds.".to_string(),
            },
        );
    });
}

fn launch_config(app: &tauri::App) -> BackendLaunchConfig {
    if cfg!(debug_assertions) {
        if let Some(root) = source_repo_root() {
            return BackendLaunchConfig {
                mode: "dev_source".to_string(),
                cwd: root.clone(),
                data_dir: root.clone(),
                state_root: root.join("state"),
                agency_root: root.join("agency"),
                env_file: root.join(".env"),
                log_dir: root.join("state").join("logs"),
            };
        }
    }

    let data_dir = app.path().app_data_dir().unwrap_or_else(|_| fallback_app_data_dir());
    BackendLaunchConfig {
        mode: "sidecar".to_string(),
        cwd: data_dir.clone(),
        data_dir: data_dir.clone(),
        state_root: data_dir.join("state"),
        agency_root: data_dir.join("agency"),
        env_file: data_dir.join(".env"),
        log_dir: data_dir.join("logs"),
    }
}

fn backend_env(config: &BackendLaunchConfig) -> HashMap<String, String> {
    let mut envs = HashMap::new();
    envs.insert("VEYRA_DESKTOP".to_string(), "1".to_string());
    envs.insert("VEYRA_HOST".to_string(), API_HOST.to_string());
    envs.insert("VEYRA_PORT".to_string(), API_PORT.to_string());
    envs.insert("VEYRA_ENV_FILE".to_string(), path_string(&config.env_file));
    envs.insert("VEYRA_DESKTOP_DATA_DIR".to_string(), path_string(&config.data_dir));
    envs.insert("VEYRA_STATE_ROOT".to_string(), path_string(&config.state_root));
    envs.insert("VEYRA_AGENCY_ROOT".to_string(), path_string(&config.agency_root));
    envs.insert("VEYRA_APP_LOG".to_string(), path_string(&config.log_dir.join("veyra_backend.log")));
    envs.insert(
        "OPENCLAW_DEVICE_STORE".to_string(),
        path_string(&config.state_root.join("local").join("openclaw_device.json")),
    );
    envs.insert(
        "VEYRA_OPENCLAW_MEMORY_MIRROR_DIR".to_string(),
        path_string(&config.state_root.join("local").join("openclaw_memory_mirror")),
    );
    envs
}

fn ensure_runtime_dirs(config: &BackendLaunchConfig) -> Result<(), String> {
    for dir in [&config.data_dir, &config.state_root, &config.agency_root, &config.log_dir] {
        fs::create_dir_all(dir).map_err(|err| format!("Failed to create {}: {err}", path_string(dir)))?;
    }
    Ok(())
}

fn set_backend_status(handle: &tauri::AppHandle, status: BackendLaunchStatus) {
    if let Ok(mut current) = handle.state::<BackendState>().status.lock() {
        *current = status;
    }
}

fn api_base_url() -> String {
    format!("http://{API_HOST}:{API_PORT}")
}

fn api_is_veyra() -> bool {
    let address = format!("{API_HOST}:{API_PORT}");
    let mut addrs = match address.to_socket_addrs() {
        Ok(addrs) => addrs,
        Err(_) => return false,
    };
    let Some(addr) = addrs.next() else {
        return false;
    };
    let mut stream = match TcpStream::connect_timeout(&addr, Duration::from_millis(350)) {
        Ok(stream) => stream,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(700)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(700)));
    let request = format!("GET /setup/status HTTP/1.1\r\nHost: {API_HOST}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = String::new();
    if stream.read_to_string(&mut response).is_err() {
        return false;
    }
    response.starts_with("HTTP/1.1 200") && response.contains("\"app_name\":\"Veyra\"")
}

fn dev_python_bin(root: &Path) -> PathBuf {
    let unix_venv = root.join(".venv").join("bin").join("python");
    if unix_venv.exists() {
        return unix_venv;
    }
    let windows_venv = root.join(".venv").join("Scripts").join("python.exe");
    if windows_venv.exists() {
        return windows_venv;
    }
    PathBuf::from(if cfg!(windows) { "python" } else { "python3" })
}

fn log_file(dir: &Path, name: &str) -> Result<std::fs::File, String> {
    fs::create_dir_all(dir).map_err(|err| format!("Failed to create log directory {}: {err}", path_string(dir)))?;
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join(name))
        .map_err(|err| format!("Failed to open log file {}: {err}", path_string(&dir.join(name))))
}

fn source_repo_root() -> Option<PathBuf> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let root = manifest_dir.parent()?.parent()?.parent()?.to_path_buf();
    if root.join("main.py").exists() && root.join("desktop_backend.py").exists() {
        Some(root)
    } else {
        None
    }
}

fn fallback_app_data_dir() -> PathBuf {
    if cfg!(target_os = "macos") {
        if let Some(home) = env::var_os("HOME") {
            return PathBuf::from(home)
                .join("Library")
                .join("Application Support")
                .join("Veyra");
        }
    }
    if cfg!(windows) {
        if let Some(app_data) = env::var_os("APPDATA") {
            return PathBuf::from(app_data).join("Veyra");
        }
    }
    if let Some(xdg_data) = env::var_os("XDG_DATA_HOME") {
        return PathBuf::from(xdg_data).join("Veyra");
    }
    if let Some(home) = env::var_os("HOME") {
        return PathBuf::from(home).join(".local").join("share").join("Veyra");
    }
    PathBuf::from(".veyra")
}

fn path_string(path: &Path) -> String {
    path.to_string_lossy().to_string()
}
