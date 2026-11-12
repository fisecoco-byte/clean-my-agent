//! One pass over an agent's data areas. It finds artifact directories, links,
//! repositories and project files, and on request also each project's disk
//! usage and newest source change, so no later step has to walk the same tree.
//! Names are handled as bytes; a directory that cannot be read is reported
//! without ending the walk of its siblings.
use crate::{
    deps::{AREAS, References},
    fsutil::{self, Directory, Kind},
    parallel, process,
    progress::Progress,
    project_types,
    storage::DirectoryUsage,
};
use anyhow::Result;
use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    ffi::OsString,
    fs,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
};

/// What the walk saw below one project root.
#[derive(Default, Clone)]
pub struct Observation {
    /// Everything on the root's volume except the directories in `holes`.
    pub usage: DirectoryUsage,
    pub identity: Option<(u64, u64)>,
    /// Newest regular file outside artifact, Git and trash directories.
    pub newest: i64,
    /// Entries the activity check would have visited.
    pub entries: usize,
    /// Artifact directories that were not entered.
    pub holes: Vec<PathBuf>,
}
impl Observation {
    fn absorb(&mut self, other: Observation) {
        self.usage.merge(&other.usage);
        self.identity = self.identity.or(other.identity);
        self.newest = self.newest.max(other.newest);
        self.entries += other.entries;
        self.holes.extend(other.holes);
    }
}

#[derive(Default)]
pub struct Survey {
    pub refs: References,
    /// Empty unless requested, and whenever any directory could not be read.
    pub projects: BTreeMap<PathBuf, Observation>,
}

#[derive(Clone)]
struct Member {
    root: Arc<Path>,
    dev: u64,
    /// Still inside the part of the project that counts as source activity.
    activity: bool,
}
#[derive(Clone)]
struct Scope {
    /// Looking for artifacts, repositories and project files.
    classify: bool,
    /// Inside a live checkout: every link is a possible reference.
    live: bool,
    /// May itself be a tagged cache directory.
    probe: bool,
    /// Below a repository or project file.
    owned: bool,
    /// Projects this directory is counted into.
    members: Arc<[Member]>,
}
impl Scope {
    fn area() -> Self {
        Self {
            classify: true,
            live: false,
            probe: false,
            owned: false,
            members: Arc::from([]),
        }
    }
    fn links() -> Self {
        Self {
            classify: false,
            live: true,
            probe: false,
            owned: true,
            members: Arc::from([]),
        }
    }
    fn inner(live: bool, members: Arc<[Member]>) -> Self {
        Self {
            classify: false,
            live,
            probe: false,
            owned: true,
            members,
        }
    }
}
fn quiet(members: &Arc<[Member]>) -> Arc<[Member]> {
    if members.iter().all(|m| !m.activity) {
        return members.clone();
    }
    members
        .iter()
        .map(|m| Member {
            activity: false,
            ..m.clone()
        })
        .collect()
}

#[derive(Default)]
struct Found {
    paths: BTreeSet<PathBuf>,
    links: BTreeSet<PathBuf>,
    repositories: BTreeSet<PathBuf>,
    workspaces: BTreeSet<PathBuf>,
    /// Top-most directories walked as live checkouts.
    live: BTreeSet<PathBuf>,
    errors: Vec<String>,
    projects: HashMap<Arc<Path>, Observation>,
}
impl Found {
    fn absorb(&mut self, other: Found) {
        self.paths.extend(other.paths);
        self.links.extend(other.links);
        self.repositories.extend(other.repositories);
        self.workspaces.extend(other.workspaces);
        self.live.extend(other.live);
        self.errors.extend(other.errors);
        for (root, seen) in other.projects {
            self.projects.entry(root).or_default().absorb(seen);
        }
    }
}

struct Walk<'a> {
    nested: bool,
    detail: bool,
    /// Registered checkouts outside the data areas, walked for links only.
    outside: bool,
    visited: AtomicUsize,
    progress: &'a Progress,
    phase: &'static str,
}
impl Walk<'_> {
    fn count(&self, entries: usize) -> Result<()> {
        let before = self.visited.fetch_add(entries, Ordering::Relaxed);
        if before / 2048 != (before + entries) / 2048 {
            let visited = (before + entries) / 2048 * 2048;
            self.progress.update(
                self.phase,
                None,
                None,
                if self.outside {
                    format!("已检查 {visited} 项")
                } else {
                    format!("{visited} 项")
                },
            )?;
        }
        Ok(())
    }
}
impl parallel::Walker for Walk<'_> {
    type Context = Scope;
    type Output = Found;
    fn directory(
        &self,
        out: &mut Found,
        path: &Path,
        directory: &Directory,
        scope: &Scope,
    ) -> Result<Vec<(OsString, Scope)>> {
        let own = if self.detail {
            Some(directory.info()?)
        } else {
            None
        };
        // Usage never crosses onto another volume.
        let mut members = match own {
            Some(own) if scope.members.iter().any(|m| m.dev != own.dev) => scope
                .members
                .iter()
                .filter(|m| m.dev == own.dev)
                .cloned()
                .collect(),
            _ => scope.members.clone(),
        };
        let entries = if self.detail && (scope.classify || !members.is_empty()) {
            directory.listing()?
        } else {
            directory
                .entries()?
                .into_iter()
                .map(|(name, kind)| (name, kind, None))
                .collect()
        };
        self.count(entries.len())?;
        let (mut classify, mut live) = (scope.classify, scope.live);
        if scope.probe
            && entries
                .iter()
                .any(|(name, kind, _)| name == "CACHEDIR.TAG" && *kind != Kind::Directory)
            && project_types::tagged(path)
        {
            out.paths.insert(path.into());
            for member in members.iter() {
                out.projects
                    .entry(member.root.clone())
                    .or_default()
                    .holes
                    .push(path.into());
            }
            if !live {
                return Ok(vec![]);
            }
            // A tagged cache inside a live checkout is only searched for links.
            classify = false;
            members = Arc::from([]);
        }
        let (mut repository, mut indicator) = (false, false);
        if classify {
            for (name, kind, _) in &entries {
                let name = name.to_string_lossy();
                if name == ".git" {
                    repository = true;
                } else if *kind != Kind::Directory
                    && (project_types::INDICATORS.contains(&name.as_ref())
                        || [".csproj", ".fsproj", ".vbproj"]
                            .iter()
                            .any(|x| name.ends_with(x)))
                {
                    indicator = true;
                }
            }
        }
        if repository {
            out.repositories.insert(path.into());
            if self.nested && !live && path.join(".git").exists() {
                live = true;
                out.live.insert(path.into());
            }
        }
        if indicator {
            out.workspaces.insert(path.into());
        }
        // Repositories are always projects; other project files only where no
        // enclosing directory already is one.
        if let Some(own) = own
            && (repository || (indicator && !scope.owned))
            && path.to_str().is_some()
        {
            let root: Arc<Path> = Arc::from(path);
            out.projects.entry(root.clone()).or_default().identity = Some(own.identity());
            members = members
                .iter()
                .cloned()
                .chain([Member {
                    root,
                    dev: own.dev,
                    activity: true,
                }])
                .collect();
        }
        let owned = scope.owned || repository || indicator;
        let mut usage = DirectoryUsage::default();
        let mut newest = 0;
        let mut holes = Vec::new();
        if let Some(own) = own
            && !members.is_empty()
        {
            usage.observe(own);
        }
        let mut children = Vec::new();
        for (name, kind, info) in &entries {
            let text = name.to_string_lossy();
            let git = text == ".git";
            let target = !git && project_types::TARGETS.contains(&text.as_ref());
            // Directories are counted when they are entered, or as holes.
            if let (Some(info), Some(own)) = (info, own)
                && *kind != Kind::Directory
                && !members.is_empty()
                && info.dev == own.dev
            {
                usage.observe(*info);
                if info.regular() && !git && text != ".DS_Store" {
                    newest = newest.max(info.modified_ns);
                }
            }
            match kind {
                Kind::Symlink => {
                    if classify && target {
                        out.paths.insert(path.join(name));
                    }
                    if live || (classify && target) {
                        out.links.insert(path.join(name));
                    }
                }
                Kind::Other => {
                    if classify && target {
                        out.paths.insert(path.join(name));
                    }
                }
                Kind::Directory if git => {
                    if !members.is_empty() {
                        children.push((name.clone(), Scope::inner(false, quiet(&members))));
                    }
                }
                Kind::Directory if target => {
                    if classify {
                        let below = path.join(name);
                        if text == ".build" {
                            for child in project_types::SWIFT_OUTPUTS {
                                let output = below.join(child);
                                if let Ok(metadata) = fs::symlink_metadata(&output) {
                                    if metadata.file_type().is_symlink() {
                                        out.links.insert(output.clone());
                                    }
                                    out.paths.insert(output);
                                }
                            }
                        }
                        out.paths.insert(below.clone());
                        holes.push(below);
                    } else if !members.is_empty() {
                        children.push((name.clone(), Scope::inner(false, members.clone())));
                    }
                }
                Kind::Directory if text == ".Trash" => {
                    let below = path.join(name);
                    let inside = if classify && project_types::tagged(&below) {
                        out.paths.insert(below.clone());
                        holes.push(below);
                        Arc::from([])
                    } else {
                        quiet(&members)
                    };
                    if live || !inside.is_empty() {
                        children.push((name.clone(), Scope::inner(live, inside)));
                    }
                }
                Kind::Directory if classify => children.push((
                    name.clone(),
                    Scope {
                        classify: true,
                        live,
                        probe: true,
                        owned,
                        members: if text == ".nx" {
                            quiet(&members)
                        } else {
                            members.clone()
                        },
                    },
                )),
                Kind::Directory => {
                    if live || !members.is_empty() {
                        children.push((name.clone(), Scope::inner(live, members.clone())));
                    }
                }
            }
        }
        for member in members.iter() {
            let seen = out.projects.entry(member.root.clone()).or_default();
            seen.usage.merge(&usage);
            seen.holes.extend(holes.iter().cloned());
            if member.activity {
                seen.entries += entries.len();
                seen.newest = seen.newest.max(newest);
            }
        }
        Ok(children)
    }
    fn failed(&self, out: &mut Found, path: &Path, _: &Scope, error: anyhow::Error) {
        out.errors.push(if self.outside {
            format!("子项目引用检查不完整：{}：{error}", path.display())
        } else {
            format!("无法读取目录 {}：{error}", path.display())
        });
    }
}

/// Read the reference index of `root`. `nested` also lists every link inside
/// live checkouts, including registered ones outside the data areas. `detail`
/// additionally records per-project usage and activity.
pub fn read(root: &Path, nested: bool, detail: bool, progress: &Progress) -> Result<Survey> {
    read_with(root, nested, detail, parallel::workers(), progress)
}
fn read_with(
    root: &Path,
    nested: bool,
    detail: bool,
    workers: usize,
    progress: &Progress,
) -> Result<Survey> {
    let mut refs = References::default();
    let mut projects = BTreeMap::new();
    if fsutil::unlinked_path(root).is_err() {
        refs.errors
            .push(format!("数据根目录含符号链接：{}", root.display()));
        return Ok(Survey { refs, projects });
    }
    let phase = if nested {
        "查找工作树"
    } else {
        "查找依赖目录"
    };
    progress.update(phase, None, None, "")?;
    let span = progress.span(if detail {
        "数据目录盘点（含项目占用与活跃度）"
    } else if nested {
        "引用复核：数据目录遍历"
    } else {
        "引用盘点：数据目录遍历"
    });
    let mut roots = Vec::new();
    for area in AREAS {
        let start = root.join(area);
        if !start.exists() && fs::symlink_metadata(&start).is_err() {
            continue;
        }
        if fsutil::stat(&start).map_or(true, |s| !s.directory()) {
            refs.errors
                .push(format!("无法核实符号链接目录：{}", start.display()));
            continue;
        }
        roots.push((start, Scope::area()));
    }
    let walk = Walk {
        nested,
        detail,
        outside: false,
        visited: AtomicUsize::new(0),
        progress,
        phase,
    };
    let mut found = Found::default();
    for output in parallel::walk(&walk, roots, workers, progress)? {
        found.absorb(output);
    }
    drop(span);
    found.errors.sort();
    let complete = found.errors.is_empty();
    refs.errors.append(&mut found.errors);

    let span = progress.span("Git 工作树清单");
    let mut checkouts = BTreeSet::new();
    for repo in &found.repositories {
        if checkouts.contains(repo) {
            continue;
        }
        match process::worktrees(repo, progress) {
            Ok(paths) => checkouts.extend(paths),
            Err(e) => {
                progress.check()?;
                refs.errors.push(e.to_string());
            }
        }
    }
    drop(span);
    let mut links = std::mem::take(&mut found.links);
    if nested {
        let _span = progress.span("引用复核：关联工作树遍历");
        let live: BTreeSet<_> = found.repositories.union(&checkouts).cloned().collect();
        progress.update("复核工作树引用", Some(0), Some(live.len()), "")?;
        // The area walk already listed every link below these.
        let mut covered = std::mem::take(&mut found.live);
        let mut roots = Vec::new();
        for repo in &live {
            if !repo.join(".git").exists() {
                continue;
            }
            let canonical = match fsutil::resolve(repo) {
                Ok(p) => p,
                Err(e) => {
                    refs.errors.push(e.to_string());
                    continue;
                }
            };
            if canonical == Path::new("/") || crate::policy::home().is_ok_and(|h| h == canonical) {
                refs.errors
                    .push(format!("工作树范围过大，无法核实引用：{}", repo.display()));
                continue;
            }
            // The walk of an enclosing checkout lists every link here, unless it
            // skips a directory on the way down.
            if canonical.ancestors().any(|outer| {
                covered.contains(outer)
                    && canonical.strip_prefix(outer).is_ok_and(|inner| {
                        !inner.iter().any(|name| {
                            name == ".git"
                                || name
                                    .to_str()
                                    .is_none_or(|n| project_types::TARGETS.contains(&n))
                        })
                    })
            }) {
                continue;
            }
            covered.insert(canonical.clone());
            roots.push((canonical, Scope::links()));
        }
        let walk = Walk {
            nested: false,
            detail: false,
            outside: true,
            visited: AtomicUsize::new(0),
            progress,
            phase: "复核工作树引用",
        };
        let mut outside = Found::default();
        for output in parallel::walk(&walk, roots, workers, progress)? {
            outside.absorb(output);
        }
        outside.errors.sort();
        refs.errors.append(&mut outside.errors);
        links.append(&mut outside.links);
        if !live.is_empty() {
            progress.update("复核工作树引用", Some(live.len()), Some(live.len()), "")?;
        }
    }

    let span = progress.span("链接解析");
    progress.update("核对依赖链接", None, None, "")?;
    // Git reports these by path; the walk never saw them.
    let mut probed = BTreeSet::new();
    for checkout in &checkouts {
        for name in project_types::TARGETS {
            let source = checkout.join(name);
            if links.contains(&source) {
                continue;
            }
            match fsutil::stat(&source) {
                Ok(s) if s.symlink() => {
                    probed.insert(source);
                }
                Ok(_) => {}
                Err(e) if fsutil::missing(&e) => {}
                Err(e) => refs
                    .errors
                    .push(format!("无法核实引用 {}：{e}", source.display())),
            }
        }
    }
    // Links share most ancestors; look each one up once.
    let mut identities = HashMap::<PathBuf, Option<(u64, u64)>>::new();
    for (index, source) in links.iter().chain(&probed).enumerate() {
        if index % 256 == 0 {
            progress.check()?;
        }
        let route = fsutil::link_route(source);
        // A route whose first link is the source itself proves it still is one.
        // Anything else is checked the slow way: the entry may have changed
        // since the walk listed it.
        if !probed.contains(source)
            && !route
                .as_ref()
                .is_ok_and(|(_, hops)| hops.first() == Some(source))
        {
            match fsutil::stat(source) {
                Ok(s) if s.symlink() => {}
                Ok(_) => continue,
                Err(e) if fsutil::missing(&e) => continue,
                Err(e) => {
                    refs.errors
                        .push(format!("无法核实引用 {}：{e}", source.display()));
                    continue;
                }
            }
        }
        let (target, hops) = match route {
            Ok(route) => route,
            Err(e) => {
                refs.errors.push(e.to_string());
                continue;
            }
        };
        refs.targets.insert(source.clone(), target.clone());
        for hop in std::iter::once(target).chain(hops.into_iter().filter(|p| p != source)) {
            for ancestor in hop.ancestors() {
                refs.by_path
                    .entry(ancestor.into())
                    .or_default()
                    .insert(source.clone());
                let identity = match identities.get(ancestor) {
                    Some(known) => *known,
                    None => {
                        let found = fsutil::stat(ancestor).ok().map(|s| s.identity());
                        identities.insert(ancestor.into(), found);
                        found
                    }
                };
                if let Some(identity) = identity {
                    refs.by_identity
                        .entry(identity)
                        .or_default()
                        .insert(source.clone());
                }
            }
        }
    }
    drop(span);

    // Reports are JSON text: a path that is not UTF-8 is named in a note and never
    // offered for cleanup. Links from and to such paths are still indexed above.
    let mut listed = |found: BTreeSet<PathBuf>| -> BTreeSet<PathBuf> {
        let (kept, hidden): (BTreeSet<_>, BTreeSet<_>) =
            found.into_iter().partition(|p| p.to_str().is_some());
        for path in hidden {
            let note = format!("路径编码无法安全写入报告，未列出：{}", path.display());
            if !refs.unlisted.contains(&note) {
                refs.unlisted.push(note);
            }
        }
        kept
    };
    let paths = listed(found.paths);
    let repositories = listed(found.repositories);
    let workspaces = listed(found.workspaces);
    (refs.paths, refs.repositories, refs.workspaces) = (paths, repositories, workspaces);
    if detail && complete {
        for (root, seen) in found.projects {
            if seen.identity.is_some() {
                projects.insert(root.to_path_buf(), seen);
            }
        }
    }
    Ok(Survey { refs, projects })
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use crate::{activity, storage};
    use serde_json::Value;
    use std::{
        os::unix::fs::{PermissionsExt, symlink},
        process::Command,
    };

    fn git(repo: &Path, args: &[&str]) {
        let output = Command::new("git")
            .arg("-C")
            .arg(repo)
            .args(["-c", "user.name=fixture", "-c", "commit.gpgsign=false"])
            .args(["-c", "user.email=fixture@example.invalid"])
            .args(args)
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    fn write(path: PathBuf, bytes: usize) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, vec![b'x'; bytes]).unwrap();
    }
    /// A repository with a registered checkout outside the data areas, a nested
    /// project, a plain project and links between them.
    fn fixture(home: &Path) -> PathBuf {
        let root = home.join("agent");
        let app = root.join("worktrees/app");
        write(app.join("Cargo.toml"), 40);
        write(app.join(".gitignore"), 0);
        write(app.join("src/main.rs"), 9000);
        write(app.join("web/package.json"), 2);
        git(&app, &["init", "-q"]);
        git(&app, &["add", "."]);
        git(&app, &["commit", "-qm", "fixture"]);
        write(app.join("target/debug/binary"), 70_000);
        write(app.join("web/node_modules/pkg/index.js"), 5000);
        write(app.join(".nx/cache/entry"), 3000);
        for n in 0..40 {
            write(app.join(format!("src/m{}/f{n}.rs", n % 7)), 100);
        }
        // One file in the source tree and in an artifact.
        fs::hard_link(app.join("src/main.rs"), app.join("target/debug/shared")).unwrap();
        let lib = root.join("worktrees/lib");
        write(lib.join("package.json"), 2);
        write(lib.join("node_modules/dep/index.js"), 4000);
        write(lib.join("docs/pyproject.toml"), 2);
        write(lib.join("docs/.venv/lib/site.py"), 800);
        symlink(app.join("web/node_modules"), lib.join("docs/node_modules")).unwrap();
        let outside = home.join("outside");
        git(
            &app,
            &[
                "worktree",
                "add",
                "-q",
                "--detach",
                outside.to_str().unwrap(),
            ],
        );
        symlink(app.join("target"), outside.join("shared-output")).unwrap();
        root
    }
    fn names(root: &Path, paths: &BTreeSet<PathBuf>) -> Vec<String> {
        paths
            .iter()
            .map(|p| fsutil::text(p.strip_prefix(root).unwrap()))
            .collect()
    }
    type Summary = Vec<(PathBuf, u64, Option<(u64, u64)>, i64, usize, Vec<PathBuf>)>;
    fn summary(found: &Survey) -> Summary {
        found
            .projects
            .iter()
            .map(|(path, seen)| {
                let mut holes = seen.holes.clone();
                holes.sort();
                (
                    path.clone(),
                    seen.usage.total(),
                    seen.identity,
                    seen.newest,
                    seen.entries,
                    holes,
                )
            })
            .collect()
    }

    #[test]
    fn one_walk_answers_like_separate_walks_on_any_worker_count() {
        let temp = tempfile::tempdir().unwrap();
        let home = fs::canonicalize(temp.path()).unwrap();
        let root = fixture(&home);
        let progress = Progress::default();
        let (app, lib) = (root.join("worktrees/app"), root.join("worktrees/lib"));
        for nested in [false, true] {
            let found = read_with(&root, nested, true, 1, &progress).unwrap();
            let refs = &found.refs;
            assert_eq!(refs.errors, Vec::<String>::new());
            assert_eq!(
                names(&root, &refs.paths),
                [
                    "worktrees/app/target",
                    "worktrees/app/web/node_modules",
                    "worktrees/lib/docs/.venv",
                    "worktrees/lib/docs/node_modules",
                    "worktrees/lib/node_modules",
                ]
            );
            assert_eq!(names(&root, &refs.repositories), ["worktrees/app"]);
            assert_eq!(
                names(&root, &refs.workspaces),
                [
                    "worktrees/app",
                    "worktrees/app/web",
                    "worktrees/lib",
                    "worktrees/lib/docs"
                ]
            );
            assert_eq!(
                refs.incoming(&app.join("web/node_modules")),
                [lib.join("docs/node_modules")]
            );
            // Only the review walks the checkout registered outside the data areas.
            assert_eq!(
                refs.incoming(&app.join("target")),
                if nested {
                    vec![home.join("outside/shared-output")]
                } else {
                    vec![]
                }
            );
            assert_eq!(refs.guard(&app.join("target")).is_err(), nested);
            assert_eq!(
                found.projects.keys().collect::<Vec<_>>(),
                [&app, &lib],
                "nested projects belong to the enclosing one"
            );
            for workers in [2, 4, 8] {
                let other = read_with(&root, nested, true, workers, &progress).unwrap();
                assert_eq!(other.refs.paths, refs.paths);
                assert_eq!(other.refs.targets, refs.targets);
                assert_eq!(other.refs.errors, refs.errors);
                assert_eq!(other.refs.repositories, refs.repositories);
                assert_eq!(other.refs.workspaces, refs.workspaces);
                assert_eq!(other.refs.by_path, refs.by_path);
                assert_eq!(other.refs.by_identity, refs.by_identity);
                assert_eq!(summary(&other), summary(&found));
            }
            // The index alone is the same with or without the project details.
            let plain = read_with(&root, nested, false, 4, &progress).unwrap();
            assert!(plain.projects.is_empty());
            assert_eq!(plain.refs.paths, refs.paths);
            assert_eq!(plain.refs.targets, refs.targets);
            assert_eq!(plain.refs.by_path, refs.by_path);
        }
    }

    #[test]
    fn observations_match_the_dedicated_usage_and_activity_walks() {
        let temp = tempfile::tempdir().unwrap();
        let home = fs::canonicalize(temp.path()).unwrap();
        let root = fixture(&home);
        let progress = Progress::default();
        let found = read_with(&root, false, true, 4, &progress).unwrap();
        // Artifacts measured before the project totals, as a full report does.
        let mut measured = BTreeMap::new();
        for path in &found.refs.paths {
            let info = fsutil::stat(path).unwrap();
            if !info.directory() {
                continue;
            }
            let mut usage = DirectoryUsage::default();
            usage.observe(info);
            storage::measure(path, &progress, &mut |_, s| {
                usage.observe(s);
                Ok(())
            })
            .unwrap();
            measured.insert(path.clone(), (info.stamp(), usage));
        }
        let now = fsutil::now_ns();
        for (path, seen) in &found.projects {
            for known in [&BTreeMap::new(), &measured] {
                let expected = storage::workspace_usage(path, known, &progress).unwrap();
                assert!(expected > 0);
                assert_eq!(
                    storage::surveyed_usage(path, seen, known, &progress).unwrap(),
                    expected,
                    "{}",
                    path.display()
                );
            }
            for limit in [50000, 3] {
                let mut walked =
                    activity::workspace(path, &[], &[], now, None, &progress, limit, None).unwrap();
                let mut reused =
                    activity::workspace(path, &[], &[], now, None, &progress, limit, Some(seen))
                        .unwrap();
                assert_eq!(walked["warning"].is_null(), limit > 3);
                if limit == 3 {
                    // Past the limit both are incomplete; the walk stopped early and
                    // saw fewer files.
                    assert!(reused["latest_ns"].as_i64() >= walked["latest_ns"].as_i64());
                    for key in ["latest_ns", "basis"] {
                        walked[key] = Value::Null;
                        reused[key] = Value::Null;
                    }
                }
                assert_eq!(reused, walked);
            }
        }
        // An observation never describes a directory that replaced the one seen.
        let lib = root.join("worktrees/lib");
        let seen = &found.projects[&lib];
        fs::rename(&lib, root.join("worktrees/old")).unwrap();
        write(lib.join("package.json"), 2);
        assert!(storage::surveyed_usage(&lib, seen, &measured, &progress).is_err());
        assert_eq!(
            activity::workspace(&lib, &[], &[], now, None, &progress, 50000, Some(seen)).unwrap(),
            activity::workspace(&lib, &[], &[], now, None, &progress, 50000, None).unwrap()
        );
    }

    #[test]
    fn an_unreadable_directory_does_not_hide_its_siblings() {
        let temp = tempfile::tempdir().unwrap();
        let home = fs::canonicalize(temp.path()).unwrap();
        let root = fixture(&home);
        let locked = root.join("worktrees/lib/private");
        write(locked.join("node_modules/x"), 1);
        fs::set_permissions(&locked, fs::Permissions::from_mode(0o000)).unwrap();
        if fs::read_dir(&locked).is_ok() {
            return; // Running with privileges that ignore permissions.
        }
        let progress = Progress::default();
        for workers in [1, 4] {
            let found = read_with(&root, true, true, workers, &progress).unwrap();
            assert_eq!(found.refs.errors.len(), 1, "{:?}", found.refs.errors);
            assert!(found.refs.errors[0].contains("worktrees/lib/private"));
            assert_eq!(found.refs.paths.len(), 5);
            assert_eq!(
                found.refs.incoming(&root.join("worktrees/app/target")),
                [home.join("outside/shared-output")]
            );
            // An incomplete inventory keeps every target and offers no shortcuts.
            assert!(
                found
                    .refs
                    .guard(&root.join("worktrees/app/target"))
                    .is_err()
            );
            assert!(
                found
                    .refs
                    .guard(&root.join("worktrees/lib/node_modules"))
                    .is_err()
            );
            assert!(found.projects.is_empty());
        }
        fs::set_permissions(&locked, fs::Permissions::from_mode(0o755)).unwrap();
    }

    #[test]
    fn names_that_are_not_text_are_indexed_but_not_listed() {
        use std::os::unix::ffi::OsStrExt;
        let temp = tempfile::tempdir().unwrap();
        let home = fs::canonicalize(temp.path()).unwrap();
        let root = fixture(&home);
        let app = root.join("worktrees/app");
        let odd = app.join(std::ffi::OsStr::from_bytes(b"caf\xe9"));
        if fs::create_dir(&odd).is_err() {
            return; // The filesystem only stores valid UTF-8 names (APFS).
        }
        write(odd.join("node_modules/x"), 1);
        symlink(app.join("target"), odd.join("link")).unwrap();
        let progress = Progress::default();
        for workers in [1, 4] {
            let found = read_with(&root, true, true, workers, &progress).unwrap();
            assert_eq!(found.refs.errors, Vec::<String>::new());
            assert_eq!(found.refs.paths.len(), 5);
            assert_eq!(found.refs.unlisted.len(), 1, "{:?}", found.refs.unlisted);
            assert!(
                found
                    .refs
                    .incoming(&app.join("target"))
                    .contains(&odd.join("link"))
            );
            assert!(
                serde_json::to_string(&fsutil::texts(&found.refs.incoming(&app.join("target"))))
                    .is_ok()
            );
        }
    }
}
