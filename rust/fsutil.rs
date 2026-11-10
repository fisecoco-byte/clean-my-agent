//! Descriptor-relative Unix traversal: links are observed, never followed.
use crate::progress::Progress;
use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    ffi::OsString,
    fs::{self, File},
    path::{Component, Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
pub struct Info {
    pub dev: u64,
    pub ino: u64,
    pub mode: u32,
    pub links: u64,
    pub size: u64,
    pub allocated: u64,
    pub modified_ns: i64,
}
impl Info {
    pub fn directory(&self) -> bool {
        self.mode & 0o170000 == 0o040000
    }
    pub fn regular(&self) -> bool {
        self.mode & 0o170000 == 0o100000
    }
    pub fn symlink(&self) -> bool {
        self.mode & 0o170000 == 0o120000
    }
    pub fn stamp(&self) -> serde_json::Value {
        serde_json::json!([self.dev, self.ino, self.size, self.modified_ns])
    }
    pub fn identity(&self) -> (u64, u64) {
        (self.dev, self.ino)
    }
}
pub fn now_ns() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
        .min(i64::MAX as u128) as i64
}
pub fn text(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}
pub fn printable(c: char) -> bool {
    static HIDDEN: std::sync::LazyLock<regex::Regex> =
        std::sync::LazyLock::new(|| regex::Regex::new(r"[\p{C}\p{Z}]").unwrap());
    c == ' ' || !HIDDEN.is_match(c.encode_utf8(&mut [0; 4]))
}
pub fn lexical(path: &Path) -> PathBuf {
    let mut result = PathBuf::new();
    for part in path.components() {
        match part {
            Component::CurDir => (),
            Component::ParentDir => {
                result.pop();
            }
            other => result.push(other.as_os_str()),
        }
    }
    result
}
pub fn resolve(path: &Path) -> Result<PathBuf> {
    Ok(link_route(path)?.0)
}
pub fn link_route(path: &Path) -> Result<(PathBuf, Vec<PathBuf>)> {
    link_route_inner(path, false)
}
/// Normalize process-owned paths like pathlib.resolve(strict=False). Inaccessible
/// components remain lexical. Never use this relaxed mode to authorize writes.
pub fn process_path(path: &Path) -> Result<PathBuf> {
    Ok(link_route_inner(path, true)?.0)
}
fn link_route_inner(path: &Path, relaxed: bool) -> Result<(PathBuf, Vec<PathBuf>)> {
    let mut current = PathBuf::new();
    let mut todo: std::collections::VecDeque<OsString> = path
        .components()
        .map(|p| p.as_os_str().to_os_string())
        .collect();
    let mut hops = Vec::new();
    while let Some(part) = todo.pop_front() {
        if part == ".." {
            current.pop();
            continue;
        }
        if part == "." {
            continue;
        }
        current.push(&part);
        // A drive/UNC prefix is not a filesystem entry. In particular, probing
        // a verbatim drive prefix (\\?\C:) as a reparse point fails on Windows.
        // Continue checking every directory/file component after the root.
        if current
            .components()
            .all(|part| matches!(part, Component::Prefix(_) | Component::RootDir))
        {
            continue;
        }
        match fs::read_link(&current) {
            Ok(target) => {
                hops.push(current.clone());
                if hops.len() > 40 {
                    bail!("符号链接循环：{}", path.display());
                }
                current.pop();
                if target.is_absolute() {
                    current = PathBuf::new();
                }
                for p in target.components().rev() {
                    todo.push_front(p.as_os_str().to_os_string());
                }
            }
            Err(e)
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::NotFound
                        | std::io::ErrorKind::NotADirectory
                        | std::io::ErrorKind::InvalidInput
                ) => {}
            Err(e) if relaxed && e.kind() == std::io::ErrorKind::PermissionDenied => (),
            Err(e) => return Err(e.into()),
        }
    }
    Ok((current, hops))
}
pub fn absolute(path: &Path) -> Result<()> {
    path.to_str().context("路径编码无法安全写入报告；保留")?;
    if !path.is_absolute() || path.components().any(|p| p == Component::ParentDir) {
        bail!("需要不含 .. 的绝对路径：{}", path.display());
    }
    Ok(())
}
pub fn unlinked_path(path: &Path) -> Result<()> {
    absolute(path)?;
    if resolve(path)? != path {
        bail!("路径含符号链接：{}", path.display());
    }
    Ok(())
}

#[cfg(unix)]
mod platform {
    use super::*;
    use std::{
        ffi::{CStr, CString, OsStr},
        os::{
            fd::{AsRawFd, FromRawFd},
            unix::ffi::{OsStrExt, OsStringExt},
        },
    };
    fn name(path: &OsStr) -> Result<CString> {
        Ok(CString::new(path.as_bytes())?)
    }
    // libc uses different widths for these fields on macOS and Linux.
    #[allow(clippy::unnecessary_cast)]
    fn info(s: libc::stat) -> Info {
        Info {
            dev: s.st_dev as u64,
            ino: s.st_ino,
            mode: s.st_mode as u32,
            links: s.st_nlink as u64,
            size: s.st_size.max(0) as u64,
            allocated: (s.st_blocks.max(0) as u64) * 512,
            modified_ns: s
                .st_mtime
                .saturating_mul(1_000_000_000)
                .saturating_add(s.st_mtime_nsec),
        }
    }
    pub struct Directory(pub File);
    impl Directory {
        pub fn open(path: &Path) -> Result<Self> {
            absolute(path)?;
            let root = name(OsStr::new("/"))?;
            let fd = unsafe {
                libc::open(
                    root.as_ptr(),
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
                )
            };
            if fd < 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            let mut directory = Self(unsafe { File::from_raw_fd(fd) });
            for component in path.components() {
                if let Component::Normal(part) = component {
                    directory = directory.child(part)?;
                }
            }
            Ok(directory)
        }
        pub fn child(&self, child: &OsStr) -> Result<Self> {
            let child = name(child)?;
            let fd = unsafe {
                libc::openat(
                    self.0.as_raw_fd(),
                    child.as_ptr(),
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                )
            };
            if fd < 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            Ok(Self(unsafe { File::from_raw_fd(fd) }))
        }
        pub fn open_file(&self, child: &OsStr) -> Result<File> {
            let child = name(child)?;
            let fd = unsafe {
                libc::openat(
                    self.fd(),
                    child.as_ptr(),
                    libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
                )
            };
            if fd < 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            let file = unsafe { File::from_raw_fd(fd) };
            if !file.metadata()?.is_file() {
                bail!("元数据不是普通文件");
            }
            Ok(file)
        }
        pub fn info(&self) -> Result<Info> {
            let mut s = std::mem::MaybeUninit::uninit();
            if unsafe { libc::fstat(self.0.as_raw_fd(), s.as_mut_ptr()) } != 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            Ok(info(unsafe { s.assume_init() }))
        }
        pub fn stat(&self, child: &OsStr) -> Result<Info> {
            let child = name(child)?;
            let mut s = std::mem::MaybeUninit::uninit();
            if unsafe {
                libc::fstatat(
                    self.0.as_raw_fd(),
                    child.as_ptr(),
                    s.as_mut_ptr(),
                    libc::AT_SYMLINK_NOFOLLOW,
                )
            } != 0
            {
                return Err(std::io::Error::last_os_error().into());
            }
            Ok(info(unsafe { s.assume_init() }))
        }
        pub fn names(&self) -> Result<Vec<OsString>> {
            let dot = name(OsStr::new("."))?;
            let fd = unsafe {
                libc::openat(
                    self.0.as_raw_fd(),
                    dot.as_ptr(),
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
                )
            };
            if fd < 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            let stream = unsafe { libc::fdopendir(fd) };
            if stream.is_null() {
                unsafe {
                    libc::close(fd);
                }
                return Err(std::io::Error::last_os_error().into());
            }
            struct Stream(*mut libc::DIR);
            impl Drop for Stream {
                fn drop(&mut self) {
                    unsafe {
                        libc::closedir(self.0);
                    }
                }
            }
            let stream = Stream(stream);
            let mut result = Vec::new();
            loop {
                #[cfg(target_os = "macos")]
                unsafe {
                    *libc::__error() = 0;
                }
                #[cfg(target_os = "linux")]
                unsafe {
                    *libc::__errno_location() = 0;
                }
                let entry = unsafe { libc::readdir(stream.0) };
                if entry.is_null() {
                    let error = std::io::Error::last_os_error();
                    if error.raw_os_error().unwrap_or(0) != 0 {
                        return Err(error.into());
                    }
                    break;
                }
                let bytes = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) }.to_bytes();
                if bytes != b"." && bytes != b".." {
                    result.push(OsString::from_vec(bytes.to_vec()));
                }
            }
            result.sort();
            Ok(result)
        }
        pub fn fd(&self) -> i32 {
            self.0.as_raw_fd()
        }
    }
    pub fn stat(path: &Path) -> Result<Info> {
        if path == Path::new("/") {
            return Directory::open(path)?.info();
        }
        Directory::open(path.parent().context("路径无父目录")?)?
            .stat(path.file_name().context("路径无文件名")?)
    }
}

#[cfg(windows)]
mod platform {
    use super::*;
    use std::os::windows::{
        ffi::OsStrExt,
        fs::{MetadataExt, OpenOptionsExt},
        io::AsRawHandle,
    };
    use windows_sys::Win32::{
        Foundation::{GetLastError, SetLastError},
        Storage::FileSystem::*,
    };
    pub struct Directory(pub PathBuf, pub File);
    fn open(path: &Path, contents: bool) -> Result<File> {
        let mut options = fs::OpenOptions::new();
        options
            .access_mode(if contents {
                windows_sys::Win32::Foundation::GENERIC_READ
            } else {
                FILE_READ_ATTRIBUTES
            })
            .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
            .custom_flags(FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT);
        Ok(options.open(path)?)
    }
    fn info(file: &File) -> Result<Info> {
        let handle = file.as_raw_handle();
        let mut identity: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
        let mut standard: FILE_STANDARD_INFO = unsafe { std::mem::zeroed() };
        if unsafe { GetFileInformationByHandle(handle, &mut identity) } == 0
            || unsafe {
                GetFileInformationByHandleEx(
                    handle,
                    FileStandardInfo,
                    (&mut standard as *mut FILE_STANDARD_INFO).cast(),
                    std::mem::size_of::<FILE_STANDARD_INFO>() as u32,
                )
            } == 0
        {
            return Err(std::io::Error::last_os_error().into());
        }
        let m = file.metadata()?;
        let linked = m.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0;
        if standard.AllocationSize < 0 {
            bail!("文件分配量无效");
        }
        Ok(Info {
            dev: identity.dwVolumeSerialNumber as u64,
            ino: ((identity.nFileIndexHigh as u64) << 32) | identity.nFileIndexLow as u64,
            mode: if linked {
                0o120000
            } else if m.is_dir() {
                0o040000
            } else {
                0o100000
            },
            links: identity.nNumberOfLinks as u64,
            size: m.len(),
            allocated: if linked || m.is_dir() {
                0
            } else {
                standard.AllocationSize as u64
            },
            modified_ns: m
                .last_write_time()
                .saturating_sub(116444736000000000)
                .saturating_mul(100)
                .min(i64::MAX as u64) as i64,
        })
    }
    impl Directory {
        pub fn open(path: &Path) -> Result<Self> {
            unlinked_path(path)?;
            for p in path.ancestors() {
                if fs::symlink_metadata(p)?.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                    bail!("路径含重新解析点，保留");
                }
            }
            let file = open(path, false)?;
            if !info(&file)?.directory() {
                bail!("不是目录");
            }
            Ok(Self(path.into(), file))
        }
        fn current(&self) -> Result<()> {
            if info(&self.1)?.identity() != stat(&self.0)?.identity() {
                bail!("测量期间目录已被替换");
            }
            Ok(())
        }
        pub fn child(&self, name: &std::ffi::OsStr) -> Result<Self> {
            self.current()?;
            let dir = Self::open(&self.0.join(name))?;
            self.current()?;
            Ok(dir)
        }
        pub fn open_file(&self, name: &std::ffi::OsStr) -> Result<File> {
            self.current()?;
            let file = open(&self.0.join(name), true)?;
            if !info(&file)?.regular() {
                bail!("元数据不是普通文件");
            }
            self.current()?;
            Ok(file)
        }
        pub fn info(&self) -> Result<Info> {
            self.current()?;
            info(&self.1)
        }
        pub fn stat(&self, name: &std::ffi::OsStr) -> Result<Info> {
            self.current()?;
            stat(&self.0.join(name))
        }
        pub fn names(&self) -> Result<Vec<OsString>> {
            self.current()?;
            let mut names = fs::read_dir(&self.0)?
                .map(|r| r.map(|e| e.file_name()))
                .collect::<std::io::Result<Vec<_>>>()?;
            names.sort();
            self.current()?;
            Ok(names)
        }
    }
    pub fn stat(path: &Path) -> Result<Info> {
        let file = open(path, false)?;
        let mut before = info(&file)?;
        if before.regular()
            && file.metadata()?.file_attributes()
                & (FILE_ATTRIBUTE_SPARSE_FILE | FILE_ATTRIBUTE_COMPRESSED)
                != 0
        {
            let absolute = std::path::absolute(path)?;
            let raw = absolute.as_os_str().encode_wide().collect::<Vec<_>>();
            let prefix: Vec<u16> = r"\\?\".encode_utf16().collect();
            let mut wide = if raw.starts_with(&prefix) {
                raw
            } else if raw.starts_with(&[92, 92]) {
                r"\\?\UNC\"
                    .encode_utf16()
                    .chain(raw[2..].iter().copied())
                    .collect()
            } else {
                prefix.into_iter().chain(raw).collect::<Vec<_>>()
            };
            wide.push(0);
            let mut high = 0;
            unsafe { SetLastError(0) };
            let low = unsafe { GetCompressedFileSizeW(wide.as_ptr(), &mut high) };
            let error = unsafe { GetLastError() };
            if low == u32::MAX && error != 0 {
                return Err(std::io::Error::from_raw_os_error(error as i32).into());
            }
            if info(&open(path, false)?)?.stamp() != before.stamp() {
                bail!("测量期间文件已被替换");
            }
            before.allocated = ((high as u64) << 32) | low as u64;
        }
        Ok(before)
    }
}

pub use platform::{Directory, stat};

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Visit {
    Descend,
    Skip,
}
pub fn walk(
    root: &Path,
    progress: &Progress,
    visit: &mut impl FnMut(&Path, &Directory, &std::ffi::OsStr, Info) -> Result<Visit>,
) -> Result<()> {
    progress.check()?;
    fn descend(
        rel: &Path,
        dir: Directory,
        progress: &Progress,
        visit: &mut impl FnMut(&Path, &Directory, &std::ffi::OsStr, Info) -> Result<Visit>,
        depth: usize,
    ) -> Result<()> {
        if depth > 512 {
            bail!("目录层级过深");
        }
        for name in dir.names()? {
            name.to_str()
                .context("目录含无法安全写入报告的文件名；保留")?;
            progress.check()?;
            let s = dir.stat(&name)?;
            let relative = rel.join(&name);
            if visit(&relative, &dir, &name, s)? == Visit::Descend && s.directory() {
                let child = dir.child(&name)?;
                if child.info()?.identity() != s.identity() {
                    bail!("遍历期间目录已替换");
                }
                descend(&relative, child, progress, visit, depth + 1)?;
            }
        }
        Ok(())
    }
    descend(Path::new(""), Directory::open(root)?, progress, visit, 0)
}
pub fn regular_text(path: &Path, limit: u64) -> Result<String> {
    use std::io::Read;
    unlinked_path(path)?;
    let before = stat(path)?;
    if !before.regular() || before.size > limit {
        bail!("元数据不是普通文件或超过读取上限");
    }
    let file = open_regular(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let opened = file.metadata()?;
        if (
            opened.dev(),
            opened.ino(),
            opened.size(),
            opened.mtime() * 1_000_000_000 + opened.mtime_nsec(),
        ) != (before.dev, before.ino, before.size, before.modified_ns)
        {
            bail!("读取期间元数据发生变化");
        }
    }
    let mut content = String::new();
    file.take(limit + 1).read_to_string(&mut content)?;
    if content.len() as u64 > limit || before.stamp() != stat(path)?.stamp() {
        bail!("读取期间元数据发生变化");
    }
    Ok(content)
}
pub fn open_regular(path: &Path) -> Result<File> {
    Directory::open(path.parent().context("元数据路径无父目录")?)?
        .open_file(path.file_name().context("元数据路径无名称")?)
}
pub fn group_size(root: &Path, progress: &Progress) -> Result<u64> {
    let initial = stat(root)?;
    let mut links = BTreeMap::new();
    let mut bytes = initial.allocated;
    walk(root, progress, &mut |_, _, _, s| {
        if s.dev != initial.dev {
            return Ok(Visit::Skip);
        }
        if s.links > 1 && !s.directory() {
            links.insert(s.identity(), s.allocated);
        } else {
            bytes += s.allocated;
        }
        Ok(Visit::Descend)
    })?;
    Ok((bytes + links.values().sum::<u64>()).div_ceil(1024) * 1024)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::unix::{ffi::OsStrExt, fs::symlink};
    #[test]
    fn traversal_never_enters_links() {
        let tmp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(tmp.path()).unwrap();
        fs::create_dir(root.join("tree")).unwrap();
        fs::create_dir(root.join("outside")).unwrap();
        fs::write(root.join("outside/secret"), "private").unwrap();
        symlink("../outside", root.join("tree/link")).unwrap();
        let mut entries = Vec::new();
        walk(
            &root.join("tree"),
            &Progress::default(),
            &mut |r, _, _, s| {
                entries.push((r.to_path_buf(), s.symlink()));
                Ok(Visit::Descend)
            },
        )
        .unwrap();
        assert_eq!(entries, vec![(PathBuf::from("link"), true)]);
        assert!(Directory::open(&root.join("tree/link")).is_err());
        assert!(regular_text(&root.join("tree/link/secret"), 100).is_err());
    }
    #[test]
    fn resolve_link_then_parent() {
        let tmp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(tmp.path()).unwrap();
        fs::create_dir_all(root.join("real/child")).unwrap();
        symlink("real/child", root.join("link")).unwrap();
        assert_eq!(
            resolve(&root.join("link/../sibling")).unwrap(),
            root.join("real/sibling")
        );
        assert!(absolute(&root.join("link/../sibling")).is_err());
    }
    #[test]
    fn loops_and_metadata_limits_are_rejected() {
        let tmp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(tmp.path()).unwrap();
        symlink("b", root.join("a")).unwrap();
        symlink("a", root.join("b")).unwrap();
        assert!(resolve(&root.join("a")).is_err());
        fs::write(root.join("data"), "123456").unwrap();
        assert!(regular_text(&root.join("data"), 5).is_err());
        assert!(regular_text(&root, 100).is_err());
        assert_eq!(regular_text(&root.join("data"), 6).unwrap(), "123456");
    }
    #[test]
    fn hardlinks_and_sparse_match_native_du() {
        let tmp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(tmp.path()).unwrap();
        fs::write(root.join("one"), vec![b'x'; 12345]).unwrap();
        fs::hard_link(root.join("one"), root.join("two")).unwrap();
        File::create(root.join("sparse"))
            .unwrap()
            .set_len(1 << 24)
            .unwrap();
        let out = std::process::Command::new("du")
            .args(["-k", "-s", "-x"])
            .arg(&root)
            .output()
            .unwrap();
        let expected = String::from_utf8(out.stdout)
            .unwrap()
            .split('\t')
            .next()
            .unwrap()
            .parse::<u64>()
            .unwrap()
            * 1024;
        assert_eq!(group_size(&root, &Progress::default()).unwrap(), expected);
    }
    #[test]
    fn cancellation_and_invalid_names_fail_closed() {
        let tmp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(tmp.path()).unwrap();
        let progress = Progress::default();
        progress.cancel();
        assert!(walk(&root, &progress, &mut |_, _, _, _| Ok(Visit::Descend)).is_err());
        let invalid = root.join(std::ffi::OsStr::from_bytes(b"bad-\xff"));
        assert!(absolute(&invalid).is_err());
        match fs::write(&invalid, "x") {
            Ok(()) => assert!(
                walk(&root, &Progress::default(), &mut |_, _, _, _| Ok(
                    Visit::Descend
                ))
                .is_err()
            ),
            Err(e) => assert_eq!(e.raw_os_error(), Some(libc::EILSEQ)),
        };
    }
    #[test]
    fn printable_labels_block_terminal_and_direction_controls() {
        for c in ['\u{202e}', '\u{2066}', '\u{1b}', '\u{e000}', '\u{a0}'] {
            assert!(!printable(c));
        }
        for c in ['中', 'é', '🙂', ' ', 'a'] {
            assert!(printable(c));
        }
    }
}
