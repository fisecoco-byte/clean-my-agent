use crate::{
    fsutil::{self, Visit},
    policy::{Layout, Policy},
    progress::Progress,
};
use anyhow::Result;
use regex::Regex;
use serde_json::{Value, json};
use std::{collections::BTreeMap, path::Path, sync::LazyLock};

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
            // One native invocation preserves hard-link accounting across top-level trees.
            progress.update("统计目录占用", Some(0), Some(folded.len()), source)?;
            let run = crate::process::run(
                std::process::Command::new("du")
                    .args(["-k", "-s", "-x"])
                    .args(&folded),
                std::time::Duration::from_secs(300),
                progress,
            );
            match run {
                Ok(output) => {
                    let mut done = 0;
                    for line in output.stdout.split(|c| *c == b'\n') {
                        if let Some(tab) = line.iter().position(|c| *c == b'\t') {
                            let amount = std::str::from_utf8(&line[..tab])
                                .ok()
                                .and_then(|s| s.parse::<u64>().ok());
                            let path = String::from_utf8_lossy(&line[tab + 1..]);
                            if let (Some(amount), Some(folder)) =
                                (amount, folded.iter().find(|p| p.to_string_lossy() == path))
                            {
                                let size = amount * 1024;
                                out.folders.insert(path.into_owned(), size);
                                let category = classify(
                                    source,
                                    &Path::new(folder.file_name().unwrap()).join("_"),
                                );
                                out.rows.entry((agent.into(), category.into())).or_default()[1] +=
                                    size;
                                done += 1;
                                progress.update(
                                    "统计目录占用",
                                    Some(done),
                                    Some(folded.len()),
                                    source,
                                )?;
                            }
                        }
                    }
                    if !output.status.success() || done != folded.len() {
                        out.errors.push(format!(
                            "{} 有目录无法完整统计；结果为部分占用",
                            root.display()
                        ));
                    }
                }
                Err(e) => out.errors.push(e.to_string()),
            }
        }
    }
    Ok(out)
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
