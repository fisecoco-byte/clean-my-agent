use crate::{
    fsutil::{self, Visit},
    policy::Policy,
    process,
    progress::Progress,
    project_types,
};
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
pub fn project_context(path: &Path, progress: &Progress) -> Result<(PathBuf, PathBuf)> {
    let repo = process::git_path(
        path.parent().context("缺少项目目录")?,
        "--show-toplevel",
        progress,
    )
    .context("未找到所属 Git 仓库；保留")?;
    if !repo.is_absolute() || path == repo || !path.starts_with(&repo) {
        bail!("无法核实所属仓库");
    }
    let project = project_types::context(path, &repo);
    Ok((repo, project))
}
pub fn verify(root: &Path, path: &Path, progress: &Progress) -> Result<()> {
    if !valid_location(root, path)
        || fsutil::unlinked_path(path).is_err()
        || !fsutil::stat(path)?.directory()
    {
        bail!("只能选择 Agent 工作树或项目中已识别的真实生成目录");
    }
    let (repo, project) = project_context(path, progress)?;
    if path.file_name().is_some_and(|n| n == "node_modules") {
        let args: Vec<_> = ["ls-files", "-z", "--", "package.json"]
            .into_iter()
            .chain(LOCKS.iter().copied())
            .collect();
        let files = process::checked_git(&project, args, progress)?;
        let tracked: BTreeSet<_> = files.split(|c| *c == 0).collect();
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
            let result = process::git(
                &repo,
                [
                    "ls-files".as_ref(),
                    "--error-unmatch".as_ref(),
                    "--".as_ref(),
                    name.as_os_str(),
                ],
                progress,
            )?;
            if !result.status.success() {
                bail!("项目清单未被 Git 跟踪；保留");
            }
        }
    }
    let relative = path.strip_prefix(&repo)?;
    let mut ignored = relative.as_os_str().to_os_string();
    ignored.push("/");
    if !process::git(
        &repo,
        [
            "check-ignore".as_ref(),
            "-q".as_ref(),
            "--".as_ref(),
            ignored.as_os_str(),
        ],
        progress,
    )?
    .status
    .success()
    {
        bail!("生成目录未被 Git 忽略");
    }
    let files = process::checked_git(
        &repo,
        [
            "ls-files".as_ref(),
            "--cached".as_ref(),
            "-z".as_ref(),
            "--".as_ref(),
            relative.as_os_str(),
        ],
        progress,
    )?;
    if !files.is_empty() {
        bail!("目录内有 Git 跟踪的文件，或无法检查 Git 索引");
    }
    Ok(())
}
pub fn check_policy(
    root: &Path,
    path: &Path,
    policy: &Policy,
    source: &str,
    progress: &Progress,
) -> Result<()> {
    if !valid_location(root, path) {
        bail!("只支持 Agent 工作树内已识别的产物绝对路径");
    }
    if policy.protected(source, path.strip_prefix(root)?, true) {
        bail!("命中保护路径");
    }
    verify(root, path, progress)?;
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
    by_path: BTreeMap<PathBuf, BTreeSet<PathBuf>>,
    by_identity: BTreeMap<(u64, u64), BTreeSet<PathBuf>>,
}
impl References {
    pub fn read(root: &Path, nested: bool, progress: &Progress) -> Result<Self> {
        let mut result = Self::default();
        if fsutil::unlinked_path(root).is_err() {
            result
                .errors
                .push(format!("数据根目录含符号链接：{}", root.display()));
            return Ok(result);
        }
        progress.update(
            if nested {
                "查找工作树"
            } else {
                "查找依赖目录"
            },
            None,
            None,
            "",
        )?;
        let mut visited = 0;
        for area in AREAS {
            let start = root.join(area);
            if !start.exists() && fs::symlink_metadata(&start).is_err() {
                continue;
            }
            if fsutil::stat(&start).map_or(true, |s| !s.directory()) {
                result
                    .errors
                    .push(format!("无法核实符号链接目录：{}", start.display()));
                continue;
            }
            let outcome = fsutil::walk(&start, progress, &mut |relative, _, name, s| {
                visited += 1;
                if visited % 2048 == 0 {
                    progress.update(
                        if nested {
                            "查找工作树"
                        } else {
                            "查找依赖目录"
                        },
                        None,
                        None,
                        format!("{visited} 项"),
                    )?;
                }
                let path = start.join(relative);
                let name = name.to_string_lossy();
                if name == ".git" {
                    result.repositories.insert(path.parent().unwrap().into());
                    return Ok(Visit::Skip);
                }
                if !s.directory()
                    && (project_types::INDICATORS.contains(&name.as_ref())
                        || [".csproj", ".fsproj", ".vbproj"]
                            .iter()
                            .any(|x| name.ends_with(x)))
                {
                    result.workspaces.insert(path.parent().unwrap().into());
                }
                if name != ".git"
                    && (project_types::TARGETS.contains(&name.as_ref())
                        || (s.directory() && project_types::tagged(&path)))
                {
                    result.paths.insert(path.clone());
                    if name == ".build" && s.directory() {
                        for child in project_types::SWIFT_OUTPUTS {
                            let p = path.join(child);
                            if fs::symlink_metadata(&p).is_ok() {
                                result.paths.insert(p);
                            }
                        }
                    }
                    return Ok(Visit::Skip);
                }
                Ok(if name == ".Trash" {
                    Visit::Skip
                } else {
                    Visit::Descend
                })
            });
            if let Err(error) = outcome {
                progress.check()?;
                result.errors.push(error.to_string());
            }
        }
        let mut checkouts = BTreeSet::new();
        for repo in &result.repositories {
            if checkouts.contains(repo) {
                continue;
            }
            match process::worktrees(repo, progress) {
                Ok(paths) => checkouts.extend(paths),
                Err(e) => {
                    progress.check()?;
                    result.errors.push(e.to_string());
                }
            }
        }
        let mut sources = result.paths.clone();
        for p in &checkouts {
            for name in project_types::TARGETS {
                sources.insert(p.join(name));
            }
        }
        if nested {
            let live: BTreeSet<_> = result.repositories.union(&checkouts).cloned().collect();
            for (index, repo) in live.iter().enumerate() {
                progress.update(
                    "复核工作树引用",
                    Some(index),
                    Some(live.len()),
                    fsutil::text(repo),
                )?;
                if !repo.join(".git").exists() {
                    continue;
                }
                let canonical = match fsutil::resolve(repo) {
                    Ok(p) => p,
                    Err(e) => {
                        result.errors.push(e.to_string());
                        continue;
                    }
                };
                if canonical == Path::new("/")
                    || crate::policy::home().is_ok_and(|h| h == canonical)
                {
                    result
                        .errors
                        .push(format!("工作树范围过大，无法核实引用：{}", repo.display()));
                    continue;
                }
                let walked = fsutil::walk(&canonical, progress, &mut |relative, _, name, s| {
                    if s.symlink() {
                        sources.insert(canonical.join(relative));
                        return Ok(Visit::Skip);
                    }
                    let name = name.to_string_lossy();
                    Ok(
                        if name == ".git" || project_types::TARGETS.contains(&name.as_ref()) {
                            Visit::Skip
                        } else {
                            Visit::Descend
                        },
                    )
                });
                if let Err(e) = walked {
                    progress.check()?;
                    result.errors.push(format!("子项目引用检查不完整：{e}"));
                }
            }
            if !live.is_empty() {
                progress.update("复核工作树引用", Some(live.len()), Some(live.len()), "")?;
            }
        }
        progress.update("核对依赖链接", None, None, "")?;
        for source in sources {
            let info = match fsutil::stat(&source) {
                Ok(s) => s,
                Err(e) => {
                    if !e
                        .downcast_ref::<std::io::Error>()
                        .is_some_and(|e| e.kind() == std::io::ErrorKind::NotFound)
                    {
                        result
                            .errors
                            .push(format!("无法核实引用 {}：{e}", source.display()));
                    }
                    continue;
                }
            };
            if !info.symlink() {
                continue;
            }
            match fsutil::link_route(&source) {
                Ok((target, hops)) => {
                    result.targets.insert(source.clone(), target.clone());
                    for hop in
                        std::iter::once(target).chain(hops.into_iter().filter(|p| p != &source))
                    {
                        for ancestor in hop.ancestors() {
                            result
                                .by_path
                                .entry(ancestor.into())
                                .or_default()
                                .insert(source.clone());
                            if let Ok(stat) = fsutil::stat(ancestor) {
                                result
                                    .by_identity
                                    .entry(stat.identity())
                                    .or_default()
                                    .insert(source.clone());
                            }
                        }
                    }
                }
                Err(e) => result.errors.push(e.to_string()),
            }
        }
        Ok(result)
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
