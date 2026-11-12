//! Bounded worker threads for read-only scans. Workers only read; every result
//! is merged by the caller, so the outcome does not depend on scheduling.
use crate::{fsutil::Directory, progress::Progress};
use anyhow::{Result, anyhow};
use std::{
    ffi::OsString,
    path::{Path, PathBuf},
    sync::{
        Condvar, Mutex, MutexGuard, PoisonError,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
};

/// Scan threads: `CLEAN_MY_AGENT_SCAN_WORKERS` (1–8), otherwise 4 on macOS where
/// it was measured, and 1 elsewhere.
pub fn workers() -> usize {
    match std::env::var("CLEAN_MY_AGENT_SCAN_WORKERS")
        .ok()
        .and_then(|v| v.trim().parse::<usize>().ok())
    {
        Some(n) => n.clamp(1, 8),
        None if cfg!(target_os = "macos") => 4,
        None => 1,
    }
}

/// Each walking thread keeps one descriptor per directory level open.
fn raise_descriptor_limit() {
    #[cfg(unix)]
    {
        static ONCE: std::sync::Once = std::sync::Once::new();
        ONCE.call_once(|| unsafe {
            let mut limit = std::mem::zeroed::<libc::rlimit>();
            if libc::getrlimit(libc::RLIMIT_NOFILE, &mut limit) == 0 && limit.rlim_cur < 4096 {
                limit.rlim_cur = limit.rlim_max.min(4096);
                libc::setrlimit(libc::RLIMIT_NOFILE, &limit);
            }
        });
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// `items.map(f)` in input order on up to `workers` threads.
pub fn map<T: Send, R: Send>(
    items: Vec<T>,
    workers: usize,
    f: impl Fn(usize, T) -> R + Sync,
) -> Vec<R> {
    if workers <= 1 || items.len() <= 1 {
        return items
            .into_iter()
            .enumerate()
            .map(|(index, item)| f(index, item))
            .collect();
    }
    raise_descriptor_limit();
    let items: Vec<_> = items.into_iter().map(|i| Mutex::new(Some(i))).collect();
    let results: Vec<_> = items.iter().map(|_| Mutex::new(None)).collect();
    let next = AtomicUsize::new(0);
    std::thread::scope(|scope| {
        for _ in 0..workers.min(items.len()) {
            scope.spawn(|| {
                loop {
                    let index = next.fetch_add(1, Ordering::Relaxed);
                    let Some(item) = items.get(index).and_then(|i| lock(i).take()) else {
                        break;
                    };
                    let result = f(index, item);
                    *lock(&results[index]) = Some(result);
                }
            });
        }
    });
    results
        .into_iter()
        .map(|r| {
            r.into_inner()
                .unwrap_or_else(PoisonError::into_inner)
                .expect("worker finished")
        })
        .collect()
}

/// What a tree walk does in each directory. Workers call it concurrently, each
/// with its own `Output`; the caller merges the outputs afterwards.
pub trait Walker: Sync {
    type Context: Clone + Send;
    type Output: Default + Send;
    /// Inspect an opened directory and name the subdirectories to enter.
    fn directory(
        &self,
        output: &mut Self::Output,
        path: &Path,
        directory: &Directory,
        context: &Self::Context,
    ) -> Result<Vec<(OsString, Self::Context)>>;
    /// A directory could not be opened or read. Its siblings are still walked.
    fn failed(
        &self,
        output: &mut Self::Output,
        path: &Path,
        context: &Self::Context,
        error: anyhow::Error,
    );
}

struct Queue<C> {
    jobs: Vec<(PathBuf, C, usize)>,
    active: usize,
}
struct Shared<C> {
    queue: Mutex<Queue<C>>,
    ready: Condvar,
    idle: AtomicUsize,
    stop: AtomicBool,
    workers: usize,
}
/// Marks a job finished even when the walker panics, so waiting workers wake up.
struct Active<'a, C>(&'a Shared<C>);
impl<C> Drop for Active<'_, C> {
    fn drop(&mut self) {
        lock(&self.0.queue).active -= 1;
        self.0.ready.notify_all();
    }
}

#[allow(clippy::too_many_arguments)] // Recursion state; bundling it would only rename it.
fn descend<W: Walker>(
    walker: &W,
    shared: &Shared<W::Context>,
    progress: &Progress,
    output: &mut W::Output,
    path: &Path,
    directory: Directory,
    context: &W::Context,
    depth: usize,
) {
    if shared.stop.load(Ordering::Relaxed) {
        return;
    }
    if progress.check().is_err() {
        shared.stop.store(true, Ordering::Relaxed);
        shared.ready.notify_all();
        return;
    }
    if depth > 512 {
        walker.failed(output, path, context, anyhow!("目录层级过深"));
        return;
    }
    let children = match walker.directory(output, path, &directory, context) {
        Ok(children) => children,
        Err(error) => {
            walker.failed(output, path, context, error);
            return;
        }
    };
    for (name, child) in children {
        let below = path.join(&name);
        // Hand a subtree to an idle worker. It reopens the directory by path, so
        // only paths `Directory::open` accepts are given away.
        if shared.workers > 1 && shared.idle.load(Ordering::Relaxed) > 0 && below.to_str().is_some()
        {
            let mut queue = lock(&shared.queue);
            if queue.jobs.len() < shared.workers {
                queue.jobs.push((below, child, depth + 1));
                drop(queue);
                shared.ready.notify_one();
                continue;
            }
        }
        match directory.child(&name) {
            Ok(opened) => descend(
                walker,
                shared,
                progress,
                output,
                &below,
                opened,
                &child,
                depth + 1,
            ),
            Err(error) => walker.failed(output, &below, &child, error),
        }
    }
}

fn work<W: Walker>(walker: &W, shared: &Shared<W::Context>, progress: &Progress) -> W::Output {
    let mut output = W::Output::default();
    loop {
        let job = {
            let mut queue = lock(&shared.queue);
            loop {
                if shared.stop.load(Ordering::Relaxed) {
                    break None;
                }
                if let Some(job) = queue.jobs.pop() {
                    queue.active += 1;
                    break Some(job);
                }
                if queue.active == 0 {
                    break None;
                }
                shared.idle.fetch_add(1, Ordering::Relaxed);
                queue = shared
                    .ready
                    .wait(queue)
                    .unwrap_or_else(PoisonError::into_inner);
                shared.idle.fetch_sub(1, Ordering::Relaxed);
            }
        };
        let Some((path, context, depth)) = job else {
            shared.ready.notify_all();
            return output;
        };
        let _active = Active(shared);
        match Directory::open(&path) {
            Ok(directory) => descend(
                walker,
                shared,
                progress,
                &mut output,
                &path,
                directory,
                &context,
                depth,
            ),
            Err(error) => walker.failed(&mut output, &path, &context, error),
        }
    }
}

/// Walk the trees below `roots` without following links. Returns one output per
/// worker. A failing directory is reported to the walker and does not stop its
/// siblings; only cancellation ends the walk early.
pub fn walk<W: Walker>(
    walker: &W,
    roots: Vec<(PathBuf, W::Context)>,
    workers: usize,
    progress: &Progress,
) -> Result<Vec<W::Output>> {
    progress.check()?;
    let workers = workers.clamp(1, 8);
    let mut jobs: Vec<_> = roots.into_iter().map(|(p, c)| (p, c, 0)).collect();
    jobs.reverse();
    let shared = Shared {
        queue: Mutex::new(Queue { jobs, active: 0 }),
        ready: Condvar::new(),
        idle: AtomicUsize::new(0),
        stop: AtomicBool::new(false),
        workers,
    };
    let outputs = if workers == 1 {
        vec![work(walker, &shared, progress)]
    } else {
        raise_descriptor_limit();
        std::thread::scope(|scope| {
            let handles: Vec<_> = (0..workers)
                .map(|_| {
                    std::thread::Builder::new()
                        .stack_size(8 << 20)
                        .spawn_scoped(scope, || work(walker, &shared, progress))
                })
                .collect();
            handles
                .into_iter()
                .map(|handle| match handle?.join() {
                    Ok(output) => Ok(output),
                    Err(panic) => std::panic::resume_unwind(panic),
                })
                .collect::<Result<Vec<_>>>()
        })?
    };
    progress.check()?;
    Ok(outputs)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use crate::fsutil::Kind;
    use std::{collections::BTreeSet, fs};

    #[derive(Default)]
    struct Names {
        fail: Option<&'static str>,
    }
    impl Walker for Names {
        type Context = usize;
        type Output = (BTreeSet<(PathBuf, usize)>, Vec<String>);
        fn directory(
            &self,
            output: &mut Self::Output,
            path: &Path,
            directory: &Directory,
            depth: &usize,
        ) -> Result<Vec<(OsString, usize)>> {
            if self.fail.is_some_and(|name| path.ends_with(name)) {
                anyhow::bail!("refused");
            }
            let mut children = Vec::new();
            for (name, kind) in directory.entries()? {
                output.0.insert((path.join(&name), *depth));
                if kind == Kind::Directory {
                    children.push((name, depth + 1));
                }
            }
            Ok(children)
        }
        fn failed(&self, output: &mut Self::Output, path: &Path, _: &usize, error: anyhow::Error) {
            output.1.push(format!("{}: {error}", path.display()));
        }
    }
    fn merged(outputs: Vec<<Names as Walker>::Output>) -> (usize, Vec<String>) {
        let mut names = BTreeSet::new();
        let mut errors = Vec::new();
        for (found, failed) in outputs {
            let before = names.len() + found.len();
            names.extend(found);
            // Every directory is visited by exactly one worker.
            assert_eq!(names.len(), before);
            errors.extend(failed);
        }
        errors.sort();
        (names.len(), errors)
    }
    #[test]
    fn every_worker_count_visits_the_same_tree_once() {
        let tmp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(tmp.path()).unwrap();
        for a in 0..12 {
            for b in 0..12 {
                let dir = root.join(format!("a{a}/b{b}/c/d"));
                fs::create_dir_all(&dir).unwrap();
                fs::write(dir.join("file"), "x").unwrap();
            }
        }
        std::os::unix::fs::symlink("a0", root.join("link")).unwrap();
        let progress = Progress::default();
        let walker = Names::default();
        let expected = 12 + 144 * 4 + 1;
        for workers in [1, 2, 4, 8] {
            let outputs = walk(&walker, vec![(root.clone(), 0)], workers, &progress).unwrap();
            assert_eq!(outputs.len(), workers);
            assert_eq!(merged(outputs), (expected, vec![]));
        }
        // A failing directory loses only its own subtree.
        let walker = Names { fail: Some("a3") };
        for workers in [1, 4] {
            let outputs = walk(
                &walker,
                vec![(root.clone(), 0), (root.join("missing"), 0)],
                workers,
                &progress,
            )
            .unwrap();
            let (count, errors) = merged(outputs);
            assert_eq!(count, expected - 12 * 4);
            assert_eq!(errors.len(), 2);
            assert!(errors[0].ends_with("a3: refused"));
        }
        progress.cancel();
        for workers in [1, 4] {
            assert!(walk(&walker, vec![(root.clone(), 0)], workers, &progress).is_err());
        }
    }
    #[test]
    fn map_keeps_input_order() {
        for workers in [1, 3, 8] {
            let items: Vec<usize> = (0..100).collect();
            assert_eq!(
                map(items, workers, |index, item| (index, item * 2)),
                (0..100).map(|n| (n, n * 2)).collect::<Vec<_>>()
            );
        }
        assert!(map(Vec::<u8>::new(), 4, |_, n| n).is_empty());
        assert!((1..=8).contains(&workers()));
    }
}
