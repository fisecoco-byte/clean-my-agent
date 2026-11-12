use crate::{
    activity,
    deps::{self, References},
    fsutil::{self, Info},
    inventory::Record,
    parallel,
    policy::{DAY_NS, Layout, Policy},
    process::Snapshot,
    progress::Progress,
    project_types::Evidence,
    storage::{self, DirectoryUsage},
    survey::{self, Survey},
};
use anyhow::{Result, bail};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
    sync::atomic::{AtomicUsize, Ordering},
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
fn order(rows: &mut [Value]) {
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
}
/// Repositories, and project directories outside any repository or other project.
fn project_paths(refs: &References) -> Vec<PathBuf> {
    let mut workspaces = refs.repositories.clone();
    workspaces.extend(
        refs.workspaces
            .iter()
            .filter(|p| !refs.repositories.iter().any(|r| p.starts_with(r)))
            .cloned(),
    );
    workspaces
        .iter()
        .filter(|p| {
            refs.repositories.contains(*p)
                || !workspaces.iter().any(|r| r != *p && p.starts_with(r))
        })
        .cloned()
        .collect()
}
fn project_row(repo: &Path, source: &str, rows: &[Value]) -> Value {
    json!({"path":repo,"agent":source,"directory_bytes":null,"artifacts":rows.iter().filter(|r|r["project"]==fsutil::text(repo)||r["path"].as_str().is_some_and(|p|Path::new(p)!=repo&&Path::new(p).starts_with(repo))).count()})
}

/// A directory that passed the checks which need no measurement of its content.
struct Job {
    row: usize,
    path: PathBuf,
    project: PathBuf,
    info: Info,
    stamp: Value,
    policy: Result<(), String>,
}
struct Sizes {
    installation: Value,
    sizes: Value,
    newest: i64,
    nested: bool,
    usage: DirectoryUsage,
    /// Why the result cannot be trusted although the walk finished.
    changed: Option<String>,
}
/// Measure one artifact. Runs on a scan worker; touches nothing shared.
fn measure(job: &Job, progress: &Progress) -> (Option<Value>, Result<Sizes>) {
    let span = progress.span("安装证据准备");
    let mut installation = Evidence::new(&job.path, &job.project, &job.path);
    drop(span);
    let detail = installation.detail.clone();
    let mut usage = DirectoryUsage::default();
    usage.observe(job.info);
    let span = progress.span("产物测量");
    let walked = storage::measure(&job.path, progress, &mut |relative, s| {
        installation.observe(relative, s)?;
        usage.observe(s);
        Ok(())
    });
    drop(span);
    let sizes = walked.map(|(sizes, newest, nested)| Sizes {
        installation: installation.finish(),
        sizes,
        newest,
        nested,
        usage,
        changed: match fsutil::stat(&job.path) {
            Ok(s) if s.stamp() == job.stamp => None,
            Ok(_) => Some("测量期间目录已变化，请重新扫描".into()),
            Err(e) => Some(e.to_string()),
        },
    });
    (detail, sizes)
}

/// Action checks measure only the selected artifacts. Whole-workspace sizes are
/// presentation data and must not be recomputed for every item in a batch.
pub fn inspect_selected(
    root: &Path,
    policy: &Policy,
    paths: &BTreeSet<PathBuf>,
    source: &str,
    progress: &Progress,
) -> Result<Value> {
    if paths.is_empty() {
        bail!("请先选择产物");
    }
    inspect_scope(
        root,
        policy,
        true,
        Some(paths.clone()),
        false,
        source,
        &(vec![], vec![]),
        false,
        None,
        None,
        progress,
    )
}

#[allow(clippy::too_many_arguments)] // Scan and action reports share all safety checks.
fn inspect_scope(
    root: &Path,
    policy: &Policy,
    full: bool,
    selected: Option<BTreeSet<PathBuf>>,
    include_projects: bool,
    source: &str,
    records: &(Vec<Record>, Vec<String>),
    annotate: bool,
    surveyed: Option<Survey>,
    mut preview: Option<&mut dyn FnMut(Value)>,
    progress: &Progress,
) -> Result<Value> {
    // A check of chosen paths needs no inventory before measuring: the reference
    // index read afterwards also says whether each path is a listed artifact.
    let deferred = full && selected.is_some() && surveyed.is_none();
    let Survey {
        mut refs,
        projects: observed,
    } = match surveyed {
        Some(found) => found,
        None if deferred => Survey::default(),
        None => survey::read(root, false, full && include_projects, progress)?,
    };
    // Git answers are reused within this report only; every action re-check
    // builds its own report and asks again.
    let mut git = deps::Git::default();
    let listed = |refs: &References| {
        selected
            .as_ref()
            .is_none_or(|paths| paths.iter().all(|path| refs.paths.contains(path)))
    };
    if selected
        .as_ref()
        .is_some_and(|paths| paths.iter().any(|path| !deps::valid_location(root, path)))
        || (!deferred && !listed(&refs))
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
    let paths = selected.clone().unwrap_or_else(|| refs.paths.clone());
    if full {
        git.plan(paths.iter().cloned());
    }
    // Everything that needs no walk of the artifact comes first, so the walks can
    // then run side by side and the likely candidates go first.
    let mut jobs = Vec::new();
    for (index, path) in paths.iter().enumerate() {
        progress.update(
            "检查依赖目录",
            Some(index),
            Some(paths.len()),
            fsutil::text(path),
        )?;
        let mut row = json!({"path":path,"kind":"directory","status":"unmeasured","reason":null,"referenced_by":[],"target":null,"installation":null,"runtime":null,"agent":source,"artifact":path.file_name().unwrap_or_default().to_string_lossy(),"project":path.parent()});
        for field in FIELDS {
            row[*field] = Value::Null;
        }
        let collected = (|| -> Result<()> {
            row["referenced_by"] = json!(fsutil::texts(&refs.incoming(path)));
            let s = fsutil::stat(path)?;
            row["stamp"] = s.stamp();
            identities.insert(path.clone(), s.identity());
            if s.symlink() {
                row["kind"] = json!("symlink");
                retained(&mut row, "仅链接，不跟随或清理");
                row["target"] = json!(refs.targets.get(path).map(|p| fsutil::text(p)));
                row["directory_bytes"] = json!(s.allocated);
            } else if !s.directory() {
                retained(&mut row, "不是依赖目录");
            } else if !full {
                if let Err(e) = refs.guard(path) {
                    retained(&mut row, e.to_string());
                }
            } else {
                deps::check_mounts(path)?;
                let project = deps::project_context(path, &mut git, progress)
                    .map(|(_, p)| p)
                    .unwrap_or_else(|_| path.parent().unwrap().into());
                row["project"] = json!(project);
                jobs.push(Job {
                    row: rows.len(),
                    path: path.clone(),
                    project,
                    info: s,
                    stamp: row["stamp"].clone(),
                    policy: deps::check_policy(root, path, policy, source, &mut git, progress)
                        .map_err(|e| e.to_string()),
                });
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
    let total = jobs.len();
    let done = AtomicUsize::new(0);
    let (likely, unlikely): (Vec<_>, Vec<_>) = jobs.into_iter().partition(|j| j.policy.is_ok());
    // Shown as retained until their sizes arrive.
    let mut waiting: BTreeMap<usize, String> = unlikely
        .iter()
        .map(|j| (j.row, j.policy.clone().unwrap_err()))
        .collect();
    let early = !likely.is_empty() && !unlikely.is_empty();
    for (round, batch) in [likely, unlikely].into_iter().enumerate() {
        let results = parallel::map(batch, parallel::workers(), |_, job| {
            let _ = progress.update(
                "测量依赖目录",
                Some(done.load(Ordering::Relaxed)),
                Some(total),
                fsutil::text(&job.path),
            );
            let result = measure(&job, progress);
            done.fetch_add(1, Ordering::Relaxed);
            (job, result)
        });
        progress.check()?;
        for (job, (detail, sizes)) in results {
            let row = &mut rows[job.row];
            waiting.remove(&job.row);
            if let Some(detail) = detail {
                row.as_object_mut()
                    .unwrap()
                    .extend(detail.as_object().unwrap().clone());
            } else if job.path.file_name().is_some_and(|p| p == "node_modules") {
                row["ecosystem"] = json!("JavaScript / TypeScript");
                row["artifact_category"] = json!("项目依赖");
                row["rebuild"] = json!("按锁文件重新安装依赖");
            }
            let failure = match sizes {
                Ok(sizes) => {
                    row["installation"] = sizes.installation;
                    row.as_object_mut()
                        .unwrap()
                        .extend(sizes.sizes.as_object().unwrap().clone());
                    if sizes.changed.is_none() {
                        measured.insert(job.path.clone(), (job.stamp, sizes.usage));
                        let reason = if let Err(reason) = job.policy {
                            Some(reason)
                        } else if sizes.nested {
                            Some("包含嵌套的 Git 仓库".into())
                        } else if sizes.newest > cutoff {
                            Some(format!(
                                "最近 {} 天有改动",
                                policy.retention["dependencies"]
                            ))
                        } else if row["installation"]["state"] != "verified" {
                            Some(
                                row["installation"]["reason"]
                                    .as_str()
                                    .unwrap_or("安装状态未核实")
                                    .to_string(),
                            )
                        } else {
                            None
                        };
                        match reason {
                            Some(reason) => retained(row, reason),
                            None => row["status"] = json!("candidate"),
                        }
                    }
                    sizes.changed
                }
                Err(e) => Some(e.to_string()),
            };
            if let Some(e) = failure {
                retained(row, format!("无法完整测量：{e}"));
                errors.push(format!("{}: {e}", job.path.display()));
            }
        }
        if round == 0
            && early
            && let Some(show) = &mut preview
        {
            show(partial(
                &rows,
                &waiting,
                &refs,
                &BTreeMap::new(),
                source,
                &errors,
            ));
        }
    }
    errors.sort();
    // Project sizes only add up what the inventory and the measurements counted.
    let mut sized = BTreeMap::new();
    if full && include_projects {
        let list = project_paths(&refs);
        for (index, repo) in list.iter().enumerate() {
            progress.update(
                "统计工作树完整占用",
                Some(index),
                Some(list.len()),
                fsutil::text(repo),
            )?;
            let _span = progress.span("工作树完整占用");
            let usage = match observed.get(repo) {
                Some(seen) => storage::surveyed_usage(repo, seen, &measured, progress),
                None => storage::workspace_usage(repo, &measured, progress),
            };
            match usage {
                Ok(bytes) => {
                    sized.insert(repo.clone(), bytes);
                }
                // Counted again below, where a failure is reported.
                Err(_) => progress.check()?,
            }
        }
        if let Some(show) = &mut preview {
            show(partial(&rows, &waiting, &refs, &sized, source, &errors));
        }
    }
    if full && !rows.is_empty() {
        refs = survey::read(root, true, false, progress)?.refs;
        if !listed(&refs) {
            bail!("请选择盘点返回的项目产物绝对路径");
        }
        progress.update("检查项目运行状态", None, None, "")?;
        let span = progress.span("运行状态");
        processes = Some(Snapshot::read(progress));
        drop(span);
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
                row["referenced_by"] = json!(fsutil::texts(&refs.incoming(&path)));
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
                row["target"] = json!(fsutil::text(target));
            }
        }
    }
    // A whole-area report says what it had to leave out; a check of chosen paths
    // is complete without them, and their links are in the index either way.
    if selected.is_none() {
        for note in &refs.unlisted {
            if !errors.contains(note) {
                errors.push(note.clone());
            }
        }
    }
    order(&mut rows);
    let mut external = json!({});
    for (source, target) in &refs.targets {
        if refs.paths.contains(source)
            && !refs.paths.contains(target)
            && selected.as_ref().is_none_or(|paths| paths.contains(source))
        {
            external[fsutil::text(target)] = json!(fsutil::texts(&refs.incoming(target)));
        }
    }
    let workspaces = if include_projects {
        project_paths(&refs)
    } else {
        vec![]
    };
    let mut projects = Vec::new();
    for (index, repo) in workspaces.iter().enumerate() {
        let mut entry = project_row(repo, source, &rows);
        match fsutil::stat(repo) {
            Ok(s) => entry["stamp"] = s.stamp(),
            Err(e) => errors.push(e.to_string()),
        }
        if let Some(bytes) = sized.get(repo) {
            entry["directory_bytes"] = json!(bytes);
        } else if full {
            progress.update(
                "统计工作树完整占用",
                Some(index),
                Some(workspaces.len()),
                fsutil::text(repo),
            )?;
            let _span = progress.span("工作树完整占用");
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
        let _span = progress.span("运行状态");
        processes = Some(Snapshot::read(progress));
    }
    let mut report = json!({"mode":if full{"measured"}else{"references_only"},"entries":rows,"errors":errors,"external_targets":external,"projects":projects,"title":"工作树 / 多语言产物","scope":"项目完整占用另列；产物是其中的子项，共享量及释放估计不能相加"});
    if annotate {
        let _span = progress.span("活跃度标注");
        activity::annotate(
            &mut report,
            records,
            processes.as_ref(),
            &observed,
            progress,
        )?;
    }
    Ok(report)
}
/// What is known while the scan is still running. Nothing in it permits an
/// action: references and running processes have not been checked again.
fn partial(
    rows: &[Value],
    waiting: &BTreeMap<usize, String>,
    refs: &References,
    sized: &BTreeMap<PathBuf, u64>,
    source: &str,
    errors: &[String],
) -> Value {
    let mut rows = rows.to_vec();
    for (index, reason) in waiting {
        retained(&mut rows[*index], reason.clone());
    }
    for row in &mut rows {
        if row["status"] == "candidate" {
            row["reason"] = json!("引用与运行状态复核中");
        }
    }
    let projects: Vec<_> = project_paths(refs)
        .iter()
        .map(|repo| {
            let mut entry = project_row(repo, source, &rows);
            if let Ok(s) = fsutil::stat(repo) {
                entry["stamp"] = s.stamp();
            }
            if let Some(bytes) = sized.get(repo) {
                entry["directory_bytes"] = json!(bytes);
            }
            entry
        })
        .collect();
    order(&mut rows);
    let mut report = empty(false);
    report["entries"] = json!(rows);
    report["projects"] = json!(projects);
    report["errors"] = json!(errors);
    report
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
    let chosen = || only.map(|p| BTreeSet::from([p.to_path_buf()]));
    let combine = |shown: &[Value]| {
        let mut combined = empty(false);
        for report in shown {
            merge(&mut combined, report.clone());
        }
        combined
    };
    // The list shown while scanning: one report per agent, replaced as it fills in.
    let mut shown = Vec::new();
    let mut surveys = BTreeMap::new();
    if full && let Some(callback) = &mut preview {
        for (agent, root) in &selected {
            let found = survey::read(root, false, true, progress)?;
            shown.push(inspect_scope(
                root,
                policy,
                false,
                chosen(),
                true,
                agent,
                &(vec![], vec![]),
                false,
                Some(Survey {
                    refs: found.refs.clone(),
                    projects: BTreeMap::new(),
                }),
                None,
                progress,
            )?);
            surveys.insert((*agent).clone(), found);
        }
        callback(combine(&shown));
    }
    let activity_roots = layout
        .roots
        .iter()
        .filter(|(k, _)| ["codex", "claude"].contains(&k.as_str()))
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let span = progress.span("会话记录");
    let records = activity::session_records(&activity_roots);
    drop(span);
    let mut combined = empty(full);
    for (index, (agent, root)) in selected.into_iter().enumerate() {
        let surveyed = surveys.remove(agent);
        let mut update = |report: Value| {
            if let Some(callback) = &mut preview
                && let Some(slot) = shown.get_mut(index)
            {
                *slot = report;
                callback(combine(&shown));
            }
        };
        let report = inspect_scope(
            root,
            policy,
            full,
            chosen(),
            true,
            agent,
            &records,
            true,
            surveyed,
            Some(&mut update),
            progress,
        )?;
        if let Some(slot) = shown.get_mut(index) {
            *slot = report.clone();
        }
        merge(&mut combined, report);
    }
    Ok(combined)
}
