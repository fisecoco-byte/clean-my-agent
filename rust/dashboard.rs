use crate::{
    extensions,
    policy::{Layout, Policy},
    progress::Progress,
    resources, scan,
};
use anyhow::Result;
use serde_json::{Value, json};
pub fn snapshot(
    layout: &Layout,
    policy: &Policy,
    agent: &str,
    full: bool,
    progress: &Progress,
) -> Result<Value> {
    let agents = layout.selected(agent);
    let result = scan::scan(layout, &agents, policy, full, progress)?;
    let groups:Vec<_>=result.rows.iter().map(|((a,c),r)|json!({"agent":a,"category":c,"bytes":r[1],"eligible_files":r[2],"eligible_bytes":r[3]})).collect();
    let extra = resources::inspect(layout, &agents, policy, false, None, progress)?;
    let extensions = extensions::inspect(layout, &agents, false, progress)?;
    let mut errors = result.errors.clone();
    for data in [&extra, &extensions] {
        errors.extend(
            data["errors"]
                .as_array()
                .unwrap()
                .iter()
                .filter_map(|v| v.as_str().map(str::to_string)),
        );
    }
    let roots: serde_json::Map<_, _> = layout
        .roots
        .iter()
        .filter(|(a, _)| {
            agents.contains(&if *a == "claude-cli" {
                "claude".into()
            } else {
                (*a).clone()
            })
        })
        .map(|(a, p)| (a.clone(), json!(p)))
        .collect();
    Ok(
        json!({"groups":groups,"bytes":groups.iter().map(|r|r["bytes"].as_u64().unwrap_or(0)).sum::<u64>(),"eligible_bytes":groups.iter().map(|r|r["eligible_bytes"].as_u64().unwrap_or(0)).sum::<u64>(),"eligible_files":result.items.len(),"full":full,"deferred_dirs":result.deferred.len(),"roots":roots,"errors":errors,"missing_roots":result.missing,"agent_resources":extra,"extensions":extensions,"policy":policy.describe(),"read_only":cfg!(windows),"bytes_scope":if cfg!(windows){"Windows 文件分配量，不含文件系统元数据；共享与释放未知；当前仅盘点"}else{"已选 Agent 主目录计块；额外目录另列，不把重叠或共享空间重复相加"},"measured_at":chrono::Local::now().to_rfc3339_opts(chrono::SecondsFormat::Secs,false)}),
    )
}
