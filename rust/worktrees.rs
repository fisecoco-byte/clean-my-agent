use crate::{
    deps,
    fsutil::{self, Visit},
    inventory,
    policy::{Layout, Policy},
    process::{self, Snapshot},
    progress::Progress,
    project_types, vault,
};
use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};
fn scope(layout: &Layout, source: &str, path: &Path, policy: &Policy) -> Result<PathBuf> {
    let root = layout.roots.get(source).context("工作树来源无法核实")?;
    fsutil::absolute(path)?;
    let relative = path
        .strip_prefix(root)
        .context("工作树不在已识别的 Agent 项目范围内")?;
    if !["codex", "claude", "t3"].contains(&source)
        || relative.components().count() < 2
        || !relative
            .iter()
            .next()
            .is_some_and(|s| deps::AREAS.contains(&s.to_string_lossy().as_ref()))
    {
        bail!("工作树不在已识别的 Agent 项目范围内");
    }
    fsutil::unlinked_path(path)?;
    if !fsutil::stat(path)?.directory() {
        bail!("工作树路径含链接或不是目录");
    }
    if policy.protected(source, relative, true) {
        bail!("工作树或其子路径受策略保护");
    }
    Ok(root.clone())
}
pub fn codex_references(root: &Path, path: &Path) -> Result<Vec<Value>> {
    let mut result = Vec::new();
    let state = root.join(".codex-global-state.json");
    if fs::symlink_metadata(&state).is_ok() {
        let doc: Value = serde_json::from_str(&fsutil::regular_text(&state, 1024 * 1024)?)?;
        doc.as_object().context("Codex 工作区登记无法解析")?;
        for key in ["electron-saved-workspace-roots", "active-workspace-roots"] {
            if let Some(values) = doc.get(key) {
                for value in values.as_array().context("Codex 工作区登记格式未知")? {
                    let raw = value.as_str().context("Codex 工作区登记格式未知")?;
                    let p = Path::new(raw);
                    if !p.is_absolute() {
                        bail!("Codex 工作区登记路径不是绝对路径");
                    }
                    let p = fsutil::resolve(p)?;
                    if p.starts_with(path)
                        || (key == "active-workspace-roots" && path.starts_with(&p))
                    {
                        result.push(json!({"kind":"workspace","path":raw,"blocking":true,"title":"Codex 工作区"}));
                    }
                }
            }
        }
    }
    let Some(database) = inventory::latest_database(root)? else {
        return Ok(result);
    };
    let (db, no_wal, before) = inventory::sqlite_read(&database)?;
    let columns = inventory::columns(&db, "threads")?;
    if !["id", "cwd", "archived"]
        .iter()
        .all(|s| columns.contains(*s))
    {
        bail!("Codex 会话索引缺少工作目录信息；引用未知");
    }
    let label = if columns.contains("title") {
        "substr(title,1,4096)"
    } else {
        "NULL"
    };
    let pin = if columns.contains("is_pinned") {
        "is_pinned"
    } else {
        "0"
    };
    let mut statement = db.prepare(&format!(
        "SELECT id,cwd,archived,{pin},{label} FROM threads"
    ))?;
    let mut cursor = statement.query([])?;
    while let Some(row) = cursor.next()? {
        let cwd = row
            .get::<_, String>(1)
            .context("Codex 会话工作目录格式未知")?;
        let cwd_path = Path::new(&cwd);
        if !cwd_path.is_absolute() {
            bail!("Codex 会话工作目录格式未知");
        }
        if fsutil::resolve(cwd_path)?.starts_with(path) {
            let id = row.get::<_, String>(0).context("Codex 会话 ID 格式未知")?;
            let archived = row.get::<_, i64>(2).ok();
            let pinned = match row.get_ref(3)? {
                rusqlite::types::ValueRef::Null => false,
                rusqlite::types::ValueRef::Integer(n) => n != 0,
                rusqlite::types::ValueRef::Real(n) => n != 0.0,
                rusqlite::types::ValueRef::Text(s) => !s.is_empty(),
                _ => true,
            };
            let title = row
                .get::<_, String>(4)
                .ok()
                .filter(|s| !s.is_empty())
                .unwrap_or_else(|| id.clone());
            result.push(json!({"kind":"session","id":id,"path":cwd,"title":inventory::label(Some(&title)),"blocking":archived!=Some(1)||pinned}));
        }
    }
    drop(cursor);
    drop(statement);
    drop(db);
    inventory::sqlite_unchanged(&database, no_wal, &before)?;
    Ok(result)
}
fn ignored_artifacts(path: &Path, root: &Path, progress: &Progress) -> Result<Vec<String>> {
    let raw = process::checked_git(
        path,
        [
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "-z",
        ],
        progress,
    )?;
    let entries: Vec<_> = raw
        .split(|c| *c == 0)
        .filter(|r| !r.is_empty())
        .map(|r| std::str::from_utf8(r).map(|s| s.trim_end_matches('/').to_string()))
        .collect::<std::result::Result<_, _>>()?;
    for (index, name) in entries.iter().enumerate() {
        progress.update("核对忽略文件", Some(index), Some(entries.len()), name)?;
        let rel = Path::new(name);
        if rel.is_absolute()
            || rel
                .components()
                .any(|p| p == std::path::Component::ParentDir)
        {
            bail!("忽略路径无法核实");
        }
        let p = path.join(rel);
        let info = fsutil::stat(&p)?;
        if info.symlink()
            && p.file_name()
                .is_some_and(|n| project_types::TARGETS.contains(&n.to_string_lossy().as_ref()))
        {
            continue;
        }
        if !info.directory() || !project_types::target(&p) {
            bail!("存在不能自动丢弃的忽略文件：{name}");
        }
        deps::verify(root, &p, progress)?;
        let (_, project) = deps::project_context(&p, progress)?;
        project_types::require_evidence(&p, &p, &project, progress)?;
    }
    Ok(entries)
}
fn object_references(
    repositories: &BTreeSet<PathBuf>,
    path: &Path,
    progress: &Progress,
) -> Result<BTreeSet<PathBuf>> {
    let mut references = BTreeSet::new();
    for repo in repositories {
        if repo.starts_with(path) {
            continue;
        }
        let common = process::git_path(repo, "--git-common-dir", progress)?;
        let objects = fsutil::resolve(&common.join("objects"))?;
        if !objects.is_dir() {
            bail!("Git 对象目录无法核实");
        }
        if common.starts_with(path) || objects.starts_with(path) {
            references.insert(repo.clone());
        }
        let alternates = objects.join("info/alternates");
        if fs::symlink_metadata(&alternates).is_ok() {
            for line in fsutil::regular_text(&alternates, 1024 * 1024)?
                .lines()
                .filter(|s| !s.is_empty())
            {
                if line.starts_with('"') {
                    bail!(
                        "Git 共享对象路径含转义，无法完整核实引用：{}",
                        repo.display()
                    );
                }
                let p = Path::new(line);
                let target = fsutil::resolve(&if p.is_absolute() {
                    p.into()
                } else {
                    objects.join(p)
                })?;
                if target.starts_with(path) {
                    references.insert(repo.clone());
                }
            }
        }
    }
    Ok(references)
}
fn check<T>(result: &mut Value, label: &str, outcome: Result<T>) -> Option<T> {
    match outcome {
        Ok(value) => {
            result["checks"][label] = json!("通过");
            Some(value)
        }
        Err(e) => {
            result["checks"][label] = json!(e.to_string());
            result["reasons"]
                .as_array_mut()
                .unwrap()
                .push(json!(e.to_string()));
            None
        }
    }
}
pub fn inspect(
    layout: &Layout,
    policy: &Policy,
    selected: &Value,
    progress: &Progress,
) -> Result<Value> {
    let path = Path::new(selected["path"].as_str().context("请选择工作目录")?);
    let source = selected["agent"].as_str().context("工作目录缺少来源")?;
    let mut result = json!({"path":path,"agent":source,"directory_bytes":selected["directory_bytes"],"activity":selected["activity"],"can_delete":false,"can_trash":false,"repository_kind":null,"reasons":[],"checks":{},"referenced_by":[],"ignored":[],"changes":[],"native_command":null,"token":null});
    if cfg!(windows) {
        result["reasons"]
            .as_array_mut()
            .unwrap()
            .push(json!("Windows 当前只读盘点"));
        return Ok(result);
    }
    progress.update("工作树身份", None, None, fsutil::text(path))?;
    let identity = (|| -> Result<PathBuf> {
        let root = scope(layout, source, path, policy)?;
        let info = fsutil::stat(path)?;
        if let Some(stamp) = selected["stamp"].as_array()
            && (stamp.first().and_then(Value::as_u64) != Some(info.dev)
                || stamp.get(1).and_then(Value::as_u64) != Some(info.ino))
        {
            bail!("工作树已被替换，请重新盘点");
        }
        if process::git_path(path, "--show-toplevel", progress)? != path {
            bail!("该路径不是仓库根目录");
        }
        let pointer = path.join(".git");
        let ptr = fsutil::stat(&pointer)?;
        if !ptr.regular() && !ptr.directory() {
            bail!("Git 元数据路径异常，保留");
        }
        let common = process::git_path(path, "--git-common-dir", progress)?;
        let gitdir = process::git_path(path, "--git-dir", progress)?;
        if ptr.directory() {
            result["repository_kind"] = json!("standalone");
            if common != pointer || gitdir != pointer {
                bail!("仓库依赖外部 Git 元数据，保留");
            }
            if process::worktrees(path, progress)?
                .into_iter()
                .collect::<BTreeSet<_>>()
                != BTreeSet::from([path.to_path_buf()])
            {
                bail!("该仓库仍管理其他附加工作树，请先处理这些工作树");
            }
            if !trash_available() {
                bail!("独立仓库移入废纸篓目前仅支持 macOS");
            }
        } else {
            result["repository_kind"] = json!("linked_worktree");
            if gitdir == common
                || gitdir.parent() != Some(&common.join("worktrees"))
                || common.starts_with(path)
            {
                bail!("不是标准附加 worktree，或仓库元数据位于目标内");
            }
            if !process::worktrees(path, progress)?.contains(&path.to_path_buf()) {
                bail!("Git 未登记此工作树");
            }
        }
        if fs::symlink_metadata(gitdir.join("locked")).is_ok() {
            bail!("Git 工作树已锁定，保留");
        }
        for name in [
            "index.lock",
            "HEAD.lock",
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "rebase-merge",
            "rebase-apply",
            "sequencer",
            "BISECT_START",
        ] {
            if fs::symlink_metadata(gitdir.join(name)).is_ok() {
                bail!("工作树有未完成的 Git 操作：{name}");
            }
        }
        let head = String::from_utf8(process::checked_git(
            path,
            ["rev-parse", "--verify", "HEAD"],
            progress,
        )?)?
        .trim()
        .to_string();
        let branch = process::git(path, ["symbolic-ref", "--quiet", "HEAD"], progress)?;
        if ![Some(0), Some(1)].contains(&branch.status.code()) {
            bail!("无法核实工作树分支");
        }
        let branch = if branch.status.success() {
            Some(String::from_utf8(branch.stdout)?.trim().to_string())
        } else {
            None
        };
        if process::checked_git(
            path,
            [
                "for-each-ref",
                "--contains=HEAD",
                "--format=%(refname)",
                "refs/heads",
                "refs/remotes",
                "refs/tags",
            ],
            progress,
        )?
        .is_empty()
        {
            bail!("HEAD 提交没有被分支或标签保留；请先保存提交");
        }
        result["stamp"] = info.stamp();
        result["head"] = json!(head);
        result["branch"] = json!(branch);
        result["common_dir"] = json!(common);
        result["git_dir"] = json!(gitdir);
        if result["repository_kind"] == "linked_worktree" {
            result["native_command"] = json!([
                "git",
                "-C",
                fsutil::text(&common),
                "worktree",
                "remove",
                "--",
                fsutil::text(path)
            ]);
        }
        Ok(root)
    })();
    let Some(root) = check(&mut result, "工作树身份", identity) else {
        progress.check()?;
        return Ok(result);
    };
    progress.update("未提交改动", None, None, fsutil::text(path))?;
    let changes = (|| -> Result<()> {
        let raw = process::checked_git(
            path,
            [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ],
            progress,
        )?;
        result["changes"] = json!(
            raw.split(|c| *c == 0)
                .filter(|s| !s.is_empty())
                .map(|s| String::from_utf8_lossy(s).into_owned())
                .collect::<Vec<_>>()
        );
        if !raw.is_empty() {
            bail!("存在未提交改动或未跟踪文件");
        }
        let staged = process::checked_git(path, ["ls-files", "--stage", "-z"], progress)?;
        if staged.split(|c| *c == 0).any(|p| p.starts_with(b"160000 ")) {
            bail!("包含子模块，保留");
        }
        Ok(())
    })();
    check(&mut result, "未提交改动", changes);
    progress.update("运行状态", None, None, fsutil::text(path))?;
    let runtime = (|| -> Result<()> {
        if fsutil::resolve(&std::env::current_dir()?)?.starts_with(path) {
            bail!("当前工具正在该工作树内运行");
        }
        let runtime = Snapshot::read(progress).check(path, Some(path));
        result["runtime"] = runtime.clone();
        if runtime["state"] != "idle" {
            bail!("{}", runtime["reason"].as_str().unwrap_or("运行状态未知"));
        }
        Ok(())
    })();
    check(&mut result, "运行状态", runtime);
    progress.update("引用关系", None, None, fsutil::text(path))?;
    let references = (|| -> Result<()> {
        let mut refs = BTreeSet::new();
        for agent in ["codex", "claude", "t3"] {
            let index = deps::References::read(&layout.roots[agent], true, progress)?;
            if let Some(error) = index.errors.first() {
                bail!("工作树引用检查不完整：{error}");
            }
            refs.extend(index.incoming(path));
            refs.extend(
                index
                    .targets
                    .iter()
                    .filter(|(p, target)| path.starts_with(target) && !p.starts_with(path))
                    .map(|(p, _)| p.clone()),
            );
            if result["repository_kind"] == "standalone" {
                refs.extend(object_references(&index.repositories, path, progress)?);
            }
        }
        result["referenced_by"] = json!(refs);
        let agent_refs = codex_references(&layout.roots["codex"], path)?;
        result["agent_references"] = json!(agent_refs);
        if !refs.is_empty() || agent_refs.iter().any(|r| r["blocking"] == true) {
            bail!("仍被其他工作树、Codex 工作区或未归档会话引用");
        }
        for key in vault::batches(layout)? {
            let batch = vault::process(layout, &key, "list", false, None, None, progress)?;
            if !batch.errors.is_empty() {
                bail!("暂存区引用无法完整核实");
            }
            if batch.detail["entries"]
                .as_array()
                .unwrap()
                .iter()
                .filter_map(|r| r["path"].as_str())
                .any(|p| Path::new(p).starts_with(path))
            {
                bail!("仍有待恢复或清除的产物批次：{key}");
            }
        }
        Ok(())
    })();
    check(&mut result, "引用关系", references);
    progress.update("忽略文件与目录", None, None, fsutil::text(path))?;
    let ignored = (|| -> Result<String> {
        deps::check_mounts(path)?;
        result["ignored"] = json!(ignored_artifacts(path, &root, progress)?);
        fsutil::walk(path, progress, &mut |relative, _, name, _| {
            if name == ".git" {
                if relative.components().count() > 1 {
                    bail!("包含嵌套仓库，保留");
                }
                return Ok(Visit::Skip);
            }
            Ok(Visit::Descend)
        })?;
        project_types::tree_digest(path, progress)
    })();
    let digest = check(&mut result, "忽略文件与目录", ignored);
    progress.check()?;
    if result["reasons"].as_array().unwrap().is_empty() {
        let mut proof = vec![];
        for key in [
            "path",
            "agent",
            "stamp",
            "head",
            "branch",
            "common_dir",
            "git_dir",
            "repository_kind",
        ] {
            proof.push(result[key].clone());
        }
        proof.push(json!(digest));
        result["token"] = json!(format!("{:x}", Sha256::digest(serde_json::to_vec(&proof)?)));
        let key = if result["repository_kind"] == "standalone" {
            "can_trash"
        } else {
            "can_delete"
        };
        result[key] = json!(true);
    }
    Ok(result)
}
pub fn trash_available() -> bool {
    cfg!(target_os = "macos") && Path::new("/usr/bin/osascript").is_file()
}
const TRASH_SCRIPT: &str = r#"use framework "Foundation"
on run argv
    set sourceURL to current application's NSURL's fileURLWithPath:(item 1 of argv)
    set manager to current application's NSFileManager's defaultManager()
    set {ok, destination, failure} to manager's trashItemAtURL:sourceURL resultingItemURL:(reference) |error|:(reference)
    if not ok then error (failure's localizedDescription() as text)
    return destination's |path|() as text
end run"#;
pub fn system_trash(path: &Path, progress: &Progress) -> Result<PathBuf> {
    if !trash_available() {
        bail!("独立仓库移入废纸篓目前仅支持 macOS");
    }
    fsutil::unlinked_path(path)?;
    #[cfg(unix)]
    let before = fsutil::stat(path)?;
    let result = process::run(
        Command::new("/usr/bin/osascript")
            .args(["-l", "AppleScript", "-e", TRASH_SCRIPT])
            .arg(path),
        Duration::from_secs(120),
        progress,
    )?;
    if !result.status.success() {
        bail!(
            "{}",
            String::from_utf8_lossy(&result.stderr)
                .chars()
                .rev()
                .take(2000)
                .collect::<String>()
                .chars()
                .rev()
                .collect::<String>()
        );
    }
    let raw = result.stdout.strip_suffix(b"\n").unwrap_or(&result.stdout);
    let destination = process::path_bytes(raw);
    if !destination.is_absolute() || destination == path || fs::symlink_metadata(path).is_ok() {
        bail!("未能核实废纸篓中的目录位置");
    }
    #[cfg(unix)]
    {
        // macOS allows lstat of the returned item even where opening the Trash
        // directory is denied. The native URL is used only for identity verification.
        use std::os::unix::fs::MetadataExt;
        let after = fs::symlink_metadata(&destination)?;
        if !after.is_dir() || (after.dev(), after.ino()) != before.identity() {
            bail!("废纸篓中的目录身份无法核实");
        }
    }
    Ok(destination)
}
pub fn apply(
    layout: &Layout,
    policy: &Policy,
    expected: &Value,
    to_trash: bool,
    progress: &Progress,
) -> Result<Value> {
    let capability = if to_trash { "can_trash" } else { "can_delete" };
    if expected[capability] != true || !expected["token"].is_string() {
        bail!("请先核对一个可清理的工作目录");
    }
    policy.current()?;
    let current = inspect(layout, policy, expected, progress)?;
    if current[capability] != true || current["token"] != expected["token"] {
        bail!(
            "工作目录状态已变化，未执行：{}",
            current["reasons"]
                .as_array()
                .unwrap()
                .iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join("；")
        );
    }
    policy.current()?;
    let path = Path::new(current["path"].as_str().unwrap());
    if fsutil::stat(path)?.stamp() != current["stamp"] {
        bail!("工作目录在执行前发生变化，未执行");
    }
    if to_trash {
        progress.update("移入系统废纸篓", None, None, fsutil::text(path))?;
        return Ok(match system_trash(path, progress) {
            Ok(destination) => {
                json!({"status":"succeeded","path":path,"trash_path":destination,"message":format!("已移入系统废纸篓，尚未释放空间。\n仓库文件、分支和 Git 历史均保留在：{}\n清空废纸篓前，可将整个目录移回原路径：{}",destination.display(),path.display())})
            }
            Err(e) => {
                json!({"status":if fs::symlink_metadata(path).is_ok(){"failed"}else{"uncertain"},"path":path,"message":format!("移入废纸篓未确认：{e}；请检查原路径和系统废纸篓，不会改为永久删除")})
            }
        });
    }
    let common = Path::new(current["common_dir"].as_str().unwrap());
    progress.update("通过 Git 删除工作树", None, None, fsutil::text(path))?;
    let command = process::git(
        common,
        [
            "worktree".as_ref(),
            "remove".as_ref(),
            "--".as_ref(),
            path.as_os_str(),
        ],
        progress,
    );
    let gone = fs::symlink_metadata(path).is_err_and(|e| e.kind() == std::io::ErrorKind::NotFound)
        && process::worktrees(common, progress)
            .is_ok_and(|rows| !rows.contains(&path.to_path_buf()));
    Ok(match command {
        Ok(command) if command.status.success() && gone => {
            json!({"status":"succeeded","path":path,"message":"已通过 Git 删除工作树；分支和会话记录保留"})
        }
        Ok(command) => {
            json!({"status":if path.exists(){"failed"}else{"uncertain"},"path":path,"message":if command.stderr.is_empty(){"删除结果未确认，请重新盘点".into()}else{String::from_utf8_lossy(&command.stderr).chars().rev().take(2000).collect::<String>().chars().rev().collect::<String>()}})
        }
        Err(e) => {
            json!({"status":"uncertain","path":path,"message":format!("Git 删除未确认：{e}；请重新盘点，勿直接重试")})
        }
    })
}
