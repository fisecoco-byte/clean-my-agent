use crate::{
    activity,
    fsutil::{self, Visit},
    policy::{DAY_NS, Layout, Policy, SESSION_DAYS},
    progress::Progress,
};
use anyhow::{Context, Result, bail};
use regex::Regex;
use rusqlite::{Connection, OpenFlags, types::ValueRef};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    env, fs,
    io::{Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    sync::LazyLock,
    time::Duration,
};

pub const UUID: &str = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}";
pub const BUCKETS: [&str; 5] = ["0–4 天", "5–9 天", "10–14 天", "15–19 天", "20 天以上"];
const WINDOW: usize = 32 * 1024;
const INDEX_LIMIT: u64 = 16 * 1024 * 1024;
static ESCAPES: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))").unwrap()
});
pub fn label(value: Option<&str>) -> String {
    let raw: String = value.unwrap_or("").chars().take(4096).collect();
    let raw = ESCAPES.replace_all(&raw, "");
    let cleaned: String = raw
        .chars()
        .map(|c| if fsutil::printable(c) { c } else { ' ' })
        .collect();
    let cleaned = cleaned.split_whitespace().collect::<Vec<_>>().join(" ");
    if cleaned.chars().count() > 160 {
        cleaned
            .chars()
            .take(159)
            .chain(std::iter::once('…'))
            .collect()
    } else {
        cleaned
    }
}
pub fn sqlite_read(path: &Path) -> Result<(Connection, bool, Value)> {
    fsutil::unlinked_path(path)?;
    let before = fsutil::stat(path)?;
    if !before.regular() {
        bail!("Codex 索引路径异常");
    }
    let side = |s: &str| PathBuf::from(format!("{}{s}", path.display()));
    for suffix in ["-wal", "-shm", "-journal"] {
        if fs::symlink_metadata(side(suffix)).is_ok_and(|m| m.file_type().is_symlink()) {
            bail!("Codex 索引附属文件路径异常");
        }
    }
    if side("-journal").exists() || side("-wal").exists() != side("-shm").exists() {
        bail!("Codex 索引正在切换状态，请重新盘点");
    }
    let no_wal = !side("-wal").exists();
    let mut url = url::Url::from_file_path(path).map_err(|_| anyhow::anyhow!("索引路径无效"))?;
    url.set_query(Some(if no_wal {
        "mode=ro&immutable=1"
    } else {
        "mode=ro"
    }));
    let db = Connection::open_with_flags(
        url.as_str(),
        OpenFlags::SQLITE_OPEN_READ_ONLY
            | OpenFlags::SQLITE_OPEN_URI
            | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    db.busy_timeout(Duration::from_secs(1))?;
    db.execute_batch("PRAGMA query_only=ON; PRAGMA trusted_schema=OFF;")?;
    Ok((db, no_wal, before.stamp()))
}
pub fn sqlite_unchanged(path: &Path, no_wal: bool, stamp: &Value) -> Result<()> {
    if no_wal
        && (PathBuf::from(format!("{}-wal", path.display())).exists()
            || PathBuf::from(format!("{}-journal", path.display())).exists()
            || fsutil::stat(path)?.stamp() != *stamp)
    {
        bail!("Codex 索引已更新，请重新盘点");
    }
    Ok(())
}
pub fn latest_database(root: &Path) -> Result<Option<PathBuf>> {
    let mut found = Vec::new();
    if !root.exists() {
        return Ok(None);
    }
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if let Some(number) = name
            .strip_prefix("state_")
            .and_then(|s| s.strip_suffix(".sqlite"))
            .and_then(|s| s.parse::<u64>().ok())
        {
            found.push((number, entry.path()));
        }
    }
    found.sort();
    Ok(found.pop().map(|(_, p)| p))
}
pub fn columns(db: &Connection, table: &str) -> Result<BTreeSet<String>> {
    if !["threads"].contains(&table) {
        bail!("未知索引表");
    }
    Ok(db
        .prepare(&format!("PRAGMA table_info({table})"))?
        .query_map([], |r| r.get::<_, String>(1))?
        .collect::<std::result::Result<_, _>>()?)
}
#[derive(Clone, Debug)]
pub struct ThreadInfo {
    pub archived: Option<i64>,
    pub pinned: bool,
    pub latest: i64,
    pub name: String,
    pub title: String,
    pub cwd: Option<PathBuf>,
}
pub type Record = (PathBuf, i64, String);
pub fn thread_index(root: &Path) -> Result<(BTreeMap<String, ThreadInfo>, String, Vec<Record>)> {
    let Some(path) = latest_database(root)? else {
        return Ok((BTreeMap::new(), "files".into(), Vec::new()));
    };
    let (db, no_wal, before) = sqlite_read(&path)?;
    let cols = columns(&db, "threads")?;
    if !cols.contains("id") || !cols.contains("archived") {
        bail!("未识别的 Codex 会话索引");
    }
    let clocks = [
        ("updated_at_ms", 1_000_000_i64),
        ("recency_at_ms", 1_000_000),
        ("updated_at", 1_000_000_000),
        ("recency_at", 1_000_000_000),
        ("archived_at", 1_000_000_000),
    ];
    let clocks: Vec<_> = clocks
        .into_iter()
        .filter(|(k, _)| cols.contains(*k))
        .collect();
    let mut fields = vec![
        "id".into(),
        "archived".into(),
        if cols.contains("is_pinned") {
            "is_pinned"
        } else {
            "0"
        }
        .into(),
    ];
    for (k, len) in [
        ("name", 4096),
        ("title", 4096),
        ("summary", 4096),
        ("cwd", 8192),
        ("rollout_path", 8192),
    ] {
        fields.push(if cols.contains(k) {
            format!("substr({k},1,{len})")
        } else {
            "NULL".into()
        });
    }
    fields.extend(clocks.iter().map(|(k, _)| (*k).to_string()));
    let mut statement = db.prepare(&format!("SELECT {} FROM threads", fields.join(",")))?;
    let mut cursor = statement.query([])?;
    let mut result = BTreeMap::new();
    let mut records = Vec::new();
    while let Some(row) = cursor.next()? {
        let Ok(sid) = row.get::<_, String>(0) else {
            continue;
        };
        let archived = row.get::<_, i64>(1).ok();
        let pinned = match row.get_ref(2)? {
            ValueRef::Null => false,
            ValueRef::Integer(n) => n != 0,
            ValueRef::Real(n) => n != 0.0,
            ValueRef::Text(s) => !s.is_empty(),
            _ => true,
        };
        let mut latest = 0;
        for (i, (_, factor)) in clocks.iter().enumerate() {
            let n = match row.get_ref(8 + i)? {
                ValueRef::Integer(n) => n.saturating_mul(*factor),
                ValueRef::Real(n) => (n * (*factor as f64)) as i64,
                _ => 0,
            };
            latest = latest.max(n);
        }
        let name = label(row.get::<_, String>(3).ok().as_deref());
        let title = label(row.get::<_, String>(4).ok().as_deref());
        let title = if title.is_empty() {
            label(row.get::<_, String>(5).ok().as_deref())
        } else {
            title
        };
        let cwd = row
            .get::<_, String>(6)
            .ok()
            .map(PathBuf::from)
            .filter(|p| p.is_absolute());
        if let Some(cwd) = &cwd {
            let mut stamp = latest;
            if let Ok(rollout) = row.get::<_, String>(7) {
                let p = Path::new(&rollout);
                if (p.starts_with(root.join("sessions"))
                    || p.starts_with(root.join("archived_sessions")))
                    && let Ok(s) = fsutil::stat(p)
                    && s.regular()
                    && fsutil::unlinked_path(p).is_ok()
                {
                    stamp = stamp.max(s.modified_ns);
                }
            }
            records.push((cwd.clone(), stamp, "Codex 会话索引 / 会话文件".into()));
        }
        result.insert(
            sid.to_lowercase(),
            ThreadInfo {
                archived,
                pinned,
                latest,
                name,
                title,
                cwd,
            },
        );
    }
    drop(cursor);
    drop(statement);
    drop(db);
    sqlite_unchanged(&path, no_wal, &before)?;
    Ok((result, "index+files".into(), records))
}
fn codex_titles(root: &Path) -> BTreeMap<String, String> {
    let load = || -> Result<BTreeMap<String, String>> {
        let mut file = fsutil::open_regular(&root.join("session_index.jsonl"))?;
        let size = file.metadata()?.len();
        let offset = size.saturating_sub(INDEX_LIMIT + 1);
        file.seek(SeekFrom::Start(offset))?;
        let mut bytes = Vec::new();
        file.take(INDEX_LIMIT + 1).read_to_end(&mut bytes)?;
        let data = if offset > 0 {
            bytes.splitn(2, |c| *c == b'\n').nth(1).unwrap_or(&[])
        } else {
            &bytes
        };
        let mut result = BTreeMap::new();
        for line in data.split(|c| *c == b'\n') {
            if line.len() > WINDOW {
                continue;
            }
            if let Ok(row) = serde_json::from_slice::<Value>(line)
                && let Some(sid) = row["id"].as_str()
            {
                let title = label(row["thread_name"].as_str());
                if !title.is_empty() {
                    result.insert(sid.to_lowercase(), title);
                }
            }
        }
        Ok(result)
    };
    load().unwrap_or_default()
}
pub fn claude_index(project: &Path) -> BTreeMap<String, (i32, String)> {
    let load = || -> Result<BTreeMap<String, (i32, String)>> {
        let doc: Value = serde_json::from_str(&fsutil::regular_text(
            &project.join("sessions-index.json"),
            INDEX_LIMIT,
        )?)?;
        let mut result = BTreeMap::new();
        for row in doc["entries"].as_array().context("缺少索引条目")? {
            if let Some(sid) = row["sessionId"].as_str() {
                for (field, rank) in [("customTitle", 3), ("aiTitle", 2), ("summary", 1)] {
                    let text = label(row[field].as_str());
                    if !text.is_empty() {
                        result.insert(sid.to_lowercase(), (rank, text));
                        break;
                    }
                }
            }
        }
        Ok(result)
    };
    load().unwrap_or_default()
}
fn claude_title(path: &Path, sid: &str, indexed: Option<&(i32, String)>) -> String {
    let mut best = indexed.cloned().unwrap_or_default();
    let load = || -> Result<Vec<u8>> {
        let mut file = fsutil::open_regular(path)?;
        let size = file.metadata()?.len();
        let mut bytes = Vec::new();
        if size <= 2 * WINDOW as u64 {
            file.take(2 * WINDOW as u64).read_to_end(&mut bytes)?;
        } else {
            (&mut file).take(WINDOW as u64).read_to_end(&mut bytes)?;
            if let Some(last) = bytes.iter().rposition(|c| *c == b'\n') {
                bytes.truncate(last + 1);
            } else {
                bytes.clear();
            }
            file.seek(SeekFrom::Start(size - WINDOW as u64 - 1))?;
            let mut tail = Vec::new();
            file.take(WINDOW as u64 + 1).read_to_end(&mut tail)?;
            if let Some(first) = tail.iter().position(|c| *c == b'\n') {
                bytes.extend_from_slice(&tail[first + 1..]);
            }
        }
        Ok(bytes)
    };
    if let Ok(bytes) = load() {
        for line in bytes.split(|c| *c == b'\n') {
            if ![
                b"custom-title".as_slice(),
                b"ai-title".as_slice(),
                b"summary".as_slice(),
            ]
            .iter()
            .any(|marker| line.windows(marker.len()).any(|p| p == *marker))
            {
                continue;
            }
            if let Ok(row) = serde_json::from_slice::<Value>(line) {
                if row.get("sessionId").is_some_and(|v| v != sid) {
                    continue;
                }
                let (field, rank) = match row["type"].as_str() {
                    Some("custom-title") => ("customTitle", 3),
                    Some("ai-title") => ("aiTitle", 2),
                    Some("summary") => ("summary", 1),
                    _ => continue,
                };
                let text = label(row[field].as_str());
                if !text.is_empty() && rank >= best.0 {
                    best = (rank, text);
                }
            }
        }
    }
    best.1
}

#[derive(Default)]
struct Session {
    bytes: u64,
    latest: i64,
    states: BTreeSet<String>,
    protected: bool,
}
#[allow(clippy::too_many_arguments)] // Explicit scan context shared by CLI, TUI and MCP.
pub fn collect(
    agent: &str,
    root: &Path,
    policy: &Policy,
    rules: &Value,
    now: i64,
    full: bool,
    limit: usize,
    progress: &Progress,
) -> Result<(Value, Vec<Value>)> {
    let mut errors = Vec::new();
    let mut groups: BTreeMap<String, Vec<Value>> = if agent == "codex" {
        ["sessions", "archived_sessions", "worktrees"]
            .into_iter()
            .map(|s| (s.into(), Vec::new()))
            .collect()
    } else {
        BTreeMap::from([("sessions".into(), Vec::new())])
    };
    let mut sessions: BTreeMap<String, Session> = BTreeMap::new();
    let mut auxiliary: BTreeMap<String, i64> = BTreeMap::new();
    let mut titles = if agent == "codex" {
        codex_titles(root)
    } else {
        BTreeMap::new()
    };
    let mut index = BTreeMap::new();
    let mut source = "files".to_string();
    let suffix = Regex::new(&format!(r"(?i)({UUID})\.jsonl(?:\.zst)?$"))?;
    let uuid = Regex::new(&format!("(?i)^{UUID}$"))?;
    let archive = Regex::new(r"^rollout-[\w.-]+\.jsonl(?:\.zst)?$")?;
    let mut track = |groups: &mut BTreeMap<String, Vec<Value>>,
                     path: &Path,
                     section: &str,
                     project_index: Option<&BTreeMap<String, (i32, String)>>|
     -> Result<()> {
        let s = fsutil::stat(path)?;
        if !s.regular() {
            return Ok(());
        }
        let name = path.file_name().unwrap_or_default().to_string_lossy();
        let sid = suffix.captures(&name).map(|c| c[1].to_lowercase());
        groups
            .get_mut(section)
            .unwrap()
            .push(json!({"path":path,"bytes":s.allocated,"session_id":sid}));
        let Some(sid) = sid else {
            return Ok(());
        };
        if let Some(project_index) = project_index {
            let title = claude_title(path, &sid, project_index.get(&sid));
            if !title.is_empty() {
                titles.insert(sid.clone(), title);
            }
        }
        let row = sessions.entry(sid).or_default();
        row.bytes += s.allocated;
        row.latest = row.latest.max(s.modified_ns);
        row.states.insert(section.into());
        let rel = path.strip_prefix(root)?;
        row.protected |= policy.protected(agent, rel, false)
            || (agent == "claude" && policy.protected(agent, &rel.with_extension(""), true));
        Ok(())
    };
    let collected = (|| -> Result<()> {
        fsutil::unlinked_path(root)?;
        if agent == "codex" {
            for section in ["sessions", "archived_sessions"] {
                let base = root.join(section);
                if !base.exists() {
                    continue;
                }
                progress.update("盘点会话文件", None, None, format!("Codex/{section}"))?;
                let dev = fsutil::stat(&base)?.dev;
                let outcome = fsutil::walk(&base, progress, &mut |rel, _, name, s| {
                    if s.dev != dev {
                        return Ok(Visit::Skip);
                    }
                    if !s.directory() && archive.is_match(&name.to_string_lossy()) {
                        track(&mut groups, &base.join(rel), section, None)?;
                    }
                    Ok(Visit::Descend)
                });
                if let Err(e) = outcome {
                    progress.check()?;
                    errors.push(e.to_string());
                }
            }
            let worktrees = root.join("worktrees");
            if worktrees.exists() {
                for name in fsutil::Directory::open(&worktrees)?.names()? {
                    let path = worktrees.join(name);
                    if !fsutil::stat(&path)?.directory() {
                        continue;
                    }
                    let mut row = json!({"path":path,"bytes":null});
                    if full {
                        match fsutil::group_size(&path, progress) {
                            Ok(n) => row["bytes"] = json!(n),
                            Err(e) => errors.push(e.to_string()),
                        }
                    }
                    groups.get_mut("worktrees").unwrap().push(row);
                }
            }
            progress.update("核对会话活动", None, None, "Codex")?;
            let found = thread_index(root)?;
            index = found.0;
            source = found.1;
        } else {
            let base = root.join("projects");
            if base.exists() {
                let mut indices = BTreeMap::new();
                fsutil::walk(&base, progress, &mut |rel, _, name, s| {
                    let parts: Vec<_> = rel.iter().collect();
                    let depth = parts.len();
                    let path = base.join(rel);
                    if depth == 1 && s.directory() {
                        indices.insert(parts[0].to_os_string(), claude_index(&path));
                    }
                    if depth == 2 && s.directory() && !uuid.is_match(&name.to_string_lossy()) {
                        return Ok(Visit::Skip);
                    }
                    if depth == 2
                        && s.regular()
                        && name.to_string_lossy().ends_with(".jsonl")
                        && uuid.is_match(&path.file_stem().unwrap_or_default().to_string_lossy())
                    {
                        track(&mut groups, &path, "sessions", indices.get(parts[0]))?;
                    }
                    if depth >= 2 && uuid.is_match(&parts[1].to_string_lossy()) {
                        let sid = parts[1].to_string_lossy().to_lowercase();
                        let value = auxiliary.entry(sid).or_default();
                        *value = (*value).max(s.modified_ns);
                    }
                    Ok(Visit::Descend)
                })?;
            }
        }
        Ok(())
    })();
    if let Err(e) = collected {
        progress.check()?;
        errors.push(e.to_string());
    }
    let mut rows = Vec::new();
    let mut histogram = [0u64; 5];
    for (sid, raw) in sessions {
        let archived = raw.states == BTreeSet::from(["archived_sessions".into()]);
        let mut uncertain =
            raw.states.len() != 1 || (source == "index+files" && !index.contains_key(&sid));
        let mut latest = raw.latest.max(*auxiliary.get(&sid).unwrap_or(&0));
        let mut pinned = false;
        let mut title = titles.get(&sid).cloned().unwrap_or_default();
        if let Some(thread) = index.get(&sid) {
            pinned = thread.pinned;
            latest = latest.max(thread.latest);
            if !thread.name.is_empty() {
                title = thread.name.clone();
            } else if title.is_empty() {
                title = thread.title.clone();
            }
            uncertain |=
                !matches!(thread.archived, Some(0 | 1)) || archived != (thread.archived == Some(1));
        }
        let age = (now.saturating_sub(latest) / DAY_NS).max(0);
        histogram[SESSION_DAYS.iter().filter(|d| age >= **d).count()] += 1;
        let held = if !errors.is_empty() {
            Some("盘点不完整")
        } else if pinned {
            Some("已置顶")
        } else if raw.protected {
            Some("命中保护路径")
        } else if uncertain {
            Some("会话状态未核实")
        } else if latest > now {
            Some("活动时间异常")
        } else if agent == "codex" && env::var("CODEX_THREAD_ID").is_ok_and(|id| id == sid) {
            Some("当前运行的会话")
        } else {
            None
        };
        let route = if held.is_some() {
            None
        } else {
            Some(if agent == "claude" {
                "review"
            } else if archived {
                "delete"
            } else {
                "archive"
            })
        };
        rows.push(json!({"session_id":sid,"title":if title.is_empty(){None}else{Some(title)},"idle_days":age,"bytes":raw.bytes,"route":route,"latest_ns":latest,"hold_reason":held,
            "activity":activity::classify(Some(latest),now,if source=="index+files"{"会话索引 / 文件最近修改"}else{"会话文件 / 子代理文件最近修改"},errors.is_empty(),held==Some("当前运行的会话"),if !errors.is_empty(){Some("会话盘点不完整")}else if latest>now{Some("活动时间异常")}else{None}),
            "state":if uncertain{"unverified"}else if agent=="claude"{"unknown"}else if archived{"archived"}else{"open"}}));
    }
    rows.sort_by(|a, b| {
        b["idle_days"]
            .as_i64()
            .cmp(&a["idle_days"].as_i64())
            .then(b["bytes"].as_u64().cmp(&a["bytes"].as_u64()))
            .then(a["session_id"].as_str().cmp(&b["session_id"].as_str()))
    });
    let mut summary = json!({});
    for (name, mut items) in groups {
        items.sort_by(|a, b| {
            b["bytes"]
                .as_u64()
                .unwrap_or(0)
                .cmp(&a["bytes"].as_u64().unwrap_or(0))
                .then(a["path"].as_str().cmp(&b["path"].as_str()))
        });
        let total = if (name == "worktrees" && !full) || items.iter().any(|r| r["bytes"].is_null())
        {
            None
        } else {
            Some(
                items
                    .iter()
                    .map(|r| r["bytes"].as_u64().unwrap_or(0))
                    .sum::<u64>(),
            )
        };
        summary[&name] = json!({"count":items.len(),"bytes":total,"entries":items.iter().take(limit).collect::<Vec<_>>()});
    }
    let mut recommendations = json!({"archive":{"count":0,"bytes":0,"entries":[]},"delete":{"count":0,"bytes":0,"entries":[]}});
    for row in &rows {
        let age = row["idle_days"].as_i64().unwrap_or(0);
        let kind = row["route"].as_str().unwrap_or("");
        let action = if ["delete", "review"].contains(&kind)
            && age >= rules["delete_days"].as_i64().unwrap_or(20)
        {
            Some("delete")
        } else if ["archive", "review"].contains(&kind)
            && age >= rules["archive_days"].as_i64().unwrap_or(10)
        {
            Some("archive")
        } else {
            None
        };
        if let Some(action) = action {
            let r = &mut recommendations[action];
            r["count"] = json!(r["count"].as_u64().unwrap() + 1);
            r["bytes"] = json!(r["bytes"].as_u64().unwrap() + row["bytes"].as_u64().unwrap());
            if r["entries"].as_array().unwrap().len() < limit {
                r["entries"].as_array_mut().unwrap().push(row.clone());
            }
        }
    }
    let histogram: BTreeMap<_, _> = BUCKETS.into_iter().zip(histogram).collect();
    let levels: BTreeMap<_, _> = ["high", "medium", "low", "very_low", "unknown"]
        .into_iter()
        .map(|k| {
            (
                k,
                rows.iter().filter(|r| r["activity"]["level"] == k).count(),
            )
        })
        .collect();
    let guidance = if agent == "codex" {
        json!({"archive":"codex archive <SESSION_ID>","delete":"codex delete <SESSION_ID>","worktrees":"先保留要用的改动，再在 Codex 桌面端归档关联聊天；在设置的 Worktrees 页面管理自动回收。"})
    } else {
        json!({"review":"用 claude --resume <SESSION_ID> 核对普通本地会话。无法识别原生归档状态，两类建议都需人工复核。"})
    };
    let report = json!({"root":root,"groups":summary,"session_count":rows.len(),"session_bytes":rows.iter().map(|r|r["bytes"].as_u64().unwrap()).sum::<u64>(),"held":rows.iter().filter(|r|r["route"].is_null()).count(),"activity_source":source,"histogram":histogram,"activity_levels":levels,"policy":rules,"recommendations":recommendations,"errors":errors,"guidance":guidance});
    Ok((report, rows))
}
#[allow(clippy::too_many_arguments)] // Explicit scan context shared by CLI, TUI and MCP.
pub fn inspect(
    layout: &Layout,
    policy: &Policy,
    agent: &str,
    archive_days: Option<i64>,
    delete_days: Option<i64>,
    full: bool,
    limit: usize,
    progress: &Progress,
) -> Result<(Value, BTreeMap<String, Vec<Value>>)> {
    let now = fsutil::now_ns();
    let mut doc = json!({"mode":"inventory","generated_at":chrono::DateTime::from_timestamp_nanos(now).to_rfc3339_opts(chrono::SecondsFormat::Secs,false),"full":full,"limit":limit,"agents":{},"errors":[]});
    let mut all_rows = BTreeMap::new();
    for agent in if agent == "all" {
        vec!["codex", "claude"]
    } else {
        vec![agent]
    } {
        if !["codex", "claude"].contains(&agent) {
            bail!("会话盘点仅支持 Codex / Claude");
        }
        let mut rules = policy.sessions[agent].clone();
        if let Some(d) = archive_days {
            rules["archive_days"] = json!(d);
        }
        if let Some(d) = delete_days {
            rules["delete_days"] = json!(d);
        }
        let (data, rows) = collect(
            agent,
            &layout.roots[agent],
            policy,
            &rules,
            now,
            full,
            limit,
            progress,
        )?;
        doc["errors"]
            .as_array_mut()
            .unwrap()
            .extend(data["errors"].as_array().unwrap().clone());
        doc["agents"][agent] = data;
        all_rows.insert(agent.into(), rows);
    }
    if !doc["agents"]["codex"].is_null() {
        for key in ["root", "groups", "guidance"] {
            doc[key] = doc["agents"]["codex"][key].clone();
        }
    }
    Ok((doc, all_rows))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn titles_strip_ansi_bidi_and_messages() {
        assert_eq!(
            label(Some("\x1b[31m复核\n</script>\u{202e}周报\x1b[0m")),
            "复核 </script> 周报"
        );
        let temp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(temp.path()).unwrap();
        let path = root.join("chat.jsonl");
        fs::write(&path,"{\"type\":\"user\",\"message\":{\"type\":\"summary\",\"summary\":\"PRIVATE_BODY\"}}\n{\"type\":\"custom-title\",\"customTitle\":\"正确名称\",\"sessionId\":\"id\"}\n{\"type\":\"ai-title\",\"aiTitle\":\"低优先级\",\"sessionId\":\"id\"}\n").unwrap();
        assert_eq!(claude_title(&path, "id", None), "正确名称");
        assert_eq!(claude_title(&path, "different", None), "");
    }
    #[test]
    fn sqlite_read_does_not_create_journals() {
        let temp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(temp.path()).unwrap();
        let path = root.join("state_5.sqlite");
        let db = Connection::open(&path).unwrap();
        db.execute_batch("CREATE TABLE threads(id TEXT, archived INTEGER, title TEXT); INSERT INTO threads VALUES('id',0,'Title');").unwrap();
        drop(db);
        let before = fs::read(&path).unwrap();
        let index = thread_index(&root).unwrap();
        assert_eq!(index.0["id"].title, "Title");
        assert_eq!(fs::read(&path).unwrap(), before);
        assert_eq!(fs::read_dir(&root).unwrap().count(), 1);
    }
    #[test]
    fn partial_wal_sidecars_are_rejected() {
        let temp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(temp.path()).unwrap();
        let path = root.join("state_5.sqlite");
        fs::write(&path, "placeholder").unwrap();
        fs::write(root.join("state_5.sqlite-wal"), "").unwrap();
        assert!(sqlite_read(&path).is_err());
    }
}
