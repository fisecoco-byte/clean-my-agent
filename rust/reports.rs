use crate::{fsutil, inventory, policy::SESSION_DAYS, terminal};
use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::OpenOptions,
    io::Write,
    path::{Path, PathBuf},
};

pub fn plan(rows: &[Value], archive: i64, delete: i64, limit: usize) -> Value {
    let mut result = json!({"archive":{"count":0,"bytes":0,"entries":[]},"delete":{"count":0,"bytes":0,"entries":[]}});
    for row in rows {
        let age = row["idle_days"].as_i64().unwrap_or(0);
        let route = row["route"].as_str().unwrap_or("");
        let action = if ["delete", "review"].contains(&route) && age >= delete {
            Some("delete")
        } else if ["archive", "review"].contains(&route) && age >= archive {
            Some("archive")
        } else {
            None
        };
        if let Some(action) = action {
            let group = &mut result[action];
            group["count"] = json!(group["count"].as_u64().unwrap() + 1);
            group["bytes"] =
                json!(group["bytes"].as_u64().unwrap() + row["bytes"].as_u64().unwrap_or(0));
            if group["entries"].as_array().unwrap().len() < limit {
                group["entries"].as_array_mut().unwrap().push(row.clone());
            }
        }
    }
    result
}
pub fn export(path: &Path, doc: &Value, rows: &BTreeMap<String, Vec<Value>>) -> Result<PathBuf> {
    let path = if path.starts_with("~") {
        crate::policy::home()?.join(path.strip_prefix("~")?)
    } else if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    if path
        .extension()
        .is_none_or(|s| !s.eq_ignore_ascii_case("html"))
    {
        bail!("报告路径请使用 .html 扩展名");
    }
    let mut agents = json!({});
    let limit = doc["limit"].as_u64().unwrap_or(10) as usize;
    for (key, data) in doc["agents"].as_object().context("缺少会话报告")? {
        let mut plans = json!({});
        for a in SESSION_DAYS {
            for d in SESSION_DAYS {
                plans[format!("{a}/{d}")] = plan(&rows[key], a, d, limit);
            }
        }
        let histogram: Vec<_> = inventory::BUCKETS
            .iter()
            .map(|k| data["histogram"][k].as_u64().unwrap_or(0))
            .collect();
        let count = data["session_count"].as_u64().unwrap_or(0);
        agents[key] = json!({"name":if key=="codex"{"Codex"}else{"Claude"},"count":count,"size":terminal::size(data["session_bytes"].as_u64()),"held":data["held"],"histogram":histogram,"policy":data["policy"],"shares":histogram.iter().map(|n|format!("{:.1}%",if count==0{0.}else{100.* *n as f64/count as f64})).collect::<Vec<_>>(),"plans":plans,"partial":data["errors"].as_array().is_some_and(|r|!r.is_empty()),"source":data["activity_source"],"worktrees":data["groups"]["worktrees"]["count"]});
    }
    let payload = json!({"agents":agents,"buckets":inventory::BUCKETS,"days":SESSION_DAYS,"generated":doc["generated_at"],"limit":limit});
    let html = include_str!("../assets/inventory.html").replace(
        "@@PAYLOAD@@",
        &serde_json::to_string(&payload)?.replace('<', "\\u003c"),
    );
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options
        .open(&path)
        .context("无法创建报告；已有文件不会被覆盖")?;
    file.write_all(html.as_bytes())?;
    file.sync_all()?;
    Ok(path)
}
pub fn load_dependencies(path: &Path) -> Result<Value> {
    let absolute = if path.starts_with("~") {
        crate::policy::home()?.join(path.strip_prefix("~")?)
    } else if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };
    let doc: Value = serde_json::from_str(&fsutil::regular_text(&absolute, 64 * 1024 * 1024)?)?;
    let mut data = doc
        .get("project_artifacts")
        .or(doc.get("node_modules"))
        .unwrap_or(&doc)
        .clone();
    let strings = |v: &Value| v.as_array().is_some_and(|a| a.iter().all(Value::is_string));
    if ![Some("measured"), Some("references_only")].contains(&data["mode"].as_str())
        || !strings(&data["errors"])
    {
        bail!("这不是有效的 deps 或 scan JSON 报告");
    }
    for row in data["entries"].as_array_mut().context("报告缺少产物清单")? {
        if !row["path"].is_string()
            || ![Some("directory"), Some("symlink"), Some("file")].contains(&row["kind"].as_str())
            || ![Some("candidate"), Some("retained"), Some("unmeasured")]
                .contains(&row["status"].as_str())
            || !strings(&row["referenced_by"])
        {
            bail!("产物报告字段无效");
        }
        for key in [
            "directory_bytes",
            "shared_bytes",
            "known_shared_bytes",
            "unknown_bytes",
            "reclaimable_bytes",
            "reclaimable_min_bytes",
            "reclaimable_max_bytes",
        ] {
            if row.get(key).is_none() || (!row[key].is_null() && !row[key].is_u64()) {
                bail!("产物报告容量无效");
            }
        }
        for key in ["reason", "target"] {
            if !row[key].is_null() && !row[key].is_string() {
                bail!("产物报告字段无效");
            }
        }
    }
    if let Some(external) = data.get("external_targets")
        && !external
            .as_object()
            .is_some_and(|o| o.values().all(strings))
    {
        bail!("报告引用无效");
    }
    if let Some(projects) = data.get("projects") {
        for row in projects.as_array().context("工作目录报告无效")? {
            if !row["path"].is_string()
                || ![Some("codex"), Some("claude"), Some("t3")].contains(&row["agent"].as_str())
                || !row["artifacts"].is_u64()
                || (!row["directory_bytes"].is_null() && !row["directory_bytes"].is_u64())
            {
                bail!("工作目录报告无效");
            }
        }
    }
    Ok(data)
}
