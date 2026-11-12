use crate::{fsutil, policy::Policy, process, progress::Progress, project_types};
use anyhow::{Context, Result, bail};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::Path,
    path::PathBuf,
};
pub const AREAS: &[&str] = &["worktrees", ".chatgpt-projects", "projects"];
pub const LOCKS: &[&str] = &[
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
];
pub fn valid_location(root: &Path, path: &Path) -> bool {
    valid_location_tagged(root, path, false)
}
pub fn valid_location_tagged(root: &Path, path: &Path, tagged: bool) -> bool {
    path.is_absolute()
        && path != root
        && path.starts_with(root)
        && fsutil::absolute(path).is_ok()
        && path
            .strip_prefix(root)
            .ok()
            .and_then(|p| p.iter().next())
            .is_some_and(|p| AREAS.contains(&p.to_string_lossy().as_ref()))
        && (project_types::target(path) || tagged)
}
/// Repository-relative path as Git prints it, or `None` when only Git itself can
/// interpret it: pathspec globs or magic, and non-ASCII names that macOS Git may
/// normalise on the command line. Those keep one query per path.
fn plain(relative: &Path) -> Option<Vec<u8>> {
    if cfg!(windows) {
        return None;
    }
    let mut bytes = Vec::new();
    for part in relative.components() {
        let std::path::Component::Normal(name) = part else {
            return None;
        };
        if !bytes.is_empty() {
            bytes.push(b'/');
        }
        bytes.extend(name.to_str()?.as_bytes());
    }
    (bytes.is_ascii()
        && !bytes.starts_with(b":")
        && !bytes
            .iter()
            .any(|c| c.is_ascii_control() || b"*?[\\".contains(c)))
    .then_some(bytes)
}
/// One `git ls-files -z` of a repository, sorted for prefix lookups.
struct Tracked {
    bytes: Vec<u8>,
    entries: Vec<(usize, usize)>,
}
impl Tracked {
    fn new(bytes: Vec<u8>) -> Self {
        let mut entries = Vec::new();
        let mut start = 0;
        for (index, byte) in bytes.iter().enumerate() {
            if *byte == 0 {
                if index > start {
                    entries.push((start, index));
                }
                start = index + 1;
            }
        }
        entries.sort_by(|a, b| bytes[a.0..a.1].cmp(&bytes[b.0..b.1]));
        Self { bytes, entries }
    }
    fn first(&self, key: &[u8]) -> Option<&[u8]> {
        let index = self
            .entries
            .partition_point(|e| &self.bytes[e.0..e.1] < key);
        self.entries.get(index).map(|e| &self.bytes[e.0..e.1])
    }
    fn file(&self, name: &[u8]) -> bool {
        self.first(name) == Some(name)
    }
    /// Same answer as the literal pathspec `name`: that entry, or anything below it.
    fn matches(&self, name: &[u8]) -> bool {
        let mut below = name.to_vec();
        below.push(b'/');
        self.file(name) || self.first(&below).is_some_and(|e| e.starts_with(&below))
    }
}
enum Index {
    Files(Tracked),
    /// Git ran and refused; holds its error output.
    Refused(String),
    /// Git could not be run to completion.
    Failed(String),
}
/// Answer to one batched ignore query: the names asked and those Git ignores.
struct Ignored {
    asked: BTreeSet<Vec<u8>>,
    ignored: BTreeSet<Vec<u8>>,
}
/// Git evidence shared by the artifacts of one scan: the owning repository of each
/// directory, one index listing and one ignore query per repository. Create a new
/// one for every report so that a re-check before moving never sees earlier answers.
#[derive(Default)]
pub struct Git {
    roots: BTreeMap<PathBuf, Result<PathBuf, String>>,
    projects: BTreeMap<PathBuf, PathBuf>,
    indexes: Vec<(PathBuf, Index)>,
    planned: Vec<PathBuf>,
    ignored: BTreeMap<PathBuf, Option<Ignored>>,
}
impl Git {
    /// Artifacts this scan will verify, so each repository is asked once which of
    /// them it ignores. Unplanned paths are still answered, one query each.
    pub fn plan(&mut self, paths: impl IntoIterator<Item = PathBuf>) {
        self.planned.extend(paths);
        self.ignored.clear();
    }
    /// `git -C directory rev-parse --show-toplevel`. Git searches upward from the
    /// directory, so one without its own repository marker, on the same device as
    /// its parent, belongs to whatever its parent belongs to.
    fn toplevel(&mut self, directory: &Path, progress: &Progress) -> Result<PathBuf, String> {
        if let Some(known) = self.roots.get(directory) {
            return known.clone();
        }
        let inherited = match directory.parent() {
            Some(parent) if !Self::boundary(directory, parent) => {
                Some(self.toplevel(parent, progress))
            }
            _ => None,
        };
        let found = inherited.unwrap_or_else(|| {
            process::git_path(directory, "--show-toplevel", progress).map_err(|e| format!("{e:#}"))
        });
        self.roots.insert(directory.into(), found.clone());
        found
    }
    /// Whether Git's upward search can end at `directory` itself.
    fn boundary(directory: &Path, parent: &Path) -> bool {
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            !fs::symlink_metadata(directory).is_ok_and(|m| m.is_dir())
                || fs::symlink_metadata(directory.join(".git")).is_ok()
                || fs::symlink_metadata(directory.join("HEAD")).is_ok()
                || !matches!(
                    (fs::metadata(directory), fs::metadata(parent)),
                    (Ok(a), Ok(b)) if a.dev() == b.dev()
                )
        }
        #[cfg(not(unix))]
        {
            let _ = (directory, parent);
            true
        }
    }
    fn index(&mut self, repo: &Path, progress: &Progress) -> &Index {
        match self.indexes.iter().position(|(r, _)| r == repo) {
            Some(index) => {
                let recent = self.indexes.remove(index);
                self.indexes.insert(0, recent);
            }
            None => {
                let listing = match process::git(repo, ["ls-files", "-z"], progress) {
                    Ok(o) if o.status.success() => Index::Files(Tracked::new(o.stdout)),
                    Ok(o) => Index::Refused(String::from_utf8_lossy(&o.stderr).into_owned()),
                    Err(e) => Index::Failed(e.to_string()),
                };
                // Large indexes take megabytes each; artifacts arrive grouped by
                // repository, so a few recent listings are enough.
                self.indexes.truncate(7);
                self.indexes.insert(0, (repo.into(), listing));
            }
        }
        &self.indexes[0].1
    }
    /// Entries matched by the literal pathspec `relative`; `None` when the path
    /// needs Git's own pathspec handling.
    fn tracked(
        &mut self,
        repo: &Path,
        relative: &Path,
        progress: &Progress,
    ) -> Option<Result<bool, &Index>> {
        let name = plain(relative)?;
        Some(match self.index(repo, progress) {
            Index::Files(files) => Ok(files.matches(&name)),
            other => Err(other),
        })
    }
    /// `git check-ignore -q -- relative/`.
    fn ignores(&mut self, repo: &Path, relative: &Path, progress: &Progress) -> Result<bool> {
        let name = plain(relative).map(|mut name| {
            name.push(b'/');
            name
        });
        if let Some(name) = &name {
            if !self.ignored.contains_key(repo) {
                let batch = self.ignore_batch(repo, progress);
                self.ignored.insert(repo.into(), batch);
            }
            if let Some(Some(batch)) = self.ignored.get(repo)
                && batch.asked.contains(name)
            {
                return Ok(batch.ignored.contains(name));
            }
        }
        let mut ignored = relative.as_os_str().to_os_string();
        ignored.push("/");
        Ok(process::git(
            repo,
            [
                "check-ignore".as_ref(),
                "-q".as_ref(),
                "--".as_ref(),
                ignored.as_os_str(),
            ],
            progress,
        )?
        .status
        .success())
    }
    /// One `git check-ignore --stdin -z` for the planned artifacts of `repo`. Git
    /// answers each path independently but aborts the whole batch on a path it
    /// rejects, so anything but a complete answer is discarded.
    fn ignore_batch(&mut self, repo: &Path, progress: &Progress) -> Option<Ignored> {
        let mut asked = BTreeSet::new();
        for path in std::mem::take(&mut self.planned) {
            if let Ok(relative) = path.strip_prefix(repo)
                && let Some(mut name) = plain(relative)
                && !name.is_empty()
                && let Some(parent) = path.parent()
                && fs::symlink_metadata(&path).is_ok_and(|m| m.is_dir())
                && self.toplevel(parent, progress).is_ok_and(|r| r == repo)
            {
                name.push(b'/');
                asked.insert(name);
            }
            self.planned.push(path);
        }
        if asked.len() < 2 {
            return None;
        }
        let mut input = Vec::new();
        for name in &asked {
            input.extend(name);
            input.push(0);
        }
        let output = process::git_input(
            repo,
            ["check-ignore", "--stdin", "-z"],
            Some(input),
            progress,
        )
        .ok()?;
        // 0: some ignored, 1: none ignored; anything else is an incomplete answer.
        if !matches!(output.status.code(), Some(0 | 1)) {
            return None;
        }
        let ignored: BTreeSet<Vec<u8>> = output
            .stdout
            .split(|c| *c == 0)
            .filter(|name| !name.is_empty())
            .map(<[u8]>::to_vec)
            .collect();
        ignored
            .is_subset(&asked)
            .then_some(Ignored { asked, ignored })
    }
}
pub fn project_context(
    path: &Path,
    git: &mut Git,
    progress: &Progress,
) -> Result<(PathBuf, PathBuf)> {
    let _span = progress.span("项目归属");
    let parent = path.parent().context("缺少项目目录")?;
    let repo = git
        .toplevel(parent, progress)
        .map_err(|e| anyhow::anyhow!(e).context("未找到所属 Git 仓库；保留"))?;
    if !repo.is_absolute() || path == repo || !path.starts_with(&repo) {
        bail!("无法核实所属仓库");
    }
    let project = git
        .projects
        .entry(parent.into())
        .or_insert_with(|| project_types::context(path, &repo))
        .clone();
    Ok((repo, project))
}
pub fn verify(root: &Path, path: &Path, git: &mut Git, progress: &Progress) -> Result<()> {
    if !valid_location(root, path)
        || fsutil::unlinked_path(path).is_err()
        || !fsutil::stat(path)?.directory()
    {
        bail!("只能选择 Agent 工作树或项目中已识别的真实生成目录");
    }
    let (repo, project) = project_context(path, git, progress)?;
    let _span = progress.span("Git 证据");
    if path.file_name().is_some_and(|n| n == "node_modules") {
        let names = || ["package.json"].into_iter().chain(LOCKS.iter().copied());
        let listed = match project.strip_prefix(&repo).ok().and_then(plain) {
            Some(prefix) => match git.index(&repo, progress) {
                Index::Files(files) => Some(files),
                Index::Refused(e) => bail!("Git 检查失败：{e}"),
                Index::Failed(e) => bail!("{e}"),
            }
            .map(|files| {
                names()
                    .filter(|name| {
                        let mut file = prefix.clone();
                        if !file.is_empty() {
                            file.push(b'/');
                        }
                        file.extend(name.as_bytes());
                        files.file(&file)
                    })
                    .map(|name| name.as_bytes().to_vec())
                    .collect::<BTreeSet<_>>()
            }),
            None => None,
        };
        let tracked = match listed {
            Some(tracked) => tracked,
            None => {
                let args: Vec<_> = ["ls-files", "-z", "--"]
                    .into_iter()
                    .chain(names())
                    .collect();
                process::checked_git(&project, args, progress)?
                    .split(|c| *c == 0)
                    .map(<[u8]>::to_vec)
                    .collect()
            }
        };
        if !tracked.contains(b"package.json".as_slice())
            || !project.join("package.json").is_file()
            || !LOCKS
                .iter()
                .any(|p| tracked.contains(p.as_bytes()) && project.join(p).is_file())
        {
            bail!("缺少 Git 跟踪的 package.json 或依赖锁文件");
        }
    } else {
        let detail = project_types::describe(path, &project, path)?;
        for name in detail["evidence"].as_array().context("缺少项目清单")? {
            let name = Path::new(name.as_str().context("清单路径错误")?).strip_prefix(&repo)?;
            let tracked = match git.tracked(&repo, name, progress) {
                Some(Ok(tracked)) => tracked,
                Some(Err(Index::Failed(e))) => bail!("{e}"),
                Some(Err(_)) => false,
                None => process::git(
                    &repo,
                    [
                        "ls-files".as_ref(),
                        "--error-unmatch".as_ref(),
                        "--".as_ref(),
                        name.as_os_str(),
                    ],
                    progress,
                )?
                .status
                .success(),
            };
            if !tracked {
                bail!("项目清单未被 Git 跟踪；保留");
            }
        }
    }
    let relative = path.strip_prefix(&repo)?;
    if !git.ignores(&repo, relative, progress)? {
        bail!("生成目录未被 Git 忽略");
    }
    let inside = match git.tracked(&repo, relative, progress) {
        Some(Ok(inside)) => inside,
        Some(Err(Index::Refused(e))) => bail!("Git 检查失败：{e}"),
        Some(Err(Index::Failed(e))) => bail!("{e}"),
        Some(Err(Index::Files(_))) => unreachable!(),
        None => !process::checked_git(
            &repo,
            [
                "ls-files".as_ref(),
                "--cached".as_ref(),
                "-z".as_ref(),
                "--".as_ref(),
                relative.as_os_str(),
            ],
            progress,
        )?
        .is_empty(),
    };
    if inside {
        bail!("目录内有 Git 跟踪的文件，或无法检查 Git 索引");
    }
    Ok(())
}
pub fn check_policy(
    root: &Path,
    path: &Path,
    policy: &Policy,
    source: &str,
    git: &mut Git,
    progress: &Progress,
) -> Result<()> {
    if !valid_location(root, path) {
        bail!("只支持 Agent 工作树内已识别的产物绝对路径");
    }
    if policy.protected(source, path.strip_prefix(root)?, true) {
        bail!("命中保护路径");
    }
    verify(root, path, git, progress)?;
    check_mounts(path)
}
pub fn check_mounts(path: &Path) -> Result<()> {
    #[cfg(target_os = "linux")]
    {
        let bytes = fs::read("/proc/self/mountinfo")?;
        for line in bytes.split(|c| *c == b'\n').filter(|l| !l.is_empty()) {
            let field = line
                .split(|c| c.is_ascii_whitespace())
                .nth(4)
                .context("挂载信息不完整")?;
            let mut decoded = Vec::new();
            let mut i = 0;
            while i < field.len() {
                if field[i] == b'\\'
                    && i + 3 < field.len()
                    && let Ok(n) =
                        u8::from_str_radix(&String::from_utf8_lossy(&field[i + 1..i + 4]), 8)
                {
                    decoded.push(n);
                    i += 4;
                    continue;
                }
                decoded.push(field[i]);
                i += 1;
            }
            if process::path_bytes(&decoded).starts_with(path) {
                bail!("暂存项内有挂载点，拒绝清除");
            }
        }
    }
    #[cfg(not(target_os = "linux"))]
    let _ = path;
    Ok(())
}
#[derive(Default, Clone)]
pub struct References {
    pub paths: BTreeSet<PathBuf>,
    pub targets: BTreeMap<PathBuf, PathBuf>,
    pub errors: Vec<String>,
    pub repositories: BTreeSet<PathBuf>,
    pub workspaces: BTreeSet<PathBuf>,
    /// Paths found but left out because a report cannot spell them.
    pub unlisted: Vec<String>,
    pub(crate) by_path: BTreeMap<PathBuf, BTreeSet<PathBuf>>,
    pub(crate) by_identity: BTreeMap<(u64, u64), BTreeSet<PathBuf>>,
}
impl References {
    pub fn read(root: &Path, nested: bool, progress: &Progress) -> Result<Self> {
        Ok(crate::survey::read(root, nested, false, progress)?.refs)
    }
    pub fn incoming(&self, path: &Path) -> Vec<PathBuf> {
        let mut paths = self.by_path.get(path).cloned().unwrap_or_default();
        if let Ok(info) = fsutil::stat(path)
            && let Some(found) = self.by_identity.get(&info.identity())
        {
            paths.extend(found.clone());
        }
        paths.into_iter().filter(|p| !p.starts_with(path)).collect()
    }
    pub fn guard(&self, path: &Path) -> Result<()> {
        let callers = self.incoming(path);
        if !callers.is_empty() {
            let origin = if callers
                .iter()
                .any(|p| !p.starts_with(path.parent().unwrap()))
            {
                "其他工作树"
            } else {
                "本工作树子项目"
            };
            bail!(
                "被{origin}引用：{}",
                callers
                    .iter()
                    .map(|p| fsutil::text(p))
                    .collect::<Vec<_>>()
                    .join("、")
            );
        }
        if let Some(error) = self.errors.first() {
            bail!("工作树引用盘点不完整，保留目标：{error}");
        }
        Ok(())
    }
}
#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::{os::unix::fs::symlink, process::Command};

    fn git(repo: &Path, args: &[&str]) {
        let output = Command::new("git")
            .arg("-C")
            .arg(repo)
            .args([
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
            ])
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

    #[test]
    fn listed_names_answer_like_literal_pathspecs() {
        let files = Tracked::new(b"web/target/x\0target2/x\0src/a\0target.rs\0src/a\0".to_vec());
        assert!(files.file(b"src/a") && !files.file(b"src") && !files.file(b"src/a/"));
        for name in ["src", "src/a", "web", "web/target", "target.rs"] {
            assert!(files.matches(name.as_bytes()), "{name}");
        }
        for name in ["target", "web/tar", "x", "src/a/b", "target2/x/y", "zz"] {
            assert!(!files.matches(name.as_bytes()), "{name}");
        }
        assert!(!Tracked::new(vec![]).matches(b"target"));
        if cfg!(not(windows)) {
            assert_eq!(
                plain(Path::new("a/b c/.venv")),
                Some(b"a/b c/.venv".to_vec())
            );
            assert_eq!(plain(Path::new("")), Some(vec![]));
        }
        for name in [
            "a/[b]",
            "a*",
            "a?",
            "a\\b",
            ":a",
            "a/../b",
            "/a",
            "caf\u{e9}/x",
        ] {
            assert_eq!(plain(Path::new(name)), None, "{name}");
        }
    }

    #[test]
    fn shared_git_answers_match_one_query_per_path() {
        let temp = tempfile::tempdir().unwrap();
        let repo = temp.path().canonicalize().unwrap().join("repo");
        let inner = repo.join("inner");
        for directory in [
            "target",
            "build",
            "keep/build",
            "dist",
            "vendor/target",
            "deep/x/y/target",
            "a[1]/target",
            "caf\u{e9}/target",
            "inner/target",
            "inner/dist",
        ] {
            fs::create_dir_all(repo.join(directory)).unwrap();
            fs::write(repo.join(directory).join("generated"), "").unwrap();
        }
        fs::write(repo.join(".gitignore"), "target/\nbuild/\n!keep/build/\n").unwrap();
        fs::write(repo.join("Cargo.toml"), "").unwrap();
        fs::write(inner.join(".gitignore"), "dist/\n").unwrap();
        for path in [&inner, &repo] {
            git(path, &["init", "-q"]);
        }
        git(&inner, &["add", "."]);
        git(&repo, &["add", "Cargo.toml", ".gitignore", "dist"]);
        git(&repo, &["add", "-f", "vendor/target/generated"]);
        git(&repo, &["commit", "-qm", "fixture"]);

        let progress = Progress::default();
        let paths: Vec<_> = [
            "target",
            "build",
            "keep/build",
            "dist",
            "vendor/target",
            "deep/x/y/target",
            "a[1]/target",
            "caf\u{e9}/target",
            "inner/target",
            "inner/dist",
            "missing/target",
        ]
        .map(|name| repo.join(name))
        .into();
        let mut shared = Git::default();
        shared.plan(paths.iter().cloned());
        let mut ignored = 0;
        for path in &paths {
            let parent = path.parent().unwrap();
            let owner = process::git_path(parent, "--show-toplevel", &progress).ok();
            assert_eq!(shared.toplevel(parent, &progress).ok(), owner, "{path:?}");
            let Some(owner) = owner else {
                assert_eq!(path, &repo.join("missing/target"));
                continue;
            };
            assert_eq!(
                &owner,
                if path.starts_with(&inner) {
                    &inner
                } else {
                    &repo
                }
            );
            let relative = path.strip_prefix(&owner).unwrap();
            let mut directory = relative.as_os_str().to_os_string();
            directory.push("/");
            let expected = process::git(
                &owner,
                [
                    "check-ignore".as_ref(),
                    "-q".as_ref(),
                    "--".as_ref(),
                    directory.as_os_str(),
                ],
                &progress,
            )
            .unwrap()
            .status
            .success();
            assert_eq!(
                shared.ignores(&owner, relative, &progress).unwrap(),
                expected,
                "{path:?}"
            );
            ignored += usize::from(expected);
            let inside = !process::checked_git(
                &owner,
                [
                    "ls-files".as_ref(),
                    "--cached".as_ref(),
                    "-z".as_ref(),
                    "--".as_ref(),
                    relative.as_os_str(),
                ],
                &progress,
            )
            .unwrap()
            .is_empty();
            match shared.tracked(&owner, relative, &progress) {
                Some(answer) => assert_eq!(answer.ok(), Some(inside), "{path:?}"),
                None => assert!(plain(relative).is_none()),
            }
        }
        // target, build, deep/x/y/target, a[1]/target, café/target and inner/dist.
        assert_eq!(ignored, 6);
        let batch = shared.ignored[&repo].as_ref().unwrap();
        assert_eq!(batch.asked.len(), 6);
        assert_eq!(batch.ignored.len(), 3);
        assert!(shared.ignored[&inner].is_some());
        // Unplanned paths and later scans are still answered.
        let mut single = Git::default();
        assert!(
            single
                .ignores(&repo, Path::new("build"), &progress)
                .unwrap()
        );
        assert!(!single.ignores(&repo, Path::new("dist"), &progress).unwrap());
    }

    #[test]
    fn checkouts_inside_walked_checkouts_keep_their_references() {
        let temp = tempfile::tempdir().unwrap();
        let home = temp.path().canonicalize().unwrap();
        let (root, main) = (home.join("agent"), home.join("main"));
        let checkout = root.join("worktrees/checkout");
        fs::create_dir_all(main.join("target")).unwrap();
        fs::create_dir_all(root.join("worktrees")).unwrap();
        fs::write(main.join(".gitignore"), "target/\nnested/\nlink\n").unwrap();
        git(&main, &["init", "-q"]);
        git(&main, &["add", "."]);
        git(&main, &["commit", "-qm", "fixture"]);
        let (nested, hidden) = (main.join("nested"), main.join("target/hidden"));
        for path in [&checkout, &nested, &hidden] {
            git(
                &main,
                &["worktree", "add", "-q", "--detach", path.to_str().unwrap()],
            );
        }
        let target = checkout.join("target");
        fs::create_dir(&target).unwrap();
        for path in [&nested, &hidden] {
            symlink(&target, path.join("link")).unwrap();
        }
        let refs = References::read(&root, true, &Progress::default()).unwrap();
        assert_eq!(refs.errors, Vec::<String>::new());
        assert_eq!(
            refs.incoming(&target),
            vec![nested.join("link"), hidden.join("link")]
        );
        assert!(refs.guard(&target).is_err());
    }
}
