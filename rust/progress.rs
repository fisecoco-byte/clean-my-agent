use anyhow::{Result, bail};
use serde::{Deserialize, Serialize};
use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc::Sender,
    },
    time::{Duration, Instant},
};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Update {
    pub phase: String,
    pub done: Option<usize>,
    pub total: Option<usize>,
    pub detail: String,
}

#[derive(Clone, Default)]
pub struct Progress {
    cancelled: Arc<AtomicBool>,
    sender: Option<Sender<Update>>,
    context: String,
    timings: Option<Timings>,
}
type Timings = Arc<Mutex<Vec<(String, Duration, usize)>>>;

/// Adds its lifetime to a named stage; inert unless the scan was started with `timed`.
pub struct Span(Option<(Timings, String, Instant)>);
impl Drop for Span {
    fn drop(&mut self) {
        if let Some((timings, name, started)) = self.0.take()
            && let Ok(mut rows) = timings.lock()
        {
            let elapsed = started.elapsed();
            match rows.iter_mut().find(|r| r.0 == name) {
                Some(row) => {
                    row.1 += elapsed;
                    row.2 += 1;
                }
                None => rows.push((name, elapsed, 1)),
            }
        }
    }
}

impl Progress {
    pub fn new(sender: Sender<Update>) -> Self {
        Self {
            sender: Some(sender),
            ..Self::default()
        }
    }
    pub fn timed(mut self) -> Self {
        self.timings = Some(Timings::default());
        self
    }
    pub fn span(&self, name: &str) -> Span {
        Span(
            self.timings
                .as_ref()
                .map(|t| (t.clone(), name.into(), Instant::now())),
        )
    }
    /// Stages in first-seen order: name, total time, number of spans.
    pub fn timings(&self) -> Vec<(String, Duration, usize)> {
        self.timings
            .as_ref()
            .and_then(|t| t.lock().ok().map(|rows| rows.clone()))
            .unwrap_or_default()
    }
    pub fn cancel(&self) {
        self.cancelled.store(true, Ordering::Release);
    }
    pub fn context(&self, context: impl Into<String>) -> Self {
        Self {
            context: context.into(),
            ..self.clone()
        }
    }
    pub fn check(&self) -> Result<()> {
        if self.cancelled.load(Ordering::Acquire) {
            bail!("操作已取消");
        }
        Ok(())
    }
    pub fn update(
        &self,
        phase: &str,
        done: Option<usize>,
        total: Option<usize>,
        detail: impl Into<String>,
    ) -> Result<()> {
        self.check()?;
        if let Some(sender) = &self.sender {
            let _ = sender.send(Update {
                phase: if self.context.is_empty() {
                    phase.into()
                } else {
                    format!("{} · {phase}", self.context)
                },
                done,
                total,
                detail: detail.into(),
            });
        }
        Ok(())
    }
}
