use crate::fsutil::{self, Info};
use anyhow::{Context, Result, bail};
use serde::{
    Deserialize, Deserializer,
    de::{self, MapAccess, SeqAccess, Visitor},
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    env, fmt, fs,
    path::{Path, PathBuf},
};
use unicode_casefold::UnicodeCaseFold;
use unicode_normalization::UnicodeNormalization;

pub const DAY_NS: i64 = 86_400_000_000_000;
pub const SESSION_DAYS: [i64; 4] = [5, 10, 15, 20];
pub const CATEGORIES: [&str; 3] = ["cache", "logs", "dependencies"];
pub fn home() -> Result<PathBuf> {
    let p = env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
        .map(PathBuf::from)
        .context("未找到用户主目录")?;
    fsutil::absolute(&p)?;
    Ok(p)
}
pub fn setting(name: &str, default: PathBuf) -> Result<PathBuf> {
    let raw = env::var_os(name)
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or(default);
    let p = if raw.starts_with("~") {
        home()?.join(raw.strip_prefix("~")?)
    } else {
        raw
    };
    fsutil::absolute(&p)?;
    Ok(fsutil::lexical(&p))
}
#[derive(Clone, Debug)]
pub struct Layout {
    pub roots: BTreeMap<String, PathBuf>,
    pub state: PathBuf,
    pub home: PathBuf,
}
impl Layout {
    pub fn load() -> Result<Self> {
        let home = home()?;
        let cache = if cfg!(target_os = "macos") {
            home.join("Library/Caches")
        } else {
            setting("XDG_CACHE_HOME", home.join(".cache"))?
        };
        let roots = BTreeMap::from([
            ("codex".into(), setting("CODEX_HOME", home.join(".codex"))?),
            (
                "claude".into(),
                setting("CLAUDE_CONFIG_DIR", home.join(".claude"))?,
            ),
            ("t3".into(), setting("T3_HOME", home.join(".t3"))?),
            ("claude-cli".into(), cache.join("claude-cli-nodejs")),
        ]);
        let state = setting("XDG_STATE_HOME", home.join(".local/state"))?.join("clean-my-agent");
        let paths: Vec<_> = roots.values().chain(std::iter::once(&state)).collect();
        for (i, p) in paths.iter().enumerate() {
            if *p == &home
                || home.starts_with(p)
                || paths[..i]
                    .iter()
                    .any(|q| p.starts_with(q) || q.starts_with(p))
            {
                bail!("数据目录不能是主目录、根目录或相互包含；请检查环境变量");
            }
        }
        Ok(Self { roots, state, home })
    }
    pub fn selected(&self, agent: &str) -> Vec<String> {
        if agent == "all" {
            vec!["codex".into(), "claude".into(), "t3".into()]
        } else {
            vec![agent.into()]
        }
    }
}
pub fn config_path() -> Result<PathBuf> {
    Ok(setting("XDG_CONFIG_HOME", home()?.join(".config"))?.join("clean-my-agent/policy.json"))
}
pub fn fold(value: &str) -> String {
    value.nfc().case_fold().collect()
}
fn components(path: &Path) -> Vec<String> {
    path.components()
        .map(|p| fold(&p.as_os_str().to_string_lossy()))
        .collect()
}

// serde_json normally accepts duplicate keys. Policies must reject them at every depth.
struct Unique(Value);
impl<'de> Deserialize<'de> for Unique {
    fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Unique;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("JSON without duplicate keys")
            }
            fn visit_map<M: MapAccess<'de>>(
                self,
                mut map: M,
            ) -> std::result::Result<Unique, M::Error> {
                let mut values = serde_json::Map::new();
                while let Some((k, v)) = map.next_entry::<String, Unique>()? {
                    if values.insert(k.clone(), v.0).is_some() {
                        return Err(de::Error::custom(format!("重复的配置项：{k}")));
                    }
                }
                Ok(Unique(Value::Object(values)))
            }
            fn visit_seq<S: SeqAccess<'de>>(
                self,
                mut seq: S,
            ) -> std::result::Result<Unique, S::Error> {
                let mut values = Vec::new();
                while let Some(v) = seq.next_element::<Unique>()? {
                    values.push(v.0);
                }
                Ok(Unique(Value::Array(values)))
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> std::result::Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> std::result::Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> std::result::Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_f64<E: de::Error>(self, v: f64) -> std::result::Result<Unique, E> {
                Ok(Unique(json!(v)))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> std::result::Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_string<E: de::Error>(self, v: String) -> std::result::Result<Unique, E> {
                Ok(Unique(v.into()))
            }
            fn visit_unit<E: de::Error>(self) -> std::result::Result<Unique, E> {
                Ok(Unique(Value::Null))
            }
        }
        d.deserialize_any(V)
    }
}
pub fn read_config(path: &Path) -> Result<Value> {
    if !path.exists() {
        if path.ancestors().any(|p| {
            fs::symlink_metadata(p).is_ok_and(|m| m.file_type().is_symlink()) && !p.exists()
        }) {
            bail!("策略配置目录链接失效");
        }
        return Ok(json!({}));
    }
    if !path.is_file() {
        bail!("需要可读取的普通 JSON 文件");
    }
    let data = serde_json::from_str::<Unique>(&fs::read_to_string(path)?)?.0;
    let map = data.as_object().context("策略需要 JSON 对象")?;
    if map
        .keys()
        .any(|k| !["profile", "retention_days", "protect", "sessions"].contains(&k.as_str()))
    {
        bail!("仅支持 profile、retention_days、protect 和 sessions 配置项");
    }
    Ok(data)
}
pub fn session_rules(raw: &Value) -> Result<Value> {
    let obj = raw.as_object().context("sessions 只接受 codex 和 claude")?;
    if obj
        .keys()
        .any(|k| !["codex", "claude"].contains(&k.as_str()))
    {
        bail!("sessions 只接受 codex 和 claude");
    }
    let mut result = json!({});
    for agent in ["codex", "claude"] {
        let mut rules = json!({"archive_days":10,"delete_days":20});
        if let Some(values) = raw.get(agent) {
            for (k, v) in values.as_object().context("会话策略格式错误")? {
                if !["archive_days", "delete_days"].contains(&k.as_str())
                    || !v.as_i64().is_some_and(|d| SESSION_DAYS.contains(&d))
                {
                    bail!("会话策略天数请选择 5、10、15 或 20");
                }
                rules[k] = v.clone();
            }
        }
        result[agent] = rules;
    }
    Ok(result)
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Policy {
    pub profile: String,
    pub retention: BTreeMap<String, i64>,
    pub protect: Vec<String>,
    pub rules: Vec<(String, Vec<String>)>,
    pub sessions: Value,
    pub path: PathBuf,
    pub cli_profile: Option<String>,
    pub cli_days: Option<i64>,
}
impl Policy {
    pub fn load(profile: Option<&str>, days: Option<i64>) -> Result<Self> {
        let path = config_path()?;
        let data =
            read_config(&path).with_context(|| format!("无法使用策略 {}", path.display()))?;
        let configured = data
            .get("profile")
            .unwrap_or(&json!("standard"))
            .as_str()
            .context("未知策略档位")?
            .to_owned();
        if !["standard", "conservative", "deep"].contains(&configured.as_str()) {
            bail!("未知策略档位");
        }
        let name = profile.unwrap_or(&configured);
        let values = match name {
            "standard" => [30, 30, 7],
            "conservative" => [60, 90, 30],
            "deep" => [7, 14, 7],
            _ => bail!("未知策略档位"),
        };
        let mut retention: BTreeMap<_, _> = CATEGORIES
            .into_iter()
            .zip(values)
            .map(|(k, v)| (k.to_string(), v))
            .collect();
        if let Some(overrides) = data.get("retention_days") {
            for (k, v) in overrides.as_object().context("retention_days 类型错误")? {
                if ![
                    "cache",
                    "logs",
                    "dependencies",
                    "snapshots",
                    "archives",
                    "tool-results",
                ]
                .contains(&k.as_str())
                {
                    bail!("retention_days 含未识别的类别");
                }
                let d = v
                    .as_i64()
                    .filter(|d| *d > 0)
                    .context("保留天数必须是正整数")?;
                if CATEGORIES.contains(&k.as_str()) {
                    retention.insert(k.clone(), d);
                }
            }
        }
        if let Some(days) = days {
            if days < 1 {
                bail!("至少保留最近 1 天");
            }
            for k in ["cache", "logs"] {
                retention.insert(k.into(), days);
            }
        }
        let mut protect = Vec::new();
        let mut rules = Vec::new();
        if let Some(entries) = data.get("protect") {
            for value in entries.as_array().context("protect 必须是路径数组")? {
                let entry = value.as_str().context("保护路径必须是字符串")?;
                let (source, relative) =
                    entry.split_once(':').context("保护路径需要 agent:path")?;
                if ![
                    "codex",
                    "claude",
                    "claude-cli",
                    "t3",
                    "cursor",
                    "copilot",
                    "antigravity",
                    "filo",
                    "qoder",
                    "opencode",
                    "chrome-devtools",
                    "agent-browser",
                ]
                .contains(&source)
                    || relative.is_empty()
                    || Path::new(relative).is_absolute()
                    || Path::new(relative)
                        .components()
                        .any(|p| p == std::path::Component::ParentDir)
                    || relative.contains(['*', '?', '[', ']', '\0'])
                {
                    bail!("无效保护路径 {entry}");
                }
                protect.push(entry.into());
                rules.push((source.into(), components(Path::new(relative))));
            }
        }
        Ok(Self {
            profile: name.into(),
            retention,
            protect,
            rules,
            sessions: session_rules(data.get("sessions").unwrap_or(&json!({})))?,
            path,
            cli_profile: profile.map(Into::into),
            cli_days: days,
        })
    }
    pub fn current(&self) -> Result<()> {
        if *self != Self::load(self.cli_profile.as_deref(), self.cli_days)? {
            bail!("扫描期间清理策略已改变；请按新策略重新预览");
        }
        Ok(())
    }
    pub fn protected(&self, source: &str, path: &Path, tree: bool) -> bool {
        let p = components(path);
        self.rules
            .iter()
            .any(|(a, r)| a == source && (p.starts_with(r) || (tree && r.starts_with(&p))))
    }
    pub fn explain(
        &self,
        source: &str,
        path: &Path,
        category: &str,
        s: Info,
        now: i64,
    ) -> Option<&'static str> {
        if self.protected(source, path, false) {
            Some("protected")
        } else if !["cache", "logs"].contains(&category) {
            Some("out-of-scope")
        } else if s.links != 1 {
            Some("linked")
        } else if s.modified_ns
            > now.saturating_sub(self.retention[category].saturating_mul(DAY_NS))
        {
            Some("recent")
        } else {
            None
        }
    }
    pub fn describe(&self) -> Value {
        json!({"profile":self.profile,"retention_days":self.retention,"protect":self.protect,"config_path":self.path,"sessions":self.sessions})
    }
}

pub fn example() -> Value {
    json!({"profile":"standard","retention_days":{"cache":30,"logs":30,"dependencies":7},"protect":["codex:worktrees/keep-this-project"],"sessions":{"codex":{"archive_days":10,"delete_days":20},"claude":{"archive_days":10,"delete_days":20}}})
}
pub fn save_session_defaults(agent: &str, values: &Value) -> Result<Policy> {
    use crate::fsops;
    if cfg!(windows) {
        bail!("Windows 原生版仅盘点");
    }
    if !["all", "codex", "claude"].contains(&agent)
        || values.as_object().is_none_or(|v| v.is_empty())
    {
        bail!("请选择 Agent 并指定会话默认天数");
    }
    let path = config_path()?;
    let parent = fsops::directory(path.parent().context("缺少配置目录")?, true)?;
    fsops::lock(&parent)?;
    let name = path.file_name().unwrap();
    let before = match parent.stat(name) {
        Ok(info) => {
            if !info.regular() {
                bail!("配置路径不是普通文件");
            }
            Some(info.stamp())
        }
        Err(e)
            if e.downcast_ref::<std::io::Error>()
                .is_some_and(|e| e.kind() == std::io::ErrorKind::NotFound) =>
        {
            None
        }
        Err(e) => return Err(e),
    };
    Policy::load(None, None)?;
    let mut data = read_config(&path)?;
    if data.get("sessions").is_none() {
        data["sessions"] = json!({});
    }
    for key in if agent == "all" {
        vec!["codex", "claude"]
    } else {
        vec![agent]
    } {
        if data["sessions"].get(key).is_none() {
            data["sessions"][key] = json!({});
        }
        for (k, v) in values.as_object().unwrap() {
            data["sessions"][key][k] = v.clone();
        }
    }
    session_rules(&data["sessions"])?;
    let tmp = format!(".policy-{}.tmp", uuid::Uuid::new_v4());
    fsops::write_exclusive(
        &parent,
        tmp.as_ref(),
        format!("{}\n", serde_json::to_string_pretty(&data)?).as_bytes(),
    )?;
    let result = (|| -> Result<()> {
        let after = match parent.stat(name) {
            Ok(info) => Some(info.stamp()),
            Err(e)
                if e.downcast_ref::<std::io::Error>()
                    .is_some_and(|e| e.kind() == std::io::ErrorKind::NotFound) =>
            {
                None
            }
            Err(e) => return Err(e),
        };
        if before != after {
            bail!("配置文件已被修改，请重新执行");
        }
        if before.is_some() {
            fsops::replace(&parent, tmp.as_ref(), name)?;
        } else {
            fsops::exclusive(&parent, tmp.as_ref(), &parent, name)?;
        }
        fsops::sync(&parent)
    })();
    let _ = fsops::unlink(&parent, tmp.as_ref(), false);
    result?;
    Policy::load(None, None)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn duplicate_keys_are_not_accepted() {
        assert!(
            serde_json::from_str::<Unique>(r#"{"profile":"standard","profile":"deep"}"#).is_err()
        );
        assert!(
            serde_json::from_str::<Unique>(
                r#"{"sessions":{"codex":{"archive_days":5,"archive_days":20}}}"#
            )
            .is_err()
        );
    }
    #[test]
    fn boolean_days_are_rejected() {
        assert!(session_rules(&json!({"codex":{"archive_days":true}})).is_err());
        assert!(session_rules(&json!({"codex":{"archive_days":0}})).is_err());
        assert!(session_rules(&json!({"codex":{"delete_days":7}})).is_err());
        assert_eq!(
            session_rules(&json!({"codex":{"archive_days":5}})).unwrap()["claude"]["archive_days"],
            10
        );
    }
    #[test]
    fn protection_normalization_matches_casefold() {
        assert_eq!(fold("Straße"), fold("STRASSE"));
        assert_eq!(fold("é"), fold("e\u{301}"));
    }
}
