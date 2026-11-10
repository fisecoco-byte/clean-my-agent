use crate::{
    fsutil,
    policy::{self, DAY_NS, Layout},
    progress::Progress,
    resources::{self, children, row},
};
use anyhow::{Context, Result};
use regex::Regex;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    env, fs,
    path::{Path, PathBuf},
    sync::LazyLock,
};
pub fn metadata(path: &Path, errors: &mut Vec<String>, toml: bool) -> Option<Value> {
    if fs::symlink_metadata(path).is_err_and(|e| e.kind() == std::io::ErrorKind::NotFound) {
        return None;
    }
    let load = (|| -> Result<Value> {
        let text = fsutil::regular_text(path, 1024 * 1024)?;
        let data: Value = if toml {
            toml::from_str(&text)?
        } else {
            serde_json::from_str(&text)?
        };
        data.as_object().context("配置不是对象")?;
        Ok(data)
    })();
    match load {
        Ok(data) => Some(data),
        Err(_) => {
            errors.push(format!("配置无法解析，相关版本保留：{}", path.display()));
            None
        }
    }
}
fn small_string(value: &Value) -> Option<&str> {
    value.as_str().filter(|s| s.chars().count() <= 512)
}
fn update(row: &mut Value, values: Value) {
    row.as_object_mut()
        .unwrap()
        .extend(values.as_object().unwrap().clone());
}
fn reference(row: &mut Value, source: impl Into<String>) {
    row["referenced_by"]
        .as_array_mut()
        .unwrap()
        .push(json!(source.into()));
}
pub fn skill_paths(root: &Path, errors: &mut Vec<String>) -> Vec<PathBuf> {
    let mut found = Vec::new();
    for path in children(root, errors) {
        if path.join("SKILL.md").is_file() {
            found.push(path.join("SKILL.md"));
        } else if fsutil::stat(&path).is_ok_and(|s| s.directory())
            && !path
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .starts_with('.')
        {
            for version in children(&path, errors) {
                if version.join("SKILL.md").is_file() {
                    found.push(version.join("SKILL.md"));
                }
            }
        }
    }
    found
}
fn bundle_mcp(rows: &mut Vec<Value>, parent: &Value, servers: &Value) {
    if let Some(servers) = servers.as_object() {
        for name in servers.keys() {
            let mut r = row(
                parent["agent"].as_str().unwrap(),
                "mcp",
                Path::new(parent["path"].as_str().unwrap()),
            );
            update(
                &mut r,
                json!({"name":name,"version":parent["version"],"source":parent["identity"],"group_key":format!("mcp:{}:{name}",parent["identity"].as_str().unwrap()),"parent_path":parent["path"],"measure":false,"reason":"插件内置 MCP，随父插件版本管理；不单独删除"}),
            );
            rows.push(r);
        }
    }
}
static PACKAGE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(?P<name>@[a-z0-9_.-]+/[a-z0-9_.-]+|[a-z0-9][a-z0-9_.-]*)(?:@(?P<version>[a-zA-Z0-9.*^~+_-][a-zA-Z0-9.*^~+_-]*))?$").unwrap()
});
pub fn command_package(args: &[Value]) -> Option<(String, Option<String>)> {
    let mut remaining = args.iter();
    while let Some(arg) = remaining.next() {
        let arg = arg.as_str()?;
        if ["-y", "--yes", "--no", "--no-install", "--", "exec"].contains(&arg) {
            continue;
        }
        if ["--registry", "--cache", "--userconfig", "--prefix"].contains(&arg) {
            remaining.next();
            continue;
        }
        let value = if ["-p", "--package"].contains(&arg) {
            remaining.next()?.as_str()?
        } else if let Some(value) = arg.strip_prefix("--package=") {
            value
        } else {
            if arg.starts_with('-') {
                return None;
            }
            arg
        };
        let capture = PACKAGE.captures(value)?;
        return Some((
            capture["name"].into(),
            capture.name("version").map(|v| v.as_str().into()),
        ));
    }
    None
}
fn configured_mcp(
    rows: &mut Vec<Value>,
    agent: &str,
    cfg: &Path,
    name: &str,
    spec: &Value,
    errors: &mut Vec<String>,
) -> Result<()> {
    let mut r = row(agent, "mcp", cfg);
    update(
        &mut r,
        json!({"name":name,"identity":name,"measure":false,"source":"MCP 配置","group_key":format!("mcp:config:{agent}:{name}"),"referenced_by":[cfg],"reason":"已配置的 MCP；未验证本地安装版本，不删除配置","guidance":"远程服务不对应本地可清理版本；本地服务应先在所属 Agent 中复核。"}),
    );
    if spec["url"].as_str().is_some_and(|s| !s.is_empty())
        || [Some("http"), Some("sse")].contains(&spec["type"].as_str())
    {
        r["reason"] = json!("远程 MCP；无可确认的本地版本占用");
        rows.push(r);
        return Ok(());
    }
    let package = spec["command"]
        .as_str()
        .filter(|c| {
            Path::new(c)
                .file_name()
                .is_some_and(|n| ["npx", "npm", "npx.cmd", "npm.cmd"].iter().any(|v| n == *v))
        })
        .and_then(|_| spec["args"].as_array())
        .and_then(|args| command_package(args));
    let Some((package, version)) = package else {
        rows.push(r);
        return Ok(());
    };
    update(
        &mut r,
        json!({"package":package,"declared_version":version,"group_key":format!("mcp:npm:{package}"),"reason":"配置声明的 npm 包版本或范围；不等于实际缓存版本"}),
    );
    rows.push(r);
    let home = policy::home()?;
    let raw = env::var_os("npm_config_cache")
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".npm"));
    let root = if let Ok(suffix) = raw.strip_prefix("~") {
        home.join(suffix)
    } else {
        raw
    };
    if fsutil::absolute(&root).is_err() {
        errors.push("npm 缓存路径无法验证，未展开 MCP 包缓存".into());
        return Ok(());
    }
    for cache in children(&root.join("_npx"), errors) {
        let path = cache.join("node_modules").join(&package);
        let Some(data) = metadata(&path.join("package.json"), errors, false) else {
            continue;
        };
        if data["name"] != package || small_string(&data["version"]).is_none_or(|s| s.is_empty()) {
            continue;
        }
        let mut cached = row(agent, "mcp", &path);
        update(
            &mut cached,
            json!({"name":name,"package":package,"version":data["version"],"source":"npm npx 缓存","group_key":format!("mcp:npm:{package}"),"referenced_by":[cfg],"reason":"同一包的缓存版本；配置范围、其他项目和运行会话可能仍需使用，保留","guidance":"不按版本号直接删除 npx 缓存；本工具只盘点已登记 MCP 对应的包。"}),
        );
        rows.push(cached);
    }
    Ok(())
}
pub fn inspect(
    layout: &Layout,
    selected: &[String],
    full: bool,
    progress: &Progress,
) -> Result<Value> {
    let mut rows = Vec::new();
    let mut errors = Vec::new();
    let mut plugins = Vec::new();
    let mut skills: Vec<(String, PathBuf, Option<usize>)> = Vec::new();
    let mut configs = Vec::new();
    let mut path_references = Vec::new();
    let home = policy::home()?;
    for agent in ["codex", "claude"] {
        let root = &layout.roots[agent];
        let registry_path = root.join("plugins/installed_plugins.json");
        let registry = metadata(&registry_path, &mut errors, false);
        let empty = json!({});
        let installed = registry
            .as_ref()
            .and_then(|r| r.get("plugins"))
            .unwrap_or(&empty);
        if !installed.is_object() {
            errors.push(format!("插件登记格式未知：{}", registry_path.display()));
        }
        let cfg = if agent == "codex" {
            root.join("config.toml")
        } else {
            home.join(".claude.json")
        };
        let data = metadata(&cfg, &mut errors, agent == "codex").unwrap_or_else(|| json!({}));
        configs.push((
            agent.to_string(),
            cfg.clone(),
            data[if agent == "codex" {
                "mcp_servers"
            } else {
                "mcpServers"
            }]
            .clone(),
        ));
        if agent == "claude"
            && let Some(projects) = data["projects"].as_object()
        {
            for settings in projects.values() {
                if settings.is_object() {
                    configs.push((agent.into(), cfg.clone(), settings["mcpServers"].clone()));
                }
            }
        }
        if let Some(settings) = data["skills"]["config"].as_array() {
            for entry in settings {
                if let Some(path) = entry["path"].as_str() {
                    path_references.push((path.to_string(), format!("{}:skills", cfg.display())));
                }
            }
        }
        for market in children(&root.join("plugins/cache"), &mut errors) {
            if !fsutil::stat(&market).is_ok_and(|s| s.directory()) {
                continue;
            }
            for component in children(&market, &mut errors) {
                if !fsutil::stat(&component).is_ok_and(|s| s.directory()) {
                    continue;
                }
                let name = component.file_name().unwrap_or_default().to_string_lossy();
                let market_name = market.file_name().unwrap_or_default().to_string_lossy();
                let plugin_id = format!("{name}@{market_name}");
                for version in children(&component, &mut errors) {
                    let version_name = version.file_name().unwrap_or_default().to_string_lossy();
                    if version_name.starts_with('.') || !version.is_dir() {
                        continue;
                    }
                    let linked = fsutil::stat(&version)?.symlink();
                    let mut manifest = None;
                    if !linked {
                        for folder in [".codex-plugin", ".claude-plugin"] {
                            manifest = metadata(
                                &version.join(folder).join("plugin.json"),
                                &mut errors,
                                false,
                            );
                            if manifest.is_some() {
                                break;
                            }
                        }
                    }
                    let manifest = manifest.unwrap_or_else(|| json!({}));
                    let mut r = row(agent, "plugin", &version);
                    update(
                        &mut r,
                        json!({"name":name,"identity":plugin_id,"source":market_name,"version":version_name,"manifest_version":small_string(&manifest["version"]),"reason":"活动版本无法完全确认；保留","group_key":format!("plugin:{plugin_id}"),"registry_verified":registry.as_ref().is_some_and(|r|r["plugins"].is_object()),"guidance":"在所属 Agent 的插件管理中复核；同名或版本较旧本身不构成删除依据。"}),
                    );
                    if let Some(records) = installed[&plugin_id].as_array() {
                        for item in records {
                            if item["installPath"] == fsutil::text(&version) {
                                reference(
                                    &mut r,
                                    format!(
                                        "{}:{}",
                                        registry_path.display(),
                                        item["scope"].as_str().unwrap_or("unknown")
                                    ),
                                );
                            }
                        }
                    }
                    if linked {
                        r["reason"] = json!("开发目录链接；保留，不跟随清理");
                    } else if manifest.as_object().is_none_or(|m| m.is_empty()) {
                        r["reason"] = json!("缺少可识别的插件清单；保留");
                    }
                    if agent == "codex"
                        && data["plugins"]
                            .as_object()
                            .is_some_and(|p| p.contains_key(&plugin_id))
                    {
                        reference(
                            &mut r,
                            format!("{}:plugins.{plugin_id}（未指定版本）", cfg.display()),
                        );
                    }
                    let orphan = version.join(".orphaned_at");
                    if agent == "claude"
                        && let Ok(s) = fsutil::stat(&orphan)
                        && s.regular()
                    {
                        r["orphaned_days"] =
                            json!((fsutil::now_ns().saturating_sub(s.modified_ns) / DAY_NS).max(0));
                        r["guidance"] = json!(
                            "检测到原生孤立标记；由 Claude Code 管理旧版本回收，不能用卸载整个插件代替。"
                        );
                    }
                    let parent = rows.len();
                    plugins.push(parent);
                    rows.push(r.clone());
                    if !linked {
                        skills.extend(
                            skill_paths(&version.join("skills"), &mut errors)
                                .into_iter()
                                .map(|p| (agent.into(), p, Some(parent))),
                        );
                        let servers = &manifest["mcpServers"];
                        bundle_mcp(&mut rows, &r, servers);
                        if let Some(mcp) = metadata(&version.join(".mcp.json"), &mut errors, false)
                        {
                            bundle_mcp(&mut rows, &r, mcp.get("mcpServers").unwrap_or(&mcp));
                        }
                        if let Some(relative) = servers.as_str() {
                            let path = version.join(relative);
                            if path != version
                                && path.starts_with(&version)
                                && !path
                                    .components()
                                    .any(|c| c == std::path::Component::ParentDir)
                                && let Some(extra) = metadata(&path, &mut errors, false)
                            {
                                bundle_mcp(
                                    &mut rows,
                                    &r,
                                    extra.get("mcpServers").unwrap_or(&extra),
                                );
                            }
                        }
                    }
                }
            }
        }
        skills.extend(
            skill_paths(&root.join("skills"), &mut errors)
                .into_iter()
                .map(|p| (agent.into(), p, None)),
        );
    }
    skills.extend(
        skill_paths(&home.join(".agents/skills"), &mut errors)
            .into_iter()
            .map(|p| ("codex".into(), p, None)),
    );
    for (agent, path, parent) in skills {
        let parent_row = parent.map(|i| rows[i].clone());
        let mut r = row(&agent, "skill", path.parent().unwrap());
        update(
            &mut r,
            json!({"identity":path.parent().unwrap().file_name().unwrap_or_default().to_string_lossy(),"source":parent_row.as_ref().map(|r|r["identity"].clone()).unwrap_or(json!("本地 Skill")),"parent_path":parent_row.as_ref().map(|r|r["path"].clone()),"measure":parent.is_none(),"reason":"本地 Skill 可能包含自定义修改；只做重复版本复核","guidance":"先核对来源与本地修改；链接不代表重复安装，不按目录名直接删除。"}),
        );
        let collect = (|| -> Result<()> {
            let real = fsutil::resolve(&path)?;
            let text = fsutil::regular_text(&real, 1024 * 1024)?;
            let header = if text.starts_with("---\n") {
                text.split("---")
                    .nth(1)
                    .filter(|_| text.matches("---").count() >= 2)
                    .unwrap_or("")
            } else {
                ""
            };
            for field in ["name", "version"] {
                let pattern =
                    Regex::new(&format!(r#"(?m)^{field}:\s*[\x22\x27]?([^\n\x22\x27]+)"#))?;
                if let Some(found) = pattern.captures(header) {
                    r[field] = json!(found[1].trim().chars().take(512).collect::<String>());
                }
            }
            r["content_fingerprint"] = json!(format!("{:x}", Sha256::digest(text.as_bytes())));
            r["resolved_path"] = json!(real.parent());
            if let Some(parent) = &parent_row {
                if r["version"].is_null() || r["version"] == "" {
                    r["version"] = parent["version"].clone();
                }
                r["group_key"] = json!(format!(
                    "skill:{}:{}",
                    parent["identity"].as_str().unwrap(),
                    r["name"].as_str().unwrap()
                ));
                r["reason"] = json!("插件内置 Skill，随父插件版本管理；不单独删除");
            } else {
                r["group_key"] = json!(format!("skill:local:{}", r["name"].as_str().unwrap()));
                for i in &plugins {
                    if real
                        .parent()
                        .unwrap()
                        .starts_with(rows[*i]["path"].as_str().unwrap())
                    {
                        reference(&mut rows[*i], fsutil::text(path.parent().unwrap()));
                    }
                }
            }
            Ok(())
        })();
        if collect.is_err() {
            r["reason"] = json!("Skill 元数据无法读取；保留");
            r["group_key"] = json!(format!(
                "skill:unknown:{}",
                path.parent().unwrap().display()
            ));
        }
        rows.push(r);
    }
    for (agent, cfg, servers) in configs {
        if let Some(servers) = servers.as_object() {
            for (name, spec) in servers {
                if spec.is_object() {
                    configured_mcp(&mut rows, &agent, &cfg, name, spec, &mut errors)?;
                    for value in std::iter::once(&spec["command"])
                        .chain(spec["args"].as_array().into_iter().flatten())
                    {
                        if let Some(s) = value.as_str().filter(|s| s.starts_with('/')) {
                            path_references
                                .push((s.into(), format!("{}:mcp:{name}", cfg.display())));
                        }
                    }
                }
            }
        }
    }
    for (value, source) in path_references {
        if let Ok(path) = fsutil::resolve(Path::new(&value)) {
            for index in &plugins {
                if path.starts_with(rows[*index]["path"].as_str().unwrap()) {
                    reference(&mut rows[*index], &source);
                }
            }
        }
    }
    let mut groups: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    for (i, r) in rows.iter().enumerate() {
        groups
            .entry(
                r["group_key"]
                    .as_str()
                    .unwrap_or(r["path"].as_str().unwrap())
                    .into(),
            )
            .or_default()
            .push(i);
    }
    let mut summary = Vec::new();
    let mut groups: Vec<_> = groups.into_iter().collect();
    groups.sort_by_key(|(_, indices)| indices[0]);
    for (key, indices) in groups {
        let versions: BTreeSet<_> = indices
            .iter()
            .filter_map(|i| rows[*i]["version"].as_str().filter(|s| !s.is_empty()))
            .map(str::to_string)
            .collect();
        let physical: BTreeSet<_> = indices
            .iter()
            .map(|i| {
                rows[*i]["resolved_path"]
                    .as_str()
                    .unwrap_or(rows[*i]["path"].as_str().unwrap())
                    .to_string()
            })
            .collect();
        if versions.len() > 1 || (physical.len() > 1 && indices.len() > 1) {
            let agents: BTreeSet<_> = indices
                .iter()
                .map(|i| rows[*i]["agent"].as_str().unwrap().to_string())
                .collect();
            for i in &indices {
                rows[*i]["multiple_versions"] = json!(versions.len() > 1);
                rows[*i]["duplicate_group"] = json!(key);
            }
            summary.push(json!({"identity":key,"category":rows[indices[0]]["category"],"versions":versions,"paths":physical,"agents":agents,"reason":if versions.len()>1{"多个已声明版本"}else{"同名多处安装；来源或版本可能不同"}}));
        }
    }
    for i in plugins {
        let r = &mut rows[i];
        if r["referenced_by"].as_array().is_some_and(|a| !a.is_empty()) {
            r["reason"] = json!("安装登记、配置或 Skill 链接仍引用；保留");
        } else if !r["orphaned_days"].is_null() && r["registry_verified"] == true {
            r["status"] = json!("owner-managed");
            r["reason"] = json!("原生孤立标记已存在；等待所属 Agent 回收（标记文件年龄仅供参考）");
        }
    }
    let mut seen = BTreeSet::new();
    rows.retain(|r| {
        selected.iter().any(|a| r["agent"] == *a)
            && seen.insert(format!(
                "{}",
                json!([
                    r["agent"],
                    r["category"],
                    r["group_key"],
                    r["path"],
                    r["version"]
                ])
            ))
    });
    summary.retain(|g| {
        g["agents"]
            .as_array()
            .unwrap()
            .iter()
            .any(|a| selected.iter().any(|s| a == s))
    });
    resources::measure_rows(&mut rows, layout, full, progress, &mut errors)?;
    Ok(
        json!({"mode":if full{"measured"}else{"discovered"},"entries":rows,"groups":summary,"errors":errors,"scope":"用户级 Skill、插件缓存与已登记 MCP；未穷举系统配置、任意项目和 Python/uv 缓存","totals_note":"只读版本复核；父插件已包含内置 Skill/MCP，不重复计量；不是清理候选。"}),
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn npx_options_are_not_packages() {
        for (args, expected) in [
            (
                json!([
                    "--registry",
                    "https://example.invalid",
                    "--yes",
                    "@example/mcp@1.2.3"
                ]),
                Some(("@example/mcp".into(), Some("1.2.3".into()))),
            ),
            (json!(["--unknown", "package"]), None),
            (
                json!(["--package=service@^1"]),
                Some(("service".into(), Some("^1".into()))),
            ),
            (json!(["--package", "../untrusted"]), None),
        ] {
            assert_eq!(command_package(args.as_array().unwrap()), expected);
        }
    }
}
