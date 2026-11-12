use crate::{
    dependency_report, fsutil, inventory,
    policy::{self, Layout, Policy, SESSION_DAYS},
    process,
    progress::Progress,
    resources, scan, vault,
};
use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
    process::Command,
    time::Duration,
};
fn writes() -> Result<()> {
    if cfg!(windows) {
        bail!("Windows 原生版仅盘点");
    }
    Ok(())
}
fn saved(mut result: vault::Saved, layout: &Layout) -> Result<Value> {
    let mut paths = Vec::new();
    if let Some(key) = &result.batch {
        let checked = (|| -> Result<()> {
            let batch = vault::batch_lock(layout, key)?;
            let data = vault::manifest(&batch)?;
            let names = batch.names()?;
            for (i, item) in data["items"]
                .as_array()
                .context("批次清单无效")?
                .iter()
                .enumerate()
            {
                if names.iter().any(|n| n == i.to_string().as_str()) {
                    paths.push(
                        Path::new(item["root"].as_str().context("无效根路径")?)
                            .join(item["path"].as_str().context("无效路径")?),
                    );
                }
            }
            Ok(())
        })();
        if let Err(e) = checked {
            result.errors.push(format!(
                "已暂存，但批次清单未能再次读取：{e}；请保留批次编号并刷新暂存区"
            ));
        }
    }
    Ok(
        json!({"batch":result.batch,"moved_items":result.count,"count":result.count,"bytes":result.bytes,"moved_paths":paths,"errors":result.errors}),
    )
}

pub fn stage_artifacts(
    layout: &Layout,
    policy: &Policy,
    selected: &[Value],
    progress: &Progress,
) -> Result<Value> {
    writes()?;
    if selected.is_empty() {
        bail!("请选择不重复的产物候选");
    }
    policy.current()?;
    let mut identities = BTreeSet::new();
    let mut groups = BTreeMap::<&str, BTreeSet<std::path::PathBuf>>::new();
    let mut items = Vec::new();
    for old in selected {
        let path = Path::new(old["path"].as_str().context("产物缺少路径")?);
        let source = old["agent"].as_str().unwrap_or("codex");
        layout.roots.get(source).context("产物来源未知")?;
        if !identities.insert(path.to_path_buf())
            || old["status"] != "candidate"
            || old["kind"] != "directory"
        {
            bail!("选择中含重复或不可暂存的产物");
        }
        if selected
            .iter()
            .filter_map(|r| r["path"].as_str())
            .map(Path::new)
            .any(|other| other != path && other.starts_with(path))
        {
            bail!("不能同时选择父目录与其子产物");
        }
        groups.entry(source).or_default().insert(path.into());
    }
    let mut checked = BTreeMap::new();
    for (source, paths) in groups {
        let review = progress.context(format!("暂存前复核 {source}（{} 项）", paths.len()));
        let report = dependency_report::inspect_selected(
            &layout.roots[source],
            policy,
            &paths,
            source,
            &review,
        )?;
        if report["errors"].as_array().is_some_and(|a| !a.is_empty()) {
            bail!("产物复核不完整，未暂存：{}", report["errors"]);
        }
        for row in report["entries"].as_array().context("产物复核缺少结果")? {
            checked.insert(
                (
                    source,
                    row["path"].as_str().context("产物缺少路径")?.to_owned(),
                ),
                row.clone(),
            );
        }
    }
    for old in selected {
        let path = Path::new(old["path"].as_str().unwrap());
        let source = old["agent"].as_str().unwrap_or("codex");
        let root = &layout.roots[source];
        let row = checked
            .get(&(source, fsutil::text(path)))
            .context("产物已不在复核范围内")?;
        if row["status"] != "candidate" || row["stamp"] != old["stamp"] {
            bail!(
                "所选产物已变化，未暂存：{} · {}",
                path.display(),
                row["reason"].as_str().unwrap_or("请刷新")
            );
        }
        items.push(json!({"source":source,"root":root,"path":path.strip_prefix(root)?,"category":"dependencies","size":row["directory_bytes"],"stamp":row["stamp"],"project":row["project"]}));
    }
    saved(vault::save(&items, layout, policy, progress)?, layout)
}
pub fn stage_resources(
    layout: &Layout,
    policy: &Policy,
    selected: &[Value],
    progress: &Progress,
) -> Result<Value> {
    writes()?;
    policy.current()?;
    if selected.is_empty() {
        bail!("请选择符合策略的资源候选");
    }
    let mut seen = BTreeSet::new();
    let mut items = Vec::new();
    let home = policy::home()?;
    for old in selected {
        let path = Path::new(old["path"].as_str().context("资源缺少路径")?);
        let agent = old["agent"].as_str().context("资源缺少来源")?;
        if !seen.insert(path.to_path_buf())
            || old["status"] != "candidate"
            || old["can_stage"] != true
        {
            bail!("请选择不重复且符合策略的资源候选");
        }
        let report =
            resources::inspect(layout, &[agent.into()], policy, true, Some(path), progress)?;
        let row = &report["entries"][0];
        if row["status"] != "candidate" || row["stamp"] != old["stamp"] {
            bail!("{}", row["reason"].as_str().unwrap_or("资源已变化"));
        }
        let base = layout
            .roots
            .get(agent)
            .filter(|r| path != *r && path.starts_with(r))
            .unwrap_or(&home);
        items.push(json!({"source":agent,"root":base,"path":path.strip_prefix(base)?,"category":"agent-resources","kind":row["kind"],"size":row["directory_bytes"],"stamp":row["stamp"]}));
    }
    saved(vault::save(&items, layout, policy, progress)?, layout)
}
pub fn stage_files(
    layout: &Layout,
    policy: &Policy,
    agent: &str,
    selected: &[Value],
    progress: &Progress,
) -> Result<Value> {
    writes()?;
    policy.current()?;
    if selected.is_empty() {
        bail!("请先选择文件");
    }
    let fresh = scan::scan(layout, &layout.selected(agent), policy, false, progress)?;
    if !fresh.errors.is_empty() {
        bail!("扫描不完整，未暂存；请先处理提示");
    }
    let mut seen = BTreeSet::new();
    let mut items = Vec::new();
    for old in selected {
        let key = (
            old["source"].as_str().context("缺少文件来源")?,
            old["path"].as_str().context("缺少文件路径")?,
        );
        if !seen.insert(key) {
            bail!("重复的文件选择");
        }
        let item = fresh
            .items
            .iter()
            .find(|r| r["source"] == key.0 && r["path"] == key.1)
            .context("文件或策略已变化，请返回后重新预览")?;
        if item["stamp"] != old["stamp"] {
            bail!("文件或策略已变化，请返回后重新预览");
        }
        items.push(item.clone());
    }
    saved(vault::save(&items, layout, policy, progress)?, layout)
}
pub fn quarantine_preview(
    layout: &Layout,
    operation: &str,
    selected: &[Value],
    progress: &Progress,
) -> Result<Value> {
    quarantine_check(layout, operation, selected, operation, progress)
}
/// The preview document for `selected`. `action` is the vault check behind it:
/// the operation itself for a full preview, or "list" to confirm only that the
/// batches still hold the same entries.
fn quarantine_check(
    layout: &Layout,
    operation: &str,
    selected: &[Value],
    action: &str,
    progress: &Progress,
) -> Result<Value> {
    if !["restore", "purge"].contains(&operation) || selected.is_empty() {
        bail!("请先用空格勾选暂存条目");
    }
    let mut groups: BTreeMap<String, Vec<&Value>> = BTreeMap::new();
    for row in selected {
        let group = groups
            .entry(row["batch"].as_str().context("无效批次")?.into())
            .or_default();
        if group.iter().any(|r| r["index"] == row["index"]) {
            bail!("选择中有重复条目");
        }
        group.push(row);
    }
    let mut previews = Vec::new();
    for (key, rows) in groups {
        progress.update("核对所选暂存条目", None, None, &key)?;
        let indices = rows
            .iter()
            .map(|r| {
                r["index"]
                    .as_u64()
                    .and_then(|n| usize::try_from(n).ok())
                    .context("无效条目编号")
            })
            .collect::<Result<Vec<_>>>()?;
        let current = vault::process(layout, &key, action, false, None, Some(&indices), progress)?;
        if !current.errors.is_empty() {
            bail!("{}", current.errors.join("；"));
        }
        for row in rows {
            let expected = json!({"index":row["index"],"path":row["path"],"bytes":row["bytes"]});
            if row["created"] != current.detail["created"]
                || !current.detail["entries"]
                    .as_array()
                    .unwrap()
                    .contains(&expected)
            {
                bail!("暂存条目已变化，请刷新后重新选择");
            }
        }
        let mut preview = current.detail;
        preview["batch"] = json!(key);
        preview["indices"] = json!(indices);
        previews.push(preview);
    }
    Ok(json!({"operation":operation,"selected":selected,"batches":previews}))
}
pub fn quarantine_apply(layout: &Layout, expected: &Value, progress: &Progress) -> Result<Value> {
    writes()?;
    let operation = expected["operation"].as_str().context("无效操作")?;
    // The entries must still be the previewed ones. Policy, references, running
    // processes and content are then checked once, as each entry is processed.
    let fresh = quarantine_check(
        layout,
        operation,
        expected["selected"].as_array().context("缺少选择")?,
        "list",
        progress,
    )?;
    if fresh != *expected {
        bail!("暂存清单已变化，未执行");
    }
    let mut count = 0;
    let mut errors = Vec::new();
    let mut moved = Vec::new();
    for batch in fresh["batches"].as_array().unwrap() {
        let key = batch["batch"].as_str().unwrap();
        progress.update(
            if operation == "purge" {
                "永久删除所选暂存条目"
            } else {
                "恢复所选暂存条目"
            },
            None,
            None,
            key,
        )?;
        let indices: Vec<_> = batch["indices"]
            .as_array()
            .unwrap()
            .iter()
            .map(|n| n.as_u64().unwrap() as usize)
            .collect();
        match vault::process(layout, key, operation, true, None, Some(&indices), progress) {
            Ok(result) => {
                count += result.count;
                errors.extend(result.errors);
                moved.extend(
                    result.detail["entries"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter_map(|row| row["path"].as_str().map(str::to_string)),
                );
            }
            Err(e) => errors.push(e.to_string()),
        }
        if !errors.is_empty() {
            break;
        }
    }
    Ok(json!({"operation":operation,"count":count,"moved_paths":moved,"errors":errors}))
}
pub fn sessions(
    layout: &Layout,
    operation: &str,
    selected: &[Value],
    rules: &Value,
    progress: &Progress,
) -> Result<Value> {
    writes()?;
    if !["archive", "delete"].contains(&operation) || selected.is_empty() {
        bail!("请选择归档或删除候选");
    }
    let threshold = rules[format!("{operation}_days")]
        .as_i64()
        .filter(|d| SESSION_DAYS.contains(d))
        .context("未识别的复核天数")?;
    let uuid = regex::Regex::new(&format!("^{}$", inventory::UUID))?;
    let mut ids = BTreeSet::new();
    for row in selected {
        let id = row["session_id"]
            .as_str()
            .context("请选择完整的会话 UUID")?;
        if !uuid.is_match(id) || !ids.insert(id) {
            bail!("请选择完整且不重复的会话 UUID");
        }
    }
    let root = &layout.roots["codex"];
    progress.update("检查 Codex 原生命令", None, None, "")?;
    let native = |args: &[&str]| {
        let mut command = Command::new("codex");
        command.args(args).env("CODEX_HOME", root).current_dir(root);
        command
    };
    let help = process::run(
        &mut native(&[operation, "--help"]),
        Duration::from_secs(15),
        progress,
    )?;
    let help_text = String::from_utf8_lossy(&help.stdout);
    if !help.status.success()
        || !help_text.contains("Usage:")
        || (operation == "delete" && !help_text.contains("--force"))
    {
        bail!("当前 Codex CLI 不支持所需的原生命令，请先更新 Codex");
    }
    let archive = rules["archive_days"].as_i64();
    let delete = rules["delete_days"].as_i64();
    let mut results = Vec::new();
    let mut stopped = false;
    for (index, old) in selected.iter().enumerate() {
        let sid = old["session_id"].as_str().unwrap();
        let mut result = json!({"session_id":sid,"title":old["title"],"action":operation,"status":"skipped","message":""});
        if stopped {
            result["message"] = json!("前一操作未能确认，未执行这一项");
            results.push(result);
            continue;
        }
        progress.update("核对并执行会话操作", Some(index), Some(selected.len()), sid)?;
        let mut launched = false;
        let performed = (|| -> Result<()> {
            let (doc, rows) = inventory::inspect(
                layout,
                &Policy::load(None, None)?,
                "codex",
                archive,
                delete,
                false,
                10,
                progress,
            )?;
            let current = rows["codex"].iter().find(|r| r["session_id"] == sid);
            if !doc["errors"].as_array().unwrap().is_empty()
                || current.is_none_or(|r| {
                    r["route"] != operation
                        || r["idle_days"].as_i64().unwrap_or(0) < threshold
                        || old["latest_ns"].is_null()
                        || r["latest_ns"] != old["latest_ns"]
                        || r["state"] != old["state"]
                })
            {
                result["message"] = json!("会话活动、状态或保护规则已变化，未执行；请刷新复核");
                return Ok(());
            }
            let mut args = vec![operation, sid];
            if operation == "delete" {
                args.push("--force");
            }
            launched = true;
            let command = process::run(&mut native(&args), Duration::from_secs(90), progress)?;
            if !command.status.success() {
                result["status"] = json!("failed");
                let text = String::from_utf8_lossy(if command.stderr.is_empty() {
                    &command.stdout
                } else {
                    &command.stderr
                });
                let tail = text
                    .chars()
                    .rev()
                    .take(2000)
                    .collect::<String>()
                    .chars()
                    .rev()
                    .collect::<String>();
                result["message"] = json!(tail);
                stopped = true;
                return Ok(());
            }
            let (after, fresh) = inventory::inspect(
                layout,
                &Policy::load(None, None)?,
                "codex",
                archive,
                delete,
                false,
                10,
                progress,
            )?;
            let changed = fresh["codex"].iter().find(|r| r["session_id"] == sid);
            let mut verified = if operation == "archive" {
                changed.is_some_and(|r| r["state"] == "archived")
            } else {
                changed.is_none()
            };
            if operation == "delete" && verified {
                verified = !inventory::thread_index(root)?.0.contains_key(sid);
            }
            if after["errors"].as_array().unwrap().is_empty() && verified {
                result["status"] = json!("succeeded");
                result["message"] = json!(if operation == "archive" {
                    "已归档"
                } else {
                    "已永久删除"
                });
            } else {
                result["status"] = json!("uncertain");
                result["message"] = json!("命令已返回，但本地状态尚未确认；请刷新核对，勿直接重试");
                stopped = true;
            }
            Ok(())
        })();
        if let Err(e) = performed {
            result["status"] = json!(if launched { "uncertain" } else { "failed" });
            result["message"] = json!(format!(
                "{e}{}",
                if launched {
                    "；状态未确认，请刷新核对，勿直接重试"
                } else {
                    ""
                }
            ));
            stopped = true;
        }
        results.push(result);
    }
    Ok(json!({"results":results}))
}
