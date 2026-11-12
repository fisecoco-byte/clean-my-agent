use crate::{fsutil, policy, progress::Progress};
use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use std::{
    ffi::OsStr,
    io::{Read, Write},
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};

pub fn run(command: &mut Command, timeout: Duration, progress: &Progress) -> Result<Output> {
    run_input(command, None, timeout, progress)
}
pub fn run_input(
    command: &mut Command,
    input: Option<Vec<u8>>,
    timeout: Duration,
    progress: &Progress,
) -> Result<Output> {
    const LIMIT: u64 = 64 * 1024 * 1024;
    progress.check()?;
    let _span = progress.span(&format!(
        "子进程 {}",
        Path::new(command.get_program())
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
    ));
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .with_context(|| format!("无法启动 {}", command.get_program().to_string_lossy()))?;
    if let Some(bytes) = input {
        // Written concurrently with the output readers; a child that exits early
        // closes the pipe and ends this thread.
        let mut stdin = child.stdin.take().unwrap();
        thread::spawn(move || {
            let _ = stdin.write_all(&bytes);
        });
    }
    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    let (done, finished) = mpsc::channel();
    fn reader(
        stream: impl Read + Send + 'static,
        done: mpsc::Sender<()>,
    ) -> thread::JoinHandle<std::io::Result<Vec<u8>>> {
        thread::spawn(move || {
            let mut bytes = Vec::new();
            let result = stream.take(LIMIT + 1).read_to_end(&mut bytes);
            let _ = done.send(());
            result.map(|_| bytes)
        })
    }
    let out = reader(stdout, done.clone());
    let err = reader(stderr, done);
    let kill = |child: &std::process::Child| {
        #[cfg(unix)]
        unsafe {
            libc::kill(-(child.id() as i32), libc::SIGKILL);
        }
        #[cfg(not(unix))]
        let _ = child;
    };
    let started = Instant::now();
    let mut open = 2;
    let mut pause = Duration::from_micros(50);
    let mut failure = None;
    // Block on the output pipes instead of polling the child: both close when it
    // exits. The short tick only bounds how long cancellation can go unnoticed.
    let status = loop {
        if open == 0
            && let Some(status) = child.try_wait()?
        {
            break status;
        }
        let elapsed = started.elapsed();
        if progress.check().is_err() || elapsed > timeout {
            if let Some(status) = child.try_wait()? {
                // The command is gone but a descendant still holds its output.
                failure = Some("命令输出未完成或已取消");
                kill(&child);
                break status;
            }
            failure = Some(if elapsed > timeout {
                "命令执行超时"
            } else {
                "操作已取消"
            });
            kill(&child);
            let _ = child.kill();
            break child.wait()?;
        }
        let tick = Duration::from_millis(20).min(timeout.saturating_sub(elapsed));
        if open == 0 {
            // Output closed before exit; the status follows almost immediately.
            thread::sleep(pause.min(tick));
            pause = (pause * 2).min(Duration::from_millis(2));
        } else {
            match finished.recv_timeout(tick.max(Duration::from_micros(50))) {
                Ok(()) => open -= 1,
                Err(mpsc::RecvTimeoutError::Timeout) => (),
                Err(mpsc::RecvTimeoutError::Disconnected) => open = 0,
            }
        }
    };
    if let Some(reason) = failure {
        // The killed group releases the pipes at once. A descendant may have
        // detached while retaining one; do not block UI shutdown on its output
        // reader, the pipe closes when that process exits.
        let grace = Instant::now() + Duration::from_millis(100);
        while open > 0 {
            match finished.recv_timeout(grace.saturating_duration_since(Instant::now())) {
                Ok(()) => open -= 1,
                Err(_) => bail!("{reason}"),
            }
        }
    }
    let stdout = out
        .join()
        .map_err(|_| anyhow::anyhow!("读取命令输出失败"))??;
    let stderr = err
        .join()
        .map_err(|_| anyhow::anyhow!("读取命令错误失败"))??;
    if let Some(reason) = failure {
        bail!("{reason}");
    }
    if stdout.len() as u64 > LIMIT || stderr.len() as u64 > LIMIT {
        bail!("命令输出超过读取上限");
    }
    Ok(Output {
        status,
        stdout,
        stderr,
    })
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    #[test]
    fn timeout_stops_descendants_with_inherited_pipes() {
        let start = Instant::now();
        let result = run(
            Command::new("sh").args(["-c", "sleep 30 & wait"]),
            Duration::from_millis(80),
            &Progress::default(),
        );
        assert!(result.is_err());
        assert!(start.elapsed() < Duration::from_secs(3));
    }
    #[test]
    fn cancelled_subprocess_exits_promptly() {
        let progress = Progress::default();
        let cancel = progress.clone();
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(80));
            cancel.cancel();
        });
        let result = run(
            Command::new("sh").args(["-c", "sleep 30"]),
            Duration::from_secs(60),
            &progress,
        );
        assert!(result.unwrap_err().to_string().contains("取消"));
    }
    #[test]
    fn input_is_streamed_while_output_is_read() {
        let input = vec![b'x'; 1 << 20];
        let result = run_input(
            &mut Command::new("cat"),
            Some(input.clone()),
            Duration::from_secs(30),
            &Progress::default(),
        )
        .unwrap();
        assert!(result.status.success());
        assert_eq!(result.stdout, input);
    }
    #[test]
    fn output_held_by_a_descendant_is_not_trusted() {
        let start = Instant::now();
        let result = run(
            Command::new("sh").args(["-c", "sleep 30 & echo done"]),
            Duration::from_millis(200),
            &Progress::default(),
        );
        assert!(result.unwrap_err().to_string().contains("未完成"));
        assert!(start.elapsed() < Duration::from_secs(3));
    }
    #[test]
    fn process_paths_are_not_substring_matches() {
        let process = Snapshot {
            files: vec![],
            commands: vec![(42, "tool /tmp/workspace-other".into())],
            error: None,
        };
        assert_eq!(
            process.check(Path::new("/tmp/workspace"), None)["state"],
            "idle"
        );
        let process = Snapshot {
            files: vec![],
            commands: vec![(42, "tool /private/tmp/workspace/src".into())],
            error: None,
        };
        assert_eq!(
            process.check(Path::new("/private/tmp/workspace"), None)["state"],
            "busy"
        );
    }
}
pub fn git(
    repo: &Path,
    args: impl IntoIterator<Item = impl AsRef<OsStr>>,
    progress: &Progress,
) -> Result<Output> {
    git_input(repo, args, None, progress)
}
pub fn git_input(
    repo: &Path,
    args: impl IntoIterator<Item = impl AsRef<OsStr>>,
    input: Option<Vec<u8>>,
    progress: &Progress,
) -> Result<Output> {
    let mut command = Command::new("git");
    command
        .args([
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-C",
        ])
        .arg(repo)
        .args(args);
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("GIT_") {
            command.env_remove(key);
        }
    }
    command
        .env("GIT_OPTIONAL_LOCKS", "0")
        .env("GIT_TERMINAL_PROMPT", "0");
    run_input(&mut command, input, Duration::from_secs(30), progress)
}
pub fn checked_git(
    repo: &Path,
    args: impl IntoIterator<Item = impl AsRef<OsStr>>,
    progress: &Progress,
) -> Result<Vec<u8>> {
    let result = git(repo, args, progress)?;
    if !result.status.success() {
        bail!("Git 检查失败：{}", String::from_utf8_lossy(&result.stderr));
    }
    Ok(result.stdout)
}
pub fn path_bytes(bytes: &[u8]) -> PathBuf {
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        PathBuf::from(OsStr::from_bytes(bytes))
    }
    #[cfg(windows)]
    {
        PathBuf::from(String::from_utf8_lossy(bytes).into_owned())
    }
}
pub fn git_path(repo: &Path, option: &str, progress: &Progress) -> Result<PathBuf> {
    let raw = checked_git(repo, ["rev-parse", option], progress)?;
    let path = path_bytes(raw.strip_suffix(b"\n").unwrap_or(&raw));
    fsutil::resolve(&if path.is_absolute() {
        path
    } else {
        repo.join(path)
    })
}
pub fn worktrees(repo: &Path, progress: &Progress) -> Result<Vec<PathBuf>> {
    let mut output = git(repo, ["worktree", "list", "--porcelain", "-z"], progress)?;
    let nul = output.status.success();
    if !nul {
        output = git(repo, ["worktree", "list", "--porcelain"], progress)?;
    }
    if !output.status.success() || output.stdout.is_empty() {
        bail!("无法核实 Git 登记的工作树：{}", repo.display());
    }
    let separator = if nul { 0 } else { b'\n' };
    let mut result = Vec::new();
    let mut record = Vec::new();
    let grammar = regex::Regex::new(
        r"^worktree [^\n]+\n(?:bare|HEAD [0-9a-f]{40,64}\n(?:branch refs/[^\n]+|detached))(?:\nlocked(?: [^\n]*)?)?(?:\nprunable(?: [^\n]*)?)?$",
    )?;
    for field in output
        .stdout
        .split(|c| *c == separator)
        .chain(std::iter::once(&[][..]))
    {
        if !field.is_empty() {
            record.push(field);
            continue;
        }
        if record.is_empty() {
            continue;
        }
        if !record[0].starts_with(b"worktree ") {
            bail!("无法解析 Git 工作树清单");
        }
        if !nul && !grammar.is_match(&String::from_utf8_lossy(&record.join(&b'\n'))) {
            bail!("旧版 Git 无法可靠解析工作树路径，请升级 Git");
        }
        let path = path_bytes(&record[0][9..]);
        if !path.is_absolute() {
            bail!("Git 工作树路径不是绝对路径");
        }
        if !record.iter().any(|s| *s == b"bare") {
            result.push(path);
        }
        record.clear();
    }
    Ok(result)
}

#[derive(Default)]
pub struct Snapshot {
    pub files: Vec<(u32, String, String)>,
    pub commands: Vec<(u32, String)>,
    pub error: Option<String>,
}
impl Snapshot {
    pub fn read(progress: &Progress) -> Self {
        let mut result = Self::default();
        if let Err(error) = result.collect(progress) {
            result.error = Some(format!(
                "运行状态检查失败或超时，无法确认未被使用：{error:#}"
            ));
        }
        result
    }
    fn collect(&mut self, progress: &Progress) -> Result<()> {
        if cfg!(windows) {
            bail!("Windows 当前只读盘点");
        }
        #[cfg(unix)]
        let user = unsafe { libc::geteuid() }.to_string();
        #[cfg(windows)]
        let user = String::new();
        let opened = run(
            Command::new("lsof").args(["-nP", "-F0pcfn", "-a", "-u", &user]),
            Duration::from_secs(15),
            progress,
        )?;
        if !opened.status.success() || !opened.stderr.is_empty() || opened.stdout.is_empty() {
            bail!("打开文件检查不完整");
        }
        let mut pid = None;
        let mut field = None;
        let mut resolved: std::collections::HashMap<PathBuf, String> =
            std::collections::HashMap::new();
        for raw in opened.stdout.split(|c| *c == 0) {
            let raw = raw.strip_prefix(b"\n").unwrap_or(raw);
            if raw.is_empty() {
                continue;
            }
            let value = String::from_utf8_lossy(&raw[1..]);
            match raw[0] {
                b'p' => {
                    pid = value.parse::<u32>().ok();
                    field = None;
                }
                b'c' => (),
                b'f' => field = Some(value.into_owned()),
                b'n' => {
                    if let (Some(pid), Some(field)) = (pid, &field) {
                        if value.starts_with('/') && pid != std::process::id() {
                            let bytes = &raw[1..];
                            let mut decoded = Vec::new();
                            let mut i = 0;
                            while i < bytes.len() {
                                if bytes[i] == b'\\'
                                    && i + 3 < bytes.len()
                                    && bytes[i + 1] == b'x'
                                    && let Ok(n) = u8::from_str_radix(
                                        &String::from_utf8_lossy(&bytes[i + 2..i + 4]),
                                        16,
                                    )
                                {
                                    decoded.push(n);
                                    i += 4;
                                    continue;
                                }
                                if bytes[i..].starts_with(b"\\\\") {
                                    decoded.push(b'\\');
                                    i += 2;
                                } else {
                                    decoded.push(bytes[i]);
                                    i += 1;
                                }
                            }
                            let path = path_bytes(&decoded);
                            let normalized = if let Some(cached) = resolved.get(&path) {
                                cached.clone()
                            } else {
                                let normalized =
                                    policy::fold(&fsutil::text(&fsutil::process_path(&path)?));
                                resolved.insert(path, normalized.clone());
                                normalized
                            };
                            self.files.push((pid, field.clone(), normalized));
                        }
                    } else {
                        bail!("打开文件结果格式不完整");
                    }
                }
                _ => bail!("打开文件结果格式不完整"),
            }
        }
        let commands = run(
            Command::new("ps").args(["-ww", "-axo", "pid=,command="]),
            Duration::from_secs(5),
            progress,
        )?;
        if !commands.status.success() || !commands.stderr.is_empty() || commands.stdout.is_empty() {
            bail!("进程列表检查不完整");
        }
        for line in String::from_utf8_lossy(&commands.stdout).lines() {
            let line = line.trim();
            let index = line
                .find(char::is_whitespace)
                .ok_or_else(|| anyhow::anyhow!("进程列表格式不完整"))?;
            let pid = line[..index].parse::<u32>()?;
            if pid != std::process::id() {
                self.commands
                    .push((pid, policy::fold(line[index..].trim())));
            }
        }
        Ok(())
    }
    pub fn check(&self, path: &Path, project: Option<&Path>) -> Value {
        let normalize = |p: &Path| {
            policy::fold(&fsutil::text(
                &fsutil::resolve(p).unwrap_or_else(|_| p.into()),
            ))
        };
        let tree = normalize(path);
        let project = project.map(normalize);
        for (pid, field, name) in &self.files {
            if name == &tree
                || name.starts_with(&(tree.clone() + "/"))
                || (field == "cwd"
                    && project
                        .as_ref()
                        .is_some_and(|p| name == p || name.starts_with(&(p.clone() + "/"))))
            {
                return json!({"state":"busy","reason":format!("仍被进程 {pid} 使用（工作目录或打开文件）"),"pid":pid});
            }
        }
        let target = project.as_ref().unwrap_or(&tree);
        for (pid, command) in &self.commands {
            for (index, _) in command.match_indices(target) {
                let before = command[..index].chars().next_back();
                let after = command[index + target.len()..].chars().next();
                if before.is_none_or(|c| !c.is_alphanumeric() && !"_/.-".contains(c))
                    && after.is_none_or(|c| c.is_whitespace() || "/,:;\"'".contains(c))
                {
                    return json!({"state":"busy","reason":format!("进程 {pid} 的启动参数引用此路径"),"pid":pid});
                }
            }
        }
        if let Some(error) = &self.error {
            json!({"state":"unknown","reason":error})
        } else {
            json!({"state":"idle","reason":"本次检查未发现进程引用（非原子快照）"})
        }
    }
}
