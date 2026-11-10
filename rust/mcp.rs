//! Local stdio MCP. Every operation uses the same Rust services as CLI and TUI.
use crate::{
    actions, dependency_report, extensions, fsutil, inventory,
    policy::{Layout, Policy},
    progress::Progress,
    resources, scan, vault,
};
use anyhow::{Context, Result, bail};
use rmcp::{ErrorData, RoleServer, ServerHandler, ServiceExt, model::*, service::RequestContext};
use serde_json::{Value, json};
use std::{path::Path, sync::Arc};

#[derive(Clone)]
pub struct Server {
    allow_stage: bool,
    gate: Arc<tokio::sync::Semaphore>,
}
fn tools(allow_stage: bool) -> Vec<Tool> {
    let agent = json!({"type":"string","enum":["all","codex","claude"],"default":"all"});
    let storage = json!({"type":"string","enum":["all","codex","claude","t3"],"default":"all"});
    let full = json!({"type":"boolean","default":false});
    let limit = json!({"type":"integer","minimum":1,"maximum":100,"default":20});
    let path = json!({"type":"string","description":"盘点返回的绝对路径"});
    let batch = json!({"type":"string"});
    let age = json!({"type":["integer","null"],"enum":[5,10,15,20,null]});
    let days = json!({"type":["integer","null"],"minimum":1});
    let mut catalog = vec![
        (
            "get_policy",
            "读取当前生效策略、保护路径与会话阈值。",
            json!({}),
            vec![],
            false,
        ),
        (
            "scan_usage",
            "盘点类别与候选容量；完整模式较慢。errors 非空表示结果不完整。",
            json!({"agent":storage,"full":full}),
            vec![],
            false,
        ),
        (
            "resource_inventory",
            "盘点 Agent 缓存、旧版本和更新残留；候选不等于已授权清理。",
            json!({"agent":{"type":"string","default":"all","enum":std::iter::once("all").chain(resources::FAMILIES.iter().copied()).collect::<Vec<_>>()},"full":full,"limit":limit}),
            vec![],
            false,
        ),
        (
            "extension_inventory",
            "只读检测 Skill/MCP/Plugin 版本及引用，不输出 MCP 凭据。",
            json!({"agent":agent,"full":full,"limit":limit}),
            vec![],
            false,
        ),
        (
            "inspect_dependencies",
            "测量项目产物、共享占用与保留原因；释放估计不能相加。",
            json!({"path":{"type":["string","null"]},"candidates_only":{"type":"boolean","default":false},"limit":limit}),
            vec![],
            false,
        ),
        (
            "session_inventory",
            "返回会话分布、标题、ID 及复核建议；不归档或删除会话。",
            json!({"agent":agent,"archive_days":age,"delete_days":age,"limit":{"type":"integer","minimum":1,"maximum":100,"default":10}}),
            vec![],
            false,
        ),
        (
            "preview_clean",
            "预览已知缓存和旧日志；files 只显示前 limit 项，eligible_files 是所有匹配项。",
            json!({"agent":agent,"days":days,"limit":limit}),
            vec![],
            false,
        ),
        (
            "list_batches",
            "列出最近暂存批次；指定批次返回原路径与清单容量，不代表可释放量。",
            json!({"batch":{"type":["string","null"]},"limit":limit}),
            vec![],
            false,
        ),
    ];
    if allow_stage {
        catalog.extend([
            (
                "stage_resource",
                "重新核查并暂存一个用户已确认的资源候选；暂存释放 0 B。",
                json!({"path":path}),
                vec!["path"],
                true,
            ),
            (
                "stage_clean",
                "按已确认 Agent 和天数重新检查并暂存所有匹配缓存及旧日志，并非只操作预览前几行。",
                json!({"agent":{"type":"string","enum":["codex","claude"]},"days":days}),
                vec!["agent"],
                true,
            ),
            (
                "stage_dependency",
                "重新核实并暂存一个已确认停用的产物候选；返回批次号，暂存释放 0 B。",
                json!({"path":path}),
                vec!["path"],
                true,
            ),
            (
                "restore_batch",
                "还原已确认的批次，不覆盖新文件；部分成功时查看 errors 和剩余批次。",
                json!({"batch":batch}),
                vec!["batch"],
                true,
            ),
        ]);
    }
    catalog.into_iter().map(|(name,description,properties,required,write)|serde_json::from_value(json!({"name":name,"description":description,"inputSchema":{"type":"object","properties":properties,"required":required,"additionalProperties":false},"annotations":{"readOnlyHint":!write,"destructiveHint":write,"openWorldHint":false}})).expect("static tool schema")).collect()
}
fn validate(tool: &Tool, args: &Value) -> Result<()> {
    let args = args.as_object().context("参数必须是对象")?;
    let schema = tool.input_schema.as_ref();
    let properties = schema["properties"].as_object().unwrap();
    for key in schema["required"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(Value::as_str)
    {
        if !args.contains_key(key) {
            bail!("缺少参数 {key}");
        }
    }
    for (key, value) in args {
        let definition = properties
            .get(key)
            .with_context(|| format!("未知参数 {key}"))?;
        let types = match &definition["type"] {
            Value::String(s) => vec![s.as_str()],
            Value::Array(a) => a.iter().filter_map(Value::as_str).collect(),
            _ => vec![],
        };
        if !types.iter().any(|t| match *t {
            "string" => value.is_string(),
            "integer" => value.is_i64(),
            "boolean" => value.is_boolean(),
            "null" => value.is_null(),
            _ => false,
        }) {
            bail!("参数 {key} 类型无效");
        }
        if let Some(choices) = definition["enum"].as_array()
            && !choices.contains(value)
        {
            bail!("参数 {key} 不在可选范围内");
        }
        if let Some(n) = value.as_i64()
            && (definition["minimum"].as_i64().is_some_and(|m| n < m)
                || definition["maximum"].as_i64().is_some_and(|m| n > m))
        {
            bail!("参数 {key} 超出范围");
        }
        if value.as_str().is_some_and(|s| s.len() > 32768) {
            bail!("参数 {key} 过长");
        }
    }
    Ok(())
}
fn truncate(doc: &mut Value, key: &str, limit: usize) {
    if let Some(rows) = doc[key].as_array_mut() {
        let total = rows.len();
        rows.truncate(limit);
        doc["total_entries"] = json!(total);
        doc["truncated"] = json!(total > limit);
    }
}
pub fn dispatch(name: &str, args: &Value, allow_stage: bool, p: &Progress) -> Result<Value> {
    let tool = tools(allow_stage)
        .into_iter()
        .find(|t| t.name == name)
        .context("未开放此工具")?;
    validate(&tool, args)?;
    let agent = args["agent"].as_str().unwrap_or("all");
    let full = args["full"].as_bool().unwrap_or(false);
    let limit = args["limit"]
        .as_u64()
        .unwrap_or(if name == "session_inventory" { 10 } else { 20 }) as usize;
    let policy = Policy::load(None, args["days"].as_i64())?;
    let layout = Layout::load()?;
    let path = args["path"].as_str().map(Path::new);
    if let Some(path) = path {
        fsutil::absolute(path)?;
    }
    match name {
        "get_policy" => Ok(policy.describe()),
        "scan_usage" => {
            let agents = layout.selected(agent);
            let result = scan::scan(&layout, &agents, &policy, full, p)?;
            let mut doc = result.report(&policy, full, None, false);
            let deps =
                dependency_report::inspect_agents(&layout, &policy, &agents, None, full, p, None)?;
            doc["dependency_summary"] = json!({"mode":deps["mode"],"count":deps["entries"].as_array().unwrap().len(),"candidates":deps["entries"].as_array().unwrap().iter().filter(|r|r["status"]=="candidate").count()});
            let mut resources = resources::inspect(&layout, &agents, &policy, full, None, p)?;
            let mut extensions = extensions::inspect(&layout, &agents, full, p)?;
            for report in [&deps, &resources, &extensions] {
                doc["errors"]
                    .as_array_mut()
                    .unwrap()
                    .extend(report["errors"].as_array().unwrap().clone());
            }
            truncate(&mut resources, "entries", 20);
            truncate(&mut extensions, "entries", 20);
            doc["agent_resources"] = resources;
            doc["extensions"] = extensions;
            doc["bytes_scope"] =
                json!("仅原有 Agent 数据根目录；额外资源另列，重叠子项不重复加入此合计");
            Ok(doc)
        }
        "resource_inventory" => {
            let agents = if agent == "all" {
                resources::FAMILIES.iter().map(|s| s.to_string()).collect()
            } else {
                vec![agent.into()]
            };
            let mut doc = resources::inspect(&layout, &agents, &policy, full, None, p)?;
            truncate(&mut doc, "entries", limit);
            Ok(doc)
        }
        "extension_inventory" => {
            let agents = if agent == "all" {
                vec!["codex".into(), "claude".into()]
            } else {
                vec![agent.into()]
            };
            let mut doc = extensions::inspect(&layout, &agents, full, p)?;
            doc["entries"].as_array_mut().unwrap().sort_by_key(|r| {
                (
                    r["duplicate_group"].is_null(),
                    r["name"].as_str().unwrap_or("").to_string(),
                )
            });
            truncate(&mut doc, "entries", limit);
            Ok(doc)
        }
        "inspect_dependencies" => {
            let doc = dependency_report::inspect_agents(
                &layout,
                &policy,
                &layout.selected("all"),
                path,
                true,
                p,
                None,
            )?;
            let all = doc["entries"].as_array().unwrap();
            let mut entries: Vec<_> = all
                .iter()
                .filter(|r| args["candidates_only"] != true || r["status"] == "candidate")
                .cloned()
                .collect();
            entries.sort_by(|a, b| {
                let n = |r: &Value| {
                    r["reclaimable_bytes"]
                        .as_u64()
                        .or(r["reclaimable_min_bytes"].as_u64())
                };
                n(a).is_none()
                    .cmp(&n(b).is_none())
                    .then(n(b).cmp(&n(a)))
                    .then(a["path"].as_str().cmp(&b["path"].as_str()))
            });
            let matched = entries.len();
            entries.truncate(limit);
            let projects = doc["projects"].as_array().cloned().unwrap_or_default();
            Ok(
                json!({"mode":doc["mode"],"total":all.len(),"matched":matched,"entries":entries,"projects":projects.iter().take(limit).collect::<Vec<_>>(),"total_projects":projects.len(),"errors":doc["errors"]}),
            )
        }
        "session_inventory" => {
            let (doc, _) = inventory::inspect(
                &layout,
                &policy,
                agent,
                args["archive_days"].as_i64(),
                args["delete_days"].as_i64(),
                false,
                limit,
                p,
            )?;
            Ok(
                json!({"generated_at":doc["generated_at"],"agents":doc["agents"],"errors":doc["errors"]}),
            )
        }
        "preview_clean" | "stage_clean" => {
            let result = scan::scan(&layout, &layout.selected(agent), &policy, false, p)?;
            let mut doc = result.report(&policy, false, args["days"].as_i64(), true);
            if name == "stage_clean" {
                if !result.errors.is_empty() {
                    doc["mode"] = json!("aborted");
                    doc["batch"] = Value::Null;
                    doc["moved_files"] = json!(0);
                    doc["moved_bytes"] = json!(0);
                } else {
                    let saved = vault::save(&result.items, &layout, &policy, p)?;
                    doc["mode"] = json!("quarantine");
                    doc["batch"] = json!(saved.batch);
                    doc["moved_files"] = json!(saved.count);
                    doc["moved_bytes"] = json!(saved.bytes);
                    doc["errors"] = json!(saved.errors);
                }
            }
            if let Some(files) = doc["files"].as_array_mut() {
                files.truncate(limit);
            }
            Ok(doc)
        }
        "list_batches" => {
            let requested = args["batch"].as_str();
            let mut keys = if let Some(key) = requested {
                vec![key.into()]
            } else {
                let mut keys = vault::batches(&layout)?;
                keys.reverse();
                keys
            };
            let total = keys.len();
            keys.truncate(limit);
            let mut rows = Vec::new();
            for key in keys {
                let result = vault::process(&layout, &key, "list", false, None, None, p)?;
                let mut row = json!({"batch":key,"created":result.detail["created"],"items":result.count,"manifest_bytes":result.bytes,"errors":result.errors});
                if requested.is_some() {
                    row["files"] = json!(
                        result.detail["files"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .take(limit)
                            .map(|r| json!({"path":r[0],"bytes":r[1]}))
                            .collect::<Vec<_>>()
                    );
                }
                rows.push(row);
            }
            Ok(json!({"batches":rows,"total_batches":total}))
        }
        "stage_dependency" => {
            let doc = dependency_report::inspect_agents(
                &layout,
                &policy,
                &layout.selected("all"),
                path,
                true,
                p,
                None,
            )?;
            if doc["errors"].as_array().is_some_and(|e| !e.is_empty()) {
                bail!("目录或引用无法完整核实，请重新预览");
            }
            actions::stage_artifacts(&layout, &policy, &[doc["entries"][0].clone()], p)
        }
        "stage_resource" => {
            let agents = resources::FAMILIES
                .iter()
                .map(|s| s.to_string())
                .collect::<Vec<_>>();
            let doc = resources::inspect(&layout, &agents, &policy, true, path, p)?;
            actions::stage_resources(&layout, &policy, &[doc["entries"][0].clone()], p)
        }
        "restore_batch" => {
            let key = args["batch"].as_str().unwrap();
            let result = vault::process(&layout, key, "restore", true, None, None, p)?;
            Ok(
                json!({"batch":key,"restored_items":result.count,"manifest_bytes":result.bytes,"errors":result.errors}),
            )
        }
        _ => bail!("未开放此工具"),
    }
}
impl ServerHandler for Server {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
            .with_instructions("本机存储报告；先预览。errors 非空表示结果不完整。暂存不释放空间。")
    }
    fn get_tool(&self, name: &str) -> Option<Tool> {
        tools(self.allow_stage).into_iter().find(|t| t.name == name)
    }
    async fn list_tools(
        &self,
        _: Option<PaginatedRequestParams>,
        _: RequestContext<RoleServer>,
    ) -> std::result::Result<ListToolsResult, ErrorData> {
        Ok(ListToolsResult::with_all_items(tools(self.allow_stage)))
    }
    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> std::result::Result<CallToolResponse, ErrorData> {
        let Some(tool) = self.get_tool(&request.name) else {
            return Err(ErrorData::invalid_params("未开放此工具", None));
        };
        let args = Value::Object(request.arguments.unwrap_or_default());
        if let Err(e) = validate(&tool, &args) {
            return Ok(CallToolResult::error(vec![ContentBlock::text(e.to_string())]).into());
        }
        let gate = self.gate.clone();
        let allow = self.allow_stage;
        let name = request.name.into_owned();
        let progress = Progress::default();
        struct Cancel(Progress);
        impl Drop for Cancel {
            fn drop(&mut self) {
                self.0.cancel();
            }
        }
        let _cancel = Cancel(progress.clone());
        let p = progress.clone();
        let permit = tokio::select! {result=gate.acquire_owned()=>result.map_err(|_|ErrorData::internal_error("服务关闭",None))?,_=context.ct.cancelled()=>return Ok(CallToolResult::error(vec![ContentBlock::text("已取消")]).into())};
        let result = tokio::task::spawn_blocking(move || {
            let _permit = permit;
            dispatch(&name, &args, allow, &p)
        });
        let result = tokio::select! {r=result=>r.map_err(|_|anyhow::anyhow!("操作异常中断，请重新盘点核对状态")).and_then(|r|r),_=context.ct.cancelled()=>{progress.cancel();Err(anyhow::anyhow!("操作已取消；写操作可能部分完成，请检查暂存批次"))}};
        Ok(match result {
            Ok(doc) => CallToolResult::structured(doc),
            Err(e) => CallToolResult::error(vec![ContentBlock::text(crate::terminal::safe(
                &format!("{e:#}"),
            ))]),
        }
        .into())
    }
}
pub fn serve(allow_stage: bool) -> Result<()> {
    if cfg!(windows) && allow_stage {
        bail!("Windows 原生版当前仅支持只读 MCP");
    }
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?
        .block_on(async move {
            let server = Server {
                allow_stage,
                gate: Arc::new(tokio::sync::Semaphore::new(1)),
            };
            let service = server.serve(rmcp::transport::stdio()).await?;
            service.waiting().await?;
            Ok(())
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn write_tools_require_explicit_opt_in() {
        assert_eq!(tools(false).len(), 8);
        assert_eq!(tools(true).len(), 12);
        assert!(
            tools(true)
                .iter()
                .all(|t| !t.name.contains("purge") && !t.name.contains("delete"))
        );
    }
    #[test]
    fn parameter_bounds_and_types_are_enforced() {
        let tool = tools(false)
            .into_iter()
            .find(|t| t.name == "session_inventory")
            .unwrap();
        for args in [
            json!({"limit":0}),
            json!({"limit":101}),
            json!({"archive_days":true}),
            json!({"delete_days":11}),
            json!({"agent":"t3"}),
            json!({"extra":true}),
        ] {
            assert!(validate(&tool, &args).is_err());
        }
        assert!(validate(&tool, &json!({"limit":100,"archive_days":5})).is_ok());
    }
}
