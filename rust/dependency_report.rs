use crate::{
    activity,
    deps::{self, References},
    fsutil,
    inventory::Record,
    policy::{DAY_NS, Layout, Policy},
    process::Snapshot,
    progress::Progress,
    project_types::Evidence,
    storage::{self, DirectoryUsage},
};
use anyhow::{Result, bail};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
};
pub const FIELDS: &[&str] = &[
    "directory_bytes",
    "shared_bytes",
    "known_shared_bytes",
    "unknown_bytes",
    "reclaimable_bytes",
    "reclaimable_min_bytes",
    "reclaimable_max_bytes",
];
pub fn empty(full: bool) -> Value {
    json!({"mode":if full{"measured"}else{"references_only"},"entries":[],"projects":[],"errors":[],"external_targets":{},"title":"工作树 / 多语言产物","scope":"项目完整占用另列；产物子项不重复相加"})
}
fn retained(row: &mut Value, reason: impl Into<String>) {
    row["status"] = json!("retained");
    row["reason"] = json!(reason.into());
}
#[allow(clippy::too_many_arguments)] // Explicit scan context shared by CLI, TUI and MCP.
pub fn inspect(
    root: &Path,
    policy: &Policy,
    full: bool,
    only: Option<&Path>,
    source: &str,
    records: &(Vec<Record>, Vec<String>),
    annotate: bool,
    references: Option<References>,
    progress: &Progress,
) -> Result<Value> {
    let mut refs = match references {
        Some(r) => r,
        None => References::read(root, false, progress)?,
    };
    if let Some(path) = only
        && (!deps::valid_location(root, path) || !refs.paths.contains(path))
    {
        bail!("请选择盘点返回的项目产物绝对路径");
    }
    let mut errors = refs.errors.clone();
    let mut rows = Vec::new();
    let mut identities = BTreeMap::new();
    let mut measured = BTreeMap::new();
    let mut processes = None;
    let cutoff =
        fsutil::now_ns().saturating_sub(policy.retention["dependencies"].saturating_mul(DAY_NS));
    let paths = only
        .map(|p| BTreeSet::from([p.to_path_buf()]))
        .unwrap_or_else(|| refs.paths.clone());
    for (index, path) in paths.iter().enumerate() {
        progress.update(
            if full {
                "测量依赖目录"
            } else {
                "检查依赖目录"
            },
            Some(index),
            Some(paths.len()),
            fsutil::text(path),
        )?;
        let mut row = json!({"path":path,"kind":"directory","status":"unmeasured","reason":null,"referenced_by":[],"target":null,"installation":null,"runtime":null,"agent":source,"artifact":path.file_name().unwrap_or_default().to_string_lossy(),"project":path.parent()});
        for field in FIELDS {
            row[*field] = Value::Null;
        }
        let collected = (|| -> Result<()> {
            row["referenced_by"] = json!(refs.incoming(path));
            let s = fsutil::stat(path)?;
            row["stamp"] = s.stamp();
            identities.insert(path.clone(), s.identity());
            if s.symlink() {
                row["kind"] = json!("symlink");
                retained(&mut row, "仅链接，不跟随或清理");
                row["target"] = json!(refs.targets.get(path));
                row["directory_bytes"] = json!(s.allocated);
            } else if !s.directory() {
                retained(&mut row, "不是依赖目录");
            } else if !full {
                if let Err(e) = refs.guard(path) {
                    retained(&mut row, e.to_string());
                }
            } else {
                deps::check_mounts(path)?;
                let project = deps::project_context(path, progress)
                    .map(|(_, p)| p)
                    .unwrap_or_else(|_| path.parent().unwrap().into());
                row["project"] = json!(project);
                let mut installation = Evidence::new(path, &project, path);
                if let Some(detail) = &installation.detail {
                    row.as_object_mut()
                        .unwrap()
                        .extend(detail.as_object().unwrap().clone());
                } else if path.file_name().is_some_and(|p| p == "node_modules") {
                    row["ecosystem"] = json!("JavaScript / TypeScript");
                    row["artifact_category"] = json!("项目依赖");
                    row["rebuild"] = json!("按锁文件重新安装依赖");
                }
                let mut usage = DirectoryUsage::default();
                usage.observe(s);
                let (sizes, newest, nested) =
                    storage::measure(path, progress, &mut |relative, s| {
                        installation.observe(relative, s)?;
                        usage.observe(s);
                        Ok(())
                    })?;
                row["installation"] = installation.finish();
                row.as_object_mut()
                    .unwrap()
                    .extend(sizes.as_object().unwrap().clone());
                if fsutil::stat(path)?.stamp() != row["stamp"] {
                    bail!("测量期间目录已变化，请重新扫描");
                }
                measured.insert(path.clone(), (row["stamp"].clone(), usage));
                let check = (|| -> Result<()> {
                    deps::check_policy(root, path, policy, source, progress)?;
                    if nested {
                        bail!("包含嵌套的 Git 仓库");
                    }
                    if newest > cutoff {
                        bail!("最近 {} 天有改动", policy.retention["dependencies"]);
                    }
                    if row["installation"]["state"] != "verified" {
                        bail!(
                            "{}",
                            row["installation"]["reason"]
                                .as_str()
                                .unwrap_or("安装状态未核实")
                        );
                    }
                    Ok(())
                })();
                match check {
                    Ok(()) => row["status"] = json!("candidate"),
                    Err(e) => retained(&mut row, e.to_string()),
                }
            }
            Ok(())
        })();
        if let Err(e) = collected {
            progress.check()?;
            retained(&mut row, format!("无法完整测量：{e}"));
            errors.push(format!("{}: {e}", path.display()));
        }
        rows.push(row);
    }
    errors.sort();
    if full && !rows.is_empty() {
        refs = References::read(root, true, progress)?;
        progress.update("检查项目运行状态", None, None, "")?;
        processes = Some(Snapshot::read(progress));
        for error in &refs.errors {
            if !errors.contains(error) {
                errors.push(error.clone());
            }
        }
        for row in &mut rows {
            let path = PathBuf::from(row["path"].as_str().unwrap());
            let checked = (|| -> Result<()> {
                if Some(fsutil::stat(&path)?.identity()) != identities.get(&path).copied() {
                    bail!("盘点期间路径被替换，请重新扫描");
                }
                row["referenced_by"] = json!(refs.incoming(&path));
                Ok(())
            })();
            if let Err(e) = checked {
                measured.remove(&path);
                for f in FIELDS {
                    row[*f] = Value::Null;
                }
                retained(row, e.to_string());
                errors.push(format!("{}: {e}", path.display()));
                continue;
            }
            if row["kind"] == "directory" {
                row["runtime"] = processes
                    .as_ref()
                    .unwrap()
                    .check(&path, row["project"].as_str().map(Path::new));
                if row["runtime"]["state"] != "idle" && row["status"] == "candidate" {
                    retained(
                        row,
                        row["runtime"]["reason"]
                            .as_str()
                            .unwrap_or("运行状态未知")
                            .to_string(),
                    );
                }
                if let Err(e) = refs.guard(&path) {
                    retained(row, e.to_string());
                }
            } else if let Some(target) = refs.targets.get(&path) {
                row["target"] = json!(target);
            }
        }
    }
    rows.sort_by(|a, b| {
        (a["kind"] == "symlink")
            .cmp(&(b["kind"] == "symlink"))
            .then(
                b["directory_bytes"]
                    .as_u64()
                    .unwrap_or(0)
                    .cmp(&a["directory_bytes"].as_u64().unwrap_or(0)),
            )
            .then(a["path"].as_str().cmp(&b["path"].as_str()))
    });
    let mut external = json!({});
    for (source, target) in &refs.targets {
        if refs.paths.contains(source)
            && !refs.paths.contains(target)
            && only.is_none_or(|p| p == source)
        {
            external[fsutil::text(target)] = json!(refs.incoming(target));
        }
    }
    let mut workspaces = refs.repositories.clone();
    workspaces.extend(
        refs.workspaces
            .iter()
            .filter(|p| !refs.repositories.iter().any(|r| p.starts_with(r)))
            .cloned(),
    );
    let workspaces: Vec<_> = workspaces
        .iter()
        .filter(|p| {
            refs.repositories.contains(*p)
                || !workspaces.iter().any(|r| r != *p && p.starts_with(r))
        })
        .cloned()
        .collect();
    let mut projects = Vec::new();
    for (index, repo) in workspaces.iter().enumerate() {
        let mut entry = json!({"path":repo,"agent":source,"directory_bytes":null,"artifacts":rows.iter().filter(|r|r["project"]==fsutil::text(repo)||r["path"].as_str().is_some_and(|p|Path::new(p)!=repo&&Path::new(p).starts_with(repo))).count()});
        match fsutil::stat(repo) {
            Ok(s) => entry["stamp"] = s.stamp(),
            Err(e) => errors.push(e.to_string()),
        }
        if full {
            progress.update(
                "统计工作树完整占用",
                Some(index),
                Some(workspaces.len()),
                fsutil::text(repo),
            )?;
            match storage::workspace_usage(repo, &measured, progress) {
                Ok(n) => entry["directory_bytes"] = json!(n),
                Err(e) => {
                    progress.check()?;
                    errors.push(e.to_string());
                }
            }
        }
        projects.push(entry);
    }
    if full && !projects.is_empty() && processes.is_none() {
        processes = Some(Snapshot::read(progress));
    }
    let mut report = json!({"mode":if full{"measured"}else{"references_only"},"entries":rows,"errors":errors,"external_targets":external,"projects":projects,"title":"工作树 / 多语言产物","scope":"项目完整占用另列；产物是其中的子项，共享量及释放估计不能相加"});
    if annotate {
        activity::annotate(&mut report, records, processes.as_ref(), progress)?;
    }
    Ok(report)
}
fn merge(combined: &mut Value, report: Value) {
    for key in ["entries", "projects", "errors"] {
        combined[key]
            .as_array_mut()
            .unwrap()
            .extend(report[key].as_array().unwrap().clone());
    }
    combined["external_targets"]
        .as_object_mut()
        .unwrap()
        .extend(report["external_targets"].as_object().unwrap().clone());
}
pub fn inspect_agents(
    layout: &Layout,
    policy: &Policy,
    agents: &[String],
    only: Option<&Path>,
    full: bool,
    progress: &Progress,
    mut preview: Option<&mut dyn FnMut(Value)>,
) -> Result<Value> {
    let selected: Vec<_> = agents
        .iter()
        .filter_map(|a| layout.roots.get(a).map(|r| (a, r)))
        .filter(|(_, r)| only.is_none_or(|p| p != *r && p.starts_with(r)))
        .collect();
    if selected.is_empty() {
        bail!("路径不在所选 Agent 的工作树范围内");
    }
    let mut references = BTreeMap::new();
    if full && let Some(callback) = &mut preview {
        let mut combined = empty(false);
        for (agent, root) in &selected {
            let refs = References::read(root, false, progress)?;
            merge(
                &mut combined,
                inspect(
                    root,
                    policy,
                    false,
                    only,
                    agent,
                    &(vec![], vec![]),
                    false,
                    Some(refs.clone()),
                    progress,
                )?,
            );
            references.insert((*agent).clone(), refs);
        }
        callback(combined);
    }
    let activity_roots = layout
        .roots
        .iter()
        .filter(|(k, _)| ["codex", "claude"].contains(&k.as_str()))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let records = activity::session_records(&activity_roots);
    let mut combined = empty(full);
    for (agent, root) in selected {
        merge(
            &mut combined,
            inspect(
                root,
                policy,
                full,
                only,
                agent,
                &records,
                true,
                references.remove(agent),
                progress,
            )?,
        );
    }
    Ok(combined)
}
