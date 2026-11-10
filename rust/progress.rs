use anyhow::{Result, bail};
use serde::{Deserialize, Serialize};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
    mpsc::Sender,
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
}

impl Progress {
    pub fn new(sender: Sender<Update>) -> Self {
        Self {
            sender: Some(sender),
            ..Self::default()
        }
    }
    pub fn cancel(&self) {
        self.cancelled.store(true, Ordering::Release);
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
                phase: phase.into(),
                done,
                total,
                detail: detail.into(),
            });
        }
        Ok(())
    }
}
