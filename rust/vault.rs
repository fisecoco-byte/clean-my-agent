use crate::{
    dependency_report, deps, fsops,
    fsutil::{self, Directory},
    policy::{Layout, Policy},
    process::Snapshot,
    progress::Progress,
    project_types, resources, scan,
};
use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    ffi::OsStr,
    io::Read,
    path::{Component, Path, PathBuf},
};
const CACHE_TAG: &str = "Signature: 8a477f597d28d172789f06886806bc55";
fn string<'a>(item: &'a Value, key: &str) -> Result<&'a str> {
    item[key]
        .as_str()
        .with_context(|| format!("清单缺少有效的 {key}"))
}
fn batch_key(key: &str) -> bool {
    key.len() == 16
        && key
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}
pub fn tree_item(item: &Value) -> bool {
    item["category"] == "dependencies"
        || (item["category"] == "agent-resources" && item["kind"] == "directory")
}
pub fn origin(
    item: &Value,
    layout: &Layout,
    restoring: bool,
    progress: &Progress,
) -> Result<PathBuf> {
    if item["category"] == "agent-resources" {
        return Ok(resources::resolve_item(item, layout, progress)?.1);
    }
    let source = string(item, "source")?;
    let root = layout
        .roots
        .get(source)
        .context("数据根目录与预览时不一致")?;
    if string(item, "root")? != root.to_str().context("根目录编码无效")? {
        bail!("数据根目录与预览时不一致");
    }
    let rel = Path::new(string(item, "path")?);
    if rel.as_os_str().is_empty()
        || rel.is_absolute()
        || rel.components().any(|p| !matches!(p, Component::Normal(_)))
    {
        bail!("原路径无效");
    }
    let category = string(item, "category")?;
    if category == "dependencies"
        && ["codex", "claude", "t3"].contains(&source)
        && deps::valid_location_tagged(root, &root.join(rel), item["cache_tag"] == CACHE_TAG)
    {
        return Ok(root.join(rel));
    }
    if category != scan::classify(source, rel) {
        bail!("原路径与清单类别不匹配");
    }
    if !["cache", "logs"].contains(&category)
        && !(restoring && ["snapshots", "archives", "tool-results"].contains(&category))
    {
        bail!("此项只可盘点或还原，不属于清理范围");
    }
    Ok(root.join(rel))
}
pub fn batch_lock(layout: &Layout, key: &str) -> Result<Directory> {
    if !batch_key(key) {
        bail!("无效的批次编号，请用 trash 查看");
    }
    let parent = fsops::directory(&layout.state, false)?;
    fsops::private(&parent)?;
    let batch = parent.child(OsStr::new(key))?;
    fsops::private(&batch)?;
    fsops::lock(&batch)?;
    Ok(batch)
}
pub fn manifest(dir: &Directory) -> Result<Value> {
    let before = dir.stat(OsStr::new("manifest.json"))?;
    let mut bytes = Vec::new();
    dir.open_file(OsStr::new("manifest.json"))?
        .take(64 * 1024 * 1024 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > 64 * 1024 * 1024
        || before.stamp() != dir.stat(OsStr::new("manifest.json"))?.stamp()
    {
        bail!("批次清单过大或读取期间发生变化");
    }
    let data: Value = serde_json::from_slice(&bytes)?;
    if data["version"] != 1 || !data["items"].is_array() {
        bail!("批次清单格式不兼容");
    }
    Ok(data)
}
fn unchanged(dir: &Directory, name: &OsStr, item: &Value, moving: bool) -> Result<u64> {
    let info = dir.stat(name)?;
    let stamp = item["stamp"]
        .as_array()
        .filter(|a| a.len() == 4)
        .context("清单文件身份无效")?;
    let dev = stamp[0].as_u64().context("清单设备号无效")?;
    let ino = stamp[1].as_u64().context("清单文件号无效")?;
    if tree_item(item) {
        if !info.directory() || info.identity() != (dev, ino) {
            bail!("依赖目录已被替换");
        }
        return item["size"].as_u64().context("清单占用值无效");
    }
    if !info.regular()
        || info.identity() != (dev, ino)
        || (moving && (info.stamp() != item["stamp"] || info.links != 1))
    {
        bail!("文件已变化或含其他硬链接");
    }
    Ok(info.allocated)
}
fn discard_empty(layout: &Layout, key: &str, dir: &Directory) -> Result<()> {
    if dir.names()? == vec![OsStr::new("manifest.json").to_os_string()] {
        let parent = fsops::directory(&layout.state, false)?;
        if parent.stat(OsStr::new(key))?.identity() != dir.info()?.identity() {
            bail!("批次目录已被替换");
        }
        fsops::unlink(dir, OsStr::new("manifest.json"), false)?;
        fsops::unlink(&parent, OsStr::new(key), true)?;
        fsops::sync(&parent)?;
    }
    Ok(())
}
#[derive(Debug, Default)]
pub struct Saved {
    pub batch: Option<String>,
    pub count: usize,
    pub bytes: u64,
    pub errors: Vec<String>,
}
pub fn save(
    items: &[Value],
    layout: &Layout,
    policy: &Policy,
    progress: &Progress,
) -> Result<Saved> {
    if cfg!(windows) {
        bail!("Windows 原生版仅盘点");
    }
    let mut result = Saved::default();
    if items.is_empty() {
        return Ok(result);
    }
    policy.current()?;
    let mut prepared = Vec::new();
    let mut seen = BTreeSet::new();
    for original in items {
        progress.check()?;
        let mut item = original.clone();
        let prepare = (|| -> Result<()> {
            let path = origin(&item, layout, false, progress)?;
            if !seen.insert(path.clone()) {
                bail!("重复的文件选择");
            }
            if tree_item(&item) {
                item["tree_digest"] = json!(project_types::tree_digest(&path, progress)?);
                if item["category"] == "dependencies" && project_types::tagged(&path) {
                    item["cache_tag"] = json!(CACHE_TAG);
                }
            }
            Ok(())
        })();
        match prepare {
            Ok(()) => prepared.push(item),
            Err(e) => result.errors.push(format!(
                "{}: {e}",
                item["path"].as_str().unwrap_or("无效路径")
            )),
        }
    }
    if prepared.is_empty() {
        return Ok(result);
    }
    let key = uuid::Uuid::new_v4().simple().to_string()[..16].to_string();
    let parent = fsops::directory(&layout.state, true)?;
    fsops::private(&parent)?;
    fsops::mkdir(&parent, OsStr::new(&key))?;
    fsops::sync(&parent)?;
    let batch = batch_lock(layout, &key)?;
    let data = json!({"version":1,"created":chrono::Local::now().to_rfc3339_opts(chrono::SecondsFormat::Secs,false),"policy":policy.describe(),"items":prepared});
    fsops::write_exclusive(
        &batch,
        OsStr::new("manifest.tmp"),
        &serde_json::to_vec(&data)?,
    )?;
    fsops::exclusive(
        &batch,
        OsStr::new("manifest.tmp"),
        &batch,
        OsStr::new("manifest.json"),
    )?;
    fsops::sync(&batch)?;
    for (index, item) in prepared.iter().enumerate() {
        let moved = (|| -> Result<()> {
            progress.update(
                "再次核对并暂存",
                Some(index),
                Some(prepared.len()),
                string(item, "path")?,
            )?;
            let path = origin(item, layout, false, progress)?;
            let _guard = if item["category"] == "agent-resources" {
                resources::guarded(item, layout, policy, None, progress)?
            } else {
                None
            };
            let src = fsops::directory(path.parent().context("缺少父目录")?, false)?;
            if item["category"] == "dependencies" {
                let source = string(item, "source")?;
                let report = dependency_report::inspect(
                    &layout.roots[source],
                    policy,
                    true,
                    Some(&path),
                    source,
                    &(vec![], vec![]),
                    false,
                    None,
                    progress,
                )?;
                if report["errors"].as_array().is_some_and(|e| !e.is_empty())
                    || report["entries"][0]["status"] != "candidate"
                {
                    bail!(
                        "{}",
                        report["entries"][0]["reason"]
                            .as_str()
                            .unwrap_or("引用或目录状态无法完整核实；未暂存")
                    );
                }
            } else if item["category"] != "agent-resources" {
                let info = src.stat(path.file_name().unwrap())?;
                if let Some(reason) = policy.explain(
                    string(item, "source")?,
                    Path::new(string(item, "path")?),
                    string(item, "category")?,
                    info,
                    fsutil::now_ns(),
                ) {
                    bail!("文件不再符合策略：{reason}");
                }
            }
            policy.current()?;
            let amount = unchanged(&src, path.file_name().unwrap(), item, true)?;
            if tree_item(item)
                && project_types::tree_digest(&path, progress)? != string(item, "tree_digest")?
            {
                bail!("核查期间目录内容发生变化；未暂存");
            }
            fsops::exclusive(
                &src,
                path.file_name().unwrap(),
                &batch,
                OsStr::new(&index.to_string()),
            )?;
            result.count += 1;
            result.bytes = result.bytes.saturating_add(amount);
            result.batch = Some(key.clone());
            fsops::sync(&src)?;
            fsops::sync(&batch)?;
            Ok(())
        })();
        if let Err(e) = moved {
            result.errors.push(format!(
                "{}: {e}",
                item["path"].as_str().unwrap_or("无效路径")
            ));
        }
    }
    if let Err(e) = discard_empty(layout, &key, &batch) {
        result.errors.push(format!("批次 {key} 状态需复核：{e}"));
        result.batch = Some(key);
    }
    Ok(result)
}
#[derive(Debug, Default)]
pub struct Processed {
    pub count: usize,
    pub bytes: u64,
    pub errors: Vec<String>,
    pub detail: Value,
}
pub fn process(
    layout: &Layout,
    key: &str,
    action: &str,
    apply: bool,
    policy: Option<&Policy>,
    indices: Option<&[usize]>,
    progress: &Progress,
) -> Result<Processed> {
    if !["list", "restore", "purge"].contains(&action) {
        bail!("不支持的批次操作");
    }
    if cfg!(windows) {
        bail!("Windows 原生版仅盘点");
    }
    let loaded_policy;
    let policy = if action == "purge" {
        Some(match policy {
            Some(p) => p,
            None => {
                loaded_policy = Policy::load(None, None)?;
                &loaded_policy
            }
        })
    } else {
        policy
    };
    let mut result = Processed::default();
    let batch = batch_lock(layout, key)?;
    let data = manifest(&batch)?;
    let items = data["items"].as_array().unwrap();
    let names = batch.names()?;
    result.detail = json!({"created":data.get("created").cloned().unwrap_or(json!("")),"policy":data["policy"],"files":[],"entries":[]});
    let index_of = |s: &str| {
        s.parse::<usize>()
            .ok()
            .filter(|i| i.to_string() == s && *i < items.len())
    };
    if let Some(indices) = indices {
        if indices.is_empty()
            || indices.iter().any(|i| *i >= items.len())
            || indices.iter().collect::<BTreeSet<_>>().len() != indices.len()
        {
            bail!("请选择有效且不重复的暂存条目");
        }
        if indices
            .iter()
            .any(|i| !names.contains(&i.to_string().into()))
        {
            bail!("所选暂存条目已变化，请刷新");
        }
        if names
            .iter()
            .any(|n| n != "manifest.json" && n.to_str().and_then(index_of).is_none())
        {
            bail!("暂存区含未知文件，未执行");
        }
    }
    for name in names {
        if name == "manifest.json" {
            continue;
        }
        let Some(index) = name.to_str().and_then(index_of) else {
            result.errors.push(format!(
                "暂存区中有未识别的文件：{}",
                name.to_string_lossy()
            ));
            continue;
        };
        if indices.is_some_and(|v| !v.contains(&index)) {
            continue;
        }
        let item = &items[index];
        let operation = (|| -> Result<u64> {
            progress.check()?;
            let physical = layout.state.join(key).join(&name);
            let mut resource_lock = None;
            if action == "purge" {
                let policy = policy.unwrap();
                let original = origin(item, layout, false, progress)?;
                if item["category"] == "dependencies" {
                    let source = string(item, "source")?;
                    let refs = deps::References::read(&layout.roots[source], true, progress)?;
                    refs.guard(&original)?;
                    refs.guard(&physical)?;
                    deps::check_mounts(&physical)?;
                    let (_, project) = deps::project_context(&original, progress)?;
                    project_types::require_evidence(&physical, &original, &project, progress)?;
                    let runtime = Snapshot::read(progress).check(&physical, Some(&project));
                    if runtime["state"] != "idle" {
                        bail!("{}", runtime["reason"].as_str().unwrap_or("运行状态未知"));
                    }
                }
                if item["category"] == "agent-resources" {
                    resource_lock =
                        resources::guarded(item, layout, policy, Some(&physical), progress)?;
                }
                if tree_item(item)
                    && let Some(digest) = item["tree_digest"].as_str()
                    && project_types::tree_digest(&physical, progress)? != digest
                {
                    bail!("暂存后内容有改动；保留，可恢复");
                }
                if policy.protected(
                    string(item, "source")?,
                    Path::new(string(item, "path")?),
                    tree_item(item),
                ) {
                    bail!("原路径已受策略保护；保留在暂存区，可还原");
                }
                policy.current()?;
            }
            let amount = unchanged(&batch, &name, item, action == "purge")?;
            let display_path = Path::new(string(item, "root")?).join(string(item, "path")?);
            result.detail["files"]
                .as_array_mut()
                .unwrap()
                .push(json!([display_path, amount]));
            result.detail["entries"]
                .as_array_mut()
                .unwrap()
                .push(json!({"index":index,"path":display_path,"bytes":amount}));
            if apply && action == "restore" {
                let path = origin(item, layout, true, progress)?;
                let tree = tree_item(item);
                let dst = fsops::directory(path.parent().context("原路径缺少父目录")?, !tree)?;
                if tree {
                    fsops::exclusive(&batch, &name, &dst, path.file_name().unwrap())?;
                } else {
                    if let Err(e) = fsops::link(&batch, &name, &dst, path.file_name().unwrap()) {
                        if !e
                            .downcast_ref::<std::io::Error>()
                            .is_some_and(|e| e.kind() == std::io::ErrorKind::AlreadyExists)
                        {
                            return Err(e);
                        }
                        if dst.stat(path.file_name().unwrap())?.identity()
                            != batch.stat(&name)?.identity()
                        {
                            bail!("原路径已有新文件，未覆盖");
                        }
                    }
                    fsops::unlink(&batch, &name, false)?;
                }
                fsops::sync(&dst)?;
                fsops::sync(&batch)?;
            } else if apply && action == "purge" {
                if tree_item(item) {
                    deps::check_mounts(&physical)?;
                    fsops::empty_tree(
                        &batch,
                        &name,
                        item["stamp"][0].as_u64().context("清单设备号无效")?,
                        progress,
                    )?;
                } else {
                    fsops::unlink(&batch, &name, false)?;
                }
                fsops::sync(&batch)?;
            }
            drop(resource_lock);
            Ok(amount)
        })();
        match operation {
            Ok(bytes) => {
                result.count += 1;
                result.bytes = result.bytes.saturating_add(bytes);
            }
            Err(e) => result.errors.push(format!(
                "{}: {e}",
                item["path"].as_str().unwrap_or("无效路径")
            )),
        }
    }
    if apply {
        discard_empty(layout, key, &batch)?;
    }
    Ok(result)
}
pub fn batches(layout: &Layout) -> Result<Vec<String>> {
    if !layout.state.exists() {
        return Ok(vec![]);
    }
    let dir = fsops::directory(&layout.state, false)?;
    fsops::private(&dir)?;
    let mut entries = Vec::new();
    for name in dir.names()? {
        if let Some(key) = name.to_str().filter(|s| batch_key(s)) {
            entries.push((dir.stat(&name)?.modified_ns, key.to_string()));
        }
    }
    entries.sort();
    Ok(entries.into_iter().map(|(_, s)| s).collect())
}
pub fn list_entries(layout: &Layout, progress: &Progress) -> Result<Value> {
    let mut rows = Vec::new();
    let mut errors = Vec::new();
    for key in batches(layout)?.into_iter().rev() {
        progress.update("读取暂存清单", None, None, &key)?;
        match process(layout, &key, "list", false, None, None, progress) {
            Ok(result) => {
                errors.extend(result.errors);
                for entry in result.detail["entries"].as_array().unwrap() {
                    let mut row = entry.clone();
                    row["batch"] = json!(key);
                    row["created"] = result.detail["created"].clone();
                    rows.push(row);
                }
            }
            Err(e) => errors.push(format!("{key}: {e}")),
        }
    }
    Ok(json!({"items":rows,"errors":errors}))
}
