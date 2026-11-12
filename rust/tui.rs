use crate::{
    activity, dashboard, dependency_report, extensions, fsutil, inventory,
    policy::{Layout, Policy, SESSION_DAYS},
    progress::{Progress, Update},
    resources, scan,
    terminal::{details, multiline, name, release, safe, size, status},
    ui_actions::{self, Outcome, Prompt, Request},
    vault,
};
use anyhow::{Result, bail};
use crossterm::{
    event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseEventKind},
    execute,
};
use ratatui::{
    Frame,
    layout::{Alignment, Constraint, Layout as Boxes, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span, Text},
    widgets::{
        Block, Borders, Cell, Clear, Gauge, Padding, Paragraph, Row, Table, TableState, Wrap,
    },
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    io::{self, IsTerminal},
    path::Path,
    sync::{
        LazyLock,
        mpsc::{self, Receiver, Sender},
    },
    thread,
    time::{Duration, Instant},
};

const BG: Color = Color::Rgb(13, 27, 29);
const PANEL: Color = Color::Rgb(24, 38, 40);
const MINT: Color = Color::Rgb(125, 226, 191);
const SOFT: Color = Color::Rgb(154, 180, 172);
const TEXT: Color = Color::Rgb(218, 232, 224);
const AMBER: Color = Color::Rgb(239, 196, 128);
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Page {
    Home,
    Dependencies,
    Sessions,
    Files,
    Resources,
    Extensions,
    Batches,
}
impl Page {
    pub fn title(self) -> &'static str {
        match self {
            Self::Home => "Agent 存储总览",
            Self::Dependencies => "工作树 / 多语言产物",
            Self::Sessions => "会话管理",
            Self::Files => "缓存与旧日志",
            Self::Resources => "Agent 缓存 / 版本 / 残留",
            Self::Extensions => "Skill / MCP / Plugin · 只读版本复核",
            Self::Batches => "暂存区",
        }
    }
}
#[derive(Clone)]
pub struct Settings {
    pub layout: Layout,
    pub policy: Policy,
    pub agent: String,
    pub full: bool,
    pub archive_days: Option<i64>,
    pub delete_days: Option<i64>,
    pub limit: usize,
    pub read_only: bool,
    pub snapshot: Option<Value>,
}
#[derive(Default)]
pub struct Loaded {
    pub doc: Value,
    pub sessions: std::collections::BTreeMap<String, Vec<Value>>,
}
enum Message {
    Preview(Value),
    Complete(Result<Loaded>),
    Action(Result<Outcome>),
}
struct Worker {
    progress: Progress,
    updates: Receiver<Update>,
    messages: Receiver<Message>,
}
impl Drop for Worker {
    fn drop(&mut self) {
        self.progress.cancel();
    }
}
fn load(
    page: Page,
    settings: &Settings,
    progress: &Progress,
    sender: &Sender<Message>,
) -> Result<Loaded> {
    if let Some(doc) = &settings.snapshot
        && page == Page::Dependencies
    {
        return Ok(Loaded {
            doc: doc.clone(),
            ..Default::default()
        });
    }
    let layout = &settings.layout;
    let policy = &settings.policy;
    let agents = layout.selected(&settings.agent);
    let doc = match page {
        Page::Home => {
            dashboard::snapshot(layout, policy, &settings.agent, settings.full, progress)?
        }
        Page::Dependencies => dependency_report::inspect_agents(
            layout,
            policy,
            &agents,
            None,
            true,
            progress,
            Some(&mut |value| {
                let _ = sender.send(Message::Preview(value));
            }),
        )?,
        Page::Sessions => {
            if settings.agent == "t3" {
                bail!("T3 会话目前仅计量占用，尚无可验证的会话管理接口");
            }
            let (doc, sessions) = inventory::inspect(
                layout,
                policy,
                &settings.agent,
                settings.archive_days,
                settings.delete_days,
                false,
                settings.limit,
                progress,
            )?;
            return Ok(Loaded { doc, sessions });
        }
        Page::Files => {
            let result = scan::scan(layout, &agents, policy, false, progress)?;
            json!({"items":result.items,"errors":result.errors})
        }
        Page::Resources => resources::inspect(
            layout,
            &if settings.agent == "all" {
                resources::FAMILIES.iter().map(|s| s.to_string()).collect()
            } else {
                agents
            },
            policy,
            true,
            None,
            progress,
        )?,
        Page::Extensions => extensions::inspect(layout, &agents, true, progress)?,
        Page::Batches => vault::list_entries(layout, progress)?,
    };
    Ok(Loaded {
        doc,
        ..Default::default()
    })
}
static FRAMES: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../assets/loading/frames.json"))
        .expect("embedded loading frames")
});
static CAT: LazyLock<Vec<Text<'static>>> = LazyLock::new(|| {
    let palette: Vec<_> = FRAMES["CAT_PALETTE"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| {
            let s = s.as_str().unwrap();
            Color::Rgb(
                u8::from_str_radix(&s[1..3], 16).unwrap(),
                u8::from_str_radix(&s[3..5], 16).unwrap(),
                u8::from_str_radix(&s[5..7], 16).unwrap(),
            )
        })
        .collect();
    FRAMES["CAT_FRAMES"]
        .as_array()
        .unwrap()
        .iter()
        .map(|frame| {
            let mut lines = Vec::new();
            let mut spans = Vec::new();
            for (c, color) in frame[0]
                .as_str()
                .unwrap()
                .chars()
                .zip(frame[1].as_str().unwrap().chars())
            {
                if c == '\n' {
                    lines.push(Line::from(std::mem::take(&mut spans)));
                } else {
                    let color = color
                        .to_digit(10)
                        .and_then(|i| palette.get(i as usize))
                        .copied()
                        .unwrap_or(MINT);
                    spans.push(Span::styled(c.to_string(), Style::default().fg(color)));
                }
            }
            lines.push(Line::from(spans));
            Text::from(lines)
        })
        .collect()
});
#[derive(Clone)]
struct Dialog {
    title: String,
    body: String,
    scroll: u16,
}
struct SavedHome {
    agent: String,
    data: Loaded,
    table: TableState,
    query: String,
}
pub struct App {
    settings: Settings,
    page: Page,
    data: Loaded,
    saved_home: Option<SavedHome>,
    rows: Vec<Value>,
    table: TableState,
    checked: BTreeSet<String>,
    worker: Option<Worker>,
    pending: bool,
    scan_complete: bool,
    phase: Update,
    started: Instant,
    notice: String,
    workspace_view: bool,
    workspace_filters: Vec<String>,
    group: u8,
    sort: u8,
    query: String,
    searching: bool,
    session_agent: String,
    session_mode: String,
    archive_days: i64,
    delete_days: i64,
    dialog: Option<Dialog>,
    confirmation: Option<(Request, String, String)>,
    writing: bool,
    quit: bool,
    table_area: Rect,
}
impl App {
    pub fn new(settings: Settings, page: Page) -> Self {
        let session_agent = if settings.agent == "claude" {
            "claude"
        } else {
            "codex"
        }
        .to_string();
        let rules = &settings.policy.sessions[&session_agent];
        let archive_days = settings
            .archive_days
            .unwrap_or(rules["archive_days"].as_i64().unwrap_or(10));
        let delete_days = settings
            .delete_days
            .unwrap_or(rules["delete_days"].as_i64().unwrap_or(20));
        Self {
            settings,
            page,
            data: Loaded::default(),
            saved_home: None,
            rows: Vec::new(),
            table: TableState::default().with_selected(0),
            checked: BTreeSet::new(),
            worker: None,
            pending: true,
            scan_complete: false,
            phase: Update {
                phase: "准备扫描".into(),
                done: None,
                total: None,
                detail: String::new(),
            },
            started: Instant::now(),
            notice: String::new(),
            workspace_view: page == Page::Dependencies,
            workspace_filters: Vec::new(),
            group: 1,
            sort: 0,
            query: String::new(),
            searching: false,
            session_agent,
            session_mode: "archive".into(),
            archive_days,
            delete_days,
            dialog: None,
            confirmation: None,
            writing: false,
            quit: false,
            table_area: Rect::default(),
        }
    }
    fn busy(&self) -> bool {
        self.pending || self.worker.is_some()
    }
    fn scan_notice(&mut self) {
        let n = self.data.doc["errors"].as_array().map_or(0, Vec::len);
        self.notice = if n > 0 {
            format!("{n} 条扫描提示 · ? 查看；涉及未核实内容的操作保持关闭")
        } else {
            String::new()
        };
    }
    fn reload(&mut self) {
        // Explicit refreshes also invalidate the parent overview, whose totals
        // may no longer match what the child page is about to discover.
        self.saved_home = None;
        self.start_scan();
    }
    /// Applies a finished action to the rows already on screen: the entries it
    /// reported as moved disappear and their projects are recounted, instead of
    /// walking the whole page again. Everything left keeps the result of its
    /// last check, which is why the notice says what was and was not redone.
    fn settle(&mut self, doc: &Value) {
        // The overview totals are derived from this page, so they are rescanned
        // on return even though the page itself is not.
        self.saved_home = None;
        let mut moved: BTreeSet<String> = doc["moved_paths"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|path| path.as_str().map(str::to_string))
            .collect();
        if let Some(path) = doc["path"].as_str() {
            moved.insert(path.into());
        }
        // Sessions change state rather than path, and a result without paths
        // says nothing about which rows it touched.
        if self.page == Page::Home || self.page == Page::Sessions || moved.is_empty() {
            self.reload();
            return;
        }
        let page = self.page;
        // File rows carry a short path under their data root; every other page
        // lists absolute paths.
        let key = |row: &Value| -> Option<String> {
            let path = match page {
                Page::Files => Path::new(row["root"].as_str()?).join(row["path"].as_str()?),
                _ => Path::new(row["path"].as_str()?).to_path_buf(),
            };
            Some(path.to_string_lossy().into_owned())
        };
        // A removed worktree takes the artifacts inside it along.
        let projects: BTreeSet<String> = self.data.doc["projects"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|row| row["path"].as_str().map(str::to_string))
            .filter(|path| moved.contains(path))
            .collect();
        let mut removed = Vec::new();
        let mut take = |rows: &mut Vec<Value>, under_project: bool| {
            rows.retain(|row| match key(row) {
                Some(path) => {
                    let gone = moved.contains(&path)
                        || (under_project
                            && projects.iter().any(|p| Path::new(&path).starts_with(p)));
                    if gone {
                        removed.push(row.clone());
                    }
                    !gone
                }
                None => true,
            });
        };
        if let Some(rows) = self.data.doc["entries"].as_array_mut() {
            take(rows, true);
        }
        if let Some(rows) = self.data.doc["items"].as_array_mut() {
            take(rows, false);
        }
        if let Some(rows) = self.data.doc["projects"].as_array_mut() {
            rows.retain(|row| !key(row).is_some_and(|path| moved.contains(&path)));
        }
        let artifacts: Vec<String> = self.data.doc["entries"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|row| row["path"].as_str().map(str::to_string))
            .collect();
        // Measured sizes lose only what the operation reported as moved out.
        let gone: BTreeMap<&str, u64> = removed
            .iter()
            .filter_map(|row| {
                Some((
                    row["project"].as_str()?,
                    row["directory_bytes"].as_u64().unwrap_or(0),
                ))
            })
            .fold(BTreeMap::new(), |mut sums, (project, bytes)| {
                *sums.entry(project).or_default() += bytes;
                sums
            });
        for project in self.data.doc["projects"]
            .as_array_mut()
            .into_iter()
            .flatten()
        {
            let repo = project["path"].as_str().unwrap_or_default().to_string();
            let moved = gone.get(repo.as_str()).copied();
            project["artifacts"] = json!(
                artifacts
                    .iter()
                    .filter(|path| {
                        let path = Path::new(path);
                        path == Path::new(&repo)
                            || (path != Path::new(&repo) && path.starts_with(&repo))
                    })
                    .count()
            );
            if let (Some(bytes), Some(moved)) = (project["directory_bytes"].as_u64(), moved) {
                project["directory_bytes"] = json!(bytes.saturating_sub(moved));
            }
        }
        self.notice = "已按操作结果更新列表；其余内容沿用上次核查，r 可完整刷新".into();
        self.rebuild();
    }
    fn start_scan(&mut self) {
        self.worker.take();
        self.pending = true;
        self.scan_complete = false;
        self.data = Loaded::default();
        self.rows.clear();
        self.checked.clear();
        self.table.select(Some(0));
        self.notice.clear();
        self.phase = Update {
            phase: "准备扫描".into(),
            done: None,
            total: None,
            detail: String::new(),
        };
        self.started = Instant::now();
    }
    fn navigate(&mut self, page: Page) {
        // Only retain a finished overview. A cancelled or failed scan must
        // still show loading and retry when the user returns.
        if self.page == Page::Home && self.scan_complete && !self.busy() {
            self.saved_home = Some(SavedHome {
                agent: self.settings.agent.clone(),
                data: std::mem::take(&mut self.data),
                table: self.table,
                query: self.query.clone(),
            });
        }
        self.page = page;
        if page == Page::Sessions {
            self.session_agent = if self.settings.agent == "claude" {
                "claude"
            } else {
                "codex"
            }
            .into();
            let rules = &self.settings.policy.sessions[&self.session_agent];
            self.archive_days = self
                .settings
                .archive_days
                .unwrap_or(rules["archive_days"].as_i64().unwrap_or(10));
            self.delete_days = self
                .settings
                .delete_days
                .unwrap_or(rules["delete_days"].as_i64().unwrap_or(20));
        }
        self.workspace_view = page == Page::Dependencies;
        self.workspace_filters.clear();
        self.query.clear();
        self.group = 1;
        self.dialog = None;
        self.start_scan();
        if page == Page::Home
            && let Some(saved) = self.saved_home.take()
            && saved.agent == self.settings.agent
        {
            self.data = saved.data;
            self.table = saved.table;
            self.query = saved.query;
            self.pending = false;
            self.scan_complete = true;
            self.scan_notice();
            self.rebuild();
        }
    }
    fn launch(&mut self) {
        if !self.pending {
            return;
        }
        self.pending = false;
        let (update_tx, updates) = mpsc::channel();
        let (message_tx, messages) = mpsc::channel();
        let progress = Progress::new(update_tx);
        let work = progress.clone();
        let page = self.page;
        let settings = self.settings.clone();
        thread::spawn(move || {
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                load(page, &settings, &work, &message_tx)
            }))
            .unwrap_or_else(|_| Err(anyhow::anyhow!("扫描未能完成，请刷新后重试")));
            let _ = message_tx.send(Message::Complete(result));
        });
        self.worker = Some(Worker {
            progress,
            updates,
            messages,
        });
    }
    fn receive(&mut self) {
        let mut messages = Vec::new();
        if let Some(worker) = &self.worker {
            while let Ok(update) = worker.updates.try_recv() {
                self.phase = update;
            }
            while let Ok(message) = worker.messages.try_recv() {
                messages.push(message);
            }
        }
        for message in messages {
            match message {
                Message::Action(result) => {
                    self.worker.take();
                    self.writing = false;
                    match result {
                        Ok(Outcome::Prompt(prompt)) => self.prompt(prompt),
                        Ok(Outcome::Detail(title, body)) => {
                            self.dialog = Some(Dialog {
                                title,
                                body: multiline(&body),
                                scroll: 0,
                            })
                        }
                        Ok(Outcome::Done(doc)) => {
                            // A clean finish lets the page keep its rows minus
                            // what moved; any uncertainty rewrites it.
                            let clean = doc["errors"].as_array().is_none_or(|e| e.is_empty())
                                && doc["status"].as_str().is_none_or(|s| s == "succeeded")
                                && doc["results"].as_array().is_none_or(|rows| {
                                    rows.iter().all(|r| r["status"] == "succeeded")
                                });
                            if clean {
                                self.settle(&doc);
                            } else {
                                self.reload();
                            }
                            self.dialog = Some(Dialog {
                                title: "操作结果".into(),
                                body: multiline(&ui_actions::result_body(&doc)),
                                scroll: 0,
                            });
                        }
                        Err(e) => {
                            // A failed read-only review does not change the
                            // overview. Writes invalidate it before they start.
                            self.start_scan();
                            self.dialog = Some(Dialog {
                                title: "操作未完成".into(),
                                body: multiline(&format!("{e:#}\n请根据刷新后的状态核对结果。")),
                                scroll: 0,
                            });
                        }
                    }
                }
                Message::Preview(doc) => {
                    self.data.doc = doc;
                    self.rebuild();
                }
                Message::Complete(result) => {
                    self.worker.take();
                    match result {
                        Ok(data) => {
                            self.data = data;
                            self.scan_complete = true;
                            self.scan_notice();
                            self.rebuild();
                        }
                        Err(e) => {
                            self.notice = format!("扫描未完成：{e:#} · r 重试");
                            self.scan_complete = false;
                            self.rows.clear();
                        }
                    }
                }
            }
        }
    }
    fn home_rows(&self) -> Vec<Value> {
        let groups = self.data.doc["groups"]
            .as_array()
            .cloned()
            .unwrap_or_default();
        let amount = |categories: &[&str]| {
            groups
                .iter()
                .filter(|r| categories.contains(&r["category"].as_str().unwrap_or("")))
                .map(|r| r["bytes"].as_u64().unwrap_or(0))
                .sum::<u64>()
        };
        [("sessions","会话与归档",size(Some(amount(&["sessions","archives"]))),"归档 / 删除"),("deps","工作树与依赖",size(Some(amount(&["worktrees","codex-projects"]))),"产物 / 清理工作目录"),("clean","缓存与旧日志",size(Some(amount(&["cache","logs"]))),"暂存"),("extensions","Skill / MCP / Plugin",size(Some(amount(&["plugins"]))),"只读版本复核"),("resources","版本与桌面资源",format!("{} 项待测量",self.data.doc["agent_resources"]["entries"].as_array().map_or(0,Vec::len)),"核对 / 暂存"),("protected","其他受保护数据",size(Some(groups.iter().filter(|r|!["sessions","archives","worktrees","codex-projects","cache","logs","plugins"].contains(&r["category"].as_str().unwrap_or(""))).map(|r|r["bytes"].as_u64().unwrap_or(0)).sum())),"保留"),("trash","暂存区","按条目".into(),"恢复 / 永久清除")].into_iter().map(|(route,label,amount,action)|json!({"route":route,"label":label,"amount":amount,"action":action})).collect()
    }
    fn rebuild(&mut self) {
        let previous = self.current().map(Self::identity);
        let mut rows = match self.page {
            Page::Home => self.home_rows(),
            Page::Dependencies if self.workspace_view => self.data.doc["projects"]
                .as_array()
                .cloned()
                .unwrap_or_default(),
            Page::Sessions => self
                .data
                .sessions
                .get(&self.session_agent)
                .cloned()
                .unwrap_or_default(),
            Page::Files | Page::Batches => self.data.doc["items"]
                .as_array()
                .cloned()
                .unwrap_or_default(),
            _ => self.data.doc["entries"]
                .as_array()
                .cloned()
                .unwrap_or_default(),
        };
        rows.retain(|r| {
            let matches = match self.page {
                Page::Dependencies | Page::Resources if !self.workspace_view => {
                    let group = if r["kind"] == "symlink" {
                        3
                    } else if r["status"] == "candidate" {
                        1
                    } else {
                        2
                    };
                    (self.group == 0 || self.group == group)
                        && (self.workspace_filters.is_empty()
                            || self.workspace_filters.iter().any(|p| {
                                r["path"].as_str().is_some_and(|s| {
                                    Path::new(s) != Path::new(p) && Path::new(s).starts_with(p)
                                })
                            }))
                }
                Page::Sessions => {
                    let route = r["route"].as_str().unwrap_or("");
                    let age = r["idle_days"].as_i64().unwrap_or(0);
                    match self.session_mode.as_str() {
                        "archive" => {
                            ["archive", "review"].contains(&route)
                                && age >= self.archive_days
                                && !(route == "review" && age >= self.delete_days)
                        }
                        "delete" => {
                            ["delete", "review"].contains(&route) && age >= self.delete_days
                        }
                        _ => true,
                    }
                }
                _ => true,
            };
            matches
                && (self.query.is_empty()
                    || [
                        name(r),
                        r["path"].as_str().unwrap_or("").into(),
                        r["session_id"].as_str().unwrap_or("").into(),
                        r["agent"].as_str().unwrap_or("").into(),
                    ]
                    .iter()
                    .any(|s| s.to_lowercase().contains(&self.query.to_lowercase())))
        });
        if self.page != Page::Home {
            rows.sort_by(|a, b| {
                let bytes = |r: &Value| {
                    r["directory_bytes"]
                        .as_u64()
                        .or(r["bytes"].as_u64())
                        .or(r["size"].as_u64())
                        .unwrap_or(0)
                };
                if (self.workspace_view || self.page == Page::Sessions) && self.sort < 2 {
                    let days = |r: &Value| {
                        r["activity"]["idle_days"]
                            .as_i64()
                            .filter(|_| r["activity"]["level"] != "unknown")
                    };
                    let x = days(a);
                    let y = days(b);
                    return x
                        .is_none()
                        .cmp(&y.is_none())
                        .then(if self.sort == 0 { y.cmp(&x) } else { x.cmp(&y) })
                        .then(bytes(b).cmp(&bytes(a)))
                        .then(Self::identity(a).cmp(&Self::identity(b)));
                }
                let amount = |r: &Value| {
                    if self.sort == 0 && !self.workspace_view {
                        r["reclaimable_bytes"]
                            .as_u64()
                            .or(r["reclaimable_min_bytes"].as_u64())
                            .unwrap_or(0)
                    } else {
                        bytes(r)
                    }
                };
                amount(b)
                    .cmp(&amount(a))
                    .then(Self::identity(a).cmp(&Self::identity(b)))
            });
        }
        self.rows = rows;
        let visible: BTreeSet<_> = self.rows.iter().map(Self::identity).collect();
        self.checked.retain(|id| visible.contains(id));
        let index = previous
            .and_then(|p| self.rows.iter().position(|r| Self::identity(r) == p))
            .unwrap_or(self.table.selected().unwrap_or(0))
            .min(self.rows.len().saturating_sub(1));
        self.table.select(Some(index));
    }
    fn identity(row: &Value) -> String {
        format!(
            "{}:{}:{}:{}",
            row["agent"]
                .as_str()
                .or(row["source"].as_str())
                .unwrap_or(""),
            row["root"].as_str().unwrap_or(""),
            row["path"]
                .as_str()
                .or(row["session_id"].as_str())
                .or(row["route"].as_str())
                .or(row["batch"].as_str())
                .unwrap_or(""),
            row["index"]
                .as_u64()
                .map(|v| v.to_string())
                .unwrap_or_default()
        )
    }
    fn current(&self) -> Option<&Value> {
        self.table.selected().and_then(|i| self.rows.get(i))
    }
    fn selectable(&self, row: &Value) -> bool {
        if self.busy() || !self.scan_complete || self.settings.read_only || cfg!(windows) {
            return false;
        }
        match self.page {
            Page::Dependencies if self.workspace_view => row["path"].is_string(),
            Page::Dependencies | Page::Resources => row["status"] == "candidate",
            Page::Files | Page::Batches => true,
            Page::Sessions => {
                self.session_agent == "codex"
                    && row["route"]
                        .as_str()
                        .is_some_and(|r| r == self.session_mode)
            }
            _ => false,
        }
    }
    fn toggle(&mut self) {
        if let Some(row) = self.current()
            && self.selectable(row)
        {
            let id = Self::identity(row);
            if !self.checked.remove(&id) {
                self.checked.insert(id);
            }
            self.notice.clear();
        } else {
            self.notice = if self.busy() || !self.scan_complete {
                "扫描尚未完成，完成后可勾选和清理".into()
            } else if self.settings.read_only || cfg!(windows) {
                "当前处于只读模式，不能勾选清理".into()
            } else {
                self.current()
                    .and_then(|row| row["reason"].as_str())
                    .map(|reason| format!("当前条目保留：{}", safe(reason)))
                    .unwrap_or_else(|| "当前条目不可勾选，请按 Enter 查看详情".into())
            };
        }
    }
    fn selected_workspaces(&self) -> Vec<Value> {
        if self.checked.is_empty() {
            self.current().cloned().into_iter().collect()
        } else {
            self.rows
                .iter()
                .filter(|row| self.checked.contains(&Self::identity(row)))
                .cloned()
                .collect()
        }
    }
    fn show_workspace_artifacts(&mut self, rows: &[Value]) {
        self.workspace_filters = rows
            .iter()
            .filter_map(|row| row["path"].as_str().map(str::to_string))
            .collect();
        if self.workspace_filters.is_empty() {
            self.notice = "请先选择工作目录".into();
            return;
        }
        self.workspace_view = false;
        self.group = 0;
        self.checked.clear();
        self.query.clear();
        self.notice.clear();
        self.table.select(Some(0));
        self.rebuild();
        if self.rows.is_empty() && !self.busy() {
            self.notice = "所选工作目录没有识别到项目产物；按 w 返回，可用 d 核对整个目录".into();
        }
    }
    fn select_all(&mut self) {
        let ids: Vec<_> = self
            .rows
            .iter()
            .filter(|r| self.selectable(r))
            .map(Self::identity)
            .collect();
        let remove = ids.iter().all(|id| self.checked.contains(id));
        for id in ids {
            if remove {
                self.checked.remove(&id);
            } else {
                self.checked.insert(id);
            }
        }
    }
    fn move_cursor(&mut self, delta: isize) {
        let current = self.table.selected().unwrap_or(0);
        self.table.select(Some(
            current
                .saturating_add_signed(delta)
                .min(self.rows.len().saturating_sub(1)),
        ));
    }
    fn open(&mut self) {
        let Some(row) = self.current().cloned() else {
            return;
        };
        if self.page == Page::Home {
            let page = match row["route"].as_str() {
                Some("sessions") => Page::Sessions,
                Some("deps") => Page::Dependencies,
                Some("clean") => Page::Files,
                Some("resources") => Page::Resources,
                Some("extensions") => Page::Extensions,
                Some("trash") => Page::Batches,
                _ => {
                    let mut body = "数据库、终端快照及用途未核实的数据仅盘点。\n".to_string();
                    if let Some(groups) = self.data.doc["groups"].as_array() {
                        for r in groups {
                            body.push_str(&format!(
                                "\n{} · {} · {}",
                                r["agent"].as_str().unwrap_or(""),
                                r["category"].as_str().unwrap_or(""),
                                size(r["bytes"].as_u64())
                            ));
                        }
                    }
                    self.dialog = Some(Dialog {
                        title: "受保护数据".into(),
                        body,
                        scroll: 0,
                    });
                    return;
                }
            };
            self.navigate(page);
        } else if self.workspace_view && !self.busy() && self.scan_complete {
            self.worktree(row, false);
        } else {
            self.dialog = Some(Dialog {
                title: if self.workspace_view {
                    "工作目录详情"
                } else {
                    "详情"
                }
                .into(),
                body: details(&row),
                scroll: 0,
            });
        }
    }
    fn help(&mut self) {
        let mut body="↑↓ / j k 移动光标 · Enter 详情 · / 搜索 · s 排序\nSpace 勾选 · a / Ctrl+A 全选 / 取消全选\nr 刷新 · q / Esc 返回 · Ctrl+C 退出\n\n工作目录：Space 可多选，t 查看所选目录的产物清理清单\nd 核对整个目录（每次一个）；未勾选时核对光标所在目录\np 仅查看光标所在目录的产物，w 切换工作目录 / 全部产物\n勾选只表示操作范围，不表示该目录已通过清理检查\n产物页：1 候选，2 保留，3 链接，Space 勾选候选，t 确认暂存\n暂存区：r 恢复，d 永久清除\n会话页：a 待归档，d 待删除，v 全部，x 操作清单\n[ 调整归档天数，] 调整删除天数，i 查看分布\n\n暂存释放 0 B，可恢复。共享量与释放区间不可相加。\n扫描期间及未完成核实时不开放文件操作。".to_string();
        if let Some(errors) = self.data.doc["errors"].as_array() {
            for error in errors {
                if let Some(error) = error.as_str() {
                    body.push_str(&format!("\n\n提示：{}", safe(error)));
                }
            }
        }
        self.dialog = Some(Dialog {
            title: "帮助与扫描提示".into(),
            body,
            scroll: 0,
        });
    }
    fn overview(&mut self) {
        let data = &self.data.doc["agents"][&self.session_agent];
        let mut body = format!(
            "{} · {} 个会话 · {}\n\n{}\n",
            self.session_agent,
            data["session_count"],
            size(data["session_bytes"].as_u64()),
            activity::LEGEND
        );
        for bucket in inventory::BUCKETS {
            let count = data["histogram"][bucket].as_u64().unwrap_or(0);
            body.push_str(&format!(
                "\n{bucket:10} {} {count}",
                "━".repeat(count.min(35) as usize)
            ));
        }
        if let Some(groups) = data["groups"].as_object() {
            for (category, group) in groups {
                body.push_str(&format!(
                    "\n\n{} · {} 项 · {}",
                    safe(category),
                    group["count"],
                    size(group["bytes"].as_u64())
                ));
                if let Some(rows) = group["entries"].as_array() {
                    for row in rows {
                        body.push_str(&format!(
                            "\n{}  {}",
                            size(row["bytes"].as_u64()),
                            safe(row["path"].as_str().unwrap_or(""))
                        ));
                    }
                }
            }
        }
        self.dialog = Some(Dialog {
            title: "会话活跃分布".into(),
            body,
            scroll: 0,
        });
    }
    fn back(&mut self) {
        if self.page == Page::Home {
            self.worker.take();
            self.quit = true;
        } else if self.page == Page::Dependencies && !self.workspace_view {
            self.workspace_view = true;
            self.workspace_filters.clear();
            self.checked.clear();
            self.query.clear();
            self.notice.clear();
            self.rebuild();
        } else {
            self.navigate(Page::Home);
        }
    }
    fn spawn_action<F>(&mut self, writing: bool, action: F)
    where
        F: FnOnce(Settings, Progress) -> Result<Outcome> + Send + 'static,
    {
        if writing {
            // Even failed writes can have partially changed files.
            self.saved_home = None;
        }
        let (update_tx, updates) = mpsc::channel();
        let (message_tx, messages) = mpsc::channel();
        let progress = Progress::new(update_tx);
        let work = progress.clone();
        let settings = self.settings.clone();
        self.writing = writing;
        self.started = Instant::now();
        self.phase = Update {
            phase: if writing {
                "正在执行"
            } else {
                "执行前核对"
            }
            .into(),
            done: None,
            total: None,
            detail: String::new(),
        };
        self.worker = Some(Worker {
            progress,
            updates,
            messages,
        });
        thread::spawn(move || {
            let result =
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| action(settings, work)))
                    .unwrap_or_else(|_| {
                        Err(anyhow::anyhow!("操作异常中断，请刷新核对状态，勿直接重试"))
                    });
            let _ = message_tx.send(Message::Action(result));
        });
    }
    fn prompt(&mut self, prompt: Prompt) {
        self.dialog = Some(Dialog {
            title: prompt.title,
            body: multiline(&prompt.body),
            scroll: 0,
        });
        self.confirmation = Some((prompt.request, prompt.phrase.into(), String::new()));
    }
    fn worktree(&mut self, row: Value, confirm: bool) {
        self.spawn_action(false, move |settings, progress| {
            ui_actions::prepare_worktree(
                &settings.layout,
                &settings.policy,
                &row,
                confirm,
                &progress,
            )
        });
    }
    fn action(&mut self, operation: &str) {
        if self.busy() || !self.scan_complete {
            self.notice = "扫描尚未完成，暂不能清理；完成后请重试".into();
            return;
        }
        if self.settings.read_only || cfg!(windows) {
            self.notice = "当前处于只读模式".into();
            return;
        }
        if self.workspace_view {
            let rows = self.selected_workspaces();
            if operation == "stage" {
                self.show_workspace_artifacts(&rows);
            } else if rows.len() == 1 {
                self.worktree(rows[0].clone(), true);
            } else if rows.is_empty() {
                self.notice = "请先选择工作目录".into();
            } else {
                self.dialog = Some(Dialog {
                    title: "整目录清理每次核对一个".into(),
                    body: format!(
                        "已勾选 {} 个工作目录。\n\n整目录清理请只勾选一个，再按 d。\n若要批量清理依赖和构建产物，按 Esc 返回后按 t。\n\n尚未执行任何清理。",
                        rows.len()
                    ),
                    scroll: 0,
                });
            }
            return;
        }
        let rows: Vec<_> = self
            .rows
            .iter()
            .filter(|r| self.checked.contains(&Self::identity(r)) && self.selectable(r))
            .cloned()
            .collect();
        if rows.is_empty() {
            self.notice = "请先用 Space 勾选可操作的条目".into();
            return;
        }
        match self.page {
            Page::Batches => {
                let operation = if operation == "restore" {
                    "restore"
                } else {
                    "purge"
                };
                self.spawn_action(false, move |settings, progress| {
                    ui_actions::prepare_quarantine(&settings.layout, operation, rows, &progress)
                });
            }
            Page::Sessions if self.session_agent == "codex" && self.session_mode != "all" => {
                let delete = self.session_mode == "delete";
                let body = format!(
                    "{} {} 个 Codex 会话。{}\n\n{}",
                    if delete { "永久删除" } else { "归档" },
                    rows.len(),
                    if delete {
                        "不可恢复；执行前逐项复核活动与状态。"
                    } else {
                        "通过 Codex 原生命令执行；执行前逐项复核活动与状态。"
                    },
                    ui_actions::paths(&rows)
                );
                self.prompt(Prompt {
                    title: "确认会话操作".into(),
                    body,
                    phrase: if delete { "DELETE" } else { "ARCHIVE" },
                    request: Request::Sessions(
                        self.session_mode.clone(),
                        rows,
                        json!({"archive_days":self.archive_days,"delete_days":self.delete_days}),
                    ),
                });
            }
            Page::Dependencies | Page::Resources | Page::Files => {
                self.prompt(Prompt{title:"确认暂存".into(),body:format!("将暂存 {} 项；暂存释放 0 B，可从暂存区恢复。执行前将重新核对目录、引用及策略。\n\n{}",rows.len(),ui_actions::paths(&rows)),phrase:"STAGE",request:Request::Stage(self.page,rows)});
            }
            _ => self.notice = "当前页面仅供复核".into(),
        }
    }
    fn key(&mut self, key: KeyEvent) {
        if key.kind != KeyEventKind::Press {
            return;
        }
        if self.writing {
            self.notice = "操作正在执行，请等待结果；完成后可返回".into();
            return;
        }
        if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
            self.worker.take();
            self.quit = true;
            return;
        }
        if let Some((_, phrase, input)) = &mut self.confirmation {
            match key.code {
                KeyCode::Esc => {
                    self.confirmation = None;
                    self.dialog = None;
                }
                KeyCode::Backspace => {
                    input.pop();
                }
                KeyCode::Char(c) if fsutil::printable(c) => {
                    if input.len() < 32 {
                        input.push(c);
                    }
                }
                KeyCode::Enter if input == phrase => {
                    let (request, _, _) = self.confirmation.take().unwrap();
                    self.dialog = None;
                    self.spawn_action(true, move |settings, progress| {
                        ui_actions::execute(
                            request,
                            &settings.layout,
                            &settings.policy,
                            &settings.agent,
                            &progress,
                        )
                    });
                }
                KeyCode::PageDown => {
                    if let Some(d) = &mut self.dialog {
                        d.scroll = d.scroll.saturating_add(10);
                    }
                }
                KeyCode::PageUp => {
                    if let Some(d) = &mut self.dialog {
                        d.scroll = d.scroll.saturating_sub(10);
                    }
                }
                _ => (),
            }
            return;
        }
        if let Some(dialog) = &mut self.dialog {
            match key.code {
                KeyCode::Esc | KeyCode::Char('q') | KeyCode::Enter => self.dialog = None,
                KeyCode::Down | KeyCode::Char('j') => {
                    dialog.scroll = dialog.scroll.saturating_add(1)
                }
                KeyCode::Up | KeyCode::Char('k') => dialog.scroll = dialog.scroll.saturating_sub(1),
                KeyCode::PageDown => dialog.scroll = dialog.scroll.saturating_add(10),
                KeyCode::PageUp => dialog.scroll = dialog.scroll.saturating_sub(10),
                _ => (),
            }
            return;
        }
        if self.searching {
            match key.code {
                KeyCode::Esc => {
                    self.query.clear();
                    self.searching = false;
                }
                KeyCode::Enter => self.searching = false,
                KeyCode::Backspace => {
                    self.query.pop();
                }
                KeyCode::Char(c) if fsutil::printable(c) => self.query.push(c),
                _ => (),
            }
            self.rebuild();
            return;
        }
        match key.code {
            KeyCode::Char('q') | KeyCode::Esc => self.back(),
            KeyCode::Char('?') => self.help(),
            KeyCode::Char('/') => self.searching = true,
            KeyCode::Down | KeyCode::Char('j') => self.move_cursor(1),
            KeyCode::Up | KeyCode::Char('k') => self.move_cursor(-1),
            KeyCode::PageDown => self.move_cursor(12),
            KeyCode::PageUp => self.move_cursor(-12),
            KeyCode::Home | KeyCode::Char('g') => self.table.select(Some(0)),
            KeyCode::End | KeyCode::Char('G') => {
                self.table.select(Some(self.rows.len().saturating_sub(1)))
            }
            KeyCode::Enter => self.open(),
            KeyCode::Char('r') if self.page == Page::Batches && !self.checked.is_empty() => {
                self.action("restore")
            }
            KeyCode::Char('r') => {
                if !self.busy() {
                    self.reload();
                }
            }
            KeyCode::Char('s') => {
                self.sort = (self.sort + 1) % 3;
                self.rebuild();
            }
            KeyCode::Char(' ') => self.toggle(),
            KeyCode::Char('a')
                if key.modifiers.contains(KeyModifiers::CONTROL) || self.page != Page::Sessions =>
            {
                self.select_all()
            }
            KeyCode::Char(c @ '0'..='3') if self.page == Page::Home => {
                self.settings.agent = match c {
                    '1' => "codex",
                    '2' => "claude",
                    '3' => "t3",
                    _ => "all",
                }
                .into();
                self.reload();
            }
            KeyCode::Char(c @ '1'..='2') if self.page == Page::Sessions => {
                let agent = if c == '1' { "codex" } else { "claude" };
                if self.data.sessions.contains_key(agent) {
                    self.session_agent = agent.into();
                    let rules = &self.settings.policy.sessions[agent];
                    self.archive_days = self
                        .settings
                        .archive_days
                        .unwrap_or(rules["archive_days"].as_i64().unwrap_or(10));
                    self.delete_days = self
                        .settings
                        .delete_days
                        .unwrap_or(rules["delete_days"].as_i64().unwrap_or(20));
                    self.checked.clear();
                    self.rebuild();
                }
            }
            KeyCode::Char(c @ '1'..='3')
                if matches!(self.page, Page::Dependencies | Page::Resources) =>
            {
                if self.workspace_view {
                    self.checked.clear();
                }
                self.workspace_view = false;
                self.group = c as u8 - b'0';
                self.rebuild();
            }
            KeyCode::Char('w') if self.page == Page::Dependencies => {
                self.workspace_view = !self.workspace_view;
                self.workspace_filters.clear();
                self.checked.clear();
                self.query.clear();
                self.notice.clear();
                self.rebuild();
            }
            KeyCode::Char('p') if self.workspace_view => {
                let rows: Vec<_> = self.current().cloned().into_iter().collect();
                self.show_workspace_artifacts(&rows);
            }
            KeyCode::Char('a' | 'd' | 'v') if self.page == Page::Sessions => {
                self.session_mode = match key.code {
                    KeyCode::Char('a') => "archive",
                    KeyCode::Char('d') => "delete",
                    _ => "all",
                }
                .into();
                self.checked.clear();
                self.rebuild();
            }
            KeyCode::Char('[' | ']') if self.page == Page::Sessions => {
                let days = if key.code == KeyCode::Char('[') {
                    &mut self.archive_days
                } else {
                    &mut self.delete_days
                };
                *days = SESSION_DAYS[(SESSION_DAYS.iter().position(|d| d == days).unwrap_or(0)
                    + 1)
                    % SESSION_DAYS.len()];
                self.checked.clear();
                self.rebuild();
            }
            KeyCode::Char('i') if self.page == Page::Sessions => self.overview(),
            KeyCode::Char('t' | 'x') => self.action("stage"),
            KeyCode::Char('d') => self.action("delete"),
            KeyCode::Char('v') => self.navigate(Page::Batches),
            KeyCode::Char('0') => self.navigate(Page::Home),
            _ => (),
        }
    }
    fn tabline(&self) -> String {
        match self.page {
            Page::Home => format!(
                "0 全部    1 Codex    2 Claude    3 T3     当前：{}",
                self.settings.agent
            ),
            Page::Dependencies if self.workspace_view => {
                "工作目录 · Enter 详情 · Space 多选 · 勾选后仍需核对清理条件".into()
            }
            Page::Dependencies | Page::Resources => format!(
                "{}1 候选   {}2 保留   {}3 链接     w 工作目录",
                if self.group == 1 { "▸ " } else { "" },
                if self.group == 2 { "▸ " } else { "" },
                if self.group == 3 { "▸ " } else { "" }
            ),
            Page::Sessions => format!(
                "1 Codex  2 Claude · {}   a 归档 ≥{}天  d 删除 ≥{}天  v 全部",
                self.session_agent, self.archive_days, self.delete_days
            ),
            Page::Extensions => "来源 · 版本 · 引用 · 原生回收建议；仅盘点".into(),
            Page::Files => "仅明确符合策略的缓存 / 旧日志；Space 勾选，t 暂存".into(),
            Page::Batches => "Space 勾选条目 · r 恢复 · d 永久清除".into(),
        }
    }
    fn actionline(&self) -> String {
        if self.settings.read_only || cfg!(windows) {
            return if self.workspace_view {
                "只读模式 · p 查看当前目录产物 · Enter 查看详情".into()
            } else {
                "只读模式 · Enter 查看详情".into()
            };
        }
        if self.workspace_view {
            let scope = if self.checked.is_empty() {
                "范围：光标所在目录".into()
            } else {
                format!("范围：已勾选 {} 个目录", self.checked.len())
            };
            return format!("[t 清理产物]  [d 清理整个目录]  [p 查看当前目录产物]\n{scope}");
        }
        match self.page {
            Page::Dependencies | Page::Resources | Page::Files => {
                "[t 暂存所选项]  [v 暂存区 / 恢复 / 永久清除]".into()
            }
            Page::Batches => "[r 恢复所选项]  [d 永久清除所选项]".into(),
            Page::Sessions => "[x 核对所选会话操作]  [i 活跃分布]".into(),
            Page::Home => "Enter 打开当前分类".into(),
            Page::Extensions => "仅盘点 · Enter 查看详情".into(),
        }
    }
    fn render_loading(&self, frame: &mut Frame, area: Rect) {
        let elapsed = self.started.elapsed();
        let index = (elapsed.as_millis() * 12 / 1000) as usize;
        let large = area.width >= 44 && area.height >= 19;
        let height = if large { 19 } else { 5 };
        let center = Rect {
            x: area.x,
            y: area.y + area.height.saturating_sub(height) / 2,
            width: area.width,
            height: height.min(area.height),
        };
        let blocks = Boxes::vertical([
            Constraint::Length(if large { 15 } else { 1 }),
            Constraint::Length(1),
            Constraint::Length(1),
            Constraint::Length(1),
            Constraint::Length(1),
        ])
        .split(center);
        if large {
            frame.render_widget(
                Paragraph::new(CAT[index % CAT.len()].clone()).alignment(Alignment::Center),
                blocks[0],
            );
        } else {
            let frames = FRAMES["SCAN_FRAMES"].as_array().unwrap();
            frame.render_widget(
                Paragraph::new(frames[index % frames.len()].as_str().unwrap())
                    .alignment(Alignment::Center)
                    .style(Style::default().fg(MINT)),
                blocks[0],
            );
        }
        frame.render_widget(
            Paragraph::new(format!("正在扫描 · {}", safe(&self.phase.phase)))
                .alignment(Alignment::Center)
                .style(Style::default().fg(MINT)),
            blocks[1],
        );
        frame.render_widget(
            Paragraph::new(safe(&self.phase.detail))
                .alignment(Alignment::Center)
                .style(Style::default().fg(SOFT)),
            blocks[2],
        );
        frame.render_widget(
            Paragraph::new(format!(
                "{:02}:{:02} · q 返回并取消",
                elapsed.as_secs() / 60,
                elapsed.as_secs() % 60
            ))
            .alignment(Alignment::Center)
            .style(Style::default().fg(SOFT)),
            blocks[3],
        );
        if let (Some(done), Some(total)) = (self.phase.done, self.phase.total)
            && total > 0
        {
            let gauge = Gauge::default()
                .ratio((done as f64 / total as f64).clamp(0., 1.))
                .label(format!("{done}/{total}"))
                .gauge_style(Style::default().fg(MINT).bg(PANEL));
            let rect = Rect {
                x: center.x + center.width.saturating_sub(36) / 2,
                width: center.width.min(36),
                ..blocks[4]
            };
            frame.render_widget(gauge, rect);
        }
    }
    pub fn draw(&mut self, frame: &mut Frame) {
        let area = frame.area();
        frame.render_widget(
            Block::default().style(Style::default().bg(BG).fg(TEXT)),
            area,
        );
        if area.width < 44 || area.height < 16 {
            frame.render_widget(
                Paragraph::new("窗口太小，请调整到至少 44 列 × 16 行。\nq 退出")
                    .wrap(Wrap { trim: false })
                    .style(Style::default().fg(AMBER)),
                area,
            );
            return;
        }
        let layout = Boxes::vertical([
            Constraint::Length(1),
            Constraint::Length(2),
            Constraint::Length(1),
            Constraint::Length(2),
            Constraint::Min(4),
            Constraint::Length(2),
            Constraint::Length(2),
            Constraint::Length(1),
        ])
        .margin(1)
        .split(area);
        frame.render_widget(
            Paragraph::new(format!(
                "CLEAN MY AGENT   /   {}",
                env!("CARGO_PKG_VERSION")
            ))
            .style(Style::default().fg(MINT).add_modifier(Modifier::BOLD)),
            layout[0],
        );
        frame.render_widget(
            Paragraph::new(format!("\n{}", self.page.title()))
                .style(Style::default().add_modifier(Modifier::BOLD)),
            layout[1],
        );
        frame.render_widget(
            Paragraph::new(self.tabline()).style(Style::default().fg(MINT)),
            layout[2],
        );
        let context = if self.searching || !self.query.is_empty() {
            format!("搜索：{}▏", safe(&self.query))
        } else if self.page == Page::Home && !self.data.doc.is_null() {
            format!(
                "主目录占用 {}   ·   缓存 / 日志可暂存 {}",
                size(self.data.doc["bytes"].as_u64()),
                size(self.data.doc["eligible_bytes"].as_u64())
            )
        } else {
            format!(
                "{} 项 · 已选 {}{}{}",
                self.rows.len(),
                self.checked.len(),
                if !self.workspace_view && !self.workspace_filters.is_empty() {
                    format!(" · 范围：{} 个工作目录", self.workspace_filters.len())
                } else {
                    String::new()
                },
                if self.writing {
                    " · 正在执行操作，请等待完成"
                } else if self.busy() && self.scan_complete {
                    " · 执行前核对中，文件操作暂不可用"
                } else if self.busy() {
                    " · 后台扫描中，文件操作暂不可用"
                } else {
                    ""
                }
            )
        };
        frame.render_widget(
            Paragraph::new(context).style(Style::default().fg(SOFT)),
            layout[3],
        );
        if self.busy() && self.rows.is_empty() {
            self.render_loading(frame, layout[4]);
        } else if self.rows.is_empty() {
            let message = if !self.notice.is_empty() {
                &self.notice
            } else if self.scan_complete {
                "扫描完成，当前筛选下没有记录。"
            } else {
                "尚未完成扫描。r 重试"
            };
            frame.render_widget(
                Paragraph::new(message)
                    .wrap(Wrap { trim: false })
                    .style(Style::default().fg(AMBER)),
                layout[4],
            );
        } else {
            self.render_table(frame, layout[4]);
        }
        let phase = if self.busy() {
            let elapsed = self.started.elapsed().as_secs();
            let counts = match (self.phase.done, self.phase.total) {
                (Some(done), Some(total)) if total > 0 => format!(" {done}/{total}"),
                _ => String::new(),
            };
            format!(
                "{}{} · {:02}:{:02} · {}{} · {}",
                if self.notice.is_empty() {
                    String::new()
                } else {
                    format!("{}\n", self.notice)
                },
                ["◐", "◓", "◑", "◒"][(self.started.elapsed().as_millis() / 100) as usize % 4],
                elapsed / 60,
                elapsed % 60,
                safe(&self.phase.phase),
                counts,
                safe(&self.phase.detail)
            )
        } else if !self.notice.is_empty() {
            self.notice.clone()
        } else if self.page == Page::Home {
            self.data.doc["bytes_scope"].as_str().unwrap_or("").into()
        } else {
            "暂存释放 0 B · 共享量和预计释放不可相加".into()
        };
        frame.render_widget(
            Paragraph::new(phase)
                .wrap(Wrap { trim: false })
                .style(Style::default().fg(if self.notice.is_empty() { SOFT } else { AMBER })),
            layout[5],
        );
        frame.render_widget(
            Paragraph::new(self.actionline())
                .wrap(Wrap { trim: false })
                .style(Style::default().fg(MINT).add_modifier(Modifier::BOLD)),
            layout[6],
        );
        let footer = if self.page == Page::Sessions {
            "Space 勾选 · x 清单 · i 分布 · / 搜索 · r 刷新 · ? 帮助 · q 返回"
        } else if self.settings.read_only
            || cfg!(windows)
            || matches!(self.page, Page::Home | Page::Extensions)
        {
            "Enter 详情 · / 搜索 · s 排序 · r 刷新 · ? 帮助 · q 返回"
        } else {
            "Space 勾选 · a 全选 · Enter 详情 · / 搜索 · s 排序 · r 刷新 · ? 帮助 · q 返回"
        };
        frame.render_widget(
            Paragraph::new(footer).style(Style::default().fg(TEXT)),
            layout[7],
        );
        if let Some(dialog) = &self.dialog {
            let width = area.width.saturating_sub(8).min(110);
            let height = area.height.saturating_sub(4);
            let rect = Rect {
                x: (area.width - width) / 2,
                y: 2,
                width,
                height,
            };
            frame.render_widget(Clear, rect);
            frame.render_widget(
                Paragraph::new(dialog.body.clone())
                    .wrap(Wrap { trim: false })
                    .scroll((dialog.scroll, 0))
                    .block(
                        Block::bordered()
                            .title(format!(" {} · Esc 返回 ", safe(&dialog.title)))
                            .padding(Padding::new(2, 2, 1, 1))
                            .style(Style::default().bg(PANEL).fg(TEXT)),
                    )
                    .style(Style::default().bg(PANEL).fg(TEXT)),
                rect,
            );
            if let Some((_, phrase, input)) = &self.confirmation {
                let entry = Rect {
                    x: rect.x + 2,
                    y: rect.y + rect.height.saturating_sub(3),
                    width: rect.width.saturating_sub(4),
                    height: 2,
                };
                frame.render_widget(Clear, entry);
                frame.render_widget(
                    Paragraph::new(format!(
                        "输入 {phrase} 后按 Enter 确认 · Esc 取消\n> {input}"
                    ))
                    .style(Style::default().bg(PANEL).fg(AMBER)),
                    entry,
                );
            }
        }
    }
    fn render_table(&mut self, frame: &mut Frame, area: Rect) {
        let sidebar = area.width >= 105 && self.page != Page::Home;
        let panes = Boxes::horizontal(if sidebar {
            vec![Constraint::Percentage(70), Constraint::Percentage(30)]
        } else {
            vec![Constraint::Percentage(100)]
        })
        .split(area);
        self.table_area = panes[0];
        let headers: Vec<&str> = if self.page == Page::Home {
            vec!["存储类别", "占用", "操作"]
        } else if self.workspace_view {
            vec![
                " ",
                "工作树 / 工作目录",
                "Agent",
                "活跃度",
                "目录占用",
                "产物",
            ]
        } else if self.page == Page::Sessions {
            vec![" ", "标题 / 会话 ID", "活跃度", "会话占用", "状态"]
        } else if self.page == Page::Extensions {
            vec!["名称 / 路径", "类型", "版本", "占用"]
        } else if matches!(self.page, Page::Files | Page::Batches) {
            vec![" ", "原路径", "来源 / 批次", "占用"]
        } else {
            vec![" ", "项目 / 资源", "预计释放", "目录大小", "共享占用"]
        };
        let widths: Vec<Constraint> = if self.page == Page::Home {
            vec![
                Constraint::Percentage(45),
                Constraint::Length(21),
                Constraint::Min(10),
            ]
        } else if self.workspace_view {
            vec![
                Constraint::Length(3),
                Constraint::Min(16),
                Constraint::Length(8),
                Constraint::Length(13),
                Constraint::Length(13),
                Constraint::Length(5),
            ]
        } else if self.page == Page::Sessions {
            vec![
                Constraint::Length(3),
                Constraint::Min(16),
                Constraint::Length(13),
                Constraint::Length(12),
                Constraint::Length(9),
            ]
        } else if self.page == Page::Extensions {
            vec![
                Constraint::Min(16),
                Constraint::Length(8),
                Constraint::Length(12),
                Constraint::Length(13),
            ]
        } else if matches!(self.page, Page::Files | Page::Batches) {
            vec![
                Constraint::Length(3),
                Constraint::Min(16),
                Constraint::Length(10),
                Constraint::Length(13),
            ]
        } else {
            vec![
                Constraint::Length(3),
                Constraint::Min(16),
                Constraint::Length(22),
                Constraint::Length(13),
                Constraint::Length(13),
            ]
        };
        let rows: Vec<_> = self
            .rows
            .iter()
            .map(|r| {
                let checkbox = if self.checked.contains(&Self::identity(r)) {
                    "☑"
                } else if self.selectable(r) {
                    "☐"
                } else {
                    " "
                }
                .to_string();
                let cells = if self.page == Page::Home {
                    vec![
                        name(r),
                        r["amount"].as_str().unwrap_or("").into(),
                        r["action"].as_str().unwrap_or("").into(),
                    ]
                } else if self.workspace_view {
                    vec![
                        checkbox,
                        name(r),
                        r["agent"].as_str().unwrap_or("").into(),
                        activity::badge(r),
                        size(r["directory_bytes"].as_u64()),
                        r["artifacts"].as_u64().unwrap_or(0).to_string(),
                    ]
                } else if self.page == Page::Sessions {
                    vec![
                        checkbox,
                        name(r),
                        activity::badge(r),
                        size(r["bytes"].as_u64()),
                        status(r).into(),
                    ]
                } else if self.page == Page::Extensions {
                    vec![
                        name(r),
                        r["category"].as_str().unwrap_or("").into(),
                        r["version"].as_str().unwrap_or("未知").into(),
                        size(r["directory_bytes"].as_u64()),
                    ]
                } else if matches!(self.page, Page::Files | Page::Batches) {
                    vec![
                        checkbox,
                        r["path"].as_str().map(safe).unwrap_or_default(),
                        if self.page == Page::Batches {
                            r["batch"].as_str().unwrap_or("").into()
                        } else {
                            r["category"].as_str().unwrap_or("").into()
                        },
                        size(r["size"].as_u64().or(r["bytes"].as_u64())),
                    ]
                } else {
                    vec![
                        checkbox,
                        name(r),
                        release(r),
                        size(r["directory_bytes"].as_u64()),
                        size(r["shared_bytes"].as_u64()),
                    ]
                };
                Row::new(cells.into_iter().map(Cell::from))
            })
            .collect();
        let table = Table::new(rows, widths)
            .header(
                Row::new(headers).style(
                    Style::default()
                        .bg(Color::Rgb(40, 60, 62))
                        .fg(TEXT)
                        .add_modifier(Modifier::BOLD),
                ),
            )
            .row_highlight_style(
                Style::default()
                    .bg(Color::Rgb(24, 78, 68))
                    .fg(Color::White)
                    .add_modifier(Modifier::BOLD),
            )
            .style(Style::default().bg(PANEL))
            .column_spacing(1);
        frame.render_stateful_widget(table, panes[0], &mut self.table);
        if sidebar {
            let body = self
                .current()
                .map(details)
                .unwrap_or_else(|| "无选中项".into());
            frame.render_widget(
                Paragraph::new(body)
                    .wrap(Wrap { trim: false })
                    .block(
                        Block::default()
                            .borders(Borders::LEFT)
                            .border_style(Style::default().fg(Color::Rgb(50, 80, 75)))
                            .padding(Padding::new(2, 0, 0, 0))
                            .title(" 当前选项 "),
                    )
                    .style(Style::default().fg(SOFT)),
                panes[1],
            );
        }
    }
}
pub fn available() -> bool {
    io::stdin().is_terminal()
        && io::stdout().is_terminal()
        && std::env::var("TERM").is_ok_and(|t| !t.is_empty() && t != "dumb")
}
pub fn run(settings: Settings, page: Page) -> Result<()> {
    let mut terminal = ratatui::init();
    struct Restore;
    impl Drop for Restore {
        fn drop(&mut self) {
            let _ = execute!(io::stdout(), event::DisableMouseCapture);
            ratatui::restore();
        }
    }
    let _restore = Restore;
    execute!(io::stdout(), event::EnableMouseCapture)?;
    let mut app = App::new(settings, page);
    while !app.quit {
        app.receive();
        terminal.draw(|frame| app.draw(frame))?;
        app.launch();
        if event::poll(Duration::from_millis(50))? {
            match event::read()? {
                Event::Key(key) => app.key(key),
                Event::Mouse(mouse) => match mouse.kind {
                    MouseEventKind::ScrollDown => app.move_cursor(3),
                    MouseEventKind::ScrollUp => app.move_cursor(-3),
                    MouseEventKind::Down(event::MouseButton::Left)
                        if app.table_area.contains((mouse.column, mouse.row).into())
                            && mouse.row > app.table_area.y =>
                    {
                        let index =
                            app.table.offset() + (mouse.row - app.table_area.y - 1) as usize;
                        if index < app.rows.len() {
                            app.table.select(Some(index));
                        }
                    }
                    _ => (),
                },
                _ => (),
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn settings() -> Settings {
        Settings {
            layout: Layout {
                roots: Default::default(),
                state: "/fixture/state".into(),
                home: "/fixture".into(),
            },
            policy: Policy {
                profile: "standard".into(),
                retention: Default::default(),
                protect: vec![],
                rules: vec![],
                sessions: json!({"codex":{"archive_days":10,"delete_days":20}}),
                path: "/fixture/policy.json".into(),
                cli_profile: None,
                cli_days: None,
            },
            agent: "codex".into(),
            full: true,
            archive_days: None,
            delete_days: None,
            limit: 10,
            read_only: false,
            snapshot: None,
        }
    }
    #[test]
    fn first_frame_is_loading_before_worker_starts() {
        let mut app = App::new(settings(), Page::Dependencies);
        let mut terminal =
            ratatui::Terminal::new(ratatui::backend::TestBackend::new(120, 40)).unwrap();
        terminal.draw(|f| app.draw(f)).unwrap();
        let text = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect::<String>()
            .replace(' ', "");
        assert!(text.contains("正在扫描"));
        assert!(!text.contains("没有记录"));
        assert!(app.worker.is_none());
    }
    #[test]
    fn preview_rows_do_not_enable_actions() {
        let mut app = App::new(settings(), Page::Dependencies);
        app.workspace_view = false;
        app.data.doc = json!({"entries":[{"path":"/fixture/target","agent":"codex","status":"candidate","kind":"directory"}],"projects":[]});
        app.rebuild();
        app.select_all();
        assert!(app.checked.is_empty());
        app.pending = false;
        app.scan_complete = true;
        app.select_all();
        assert_eq!(app.checked.len(), usize::from(!cfg!(windows)));
        app.select_all();
        assert!(app.checked.is_empty());
    }
    #[test]
    fn cleanup_progress_shows_action_counts_and_elapsed_time() {
        let mut app = workspaces();
        app.pending = true;
        app.writing = true;
        app.phase = Update {
            phase: "暂存 2/3 · 复核工作树引用".into(),
            done: Some(4),
            total: Some(22),
            detail: "已检查 2048 项 · /fixture/repo".into(),
        };
        app.started = Instant::now() - Duration::from_secs(65);
        let mut terminal =
            ratatui::Terminal::new(ratatui::backend::TestBackend::new(140, 40)).unwrap();
        terminal.draw(|f| app.draw(f)).unwrap();
        let text = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect::<String>()
            .replace(' ', "");
        assert!(text.contains("正在执行操作"));
        assert!(!text.contains("后台扫描中"));
        assert!(text.contains("01:05"));
        assert!(text.contains("暂存2/3·复核工作树引用4/22"));
        assert!(text.contains("已检查2048项"));
    }
    #[test]
    fn failure_is_distinct_from_empty_success() {
        let mut app = App::new(settings(), Page::Dependencies);
        app.pending = false;
        app.notice = "扫描未完成：无法读取目录".into();
        let mut terminal =
            ratatui::Terminal::new(ratatui::backend::TestBackend::new(100, 30)).unwrap();
        terminal.draw(|f| app.draw(f)).unwrap();
        let text = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect::<String>()
            .replace(' ', "");
        assert!(text.contains("扫描未完成"));
        assert!(!text.contains("扫描完成，当前筛选下没有记录"));
    }
    fn workspaces() -> App {
        let mut app = App::new(settings(), Page::Dependencies);
        app.pending = false;
        app.scan_complete = true;
        app.data.doc = json!({
            "projects":[
                {"agent":"codex","path":"/fixture/alpha"},
                {"agent":"codex","path":"/fixture/alpha-other"},
                {"agent":"codex","path":"/fixture/beta"}
            ],
            "entries":[
                {"agent":"codex","path":"/fixture/alpha/target","status":"candidate","kind":"directory"},
                {"agent":"codex","path":"/fixture/alpha/cache","status":"retained","reason":"当前目录正在使用","kind":"directory"},
                {"agent":"codex","path":"/fixture/alpha-other/target","status":"candidate","kind":"directory"},
                {"agent":"codex","path":"/fixture/beta/build","status":"candidate","kind":"directory"}
            ]
        });
        app.rebuild();
        app.table.select(
            app.rows
                .iter()
                .position(|row| row["path"] == "/fixture/alpha"),
        );
        app
    }
    fn press(app: &mut App, c: char) {
        app.key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
    }
    fn finished_home() -> App {
        let mut app = App::new(settings(), Page::Home);
        app.pending = false;
        app.scan_complete = true;
        app.data.doc = json!({
            "groups":[{"agent":"codex","category":"logs","bytes":8192}],
            "errors":[], "measured_at":"original observation"
        });
        app.rebuild();
        app.table.select(Some(2)); // caches and old logs
        app
    }
    #[test]
    fn browsing_children_restores_overview_without_starting_a_scan() {
        let mut app = finished_home();
        let original = app.data.doc.clone();
        for page in [
            Page::Files,
            Page::Resources,
            Page::Dependencies,
            Page::Sessions,
            Page::Extensions,
            Page::Batches,
        ] {
            app.navigate(page);
            assert!(app.pending); // child still gets its own initial scan
            app.back(); // returning during a child scan is also supported
            assert_eq!(app.page, Page::Home);
            assert!(!app.busy());
            assert!(app.scan_complete);
            assert_eq!(app.data.doc, original);
            assert_eq!(app.current().unwrap()["route"], "clean");
            app.launch();
            assert!(app.worker.is_none());
        }
        app.open();
        assert_eq!(app.page, Page::Files);

        // Completed reports retain their warnings too, without retrying the
        // same inaccessible paths on every return from a read-only page.
        let mut app = finished_home();
        app.data.doc["errors"] = json!(["fixture inaccessible directory"]);
        app.scan_notice();
        let notice = app.notice.clone();
        app.navigate(Page::Files);
        app.back();
        assert!(!app.busy());
        assert_eq!(app.notice, notice);
        assert_eq!(app.data.doc["errors"].as_array().unwrap().len(), 1);
    }
    #[test]
    fn unfinished_or_failed_overviews_are_not_reused() {
        for case in 0..2 {
            let mut app = finished_home();
            match case {
                0 => app.pending = true,
                _ => app.scan_complete = false,
            }
            app.navigate(Page::Files);
            app.back();
            assert!(app.pending);
            assert!(!app.scan_complete);
            assert!(app.data.doc.is_null());
        }
    }
    #[test]
    fn refreshes_and_agent_changes_do_not_restore_old_totals() {
        let mut app = finished_home();
        app.navigate(Page::Files);
        app.pending = false;
        app.scan_complete = true;
        press(&mut app, 'r');
        app.back();
        assert!(app.pending);
        assert!(app.data.doc.is_null());

        let mut app = finished_home();
        app.navigate(Page::Files);
        app.back();
        press(&mut app, 'r');
        assert!(app.pending);
        assert!(app.data.doc.is_null());

        let mut app = finished_home();
        press(&mut app, '2');
        assert_eq!(app.settings.agent, "claude");
        app.navigate(Page::Files);
        app.back();
        assert!(app.pending);
        assert!(app.data.doc.is_null());

        let mut app = finished_home();
        app.navigate(Page::Files);
        app.settings.agent = "all".into();
        app.back();
        assert!(app.pending);
        assert!(app.data.doc.is_null());
    }
    fn finish_action(app: &mut App) {
        let deadline = Instant::now() + Duration::from_secs(5);
        while app.worker.is_some() && Instant::now() < deadline {
            app.receive();
            thread::sleep(Duration::from_millis(1));
        }
        assert!(app.worker.is_none());
        app.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        press(app, 'q');
        assert_eq!(app.page, Page::Home);
    }
    #[test]
    fn successful_and_failed_writes_invalidate_the_overview() {
        for failed in [false, true] {
            let mut app = finished_home();
            app.navigate(Page::Files);
            app.pending = false;
            app.scan_complete = true;
            app.spawn_action(true, move |_, _| {
                if failed {
                    bail!("fixture partial write failure");
                }
                Ok(Outcome::Done(json!({"moved_files":1})))
            });
            finish_action(&mut app);
            assert!(app.pending);
            assert!(!app.scan_complete);
            assert!(app.data.doc.is_null());
        }
    }
    #[test]
    fn finished_actions_drop_moved_rows_without_another_walk() {
        fn finished(app: &mut App) {
            let deadline = Instant::now() + Duration::from_secs(5);
            while app.worker.is_some() && Instant::now() < deadline {
                app.receive();
                thread::sleep(Duration::from_millis(1));
            }
            assert!(app.worker.is_none());
        }
        let mut app = workspaces();
        app.spawn_action(true, |_, _| {
            Ok(Outcome::Done(
                json!({"status":"succeeded","path":"/fixture/beta"}),
            ))
        });
        finished(&mut app);
        // The whole worktree left, with the artifact it carried.
        assert!(!app.busy());
        assert!(app.scan_complete);
        assert_eq!(
            app.data.doc["projects"]
                .as_array()
                .unwrap()
                .iter()
                .filter_map(|p| p["path"].as_str())
                .collect::<Vec<_>>(),
            ["/fixture/alpha", "/fixture/alpha-other"]
        );
        assert!(
            app.data.doc["entries"]
                .as_array()
                .unwrap()
                .iter()
                .all(|e| !e["path"].as_str().unwrap().starts_with("/fixture/beta"))
        );
        assert!(app.notice.contains("沿用上次核查"));
        // Artifact counts follow the rows that remain.
        app.workspace_view = true;
        app.rebuild();
        let alpha = app
            .rows
            .iter()
            .find(|r| r["path"] == "/fixture/alpha")
            .unwrap();
        assert_eq!(alpha["artifacts"], 2);
        assert_eq!(
            app.rows
                .iter()
                .filter(|r| r["path"] == "/fixture/alpha-other")
                .count(),
            1
        );

        // A moved artifact also shortens its worktree's measured size.
        let mut app = workspaces();
        app.data.doc["entries"][0]["directory_bytes"] = json!(4096);
        app.data.doc["entries"][0]["project"] = json!("/fixture/alpha");
        app.data.doc["projects"][0]["directory_bytes"] = json!(8192);
        app.spawn_action(true, |_, _| {
            Ok(Outcome::Done(
                json!({"moved_paths":["/fixture/alpha/target"]}),
            ))
        });
        finished(&mut app);
        assert_eq!(app.data.doc["projects"][0]["directory_bytes"], 4096);
        assert_eq!(app.data.doc["projects"][0]["artifacts"], 1);
        assert!(
            app.data.doc["entries"]
                .as_array()
                .unwrap()
                .iter()
                .all(|e| e["path"] != "/fixture/alpha/target")
        );

        // Uncertain results still refresh everything.
        let mut app = workspaces();
        app.spawn_action(true, |_, _| {
            Ok(Outcome::Done(
                json!({"status":"uncertain","path":"/fixture/beta"}),
            ))
        });
        finished(&mut app);
        assert!(app.pending);
        assert!(app.data.doc.is_null());
    }
    #[test]
    fn cancelled_confirmations_and_failed_reviews_keep_the_overview() {
        let mut app = finished_home();
        let original = app.data.doc.clone();
        app.navigate(Page::Files);
        app.prompt(Prompt {
            title: "Review".into(),
            body: "Fixture".into(),
            phrase: "STAGE",
            request: Request::Stage(Page::Files, vec![]),
        });
        app.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        app.back();
        assert!(!app.busy());
        assert_eq!(app.data.doc, original);

        app.navigate(Page::Dependencies);
        app.pending = false;
        app.scan_complete = true;
        app.spawn_action(false, |_, _| bail!("fixture review failure"));
        finish_action(&mut app);
        assert!(!app.busy());
        assert_eq!(app.data.doc, original);
    }
    #[test]
    #[cfg(not(windows))]
    fn workspace_space_selects_and_cleanup_scopes_to_checked_rows() {
        let mut app = workspaces();
        press(&mut app, ' ');
        assert_eq!(app.checked.len(), 1);
        app.table.select(Some(2));
        assert_eq!(app.selected_workspaces()[0]["path"], "/fixture/alpha");
        press(&mut app, ' ');
        assert_eq!(app.checked.len(), 2);
        press(&mut app, 's');
        assert_eq!(app.checked.len(), 2);
        // Whole-directory cleanup never silently uses the cursor instead of the selection.
        press(&mut app, 'd');
        assert!(app.dialog.as_ref().unwrap().title.contains("每次核对一个"));
        assert!(app.worker.is_none());
        assert!(app.confirmation.is_none());
        app.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        press(&mut app, 't');
        assert!(!app.workspace_view);
        assert_eq!(app.workspace_filters.len(), 2);
        assert_eq!(app.rows.len(), 3);
        assert!(app.rows.iter().all(|row| {
            !row["path"]
                .as_str()
                .unwrap()
                .starts_with("/fixture/alpha-other/")
        }));
        assert!(app.checked.is_empty());
        assert!(app.worker.is_none());
        assert!(app.confirmation.is_none());
        press(&mut app, 'a');
        assert_eq!(app.checked.len(), 2); // retained artifact stays unselected
        press(&mut app, 'w');
        assert!(app.workspace_view);
        assert!(app.checked.is_empty());
        assert!(app.workspace_filters.is_empty());
        press(&mut app, 'a');
        assert_eq!(app.checked.len(), 3);
        press(&mut app, 'a');
        assert!(app.checked.is_empty());
    }
    #[test]
    fn workspace_selection_is_disabled_during_scan_and_in_readonly_mode() {
        let mut app = workspaces();
        app.pending = true;
        press(&mut app, ' ');
        assert!(app.checked.is_empty());
        assert!(app.notice.contains("扫描尚未完成"));
        press(&mut app, 'd');
        assert!(app.worker.is_none());
        assert!(app.dialog.is_none());
        app.pending = false;
        app.settings.read_only = true;
        press(&mut app, ' ');
        assert!(app.checked.is_empty());
        assert!(app.notice.contains("只读"));
        assert!(!app.actionline().contains("清理"));
    }
    #[test]
    #[cfg(not(windows))]
    fn workspace_checkboxes_and_both_cleanup_actions_are_visible() {
        let mut app = workspaces();
        press(&mut app, ' ');
        let mut terminal =
            ratatui::Terminal::new(ratatui::backend::TestBackend::new(105, 36)).unwrap();
        terminal.draw(|f| app.draw(f)).unwrap();
        let text = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect::<String>()
            .replace(' ', "");
        assert!(text.contains("☑"));
        assert!(text.contains("已选1"));
        assert!(text.contains("t清理产物"));
        assert!(text.contains("d清理整个目录"));
        assert!(text.contains("范围：已勾选1个目录"));
    }
    #[test]
    #[cfg(not(windows))]
    fn workspace_search_and_current_artifacts_do_not_carry_hidden_selections() {
        let mut app = workspaces();
        press(&mut app, ' ');
        press(&mut app, '/');
        for c in "beta".chars() {
            press(&mut app, c);
        }
        app.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.checked.is_empty());
        assert_eq!(app.rows.len(), 1);
        press(&mut app, 'p');
        assert!(app.query.is_empty());
        assert_eq!(app.rows.len(), 1);
        assert_eq!(app.rows[0]["path"], "/fixture/beta/build");
        app.back();
        assert!(app.workspace_view);
        assert_eq!(app.rows.len(), 3);
    }
}
