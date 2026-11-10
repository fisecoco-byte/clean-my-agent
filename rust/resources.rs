use crate::{
    dependency_report::FIELDS,
    deps,
    fsutil::{self, Visit},
    policy::{self, DAY_NS, Layout, Policy},
    process::Snapshot,
    progress::Progress,
    storage,
};
use anyhow::{Context, Result, bail};
use regex::Regex;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    env, fs,
    path::{Path, PathBuf},
    sync::LazyLock,
};
pub const FAMILIES: &[&str] = &[
    "codex",
    "claude",
    "cursor",
    "copilot",
    "t3",
    "antigravity",
    "filo",
    "qoder",
    "opencode",
    "chrome-devtools",
    "agent-browser",
];
const LEAVES: &[&str] = &[
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
];
static VERSION: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?:\d+(?:\.\d+)+[-\w.]*|local-[a-f0-9]{8,}-[-\w.]+)$").unwrap());
static CHROME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^chrome-\d+(?:\.\d+)+$").unwrap());
#[derive(Clone, Debug)]
pub struct Rule {
    pub agent: String,
    pub category: String,
    pub root: PathBuf,
    pub mode: String,
    pub active: Option<PathBuf>,
}
pub fn children(root: &Path, errors: &mut Vec<String>) -> Vec<PathBuf> {
    if fs::symlink_metadata(root).is_err_and(|e| e.kind() == std::io::ErrorKind::NotFound) {
        return Vec::new();
    }
    let read = (|| -> Result<Vec<PathBuf>> {
        fsutil::unlinked_path(root)?;
        Ok(fsutil::Directory::open(root)?
            .names()?
            .into_iter()
            .map(|p| root.join(p))
            .collect())
    })();
    match read {
        Ok(rows) => rows,
        Err(e) => {
            errors.push(format!("{}: {e}", root.display()));
            Vec::new()
        }
    }
}
pub fn rules(layout: &Layout, errors: &mut Vec<String>) -> Result<Vec<Rule>> {
    let home = policy::home()?;
    let mut rows = Vec::new();
    let mut add =
        |agent: &str, category: &str, root: PathBuf, mode: &str, active: Option<PathBuf>| {
            rows.push(Rule {
                agent: agent.into(),
                category: category.into(),
                root,
                mode: mode.into(),
                active,
            })
        };
    let daemon = layout.roots["codex"].join("packages/app-server-daemon");
    add(
        "codex",
        "agent-version",
        daemon.join("releases"),
        "daemon",
        Some(daemon.join("current")),
    );
    for (agent, path, launch) in [
        (
            "claude",
            ".local/share/claude/versions",
            ".local/bin/claude",
        ),
        (
            "cursor",
            ".local/share/cursor-agent/versions",
            ".local/bin/cursor-agent",
        ),
        ("copilot", ".copilot/pkg/universal", ".local/bin/copilot"),
    ] {
        add(
            agent,
            "agent-version",
            home.join(path),
            "versions",
            Some(home.join(launch)),
        );
    }
    add(
        "codex",
        "runtime",
        home.join(".cache/codex-runtimes"),
        "runtime",
        None,
    );
    for path in [".tmp/bundled-marketplaces", ".tmp/marketplaces/.staging"] {
        add(
            "codex",
            "marketplace-staging",
            layout.roots["codex"].join(path),
            "marketplace",
            None,
        );
    }
    for path in [
        "plugins/.remote-plugin-install-staging",
        "plugins/.marketplace-plugin-source-staging",
    ] {
        add(
            "codex",
            "marketplace-staging",
            layout.roots["codex"].join(path),
            "review",
            None,
        );
    }
    add(
        "opencode",
        "desktop-cache",
        home.join(".cache/opencode"),
        "leaf",
        None,
    );
    add(
        "agent-browser",
        "agent-version",
        home.join(".agent-browser/browsers"),
        "browser",
        None,
    );
    add(
        "cursor",
        "logs",
        home.join(".local/share/cursor-agent"),
        "logs",
        None,
    );
    for (agent, profile) in [
        (
            "antigravity",
            home.join(".gemini/antigravity-browser-profile"),
        ),
        (
            "chrome-devtools",
            home.join(".cache/chrome-devtools-mcp/chrome-profile"),
        ),
    ] {
        for leaf in LEAVES.iter().copied().chain([
            "DawnCache",
            "GrShaderCache",
            "GraphiteDawnCache",
            "Service Worker/CacheStorage",
        ]) {
            add(
                agent,
                "desktop-cache",
                profile.join("Default").join(leaf),
                "leaf",
                None,
            );
        }
        for leaf in [
            "GraphiteDawnCache",
            "component_crx_cache",
            "extensions_crx_cache",
        ] {
            add(agent, "desktop-cache", profile.join(leaf), "leaf", None);
        }
    }
    if cfg!(target_os = "macos") {
        let support = home.join("Library/Application Support");
        let cache = home.join("Library/Caches/Codex");
        for profile in [
            "Default",
            "Default/Partitions/codex-browser-app",
            "codex-browser-app",
        ] {
            for leaf in ["Cache", "Code Cache"] {
                add(
                    "codex",
                    "desktop-cache",
                    cache.join(profile).join(leaf),
                    "leaf",
                    None,
                );
            }
        }
        add(
            "codex",
            "update-staging",
            home.join("Library/Caches/com.openai.codex/org.sparkle-project.Sparkle/Installation"),
            "sparkle",
            None,
        );
        add(
            "codex",
            "crash-reports",
            support.join("Codex/Crashpad/pending"),
            "pending",
            None,
        );
        for (agent, folder) in [
            ("claude", "Claude"),
            ("antigravity", "Antigravity"),
            ("filo", "Filo/production"),
            ("qoder", "Qoder"),
        ] {
            for leaf in LEAVES {
                add(
                    agent,
                    "desktop-cache",
                    support.join(folder).join(leaf),
                    "leaf",
                    None,
                );
            }
        }
        add(
            "claude",
            "desktop-cache",
            support.join("Claude/sentry"),
            "leaf",
            None,
        );
        for leaf in ["CachedData", "CachedExtensionVSIXs", "logs"] {
            add(
                "qoder",
                if leaf == "logs" {
                    "logs"
                } else {
                    "desktop-cache"
                },
                support.join("Qoder").join(leaf),
                "leaf",
                None,
            );
        }
        for leaf in ["claude-code", "claude-code-vm"] {
            add(
                "claude",
                "agent-version",
                support.join("Claude").join(leaf),
                "sdk",
                Some(support.join("Claude/claude-code-vm/.sdk-version")),
            );
        }
        for folder in ["t3code-v2", "T3 Code (Alpha)"] {
            for partition in children(&support.join(folder).join("Partitions"), errors) {
                if partition
                    .file_name()
                    .is_some_and(|n| n.to_string_lossy().starts_with("t3code-preview-"))
                {
                    for leaf in ["Cache", "Code Cache"] {
                        add("t3", "desktop-cache", partition.join(leaf), "leaf", None);
                    }
                }
            }
        }
    }
    Ok(rows)
}
pub fn paths(
    rule: &Rule,
    errors: &mut Vec<String>,
    include: Option<&Path>,
    progress: &Progress,
) -> Result<Vec<PathBuf>> {
    if ["leaf", "file", "review"].contains(&rule.mode.as_str()) {
        return Ok(
            if fs::symlink_metadata(&rule.root).is_ok() || include == Some(&rule.root) {
                vec![rule.root.clone()]
            } else {
                vec![]
            },
        );
    }
    if rule.mode == "logs" {
        let mut rows = BTreeSet::new();
        if rule.root.is_dir() && fsutil::unlinked_path(&rule.root).is_ok() {
            let collected = fsutil::walk(&rule.root, progress, &mut |relative, _, name, s| {
                if s.directory() && name == "versions" {
                    return Ok(Visit::Skip);
                }
                if !s.directory() && name.to_string_lossy().ends_with(".log") {
                    rows.insert(rule.root.join(relative));
                }
                Ok(Visit::Descend)
            });
            if let Err(e) = collected {
                progress.check()?;
                errors.push(e.to_string());
            }
        }
        if let Some(path) = include
            && path != rule.root
            && path.starts_with(&rule.root)
            && path
                .file_name()
                .is_some_and(|n| n.to_string_lossy().ends_with(".log"))
            && !path
                .strip_prefix(&rule.root)?
                .iter()
                .any(|s| s == "versions")
        {
            rows.insert(path.into());
        }
        return Ok(rows.into_iter().collect());
    }
    let mut rows = children(&rule.root, errors);
    if let Some(path) = include
        && path.parent() == Some(&rule.root)
        && !rows.contains(&path.to_path_buf())
    {
        rows.push(path.into());
    }
    Ok(rows
        .into_iter()
        .filter(|p| {
            !p.file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .starts_with('.')
        })
        .collect())
}
pub fn version_keep(rule: &Rule) -> Result<(BTreeSet<PathBuf>, PathBuf)> {
    let entries = children(&rule.root, &mut Vec::new());
    let marker = rule.active.as_ref().context("活动版本标记缺失")?;
    let active = if rule.mode == "sdk" {
        let name = fsutil::regular_text(marker, 4096)?;
        let name = name.trim();
        if !VERSION.is_match(name) {
            bail!("桌面 SDK 活动版本无法确认");
        }
        let active = rule.root.join(name);
        if !active.exists() || fsutil::stat(&active)?.symlink() {
            bail!("桌面 SDK 活动版本不存在");
        }
        active
    } else {
        if !fsutil::stat(marker).is_ok_and(|s| s.symlink()) {
            bail!("活动启动链接不存在或无法确认；所有版本保留");
        }
        let target = fsutil::resolve(marker)?;
        if !target.exists() {
            bail!("活动启动链接不存在或无法确认；所有版本保留");
        }
        entries
            .iter()
            .find(|p| target.starts_with(p))
            .context("活动启动链接不指向已知版本")?
            .clone()
    };
    let mut keep = BTreeSet::from([active.clone()]);
    if rule.mode == "daemon" {
        let marker = rule.root.parent().unwrap().join("auto-update-version");
        if fs::symlink_metadata(&marker).is_ok() {
            let name = fsutil::regular_text(&marker, 4096)?;
            let name = name.trim();
            if !VERSION.is_match(name) || !rule.root.join(name).exists() {
                bail!("daemon 更新标记无法确认；保留");
            }
            keep.insert(rule.root.join(name));
        }
    }
    let mut previous = Vec::new();
    for p in entries {
        if !keep.contains(&p)
            && VERSION.is_match(&p.file_name().unwrap_or_default().to_string_lossy())
        {
            let s = fsutil::stat(&p)?;
            if !s.symlink() {
                previous.push((s.modified_ns, p));
            }
        }
    }
    previous.sort_by_key(|a| std::cmp::Reverse(a.0));
    if let Some((_, p)) = previous.first() {
        keep.insert(p.clone());
    }
    Ok((keep, active))
}
pub fn browser_keep(rule: &Rule, include: Option<&Path>) -> Result<BTreeSet<PathBuf>> {
    let entries: Vec<_> = children(&rule.root, &mut Vec::new())
        .into_iter()
        .filter(|p| CHROME.is_match(&p.file_name().unwrap_or_default().to_string_lossy()))
        .collect();
    let complete:Vec<_>=entries.iter().filter(|p|["Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing","chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing","chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing","chrome-linux64/chrome","chrome-win64/chrome.exe"].iter().any(|n|p.join(n).is_file())).cloned().collect();
    let newest = complete
        .iter()
        .max()
        .context("未识别完整 Chrome 安装；保留")?;
    let mut keep = BTreeSet::from([newest.clone()]);
    keep.extend(entries.iter().filter(|p| !complete.contains(p)).cloned());
    let config = rule.root.parent().unwrap().join("config.json");
    let text = if fs::symlink_metadata(&config).is_ok() {
        fsutil::regular_text(&config, 1024 * 1024)?
    } else {
        String::new()
    };
    fn strings(v: &Value, output: &mut Vec<String>) {
        match v {
            Value::String(s) => output.push(s.clone()),
            Value::Array(a) => {
                for v in a {
                    strings(v, output)
                }
            }
            Value::Object(o) => {
                for v in o.values() {
                    strings(v, output)
                }
            }
            _ => (),
        }
    }
    let mut pins = vec![env::var("AGENT_BROWSER_EXECUTABLE_PATH").unwrap_or_default()];
    if !text.is_empty() {
        strings(&serde_json::from_str(&text)?, &mut pins);
    }
    for p in entries.into_iter().chain(include.map(Path::to_path_buf)) {
        if text.contains(p.file_name().unwrap().to_string_lossy().as_ref())
            || pins
                .iter()
                .filter(|s| !s.is_empty())
                .any(|s| fsutil::resolve(Path::new(s)).is_ok_and(|target| target.starts_with(&p)))
        {
            keep.insert(p);
        }
    }
    Ok(keep)
}
fn app_build(path: &Path) -> Option<u64> {
    let file = fsutil::open_regular(path).ok()?;
    if file.metadata().ok()?.len() > 1024 * 1024 {
        return None;
    }
    let value = plist::Value::from_reader(file).ok()?;
    let version = value.as_dictionary()?.get("CFBundleVersion")?;
    version
        .as_string()
        .and_then(|s| s.parse().ok())
        .or_else(|| version.as_unsigned_integer())
}
fn newer_update(path: &Path, progress: &Progress) -> Result<bool> {
    let installed: Vec<_> = [
        PathBuf::from("/Applications/Codex.app"),
        policy::home()?.join("Applications/Codex.app"),
    ]
    .iter()
    .filter_map(|p| app_build(&p.join("Contents/Info.plist")))
    .collect();
    let Some(minimum) = installed.iter().min() else {
        return Ok(false);
    };
    let mut found = false;
    fsutil::walk(path, progress, &mut |relative, _, name, s| {
        let parent = relative.parent().unwrap();
        if name == "Info.plist"
            && parent.file_name().is_some_and(|n| n == "Contents")
            && parent
                .parent()
                .and_then(|p| p.file_name())
                .is_some_and(|n| n == "Codex.app")
        {
            found |= app_build(&path.join(relative)).is_some_and(|v| v > *minimum);
        }
        Ok(if s.directory() && relative.components().count() >= 6 {
            Visit::Skip
        } else {
            Visit::Descend
        })
    })?;
    Ok(found)
}
pub fn semantics(rule: &Rule, original: &Path, physical: &Path, progress: &Progress) -> Result<()> {
    let name = original.file_name().unwrap_or_default().to_string_lossy();
    match rule.mode.as_str() {
        "review" => bail!("此安装暂存格式尚未被上游规则确认；仅盘点"),
        "versions" | "daemon" | "sdk" => {
            if !VERSION.is_match(&name) {
                bail!("版本名称无法验证；保留");
            }
            if version_keep(rule)?.0.contains(original) {
                bail!("活动版本或保留的上一版本");
            }
        }
        "browser" => {
            if !CHROME.is_match(&name) || browser_keep(rule, Some(original))?.contains(original) {
                bail!("活动、配置引用或未完成的 Chrome 安装；保留");
            }
        }
        "runtime" => {
            let active = physical.join("runtime.json").exists()
                && ["node", "python"]
                    .iter()
                    .any(|n| physical.join("dependencies").join(n).is_dir());
            let leftover = ["tmp", "temp", "incomplete", "partial"]
                .iter()
                .any(|p| name.starts_with(p))
                || [".tmp", ".incomplete", "-incomplete", ".partial"]
                    .iter()
                    .any(|p| name.ends_with(p));
            if active
                || (!leftover
                    && (physical.join("runtime.json").exists()
                        || physical.join("dependencies").exists()))
            {
                bail!("完整或用途未确认的运行环境；保留");
            }
        }
        "marketplace" => {
            if ![
                "openai-bundled.staging-",
                "marketplace-upgrade-",
                "marketplace-add-",
            ]
            .iter()
            .any(|p| name.starts_with(p))
            {
                bail!("完成的市场或非 staging 项；保留");
            }
        }
        "sparkle" if newer_update(physical, progress)? => {
            bail!("暂存更新比当前安装版本新；保留");
        }
        _ => (),
    }
    if ["daemon", "browser", "runtime", "marketplace", "sparkle"].contains(&rule.mode.as_str())
        && !physical.is_dir()
    {
        bail!("不是预期的资源目录");
    }
    if ["pending", "file", "logs"].contains(&rule.mode.as_str()) && !physical.is_file() {
        bail!("不是普通诊断文件");
    }
    Ok(())
}
pub fn running(rule: &Rule, processes: &Snapshot, physical: &Path) -> Result<Value> {
    if let Some(error) = &processes.error {
        bail!("{error}");
    }
    let pattern = match rule.agent.as_str() {
        "codex" => r"(?i)(?:/Codex\.app/|(?:^|[/\s])codex(?:[\s/]|$)|sparkle|crashpad_handler)",
        "claude" => r"(?i)(?:/Claude\.app/|(?:^|[/\s])claude(?:[\s/]|$))",
        "cursor" => r"(?i)(?:cursor-agent|/Cursor\.app/)",
        "copilot" => r"(?i)(?:^|[/\s])copilot(?:[\s/]|$)",
        "t3" => r"(?i)(?:T3 Code|t3code)",
        "antigravity" => r"(?i)(?:antigravity|(?:^|[/\s])gemini(?:[\s/]|$))",
        "filo" => r"(?i)/Filo\.app/",
        "qoder" => r"(?i)(?:/Qoder\.app/|(?:^|[/\s])qoder(?:[\s/]|$))",
        "opencode" => r"(?i)(?:^|[/\s])opencode(?:[\s/]|$)",
        "chrome-devtools" => "chrome-devtools-mcp",
        "agent-browser" => r"(?:agent-browser|[.]agent-browser/browsers/)",
        _ => bail!("未知 Agent"),
    };
    let expression = Regex::new(pattern)?;
    for (pid, command) in &processes.commands {
        if !["versions", "daemon"].contains(&rule.mode.as_str()) && expression.is_match(command) {
            bail!("{} 或更新/上传进程正在运行（{pid}）；保留", rule.agent);
        }
    }
    let result = processes.check(
        physical,
        if ["leaf", "file"].contains(&rule.mode.as_str()) {
            None
        } else {
            Some(&rule.root)
        },
    );
    if result["state"] != "idle" {
        bail!("{}", result["reason"].as_str().unwrap_or("运行状态未知"));
    }
    Ok(result)
}
pub fn check_policy(
    rule: &Rule,
    original: &Path,
    physical: &Path,
    layout: &Layout,
    policy: &Policy,
    newest: i64,
    progress: &Progress,
) -> Result<()> {
    if cfg!(windows) {
        bail!("Windows 原生版仅盘点");
    }
    fsutil::unlinked_path(original)?;
    fsutil::unlinked_path(physical)?;
    if fsutil::stat(physical)?.symlink() {
        bail!("路径含链接或重解析点；保留");
    }
    let home = policy::home()?;
    let base = layout
        .roots
        .get(&rule.agent)
        .filter(|b| original != *b && original.starts_with(b))
        .unwrap_or(&home);
    if policy.protected(&rule.agent, original.strip_prefix(base)?, physical.is_dir()) {
        bail!("命中保护路径");
    }
    semantics(rule, original, physical, progress)?;
    let mut days = policy.retention[if ["logs", "crash-reports"].contains(&rule.category.as_str()) {
        "logs"
    } else {
        "cache"
    }];
    if ["pending", "sparkle"].contains(&rule.mode.as_str()) {
        days = days.max(30);
    }
    if newest > fsutil::now_ns().saturating_sub(days.saturating_mul(DAY_NS)) {
        bail!("最近 {days} 天有改动；保留");
    }
    if rule.mode == "daemon"
        && !fsutil::stat(&rule.root.parent().unwrap().join("install.lock"))
            .is_ok_and(|s| s.regular())
    {
        bail!("daemon 安装锁不可核实；保留");
    }
    Ok(())
}
pub fn inspect(
    layout: &Layout,
    selected: &[String],
    policy: &Policy,
    full: bool,
    only: Option<&Path>,
    progress: &Progress,
) -> Result<Value> {
    let mut rows = Vec::new();
    let mut errors = Vec::new();
    let all = selected.iter().any(|s| s == "codex") && selected.iter().any(|s| s == "claude");
    let processes = if full && !cfg!(windows) {
        Some(Snapshot::read(progress))
    } else {
        None
    };
    for rule in rules(layout, &mut errors)? {
        if !all && !selected.contains(&rule.agent) {
            continue;
        }
        for path in paths(&rule, &mut errors, None, progress)? {
            if only.is_some_and(|p| p != path) {
                continue;
            }
            let name = path.file_name().unwrap_or_default().to_string_lossy();
            let mut row = json!({"agent":rule.agent,"category":rule.category,"path":path,"name":name,"label":format!("{} / {} / {name}",rule.agent,path.parent().unwrap().file_name().unwrap_or_default().to_string_lossy()),"version":if rule.category=="agent-version"{Some(&name)}else{None},"status":"retained","reason":"未测量","can_stage":false,"referenced_by":[],"target":null,"kind":"directory","installation":null,"runtime":null,"included_in_primary":layout.roots.values().any(|p|p!=&path&&path.starts_with(p)),"measurement":"not-requested"});
            for f in FIELDS {
                row[*f] = Value::Null;
            }
            progress.update(
                "盘点 Agent 资源",
                None,
                None,
                format!("{} / {name}", rule.agent),
            )?;
            let collect = (|| -> Result<()> {
                let info = fsutil::stat(&path)?;
                row["stamp"] = info.stamp();
                row["kind"] = json!(if info.directory() {
                    "directory"
                } else {
                    "file"
                });
                if info.symlink() || fsutil::unlinked_path(&path).is_err() {
                    row["measurement"] = json!("link");
                    row["kind"] = json!("symlink");
                    row["reason"] = json!("链接仅盘点，不跟随或清理");
                    return Ok(());
                }
                if ["versions", "daemon", "sdk"].contains(&rule.mode.as_str())
                    && let Ok((_, active)) = version_keep(&rule)
                    && path == active
                {
                    row["referenced_by"] = json!([rule.active]);
                }
                if !full {
                    return Ok(());
                }
                let newest;
                if info.directory() {
                    deps::check_mounts(&path)?;
                    let (sizes, stamp, nested) =
                        storage::measure(&path, progress, &mut |_, _| Ok(()))?;
                    newest = stamp;
                    row.as_object_mut()
                        .unwrap()
                        .extend(sizes.as_object().unwrap().clone());
                    if nested && rule.mode != "marketplace" {
                        bail!("包含嵌套源码仓库；保留");
                    }
                } else if info.regular() {
                    newest = info.modified_ns;
                    row["directory_bytes"] = json!(info.allocated);
                    row["unknown_bytes"] = json!(info.allocated);
                    row["known_shared_bytes"] = json!(0);
                    row["reclaimable_min_bytes"] = json!(0);
                    row["reclaimable_max_bytes"] = json!(info.allocated);
                    if info.links != 1 {
                        bail!("文件存在其他硬链接；保留");
                    }
                } else {
                    bail!("特殊文件；保留");
                }
                row["measurement"] = json!("measured");
                check_policy(&rule, &path, &path, layout, policy, newest, progress)?;
                row["runtime"] =
                    running(&rule, processes.as_ref().context("运行状态未知")?, &path)?;
                row["status"] = json!("candidate");
                row["can_stage"] = json!(true);
                row["reason"] = json!("符合资源规则；可暂存，暂存释放 0 B");
                Ok(())
            })();
            if let Err(e) = collect {
                progress.check()?;
                row["status"] = json!("retained");
                row["reason"] = json!(e.to_string());
                row["can_stage"] = json!(false);
            }
            rows.push(row);
        }
    }
    if only.is_some() && rows.is_empty() {
        bail!("路径不是已识别的 Agent 资源");
    }
    Ok(
        json!({"mode":if full{"measured"}else{"references_only"},"entries":rows,"errors":errors,"external_targets":{},"title":"Agent 缓存 / 版本 / 残留","totals_note":"目录与共享量不能相加；暂存释放 0 B。","scope":"固定已知路径；活动版本和状态数据保留；额外资源与主目录不重复相加"}),
    )
}
pub fn row(agent: &str, category: &str, path: &Path) -> Value {
    json!({"agent":agent,"category":category,"path":path,"name":path.file_name().unwrap_or_default().to_string_lossy(),"version":null,"status":"retained","reason":"只读盘点；不进入常规清理","can_stage":false,"directory_bytes":null,"shared_bytes":null,"reclaimable_bytes":null,"referenced_by":[]})
}

pub fn resolve_item(item: &Value, layout: &Layout, progress: &Progress) -> Result<(Rule, PathBuf)> {
    let source = item["source"].as_str().context("资源缺少来源")?;
    let base = Path::new(item["root"].as_str().context("资源缺少根路径")?);
    let relative = Path::new(item["path"].as_str().context("资源缺少路径")?);
    let home = policy::home()?;
    if (base != home && layout.roots.get(source).is_none_or(|p| base != p))
        || relative.is_absolute()
        || relative.as_os_str().is_empty()
        || relative
            .components()
            .any(|p| !matches!(p, std::path::Component::Normal(_)))
    {
        bail!("资源原路径无效");
    }
    let original = base.join(relative);
    let mut errors = Vec::new();
    for rule in rules(layout, &mut errors)? {
        if rule.agent == source
            && paths(&rule, &mut errors, Some(&original), progress)?.contains(&original)
        {
            return Ok((rule, original));
        }
    }
    bail!("资源路径已不在已知规则中")
}
pub fn guarded(
    item: &Value,
    layout: &Layout,
    policy: &Policy,
    physical: Option<&Path>,
    progress: &Progress,
) -> Result<Option<fs::File>> {
    let (rule, original) = resolve_item(item, layout, progress)?;
    let physical = physical.unwrap_or(&original);
    let lock = if rule.mode == "daemon" {
        Some(crate::fsops::lock_file(
            &rule.root.parent().unwrap().join("install.lock"),
        )?)
    } else {
        None
    };
    let info = fsutil::stat(physical)?;
    let newest = if info.directory() {
        deps::check_mounts(physical)?;
        let (_, newest, nested) = storage::measure(physical, progress, &mut |_, _| Ok(()))?;
        if nested && rule.mode != "marketplace" {
            bail!("包含嵌套源码仓库；保留");
        }
        newest
    } else {
        if !info.regular() || info.links != 1 {
            bail!("不是独占普通文件");
        }
        info.modified_ns
    };
    check_policy(&rule, &original, physical, layout, policy, newest, progress)?;
    running(&rule, &Snapshot::read(progress), physical)?;
    policy.current()?;
    Ok(lock)
}
pub fn measure_rows(
    rows: &mut [Value],
    layout: &Layout,
    full: bool,
    progress: &Progress,
    errors: &mut Vec<String>,
) -> Result<()> {
    let mut measured = BTreeMap::new();
    let total = rows.len();
    for (index, row) in rows.iter_mut().enumerate() {
        let path = PathBuf::from(row["path"].as_str().context("资源缺少路径")?);
        row["included_in_primary"] = json!(layout.roots.values().any(|p| path.starts_with(p)));
        row["measurement"] = json!("not-requested");
        if row["measure"] == false {
            row["measurement"] = json!(if row["parent_path"].is_string() {
                "component-of-parent"
            } else {
                "configuration-only"
            });
            continue;
        }
        progress.update(
            "盘点 Agent 资源",
            Some(index),
            Some(total),
            row["name"].as_str().unwrap_or(""),
        )?;
        let collect = (|| -> Result<()> {
            let s = fsutil::stat(&path)?;
            row["stamp"] = s.stamp();
            if s.symlink() || fsutil::unlinked_path(&path).is_err() {
                row["measurement"] = json!("link");
                row["reason"] = json!("链接仅盘点，不跟随或清理");
                row["directory_bytes"] = json!(s.allocated);
                return Ok(());
            }
            if !full {
                return Ok(());
            }
            if let Some(alias) = measured.get(&s.identity()) {
                row["measurement"] = json!("alias");
                row["alias_of"] = json!(alias);
                return Ok(());
            }
            measured.insert(s.identity(), path.clone());
            if s.directory() {
                row.as_object_mut().unwrap().extend(
                    storage::measure(&path, progress, &mut |_, _| Ok(()))?
                        .0
                        .as_object()
                        .unwrap()
                        .clone(),
                );
            } else if s.regular() {
                row["directory_bytes"] = json!(s.allocated);
            } else {
                row["measurement"] = json!("unsupported");
                row["reason"] = json!("特殊文件，仅列出路径");
                return Ok(());
            }
            row["measurement"] = json!("measured");
            Ok(())
        })();
        if collect.is_err() {
            progress.check()?;
            row["measurement"] = json!("unknown");
            row["directory_bytes"] = Value::Null;
            row["reason"] = json!("无法完整测量；保留");
            errors.push(format!("无法完整测量资源：{}", path.display()));
        }
    }
    Ok(())
}
