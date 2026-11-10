use serde_json::Value;
pub fn size(value: Option<u64>) -> String {
    let Some(n) = value else {
        return "未知".into();
    };
    if n < 1024 {
        return format!("{n} B");
    }
    let mut amount = n as f64;
    for unit in ["KiB", "MiB", "GiB", "TiB", "PiB"] {
        amount /= 1024.0;
        if amount < 1024.0 || unit == "PiB" {
            return format!("{amount:.2} {unit}");
        }
    }
    unreachable!()
}
pub fn safe(text: &str) -> String {
    text.chars()
        .map(|c| if crate::fsutil::printable(c) { c } else { ' ' })
        .collect()
}
pub fn multiline(text: &str) -> String {
    text.lines().map(safe).collect::<Vec<_>>().join("\n")
}
pub fn name(row: &Value) -> String {
    if let Some(title) = row["title"].as_str() {
        return safe(title);
    }
    if let Some(label) = row["label"].as_str() {
        return safe(label);
    }
    let path = row["path"].as_str().unwrap_or("");
    if !path.is_empty() {
        let p = std::path::Path::new(path);
        return safe(&format!(
            "{} / {}",
            p.parent()
                .and_then(|p| p.file_name())
                .unwrap_or_default()
                .to_string_lossy(),
            p.file_name().unwrap_or_default().to_string_lossy()
        ));
    }
    safe(
        row["name"]
            .as_str()
            .or(row["session_id"].as_str())
            .or(row["batch"].as_str())
            .unwrap_or("（无标题或摘要）"),
    )
}
pub fn release(row: &Value) -> String {
    if let Some(n) = row["reclaimable_bytes"].as_u64() {
        size(Some(n))
    } else if let (Some(low), Some(high)) = (
        row["reclaimable_min_bytes"].as_u64(),
        row["reclaimable_max_bytes"].as_u64(),
    ) {
        format!("{}–{}", size(Some(low)), size(Some(high)))
    } else {
        "未知".into()
    }
}
pub fn status(row: &Value) -> &str {
    match row["status"].as_str() {
        Some("candidate") => "可暂存",
        Some("retained") => "保留",
        Some("unmeasured") => "未测量",
        Some("owner-managed") => "原生管理",
        _ => match row["state"].as_str() {
            Some("open") => "未归档",
            Some("archived") => "已归档",
            Some("unverified") => "未核实",
            _ => "仅盘点",
        },
    }
}
pub fn details(row: &Value) -> String {
    let mut lines = vec![name(row)];
    for (key, title) in [
        ("path", "完整路径"),
        ("session_id", "会话 ID"),
        ("agent", "Agent"),
        ("ecosystem", "类型"),
        ("artifact_category", "类别"),
        ("version", "版本"),
        ("source", "来源"),
        ("rebuild", "重建方式"),
        ("reason", "保留 / 操作说明"),
        ("hold_reason", "保留原因"),
        ("guidance", "建议"),
        ("batch", "暂存批次"),
        ("created", "暂存时间"),
    ] {
        if let Some(value) = row[key].as_str().filter(|s| !s.is_empty()) {
            lines.push(format!("\n{title}\n{}", safe(value)));
        }
    }
    if row.get("directory_bytes").is_some()
        || row.get("bytes").is_some()
        || row.get("size").is_some()
    {
        lines.push(format!(
            "\n目录 / 文件占用\n{}",
            size(
                row["directory_bytes"]
                    .as_u64()
                    .or(row["bytes"].as_u64())
                    .or(row["size"].as_u64())
            )
        ));
    }
    if row.get("reclaimable_bytes").is_some() {
        lines.push(format!(
            "\n预计永久删除释放\n{}\n\n共享占用\n{}",
            release(row),
            size(row["shared_bytes"].as_u64())
        ));
    }
    if row["activity"].is_object() {
        lines.push(format!(
            "\n活跃度\n{}\n{}",
            crate::activity::badge(row),
            safe(row["activity"]["basis"].as_str().unwrap_or("缺少活动记录"))
        ));
        if let Some(warning) = row["activity"]["warning"].as_str() {
            lines.push(safe(warning));
        }
    }
    for (key, title) in [("installation", "安装检查"), ("runtime", "运行检查")] {
        if let Some(reason) = row[key]["reason"].as_str() {
            lines.push(format!("\n{title}\n{}", safe(reason)));
        }
    }
    if let Some(references) = row["referenced_by"].as_array().filter(|r| !r.is_empty()) {
        lines.push("\n引用".into());
        lines.extend(references.iter().filter_map(|v| v.as_str().map(safe)));
    }
    lines.join("\n")
}
pub fn print_report(doc: &Value) {
    if let Some(operation) = doc["operation"].as_str() {
        let label = if operation == "restore" {
            "恢复"
        } else {
            "永久清除"
        };
        println!(
            "{} · {} · {} 项 · 清单容量 {}",
            label,
            if doc["mode"] == "preview" {
                "仅预览，未执行"
            } else {
                "执行结果"
            },
            doc["count"],
            size(doc["bytes"].as_u64())
        );
        if let Some(batch) = doc["batch"].as_str() {
            println!("批次：{}", safe(batch));
        }
        if let Some(files) = doc["detail"]["files"].as_array() {
            for row in files {
                println!(
                    "  {}  {}",
                    size(row[1].as_u64()),
                    safe(row[0].as_str().unwrap_or(""))
                );
            }
        }
        if doc["mode"] == "preview" {
            println!("确认后对该批次添加 --apply 执行；永久清除不可恢复。");
        }
    } else if let Some(items) = doc["items"].as_array() {
        println!("暂存区 · {} 项 · 暂存不释放空间", items.len());
        for row in items {
            println!(
                "{}  {}  {}",
                safe(row["batch"].as_str().unwrap_or("")),
                size(row["bytes"].as_u64()),
                safe(row["path"].as_str().unwrap_or(""))
            );
        }
    } else if let Some(entries) = doc["entries"].as_array() {
        println!("{}", doc["title"].as_str().unwrap_or("盘点结果"));
        for row in entries {
            println!(
                "{}  {}  {}",
                status(row),
                size(row["directory_bytes"].as_u64()),
                row["path"].as_str().map(safe).unwrap_or_else(|| name(row))
            );
            if let Some(reason) = row["reason"].as_str() {
                println!("  {}", safe(reason));
            }
        }
        if entries.is_empty() {
            println!("未发现已识别的资源。");
        }
    } else if let Some(agents) = doc["agents"].as_object() {
        for (agent, data) in agents {
            println!(
                "{agent} · {} 个会话 · {}",
                data["session_count"],
                size(data["session_bytes"].as_u64())
            );
            println!("  未活跃天数分布：");
            for bucket in crate::inventory::BUCKETS {
                println!("    {bucket} · {} 个", data["histogram"][bucket]);
            }
            for action in ["archive", "delete"] {
                let plan = &data["recommendations"][action];
                println!(
                    "{}复核：{} 个",
                    if action == "archive" {
                        "归档"
                    } else {
                        "删除"
                    },
                    plan["count"]
                );
                if let Some(rows) = plan["entries"].as_array() {
                    for row in rows {
                        println!(
                            "  {}  {}  {}",
                            crate::activity::badge(row),
                            name(row),
                            row["session_id"].as_str().unwrap_or("")
                        );
                    }
                }
            }
        }
    } else if let Some(groups) = doc["groups"].as_array() {
        println!(
            "已统计 {} · 可暂存 {}",
            size(doc["bytes"].as_u64()),
            size(doc["eligible_bytes"].as_u64())
        );
        for row in groups {
            println!(
                "{}  {}  {}",
                row["agent"].as_str().unwrap_or(""),
                size(row["bytes"].as_u64()),
                row["category"].as_str().unwrap_or("")
            );
        }
        for (key, label) in [
            ("project_artifacts", "项目产物"),
            ("agent_resources", "Agent 资源"),
            ("extensions", "扩展版本"),
        ] {
            if let Some(rows) = doc[key]["entries"].as_array() {
                println!(
                    "{} · {} 项 · {} 项可暂存",
                    label,
                    rows.len(),
                    rows.iter().filter(|r| r["status"] == "candidate").count()
                );
            }
        }
    } else {
        println!("{}", serde_json::to_string_pretty(doc).unwrap_or_default());
    }
    if let Some(batch) = doc["batch"].as_str()
        && (doc.get("moved_items").is_some() || doc.get("moved_files").is_some())
    {
        println!(
            "已暂存 {} 项；批次 {}。暂存释放 0 B，可恢复。",
            doc.get("moved_items").or(doc.get("moved_files")).unwrap(),
            safe(batch)
        );
    }
    if let Some(files) = doc["files"].as_array() {
        for row in files {
            if let Some(path) = row["path"].as_str() {
                println!("  {}  {}", size(row["bytes"].as_u64()), safe(path));
            }
        }
    }
    if let Some(path) = doc["html_path"].as_str() {
        println!("离线报告：{}", safe(path));
    }
    if doc["mode"] == "aborted" {
        println!("核对未完成，没有执行暂存。请处理以下提示后重新预览。");
    }
    if let Some(errors) = doc["errors"].as_array() {
        for error in errors.iter().take(10) {
            eprintln!("提示：{}", safe(error.as_str().unwrap_or("未完成")));
        }
    }
}

pub fn print_details(doc: &Value) {
    if let Some(agents) = doc["agents"].as_object() {
        for (agent, data) in agents {
            if let Some(groups) = data["groups"].as_object() {
                for (category, group) in groups {
                    println!(
                        "\n{} / {} · {} 项",
                        safe(agent),
                        safe(category),
                        group["count"]
                    );
                    if let Some(rows) = group["entries"].as_array() {
                        for row in rows {
                            println!(
                                "  {}  {}",
                                size(row["bytes"].as_u64()),
                                safe(row["path"].as_str().unwrap_or(""))
                            );
                        }
                    }
                }
            }
        }
    }
    for data in std::iter::once(doc).chain(
        ["project_artifacts", "agent_resources", "extensions"]
            .iter()
            .filter_map(|k| doc.get(k)),
    ) {
        if let Some(rows) = data["entries"].as_array() {
            for row in rows {
                println!("\n{}", details(row));
            }
        }
    }
}
