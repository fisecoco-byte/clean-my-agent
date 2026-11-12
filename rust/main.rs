use anyhow::{Result, bail};
use clap::Parser;
use clean_my_agent::{
    actions, dashboard, dependency_report, extensions, inventory,
    policy::{Layout, Policy},
    progress::Progress,
    resources, scan, terminal, tui, vault,
};

#[derive(Parser, Debug)]
#[command(
    name = "clean-my-agent",
    version,
    about = "查看并安全清理本地 Agent 存储"
)]
struct Args {
    #[arg(default_value = "dashboard",value_parser=["dashboard","home","scan","clean","inventory","report","reporter","policy","deps","resources","extensions","trash","restore","purge","mcp"])]
    command: String,
    path: Option<String>,
    #[arg(long, default_value = "all")]
    agent: String,
    #[arg(long)]
    profile: Option<String>,
    #[arg(long)]
    days: Option<i64>,
    #[arg(long)]
    json: bool,
    #[arg(long)]
    plain: bool,
    #[arg(long, conflicts_with = "quick")]
    full: bool,
    #[arg(long)]
    quick: bool,
    #[arg(long)]
    details: bool,
    #[arg(long)]
    archive_days: Option<i64>,
    #[arg(long)]
    delete_days: Option<i64>,
    #[arg(long, default_value_t = 10)]
    limit: usize,
    #[arg(long)]
    table: bool,
    #[arg(long)]
    tui: bool,
    #[arg(long, conflicts_with = "apply")]
    dry_run: bool,
    #[arg(long)]
    apply: bool,
    #[arg(long)]
    html: Option<String>,
    #[arg(long)]
    from_report: Option<String>,
    #[arg(long)]
    set_session_defaults: bool,
    #[arg(long)]
    example: bool,
    #[arg(long)]
    allow_stage: bool,
    #[arg(long, hide = true)]
    include_history: bool,
    #[arg(long)]
    build_info: bool,
    // Per-stage scan times on stderr; the report itself is unchanged.
    #[arg(long, conflicts_with_all = ["apply", "from_report"])]
    timings: bool,
}
fn run() -> Result<i32> {
    let args = Args::parse();
    if args.build_info {
        println!(
            "{}",
            serde_json::to_string_pretty(
                &serde_json::json!({"name":"clean-my-agent","version":env!("CARGO_PKG_VERSION"),"implementation":"rust","source_sha256":env!("CMA_SOURCE_SHA256"),"revision":env!("CMA_REVISION"),"target":env!("CMA_TARGET"),"compiler":env!("CMA_RUSTC")})
            )?
        );
        return Ok(0);
    }
    if args.dry_run
        && !matches!(
            args.command.as_str(),
            "clean"
                | "deps"
                | "resources"
                | "purge"
                | "scan"
                | "dashboard"
                | "home"
                | "inventory"
                | "report"
                | "reporter"
                | "trash"
        )
    {
        bail!("该入口不支持 --dry-run");
    }
    if args.set_session_defaults && args.dry_run {
        bail!("保存默认天数与 --dry-run 不能同时使用");
    }
    if args.command == "restore" && args.dry_run {
        bail!("restore 会恢复指定批次；请用 trash 批次编号只读查看");
    }
    if args.include_history {
        bail!("历史记录不属于自动清理范围，请使用会话管理入口");
    }
    if args.command == "mcp" {
        clean_my_agent::mcp::serve(args.allow_stage)?;
        return Ok(0);
    }
    if args.allow_stage {
        bail!("--allow-stage 仅用于 mcp");
    }
    let mut policy = Policy::load(args.profile.as_deref(), args.days)?;
    if args.set_session_defaults {
        if args.command != "policy" || args.example || args.profile.is_some() {
            bail!("保存默认值请使用 policy --set-session-defaults，不与 --example、--profile 混用");
        }
        let mut values = serde_json::json!({});
        if let Some(n) = args.archive_days {
            values["archive_days"] = serde_json::json!(n);
        }
        if let Some(n) = args.delete_days {
            values["delete_days"] = serde_json::json!(n);
        }
        policy = clean_my_agent::policy::save_session_defaults(&args.agent, &values)?;
    } else if args.command == "policy"
        && (args.archive_days.is_some() || args.delete_days.is_some())
    {
        bail!("保存默认天数请加 --set-session-defaults");
    }
    if args.example && args.command != "policy" {
        bail!("--example 仅用于 policy");
    }
    if args.html.is_some() && !matches!(args.command.as_str(), "inventory" | "report" | "reporter")
    {
        bail!("--html 仅用于会话盘点");
    }
    if args.from_report.is_some()
        && (args.command != "deps"
            || args.apply
            || args.path.is_some()
            || args.profile.is_some()
            || args.days.is_some())
    {
        bail!("--from-report 仅用于只读查看已有依赖报告");
    }
    if args.timings && args.command != "deps" {
        bail!("--timings 仅用于 deps 报告");
    }
    if args.path.is_some()
        && !matches!(
            args.command.as_str(),
            "deps" | "resources" | "trash" | "restore" | "purge"
        )
    {
        bail!("该入口不接受额外路径参数");
    }
    if args.agent == "t3"
        && matches!(
            args.command.as_str(),
            "inventory" | "report" | "reporter" | "extensions"
        )
    {
        bail!("此入口支持 Codex 与 Claude");
    }
    if args.limit < 1 {
        bail!("请输入正整数");
    }
    for days in [args.archive_days, args.delete_days].into_iter().flatten() {
        if !clean_my_agent::policy::SESSION_DAYS.contains(&days) {
            bail!("会话策略天数请选择 5、10、15 或 20");
        }
    }
    if !resources::FAMILIES.contains(&args.agent.as_str()) && args.agent != "all" {
        bail!("未识别的 Agent");
    }
    if !matches!(args.command.as_str(), "resources" | "policy")
        && !matches!(args.agent.as_str(), "all" | "codex" | "claude" | "t3")
    {
        bail!("该入口不支持所选 Agent");
    }
    if args.apply
        && !matches!(
            args.command.as_str(),
            "clean" | "deps" | "resources" | "purge"
        )
    {
        bail!("该入口不支持 --apply");
    }
    if cfg!(windows) && (args.apply || args.command == "restore") {
        bail!("Windows 原生版仅盘点");
    }
    if args.apply
        && matches!(args.command.as_str(), "deps" | "resources")
        && (args.path.is_none() || args.quick)
    {
        bail!("请先预览，再指定一个完整测量过的绝对路径");
    }
    let interactive = !args.json
        && !args.plain
        && !args.table
        && !args.details
        && args.html.is_none()
        && args.path.is_none()
        && !args.timings
        && tui::available();
    let page = match args.command.as_str() {
        "dashboard" | "home" | "scan" => Some(tui::Page::Home),
        "deps" => Some(tui::Page::Dependencies),
        "resources" => Some(tui::Page::Resources),
        "extensions" => Some(tui::Page::Extensions),
        "report" | "reporter" => Some(tui::Page::Sessions),
        "inventory" if args.tui => Some(tui::Page::Sessions),
        "trash" => Some(tui::Page::Batches),
        _ => None,
    };
    if interactive && let Some(page) = page {
        tui::run(
            tui::Settings {
                layout: Layout::load()?,
                policy: policy.clone(),
                agent: args.agent.clone(),
                full: !args.quick,
                archive_days: args.archive_days,
                delete_days: args.delete_days,
                limit: args.limit,
                read_only: args.dry_run || args.from_report.is_some(),
                snapshot: args
                    .from_report
                    .as_deref()
                    .map(std::path::Path::new)
                    .map(clean_my_agent::reports::load_dependencies)
                    .transpose()?,
            },
            page,
        )?;
        return Ok(0);
    }
    let document = match args.command.as_str() {
        "dashboard" | "home" => dashboard::snapshot(
            &Layout::load()?,
            &policy,
            &args.agent,
            !args.quick,
            &Progress::default(),
        )?,
        "policy" => {
            if args.example {
                clean_my_agent::policy::example()
            } else {
                policy.describe()
            }
        }
        "scan" | "clean" => {
            let layout = Layout::load()?;
            let agents = layout.selected(&args.agent);
            let full = args.command == "scan" && !args.quick;
            let progress = Progress::default();
            let mut result = scan::scan(&layout, &agents, &policy, full, &progress)?;
            let mut doc = result.report(&policy, full, args.days, args.details);
            if args.command == "scan" {
                let dependencies = dependency_report::inspect_agents(
                    &layout, &policy, &agents, None, full, &progress, None,
                )?;
                let resources =
                    resources::inspect(&layout, &agents, &policy, full, None, &progress)?;
                let extensions = extensions::inspect(&layout, &agents, full, &progress)?;
                for report in [&dependencies, &resources, &extensions] {
                    result.errors.extend(
                        report["errors"]
                            .as_array()
                            .unwrap()
                            .iter()
                            .filter_map(|e| e.as_str().map(str::to_string)),
                    );
                }
                doc["project_artifacts"] = dependencies.clone();
                doc["node_modules"] = dependencies;
                doc["agent_resources"] = resources;
                doc["extensions"] = extensions;
                doc["bytes_scope"] = serde_json::json!(
                    "仅原有 Agent 数据根目录；额外资源另列，重叠子项不重复加入此合计"
                );
                doc["errors"] = serde_json::json!(result.errors);
            }
            if args.command == "clean" && args.apply {
                if !result.errors.is_empty() {
                    doc["mode"] = serde_json::json!("aborted");
                    doc["batch"] = serde_json::Value::Null;
                    doc["moved_files"] = serde_json::json!(0);
                    doc["moved_bytes"] = serde_json::json!(0);
                } else {
                    let saved = vault::save(&result.items, &layout, &policy, &progress)?;
                    doc["mode"] = serde_json::json!("quarantine");
                    doc["batch"] = serde_json::json!(saved.batch);
                    doc["moved_files"] = serde_json::json!(saved.count);
                    doc["moved_bytes"] = serde_json::json!(saved.bytes);
                    doc["errors"]
                        .as_array_mut()
                        .unwrap()
                        .extend(saved.errors.into_iter().map(serde_json::Value::String));
                }
            }
            doc
        }
        "inventory" | "report" | "reporter" => {
            let (mut doc, rows) = inventory::inspect(
                &Layout::load()?,
                &policy,
                &args.agent,
                args.archive_days,
                args.delete_days,
                args.full,
                args.limit,
                &Progress::default(),
            )?;
            if let Some(path) = &args.html {
                doc["html_path"] = serde_json::json!(clean_my_agent::reports::export(
                    std::path::Path::new(path),
                    &doc,
                    &rows
                )?);
            }
            doc
        }
        "deps" if args.from_report.is_some() => clean_my_agent::reports::load_dependencies(
            std::path::Path::new(args.from_report.as_ref().unwrap()),
        )?,
        "deps" => {
            let layout = Layout::load()?;
            let progress = if args.timings {
                Progress::default().timed()
            } else {
                Progress::default()
            };
            let started = std::time::Instant::now();
            let mut data = dependency_report::inspect_agents(
                &layout,
                &policy,
                &layout.selected(&args.agent),
                args.path.as_deref().map(std::path::Path::new),
                !args.quick,
                &progress,
                None,
            )?;
            if args.timings {
                // Subprocess rows are also part of the stage that started them.
                eprintln!("阶段耗时（秒 / 次数）");
                for (name, spent, count) in progress.timings() {
                    eprintln!("{:>10.3}  {count:>7}  {name}", spent.as_secs_f64());
                }
                eprintln!("{:>10.3}  {:>7}  合计", started.elapsed().as_secs_f64(), 1);
            }
            if args.apply {
                let row = data["entries"][0].clone();
                if row["status"] != "candidate" || !data["errors"].as_array().unwrap().is_empty() {
                    data["errors"]
                        .as_array_mut()
                        .unwrap()
                        .push(serde_json::json!(
                            row["reason"]
                                .as_str()
                                .unwrap_or("引用或目录状态无法完整核实；未暂存")
                        ));
                } else {
                    let result =
                        actions::stage_artifacts(&layout, &policy, &[row], &Progress::default())?;
                    data["batch"] = result["batch"].clone();
                    data["moved_items"] = result["moved_items"].clone();
                    data["errors"]
                        .as_array_mut()
                        .unwrap()
                        .extend(result["errors"].as_array().unwrap().clone());
                }
            }
            data
        }
        "resources" => {
            let layout = Layout::load()?;
            let selected = if args.agent == "all" {
                resources::FAMILIES.iter().map(|s| s.to_string()).collect()
            } else {
                vec![args.agent.clone()]
            };
            let mut data = resources::inspect(
                &layout,
                &selected,
                &policy,
                !args.quick,
                args.path.as_deref().map(std::path::Path::new),
                &Progress::default(),
            )?;
            if args.apply {
                let result = actions::stage_resources(
                    &layout,
                    &policy,
                    &[data["entries"][0].clone()],
                    &Progress::default(),
                )?;
                data["batch"] = result["batch"].clone();
                data["moved_items"] = result["moved_items"].clone();
                data["errors"]
                    .as_array_mut()
                    .unwrap()
                    .extend(result["errors"].as_array().unwrap().clone());
            }
            data
        }
        "extensions" => extensions::inspect(
            &Layout::load()?,
            &if args.agent == "all" {
                vec!["codex".into(), "claude".into()]
            } else {
                vec![args.agent.clone()]
            },
            !args.quick,
            &Progress::default(),
        )?,
        "trash" => {
            let layout = Layout::load()?;
            if let Some(key) = args.path.as_deref() {
                let result = vault::process(
                    &layout,
                    key,
                    "list",
                    false,
                    None,
                    None,
                    &Progress::default(),
                )?;
                let mut doc = result.detail;
                doc["batch"] = serde_json::json!(key);
                doc["count"] = serde_json::json!(result.count);
                doc["bytes"] = serde_json::json!(result.bytes);
                doc["errors"] = serde_json::json!(result.errors);
                doc
            } else {
                vault::list_entries(&layout, &Progress::default())?
            }
        }
        "restore" | "purge" => {
            let key = args
                .path
                .as_deref()
                .ok_or_else(|| anyhow::anyhow!("请指定批次编号"))?;
            let result = vault::process(
                &Layout::load()?,
                key,
                &args.command,
                args.command == "restore" || args.apply,
                Some(&policy),
                None,
                &Progress::default(),
            )?;
            serde_json::json!({"batch":key,"operation":args.command,"mode":if args.command=="restore"||args.apply{"applied"}else{"preview"},"count":result.count,"bytes":result.bytes,"errors":result.errors,"detail":result.detail})
        }
        _ => bail!("未知命令：{}", args.command),
    };
    if args.json {
        println!("{}", serde_json::to_string_pretty(&document)?);
    } else {
        terminal::print_report(&document);
        if args.details {
            terminal::print_details(&document);
        }
    }
    Ok(
        if document["errors"].as_array().is_some_and(|e| !e.is_empty()) {
            1
        } else {
            0
        },
    )
}
fn main() {
    match run() {
        Ok(code) => std::process::exit(code),
        Err(error) => {
            eprintln!("未完成：{}", terminal::safe(&format!("{error:#}")));
            std::process::exit(1);
        }
    }
}
