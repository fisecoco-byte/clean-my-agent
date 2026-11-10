use crate::{
    fsutil::{self, Visit},
    inventory::{self, Record},
    policy::DAY_NS,
    process::Snapshot,
    progress::Progress,
    project_types,
};
use anyhow::{Result, bail};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

pub const LEGEND: &str = "高 <5 天 · 中 5–9 天 · 低 10–14 天 · 极低 ≥15 天 · 未知另列";
pub fn classify(
    latest: Option<i64>,
    now: i64,
    basis: &str,
    complete: bool,
    running: bool,
    warning: Option<&str>,
) -> Value {
    let days = latest
        .filter(|n| *n > 0 && *n <= now)
        .map(|n| (now - n) / DAY_NS);
    let level = if running {
        "high"
    } else if !complete || days.is_none() {
        "unknown"
    } else {
        match days.unwrap() {
            15.. => "very_low",
            10.. => "low",
            5.. => "medium",
            _ => "high",
        }
    };
    let label = match level {
        "high" => "高",
        "medium" => "中",
        "low" => "低",
        "very_low" => "极低",
        _ => "未知",
    };
    json!({"level":level,"label":label,"idle_days":if running{Some(0)}else if level=="unknown"{None}else{days},"latest_ns":if running{Some(now)}else{latest},"basis":if running{"当前运行进程"}else{basis},"estimated":!running,"warning":warning,"observed_at_ns":now})
}
pub fn badge(row: &Value) -> String {
    let data = &row["activity"];
    let name = data["label"].as_str().unwrap_or("未知");
    if data["level"] != "unknown"
        && let Some(days) = data["idle_days"].as_u64()
    {
        return format!("{name} · {days}天");
    }
    name.into()
}
pub fn session_records(roots: &BTreeMap<String, PathBuf>) -> (Vec<Record>, Vec<String>) {
    let mut records = Vec::new();
    let mut warnings = Vec::new();
    for (agent, root) in roots {
        let collect = (|| -> Result<()> {
            fsutil::unlinked_path(root)?;
            match agent.as_str() {
                "codex" => records.extend(inventory::thread_index(root)?.2),
                "claude" => {
                    let base = root.join("projects");
                    if !base.exists() {
                        return Ok(());
                    }
                    for name in fsutil::Directory::open(&base)?.names()? {
                        let directory = base.join(name);
                        if !fsutil::stat(&directory)?.directory() {
                            continue;
                        }
                        let path = directory.join("sessions-index.json");
                        if !path.exists() {
                            continue;
                        }
                        let doc: Value =
                            serde_json::from_str(&fsutil::regular_text(&path, 16 * 1024 * 1024)?)?;
                        if let Some(rows) = doc["entries"].as_array() {
                            for row in rows {
                                if let (Some(cwd), Some(modified)) =
                                    (row["projectPath"].as_str(), row["modified"].as_str())
                                    && Path::new(cwd).is_absolute()
                                {
                                    let date = chrono::DateTime::parse_from_rfc3339(modified)?;
                                    if let Some(stamp) = date.timestamp_nanos_opt() {
                                        records.push((cwd.into(), stamp, "Claude 会话索引".into()));
                                    }
                                }
                            }
                        }
                    }
                }
                _ => (),
            }
            Ok(())
        })();
        if let Err(e) = collect {
            warnings.push(format!("{agent} 活动索引未完整读取：{e}"));
        }
    }
    (records, warnings)
}
pub fn workspace(
    path: &Path,
    records: &[Record],
    warnings: &[String],
    now: i64,
    runtime: Option<&Value>,
    progress: &Progress,
    limit: usize,
) -> Result<Value> {
    if runtime.is_some_and(|r| r["state"] == "busy") {
        return Ok(classify(Some(now), now, "", true, true, None));
    }
    let (mut latest, mut basis) = (0, String::new());
    let mut notes = warnings.to_vec();
    let mut complete = notes.is_empty();
    let mut observe = |stamp: i64, source: &str| {
        if stamp > latest {
            latest = stamp;
            basis = source.into();
        }
    };
    for (cwd, stamp, source) in records {
        if cwd.starts_with(path) {
            observe(*stamp, source);
        }
    }
    let collect = (|| -> Result<()> {
        fsutil::unlinked_path(path)?;
        let root = fsutil::stat(path)?;
        if !root.directory() {
            bail!("目录含符号链接或不可读取");
        }
        let mut count = 0;
        fsutil::walk(path, progress, &mut |relative, _, name, s| {
            count += 1;
            if count > limit {
                bail!("文件数量超过活跃度检查上限");
            }
            if count % 500 == 0 {
                progress.update(
                    "检查工作目录活跃度",
                    None,
                    None,
                    format!(
                        "{} · {count} 项",
                        path.file_name().unwrap_or_default().to_string_lossy()
                    ),
                )?;
            }
            let name = name.to_string_lossy();
            if s.directory() {
                return Ok(
                    if project_types::TARGETS.contains(&name.as_ref())
                        || [".git", ".Trash", ".nx"].contains(&name.as_ref())
                        || s.dev != root.dev
                        || project_types::tagged(&path.join(relative))
                    {
                        Visit::Skip
                    } else {
                        Visit::Descend
                    },
                );
            }
            if s.regular() && ![".git", ".DS_Store"].contains(&name.as_ref()) {
                observe(s.modified_ns, "工作目录文件修改（不含依赖与构建目录）");
            }
            Ok(Visit::Skip)
        })?;
        let mut gitdir = path.join(".git");
        if fsutil::stat(&gitdir).is_ok_and(|s| s.regular()) {
            let text = fsutil::regular_text(&gitdir, 8192)?;
            let pointer = text
                .trim()
                .strip_prefix("gitdir: ")
                .ok_or_else(|| anyhow::anyhow!("Git 工作树元数据无法识别"))?;
            gitdir = fsutil::resolve(&path.join(pointer))?;
        }
        if fsutil::stat(&gitdir).is_ok_and(|s| s.directory()) {
            for name in ["HEAD", "index", "logs/HEAD"] {
                let item = gitdir.join(name);
                if let Ok(s) = fsutil::stat(&item)
                    && !s.symlink()
                {
                    observe(s.modified_ns, "本工作树 Git 操作记录");
                }
            }
        }
        Ok(())
    })();
    if let Err(e) = collect {
        progress.check()?;
        complete = false;
        notes.push(e.to_string());
    }
    if latest > now {
        notes.push("活动时间晚于扫描时间".into());
    }
    let warning = notes.join("；");
    Ok(classify(
        if latest == 0 { None } else { Some(latest) },
        now,
        &basis,
        complete,
        false,
        if warning.is_empty() {
            None
        } else {
            Some(&warning)
        },
    ))
}
pub fn annotate(
    report: &mut Value,
    records: &(Vec<Record>, Vec<String>),
    processes: Option<&Snapshot>,
    progress: &Progress,
) -> Result<()> {
    let now = fsutil::now_ns();
    let projects = report["projects"].as_array_mut().unwrap();
    let total = projects.len();
    for (index, row) in projects.iter_mut().enumerate() {
        let path = PathBuf::from(row["path"].as_str().unwrap());
        progress.update(
            "检查工作目录活跃度",
            Some(index),
            Some(total),
            fsutil::text(&path),
        )?;
        let runtime = processes.map(|p| p.check(&path, Some(&path)));
        row["activity"] = workspace(
            &path,
            &records.0,
            &records.1,
            now,
            runtime.as_ref(),
            progress,
            50000,
        )?;
    }
    let mut parents = projects.clone();
    parents.sort_by_key(|r| {
        std::cmp::Reverse(Path::new(r["path"].as_str().unwrap()).components().count())
    });
    for row in report["entries"].as_array_mut().unwrap() {
        let path = Path::new(row["path"].as_str().unwrap());
        row["activity"] = if let Some(parent) = parents.iter().find(|r| {
            let p = Path::new(r["path"].as_str().unwrap());
            path != p && path.starts_with(p)
        }) {
            let mut value = parent["activity"].clone();
            value["workspace"] = parent["path"].clone();
            value
        } else {
            classify(None, now, "", true, false, None)
        };
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    #[test]
    fn boundaries_and_incomplete() {
        let now = 40 * DAY_NS;
        for (days, level) in [
            (0, "high"),
            (4, "high"),
            (5, "medium"),
            (9, "medium"),
            (10, "low"),
            (14, "low"),
            (15, "very_low"),
        ] {
            assert_eq!(
                classify(Some(now - days * DAY_NS), now, "test", true, false, None)["level"],
                level
            );
        }
        assert_eq!(
            classify(Some(now - DAY_NS), now, "test", false, false, None)["idle_days"],
            Value::Null
        );
        assert_eq!(
            classify(Some(now + 1), now, "test", true, false, None)["level"],
            "unknown"
        );
        assert_eq!(
            classify(None, now, "test", false, true, None)["level"],
            "high"
        );
    }
    #[test]
    fn prunes_generated_files() {
        let temp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(temp.path()).unwrap();
        fs::create_dir(root.join("node_modules")).unwrap();
        fs::write(root.join("node_modules/recent"), "x").unwrap();
        let info = workspace(
            &root,
            &[],
            &[],
            fsutil::now_ns(),
            None,
            &Progress::default(),
            50000,
        )
        .unwrap();
        assert_eq!(info["level"], "unknown");
        fs::write(root.join("source.rs"), "x").unwrap();
        let info = workspace(
            &root,
            &[],
            &[],
            fsutil::now_ns(),
            None,
            &Progress::default(),
            50000,
        )
        .unwrap();
        assert_eq!(info["level"], "high");
    }
}
