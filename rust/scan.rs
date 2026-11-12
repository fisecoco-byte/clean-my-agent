use crate::{
    fsutil::{self, Visit},
    policy::{Layout, Policy},
    progress::Progress,
};
use anyhow::Result;
use regex::Regex;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    sync::LazyLock,
};

static LOG: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^.+\.log(?:\.\d+(?:\.gz)?)?$").unwrap());
static ARCHIVE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^rollout-[\w.-]+\.jsonl(?:\.zst)?$").unwrap());
static DB: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r".*\.(sqlite3?|db)(?:-(wal|shm|journal))?$").unwrap());
pub fn classify<'a>(source: &str, rel: &Path) -> &'a str {
    let parts: Vec<_> = rel.iter().map(|p| p.to_string_lossy()).collect();
    if parts.is_empty() {
        return "other";
    }
    let top = parts[0].as_ref();
    let name = parts.last().unwrap().as_ref();
    let len = parts.len();
    if ["codex", "claude", "t3"].contains(&source) && top == "worktrees" {
        return "worktrees";
    }
    if source == "claude-cli" {
        return if len >= 3
            && parts[..len - 1].iter().any(|p| p.starts_with("mcp-logs-"))
            && name.ends_with(".jsonl")
        {
            "logs"
        } else {
            "other"
        };
    }
    if top == "cache" {
        if source == "claude"
            && len == 2
            && ["changelog.md", "my-closed-issues.json"].contains(&name)
        {
            return "cache";
        }
        if source == "codex"
            && len == 3
            && name.ends_with(".json")
            && [
                "codex_apps_tools",
                "remote_plugin_catalog",
                "bundled_plugin_exclusions",
                "codex_app_directory",
                "codex_apps_server_info",
            ]
            .contains(&parts[1].as_ref())
        {
            return "cache";
        }
    }
    if source == "codex" {
        if len == 2 && top == "log" && LOG.is_match(name) {
            return "logs";
        }
        if len == 2
            && top == "shell_snapshots"
            && [".sh", ".zsh", ".bash", ".fish"]
                .iter()
                .any(|s| name.ends_with(s))
        {
            return "snapshots";
        }
        if len == 2 && top == "archived_sessions" && ARCHIVE.is_match(name) {
            return "archives";
        }
        if top == ".chatgpt-projects" {
            return "codex-projects";
        }
        if ["tmp", ".tmp"].contains(&top) {
            return "temporary";
        }
    }
    if source == "claude" {
        if len == 2 && top == "debug" && (name.ends_with(".txt") || name.ends_with(".log")) {
            return "logs";
        }
        if len == 2
            && top == "shell-snapshots"
            && [".sh", ".zsh", ".bash", ".fish"]
                .iter()
                .any(|s| name.ends_with(s))
        {
            return "snapshots";
        }
        if len == 5
            && top == "projects"
            && parts[len - 2] == "tool-results"
            && (name.ends_with(".txt") || name.ends_with(".json"))
        {
            return "tool-results";
        }
    }
    if [
        "sessions",
        "projects",
        "archived_sessions",
        "history.jsonl",
        "file-history",
    ]
    .contains(&top)
    {
        return "sessions";
    }
    if DB.is_match(name) {
        return "database";
    }
    if ["plugins", "skills"].contains(&top) {
        return "plugins";
    }
    "other"
}
#[derive(Default)]
pub struct Scan {
    pub rows: indexmap::IndexMap<(String, String), [u64; 4]>,
    pub items: Vec<Value>,
    pub errors: Vec<String>,
    pub missing: Vec<String>,
    pub deferred: Vec<String>,
    pub folders: BTreeMap<String, u64>,
    pub kept: BTreeMap<String, [u64; 2]>,
}
pub fn scan(
    layout: &Layout,
    agents: &[String],
    policy: &Policy,
    full: bool,
    progress: &Progress,
) -> Result<Scan> {
    let mut out = Scan::default();
    let now = fsutil::now_ns();
    for source in ["codex", "claude", "t3", "claude-cli"] {
        let agent = if source == "claude-cli" {
            "claude"
        } else {
            source
        };
        if !agents.iter().any(|a| a == agent) {
            continue;
        }
        let root = &layout.roots[source];
        progress.update("读取文件", None, None, source)?;
        if let Err(e) = fsutil::unlinked_path(root) {
            out.errors.push(e.to_string());
            continue;
        }
        if !root.exists() {
            out.missing.push(fsutil::text(root));
            continue;
        }
        let initial = match fsutil::stat(root) {
            Ok(s) if s.directory() => s,
            other => {
                out.errors
                    .push(format!("无法读取目录 {}：{other:?}", root.display()));
                continue;
            }
        };
        let quick = match source {
            "codex" => Some(
                &[
                    "cache",
                    "log",
                    "shell_snapshots",
                    "archived_sessions",
                    "sessions",
                ][..],
            ),
            "claude" => Some(&["cache", "debug", "shell-snapshots", "projects"][..]),
            _ => None,
        };
        let mut folded = Vec::new();
        let result = fsutil::walk(root, progress, &mut |rel, _, _, s| {
            if s.dev != initial.dev {
                return Ok(Visit::Skip);
            }
            if s.directory()
                && rel.components().count() == 1
                && quick.is_some_and(|allowed| !allowed.contains(&rel.to_string_lossy().as_ref()))
            {
                if full {
                    folded.push(root.join(rel));
                } else {
                    out.deferred.push(fsutil::text(&root.join(rel)));
                }
                return Ok(Visit::Skip);
            }
            if !s.regular() {
                return Ok(Visit::Descend);
            }
            let category = classify(source, rel);
            let row = out.rows.entry((agent.into(), category.into())).or_default();
            let group = if rel.components().count() > 1 {
                root.join(rel.components().next().unwrap().as_os_str())
            } else {
                root.join("(根目录文件)")
            };
            *out.folders.entry(fsutil::text(&group)).or_default() += s.allocated;
            row[0] += 1;
            row[1] += s.allocated;
            if let Some(reason) = policy.explain(source, rel, category, s, now) {
                let kept = out.kept.entry(reason.into()).or_default();
                kept[0] += 1;
                kept[1] += s.allocated;
            } else {
                row[2] += 1;
                row[3] += s.allocated;
                out.items.push(json!({"source":source,"root":root,"path":rel,"category":category,"size":s.allocated,"stamp":s.stamp()}));
            }
            Ok(Visit::Descend)
        });
        if let Err(e) = result {
            progress.check()?;
            out.errors.push(e.to_string());
        }
        #[cfg(windows)]
        if !folded.is_empty() {
            let mut seen = std::collections::BTreeSet::new();
            for folder in &folded {
                progress.update("统计目录占用", None, None, fsutil::text(folder))?;
                let mut size = 0;
                let measured = fsutil::walk(folder, progress, &mut |_, _, _, info| {
                    if info.dev != initial.dev {
                        return Ok(Visit::Skip);
                    }
                    if info.regular() && (info.links < 2 || seen.insert(info.identity())) {
                        size += info.allocated;
                    }
                    Ok(Visit::Descend)
                });
                if let Err(e) = measured {
                    progress.check()?;
                    out.errors.push(e.to_string());
                }
                out.folders.insert(fsutil::text(folder), size);
                let category = classify(source, &Path::new(folder.file_name().unwrap()).join("_"));
                out.rows.entry((agent.into(), category.into())).or_default()[1] += size;
            }
        }
        #[cfg(unix)]
        if !folded.is_empty() {
            let (sizes, failed) = folded_sizes(&folded, progress, source)?;
            if failed {
                out.errors.push(format!(
                    "{} 有目录无法完整统计；结果为部分占用",
                    root.display()
                ));
            }
            for (folder, size) in sizes {
                let key = fsutil::text(&folder);
                let category = classify(source, &Path::new(folder.file_name().unwrap()).join("_"));
                out.rows.entry((agent.into(), category.into())).or_default()[1] += size;
                out.folders.insert(key, size);
            }
        }
    }
    Ok(out)
}
/// Sizes of the top-level directories skipped by the walk, in the manner of one
/// `du -k -s -x` run: a file hard-linked into two of them counts under the first
/// one reached. Allocation blocks and device bounds are the same as `du`'s, so
/// the totals agree. A directory that cannot be read completely reports what was
/// seen and sets `incomplete`; only cancellation is an error.
#[cfg(unix)]
fn folded_sizes(
    folded: &[PathBuf],
    progress: &Progress,
    source: &str,
) -> Result<(BTreeMap<PathBuf, u64>, bool)> {
    let mut links = std::collections::BTreeSet::new();
    let mut sizes = BTreeMap::new();
    let mut incomplete = false;
    for (index, folder) in folded.iter().enumerate() {
        progress.update("统计目录占用", Some(index), Some(folded.len()), source)?;
        let _span = progress.span("折叠目录占用");
        let initial = match fsutil::stat(folder) {
            Ok(s) => s,
            Err(_) => {
                incomplete = true;
                continue;
            }
        };
        let mut bytes = initial.allocated;
        if let Ok(dir) = fsutil::Directory::open(folder) {
            if let Err(e) = tally(&dir, initial.dev, progress, &mut bytes, &mut links, 0) {
                progress.check()?;
                let _ = e;
                incomplete = true;
            }
        } else {
            incomplete = true;
        }
        sizes.insert(folder.clone(), bytes.div_ceil(1024) * 1024);
    }
    Ok((sizes, incomplete))
}
/// The same entries `walk` visits, read with one directory listing per folder so
/// a file's metadata does not cost a separate system call. Directories are still
/// opened without following links and checked against the device bound.
#[cfg(unix)]
fn tally(
    dir: &fsutil::Directory,
    root_dev: u64,
    progress: &Progress,
    bytes: &mut u64,
    links: &mut std::collections::BTreeSet<(u64, u64)>,
    depth: usize,
) -> Result<()> {
    if depth > 512 {
        anyhow::bail!("目录层级过深");
    }
    for (name, kind, info) in dir.listing()? {
        progress.check()?;
        if kind == fsutil::Kind::Directory {
            let child = dir.child(&name)?;
            let own = child.info()?;
            if own.dev != root_dev {
                continue;
            }
            if !own.directory() {
                anyhow::bail!("遍历期间目录已替换");
            }
            *bytes += own.allocated;
            tally(&child, root_dev, progress, bytes, links, depth + 1)?;
            continue;
        }
        let info = match info {
            Some(info) => info,
            None => dir.stat(&name)?,
        };
        if info.dev != root_dev || (info.links > 1 && !links.insert(info.identity())) {
            continue;
        }
        *bytes += info.allocated;
    }
    Ok(())
}
impl Scan {
    pub fn groups(&self) -> Vec<Value> {
        let mut rows:Vec<_>=self.rows.iter().map(|((agent,category),r)|json!({"agent":agent,"category":category,"bytes":r[1],"eligible_files":r[2],"eligible_bytes":r[3]})).collect();
        rows.sort_by(|a, b| {
            a["agent"]
                .as_str()
                .cmp(&b["agent"].as_str())
                .then(b["bytes"].as_u64().cmp(&a["bytes"].as_u64()))
        });
        rows
    }
    pub fn report(&self, policy: &Policy, full: bool, days: Option<i64>, details: bool) -> Value {
        let groups = self.groups();
        let bytes: u64 = groups
            .iter()
            .map(|r| r["bytes"].as_u64().unwrap_or(0))
            .sum();
        let eligible: u64 = self
            .items
            .iter()
            .map(|r| r["size"].as_u64().unwrap_or(0))
            .sum();
        let kept: serde_json::Map<String, Value> = self
            .kept
            .iter()
            .map(|(k, v)| (k.clone(), json!({"files":v[0],"bytes":v[1]})))
            .collect();
        let mut result = json!({"mode":"preview","full":full,"days":days,"bytes":bytes,"eligible_bytes":eligible,"eligible_files":self.items.len(),"groups":groups,"errors":self.errors,"missing_roots":self.missing,"deferred_dirs":self.deferred.len(),"policy":policy.describe(),"kept":kept});
        if details {
            result["files"]=json!(self.items.iter().map(|i|json!({"path":Path::new(i["root"].as_str().unwrap()).join(i["path"].as_str().unwrap()),"bytes":i["size"],"category":i["category"],"retention_days":policy.retention[i["category"].as_str().unwrap()]})).collect::<Vec<_>>());
        }
        if full {
            let mut folders: Vec<_> = self.folders.iter().collect();
            folders.sort_by(|(a, x), (b, y)| y.cmp(x).then(a.cmp(b)));
            result["largest_groups"] = json!(
                folders
                    .into_iter()
                    .take(10)
                    .map(|(p, n)| json!({"path":p,"bytes":n}))
                    .collect::<Vec<_>>()
            );
        }
        result
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::symlink};
    /// The same directories in one `du -k -s -x` run: a hard link shared by two
    /// of them is accounted once, by whichever comes first.
    #[test]
    fn folded_sizes_match_one_native_du_run() {
        let tmp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(tmp.path()).unwrap();
        let (a, b) = (root.join("a"), root.join("b"));
        fs::create_dir_all(a.join("sub")).unwrap();
        fs::create_dir_all(&b).unwrap();
        fs::write(a.join("file"), vec![b'x'; 12345]).unwrap();
        fs::hard_link(a.join("file"), b.join("alias")).unwrap();
        fs::hard_link(a.join("file"), a.join("sub/third")).unwrap();
        fs::write(b.join("other"), vec![b'y'; 9000]).unwrap();
        fs::File::create(a.join("sparse"))
            .unwrap()
            .set_len(1 << 22)
            .unwrap();
        symlink("/etc/passwd", a.join("link")).unwrap();
        fs::create_dir(root.join("empty")).unwrap();
        let folded = vec![a.clone(), b.clone(), root.join("empty")];
        let progress = Progress::default().timed();
        let (sizes, incomplete) = folded_sizes(&folded, &progress, "codex").unwrap();
        assert!(!incomplete);
        let output = std::process::Command::new("du")
            .args(["-k", "-s", "-x"])
            .args(&folded)
            .output()
            .unwrap();
        assert!(output.status.success());
        let native: BTreeMap<PathBuf, u64> = String::from_utf8(output.stdout)
            .unwrap()
            .lines()
            .map(|line| {
                let (amount, path) = line.split_once('\t').unwrap();
                (
                    root.join(
                        Path::new(path)
                            .strip_prefix(&root)
                            .expect("du reports the paths it was given"),
                    ),
                    amount.parse::<u64>().unwrap() * 1024,
                )
            })
            .collect();
        assert_eq!(sizes, native);
        // Cancellation is reported, not treated as an unreadable directory.
        let cancelled = Progress::default();
        cancelled.cancel();
        assert!(folded_sizes(&folded, &cancelled, "codex").is_err());
    }
}
