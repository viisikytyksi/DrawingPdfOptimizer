//! Shift_JIS CSV processing log, matching `cli.py`'s format and behaviour:
//! one row per file plus run start/end rows, and automatic fallback to another
//! directory when the primary location cannot be written.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};

use chrono::Local;
use encoding_rs::SHIFT_JIS;

const LOG_NAME: &str = "DrawingPdfOptimizer.log";
const HEADER: [&str; 16] = [
    "timestamp", "version", "user", "fullname", "hostname", "file", "filesize_bytes",
    "total_images", "converted", "skipped_small", "skipped_unsupported", "bytes_after",
    "elapsed_sec", "dpi", "status", "error",
];

pub struct ProcessingLog {
    path: Option<PathBuf>,
    fallback_dir: Option<PathBuf>,
    output_dir: Option<PathBuf>,
    source_parent: PathBuf,
    version: &'static str,
    user: String,
    fullname: String,
    hostname: String,
    dpi: u32,
    progress: bool,
    moved_reported: bool,
    failed_reported: bool,
}

impl ProcessingLog {
    pub fn new(
        log_dir: Option<PathBuf>,
        fallback_dir: Option<PathBuf>,
        output_dir: Option<PathBuf>,
        first_input: PathBuf,
        version: &'static str,
        fullname: &str,
        dpi: u32,
        progress: bool,
    ) -> Self {
        let source_parent = first_input
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_default();
        let base = log_dir
            .clone()
            .or_else(|| output_dir.clone())
            .unwrap_or_else(|| source_parent.clone());
        ProcessingLog {
            path: Some(base.join(LOG_NAME)),
            fallback_dir,
            output_dir,
            source_parent,
            version,
            user: current_user(),
            fullname: fullname.to_string(),
            hostname: current_hostname(),
            dpi,
            progress,
            moved_reported: false,
            failed_reported: false,
        }
    }

    pub fn write_run_start(&mut self, files: usize) {
        let row = self.row(
            &format!("(実行開始) 入力{files}件"),
            0,
            "run-start",
            "",
            0,
            0,
            0,
            0,
            0,
            0.0,
        );
        self.append(row, None);
    }

    #[allow(clippy::too_many_arguments)]
    pub fn write_file_row(
        &mut self,
        source: &Path,
        status: &str,
        error: &str,
        total_images: usize,
        converted: usize,
        skipped_small: usize,
        skipped_unsupported: usize,
        bytes_after: u64,
        elapsed: f64,
    ) {
        let size = std::fs::metadata(source).map(|m| m.len()).unwrap_or(0);
        let row = self.row(
            &source.display().to_string(),
            size,
            status,
            error,
            total_images,
            converted,
            skipped_small,
            skipped_unsupported,
            bytes_after,
            elapsed,
        );
        self.append(row, Some(source));
    }

    pub fn write_run_done(
        &mut self,
        inputs: usize,
        ok: usize,
        failed: usize,
        elapsed: f64,
        failures: &[(String, String)],
    ) {
        let mut summary = String::new();
        if failed > 0 {
            summary = format!("成功{ok}件/失敗{failed}件: ");
            for (index, (input, error)) in failures.iter().take(5).enumerate() {
                if index > 0 {
                    summary.push_str("; ");
                }
                let name = Path::new(input)
                    .file_name()
                    .map(|n| n.to_string_lossy().to_string())
                    .unwrap_or_else(|| input.clone());
                let first_line = error.lines().next().unwrap_or("").to_string();
                summary.push_str(&format!("{name}: {first_line}"));
            }
        }
        let row = self.row(
            &format!("(実行完了) 入力{inputs}件 成功{ok}件 失敗{failed}件"),
            0,
            "run-done",
            &summary,
            0,
            0,
            0,
            0,
            0,
            elapsed,
        );
        self.append(row, None);
    }

    #[allow(clippy::too_many_arguments)]
    fn row(
        &self,
        file: &str,
        file_size: u64,
        status: &str,
        error: &str,
        total_images: usize,
        converted: usize,
        skipped_small: usize,
        skipped_unsupported: usize,
        bytes_after: u64,
        elapsed: f64,
    ) -> Vec<String> {
        vec![
            timestamp(),
            self.version.to_string(),
            self.user.clone(),
            self.fullname.clone(),
            self.hostname.clone(),
            file.to_string(),
            file_size.to_string(),
            total_images.to_string(),
            converted.to_string(),
            skipped_small.to_string(),
            skipped_unsupported.to_string(),
            bytes_after.to_string(),
            format!("{elapsed:.1}"),
            self.dpi.to_string(),
            status.to_string(),
            error.to_string(),
        ]
    }

    /// Append one row, moving to the fallback location when the primary fails.
    fn append(&mut self, row: Vec<String>, source: Option<&Path>) {
        let Some(path) = self.path.clone() else {
            return;
        };
        if let Err(error) = append_row(&path, &row) {
            if let Some(fallback) = self.fallback_path(source, &path) {
                if append_row(&fallback, &row).is_ok() {
                    self.path = Some(fallback.clone());
                    if !self.moved_reported {
                        self.moved_reported = true;
                        self.notify(
                            "log_moved",
                            &[
                                ("path", fallback.display().to_string()),
                                ("previous", path.display().to_string()),
                                ("error", error.clone()),
                            ],
                            &format!("ログの保存先を変更しました: {} ({error})", fallback.display()),
                        );
                    }
                    return;
                }
            }
            if !self.failed_reported {
                self.failed_reported = true;
                self.notify(
                    "log_error",
                    &[
                        ("path", path.display().to_string()),
                        ("error", error.clone()),
                    ],
                    &format!("処理ログを保存できません: {} ({error})", path.display()),
                );
            }
        }
    }

    fn fallback_path(&self, source: Option<&Path>, current: &Path) -> Option<PathBuf> {
        let mut candidates: Vec<PathBuf> = Vec::new();
        if let Some(dir) = &self.fallback_dir {
            candidates.push(dir.join(LOG_NAME));
        }
        if let Some(dir) = &self.output_dir {
            candidates.push(dir.join(LOG_NAME));
        }
        let parent = source
            .and_then(Path::parent)
            .map(Path::to_path_buf)
            .unwrap_or_else(|| self.source_parent.clone());
        candidates.push(parent.join(LOG_NAME));
        candidates.into_iter().find(|candidate| candidate != current)
    }

    fn notify(&self, event: &str, fields: &[(&str, String)], message: &str) {
        if self.progress {
            let mut line = format!("{{\"type\": \"{event}\"");
            for (key, value) in fields {
                line.push_str(&format!(", \"{key}\": \"{}\"", escape(value)));
            }
            line.push('}');
            eprintln!("{line}");
        } else {
            eprintln!("警告: {message}");
        }
    }
}

fn append_row(path: &Path, row: &[String]) -> Result<(), String> {
    let write_header = !path.exists() || std::fs::metadata(path).map(|m| m.len()).unwrap_or(0) == 0;
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|e| format!("{}: {e}", io_kind(&e)))?;
    let mut line = String::new();
    if write_header {
        line.push_str(&HEADER.join(","));
        line.push_str("\r\n");
    }
    line.push_str(&row.iter().map(|field| csv_field(field)).collect::<Vec<_>>().join(","));
    line.push_str("\r\n");
    let (encoded, _, _) = SHIFT_JIS.encode(&line);
    file.write_all(&encoded)
        .map_err(|e| format!("{}: {e}", io_kind(&e)))?;
    Ok(())
}

fn io_kind(error: &std::io::Error) -> String {
    let name = match error.kind() {
        std::io::ErrorKind::NotFound => "FileNotFoundError",
        std::io::ErrorKind::PermissionDenied => "PermissionError",
        _ => "OSError",
    };
    name.to_string()
}

fn csv_field(value: &str) -> String {
    if value.contains(',') || value.contains('"') || value.contains('\n') || value.contains('\r') {
        format!("\"{}\"", value.replace('"', "\"\""))
    } else {
        value.to_string()
    }
}

fn escape(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

fn current_user() -> String {
    std::env::var("USERNAME")
        .or_else(|_| std::env::var("USER"))
        .unwrap_or_else(|_| "unknown".to_string())
}

fn current_hostname() -> String {
    if let Ok(name) = std::env::var("COMPUTERNAME") {
        return name;
    }
    if let Ok(name) = std::env::var("HOSTNAME") {
        return name;
    }
    std::fs::read_to_string("/etc/hostname")
        .map(|text| text.trim().to_string())
        .unwrap_or_else(|_| "unknown".to_string())
}

fn timestamp() -> String {
    // Local time in the same shape as Python's datetime.isoformat(timespec="seconds").
    Local::now().format("%Y-%m-%dT%H:%M:%S").to_string()
}
