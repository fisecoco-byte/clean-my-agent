use crate::{
    actions,
    policy::{Layout, Policy},
    progress::Progress,
    terminal,
    tui::Page,
    worktrees,
};
use anyhow::Result;
use serde_json::Value;

#[derive(Clone)]
pub enum Request {
    Stage(Page, Vec<Value>),
    Sessions(String, Vec<Value>, Value),
    Quarantine(Value),
    Worktree(Value, bool),
}
pub struct Prompt {
    pub title: String,
    pub body: String,
    pub phrase: &'static str,
    pub request: Request,
}
pub enum Outcome {
    Prompt(Prompt),
    Detail(String, String),
    Done(Value),
}
pub fn worktree_body(doc: &Value) -> String {
    let mut body = format!("{}\n\n", terminal::safe(doc["path"].as_str().unwrap_or("")));
    if let Some(checks) = doc["checks"].as_object() {
        for (label, result) in checks {
            body.push_str(&format!("{label}：{}\n", result.as_str().unwrap_or("未知")));
        }
    }
    if let Some(reasons) = doc["reasons"].as_array() {
        for reason in reasons {
            body.push_str(&format!(
                "\n保留原因：{}",
                reason.as_str().unwrap_or("无法核实")
            ));
        }
    }
    body
}
pub fn prepare_worktree(
    layout: &Layout,
    policy: &Policy,
    row: &Value,
    confirm: bool,
    progress: &Progress,
) -> Result<Outcome> {
    let doc = worktrees::inspect(layout, policy, row, progress)?;
    let mut body = worktree_body(&doc);
    let trash = doc["can_trash"] == true;
    if !confirm || (!trash && doc["can_delete"] != true) {
        return Ok(Outcome::Detail("工作目录核对结果".into(), body));
    }
    body.push_str(if trash {
        "\n\n整个独立仓库将移入系统废纸篓，包括文件与 Git 历史。暂不释放空间；清空废纸篓前可恢复。"
    } else {
        "\n\n将通过 Git 删除整个附加工作树。此目录无法由暂存区恢复；仓库分支与会话记录保留。"
    });
    Ok(Outcome::Prompt(Prompt {
        title: if trash {
            "确认移入废纸篓"
        } else {
            "确认删除工作树"
        }
        .into(),
        body,
        phrase: if trash { "TRASH" } else { "DELETE" },
        request: Request::Worktree(doc, trash),
    }))
}
pub fn prepare_quarantine(
    layout: &Layout,
    operation: &str,
    rows: Vec<Value>,
    progress: &Progress,
) -> Result<Outcome> {
    let preview = actions::quarantine_preview(layout, operation, &rows, progress)?;
    let purge = operation == "purge";
    let body = format!(
        "{}\n\n{}",
        if purge {
            "永久清除所选暂存条目，不可恢复。同批次未勾选的内容保留。"
        } else {
            "恢复所选条目到原路径；已有新文件时保留两份，不覆盖。"
        },
        paths(&rows)
    );
    Ok(Outcome::Prompt(Prompt {
        title: if purge {
            "确认永久清除"
        } else {
            "确认恢复"
        }
        .into(),
        body,
        phrase: if purge { "DELETE" } else { "RESTORE" },
        request: Request::Quarantine(preview),
    }))
}
pub fn paths(rows: &[Value]) -> String {
    rows.iter()
        .map(|r| {
            format!(
                "{}  {}",
                terminal::name(r),
                r["path"]
                    .as_str()
                    .or(r["session_id"].as_str())
                    .unwrap_or("")
            )
        })
        .collect::<Vec<_>>()
        .join("\n")
}
pub fn execute(
    request: Request,
    layout: &Layout,
    policy: &Policy,
    agent: &str,
    progress: &Progress,
) -> Result<Outcome> {
    let result = match request {
        Request::Stage(page, rows) => match page {
            Page::Dependencies => actions::stage_artifacts(layout, policy, &rows, progress)?,
            Page::Resources => actions::stage_resources(layout, policy, &rows, progress)?,
            _ => actions::stage_files(layout, policy, agent, &rows, progress)?,
        },
        Request::Sessions(operation, rows, rules) => {
            actions::sessions(layout, &operation, &rows, &rules, progress)?
        }
        Request::Quarantine(preview) => actions::quarantine_apply(layout, &preview, progress)?,
        Request::Worktree(preview, trash) => {
            worktrees::apply(layout, policy, &preview, trash, progress)?
        }
    };
    Ok(Outcome::Done(result))
}
pub fn result_body(doc: &Value) -> String {
    let mut body = String::new();
    if let Some(message) = doc["message"].as_str() {
        body.push_str(message);
    }
    if let Some(count) = doc["count"].as_u64() {
        body.push_str(&format!("已处理 {count} 项。\n"));
    }
    if let Some(batch) = doc["batch"].as_str() {
        body.push_str(&format!(
            "暂存批次：{batch}\n暂存释放 0 B；可在暂存区恢复或永久清除。\n"
        ));
    }
    if let Some(results) = doc["results"].as_array() {
        for row in results {
            body.push_str(&format!(
                "\n{} · {} · {}",
                row["session_id"].as_str().unwrap_or(""),
                row["status"].as_str().unwrap_or(""),
                row["message"].as_str().unwrap_or("")
            ));
        }
    }
    if let Some(errors) = doc["errors"].as_array() {
        for error in errors {
            body.push_str(&format!(
                "\n未完成：{}",
                error.as_str().unwrap_or("未知原因")
            ));
        }
    }
    body
}
