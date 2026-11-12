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
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Directory,
    Symlink,
    Other,
}
impl Kind {
    pub(crate) fn of(info: Info) -> Self {
        if info.directory() {
            Self::Directory
        } else if info.symlink() {
            Self::Symlink
        } else {
            Self::Other
        }
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
/// Paths as report text. Names that are not UTF-8 are shown lossily instead of
/// failing JSON serialization.
pub fn texts(paths: &[PathBuf]) -> Vec<String> {
    paths.iter().map(|p| text(p)).collect()
}
pub(crate) fn missing(error: &anyhow::Error) -> bool {
    error
        .downcast_ref::<std::io::Error>()
        .is_some_and(|e| e.kind() == std::io::ErrorKind::NotFound)
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
            Err(e) => {
                // Windows reports ordinary files with a native error that is
                // not mapped to InvalidInput like Unix's EINVAL.
                #[cfg(windows)]
                if e.raw_os_error()
                    == Some(windows_sys::Win32::Foundation::ERROR_NOT_A_REPARSE_POINT as i32)
                {
                    continue;
                }
                return Err(e.into());
            }
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
        /// Sorted names with the `d_type` reported by the directory itself.
        fn read(&self) -> Result<Vec<(OsString, u8)>> {
            let fd = unsafe { libc::fcntl(self.0.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 0) };
            if fd < 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            // The duplicate shares its offset with every earlier listing of this
            // descriptor, so rewind before reading.
            let stream = if unsafe { libc::lseek(fd, 0, libc::SEEK_SET) } < 0 {
                std::ptr::null_mut()
            } else {
                unsafe { libc::fdopendir(fd) }
            };
            if stream.is_null() {
                let error = std::io::Error::last_os_error();
                unsafe {
                    libc::close(fd);
                }
                return Err(error.into());
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
                    result.push((OsString::from_vec(bytes.to_vec()), unsafe {
                        (*entry).d_type
                    }));
                }
            }
            result.sort();
            Ok(result)
        }
        pub fn names(&self) -> Result<Vec<OsString>> {
            Ok(self.read()?.into_iter().map(|(name, _)| name).collect())
        }
        /// Entry types without one `fstatat` per name. The type is only a hint:
        /// callers still open directories with `O_NOFOLLOW`.
        pub fn entries(&self) -> Result<Vec<(OsString, Kind)>> {
            self.read()?
                .into_iter()
                .map(|(name, kind)| {
                    let kind = match kind {
                        libc::DT_DIR => Kind::Directory,
                        libc::DT_LNK => Kind::Symlink,
                        libc::DT_UNKNOWN => Kind::of(self.stat(&name)?),
                        _ => Kind::Other,
                    };
                    Ok((name, kind))
                })
                .collect()
        }
        /// Names, types and metadata of everything that is not a directory.
        /// Directories carry no metadata: callers `fstat` them after opening.
        /// macOS reads the whole directory in bulk instead of one `fstatat` per
        /// entry; entries the bulk call cannot describe exactly fall back to it.
        pub fn listing(&self) -> Result<Vec<(OsString, Kind, Option<Info>)>> {
            #[cfg(target_os = "macos")]
            {
                bulk::listing(self)
            }
            #[cfg(not(target_os = "macos"))]
            {
                let mut result = Vec::new();
                for (name, kind) in self.read()? {
                    if kind == libc::DT_DIR {
                        result.push((name, Kind::Directory, None));
                    } else if let Some(entry) = self.described(name)? {
                        result.push(entry);
                    }
                }
                Ok(result)
            }
        }
        /// One listing entry by `fstatat`; `None` when it vanished meanwhile.
        fn described(&self, name: OsString) -> Result<Option<(OsString, Kind, Option<Info>)>> {
            match self.stat(&name) {
                Ok(info) if info.directory() => Ok(Some((name, Kind::Directory, None))),
                Ok(info) => Ok(Some((name, Kind::of(info), Some(info)))),
                Err(e) if missing(&e) => Ok(None),
                Err(e) => Err(e),
            }
        }
        pub fn fd(&self) -> i32 {
            self.0.as_raw_fd()
        }
    }
    #[cfg(target_os = "macos")]
    mod bulk {
        use super::*;
        #[repr(C)]
        struct Request {
            count: u16,
            reserved: u16,
            common: u32,
            volume: u32,
            directory: u32,
            file: u32,
            fork: u32,
        }
        const NAME: u32 = 0x1;
        const DEVICE: u32 = 0x2;
        const TYPE: u32 = 0x8;
        const MODIFIED: u32 = 0x400;
        const ACCESS: u32 = 0x2_0000;
        const FLAGS: u32 = 0x4_0000;
        const FILE: u32 = 0x0200_0000;
        const ERROR: u32 = 0x2000_0000;
        const RETURNED: u32 = 0x8000_0000;
        const LINKS: u32 = 0x1;
        const ALLOCATED: u32 = 0x4;
        const LENGTH: u32 = 0x200;
        const REGULAR: u32 = 1;
        const DIRECTORY: u32 = 2;
        const LINK: u32 = 5;
        /// `UF_COMPRESSED`: sizes of transparently compressed files are left to `fstatat`.
        const COMPRESSED: u32 = 0x20;
        struct Fields<'a>(&'a [u8], usize);
        impl Fields<'_> {
            fn take<const N: usize>(&mut self) -> Result<[u8; N]> {
                let bytes = self
                    .0
                    .get(self.1..self.1 + N)
                    .context("目录批量读取结果不完整")?;
                self.1 += N;
                Ok(bytes.try_into().unwrap())
            }
            fn word(&mut self) -> Result<u32> {
                Ok(u32::from_ne_bytes(self.take()?))
            }
            fn long(&mut self) -> Result<i64> {
                Ok(i64::from_ne_bytes(self.take()?))
            }
        }
        /// One record: its name and, when every field came back, the same values
        /// `fstatat` reports. `None` metadata asks the caller for `fstatat`.
        fn record(bytes: &[u8]) -> Result<(OsString, Option<u32>, Option<Info>)> {
            let mut fields = Fields(bytes, 4);
            let common = fields.word()?;
            fields.take::<8>()?;
            let file = fields.word()?;
            fields.take::<4>()?;
            let mut failed = false;
            if common & ERROR != 0 {
                failed = fields.word()? != 0;
            }
            if common & NAME == 0 {
                bail!("目录批量读取结果缺少名称");
            }
            let start = fields.1;
            let offset = i32::from_ne_bytes(fields.take()?);
            let length = fields.word()? as usize;
            let start = usize::try_from(start as i64 + offset as i64)
                .ok()
                .context("目录批量读取结果不完整")?;
            let name = bytes
                .get(start..start + length)
                .context("目录批量读取结果不完整")?;
            let name = name.split(|c| *c == 0).next().unwrap_or_default();
            if name.is_empty() {
                bail!("目录批量读取结果缺少名称");
            }
            let name = OsString::from_vec(name.to_vec());
            let mut info = Info {
                dev: 0,
                ino: 0,
                mode: 0,
                links: 0,
                size: 0,
                allocated: 0,
                modified_ns: 0,
            };
            let mut kind = None;
            let mut complete = !failed;
            if common & DEVICE != 0 {
                info.dev = i32::from_ne_bytes(fields.take()?) as u64;
            }
            if common & TYPE != 0 {
                kind = Some(fields.word()?);
            }
            if common & MODIFIED != 0 {
                let seconds = fields.long()?;
                info.modified_ns = seconds
                    .saturating_mul(1_000_000_000)
                    .saturating_add(fields.long()?);
            }
            if common & ACCESS != 0 {
                info.mode = fields.word()? & 0o7777;
            }
            if common & FLAGS != 0 {
                complete &= fields.word()? & COMPRESSED == 0;
            }
            if common & FILE != 0 {
                info.ino = u64::from_ne_bytes(fields.take()?);
            }
            if file & LINKS != 0 {
                info.links = fields.word()? as u64;
            }
            if file & ALLOCATED != 0 {
                info.allocated = fields.long()?.max(0) as u64;
            }
            if file & LENGTH != 0 {
                info.size = fields.long()?.max(0) as u64;
            }
            if failed {
                return Ok((name, None, None));
            }
            match kind {
                Some(REGULAR) => info.mode |= 0o100000,
                Some(LINK) => info.mode |= 0o120000,
                _ => complete = false,
            }
            complete &= common & (DEVICE | MODIFIED | ACCESS | FLAGS | FILE)
                == DEVICE | MODIFIED | ACCESS | FLAGS | FILE
                && file & (LINKS | ALLOCATED | LENGTH) == LINKS | ALLOCATED | LENGTH
                && info.allocated.is_multiple_of(512);
            Ok((name, kind, complete.then_some(info)))
        }
        pub fn listing(dir: &Directory) -> Result<Vec<(OsString, Kind, Option<Info>)>> {
            // Earlier listings of this descriptor moved its offset.
            if unsafe { libc::lseek(dir.fd(), 0, libc::SEEK_SET) } < 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            let mut request = Request {
                count: 5,
                reserved: 0,
                common: RETURNED | NAME | DEVICE | TYPE | MODIFIED | ACCESS | FLAGS | FILE | ERROR,
                volume: 0,
                directory: 0,
                file: LINKS | ALLOCATED | LENGTH,
                fork: 0,
            };
            let mut buffer = vec![0u8; 1 << 16];
            let mut result = Vec::new();
            loop {
                let count = unsafe {
                    libc::getattrlistbulk(
                        dir.fd(),
                        (&mut request as *mut Request).cast(),
                        buffer.as_mut_ptr().cast(),
                        buffer.len(),
                        0,
                    )
                };
                if count < 0 {
                    return Err(std::io::Error::last_os_error().into());
                }
                if count == 0 {
                    break;
                }
                let mut rest = &buffer[..];
                for _ in 0..count {
                    let length = u32::from_ne_bytes(
                        rest.get(..4)
                            .context("目录批量读取结果不完整")?
                            .try_into()
                            .unwrap(),
                    ) as usize;
                    if length < 4 || length > rest.len() {
                        bail!("目录批量读取结果不完整");
                    }
                    let (entry, next) = rest.split_at(length);
                    rest = next;
                    let (name, kind, info) = record(entry)?;
                    if kind == Some(DIRECTORY) {
                        result.push((name, Kind::Directory, None));
                    } else if let Some(info) = info {
                        result.push((name, Kind::of(info), Some(info)));
                    } else if let Some(entry) = dir.described(name)? {
                        result.push(entry);
                    }
                }
            }
            result.sort_by(|a, b| a.0.cmp(&b.0));
            Ok(result)
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
        pub fn entries(&self) -> Result<Vec<(OsString, Kind)>> {
            self.names()?
                .into_iter()
                .map(|name| {
                    let kind = Kind::of(self.stat(&name)?);
                    Ok((name, kind))
                })
                .collect()
        }
        pub fn listing(&self) -> Result<Vec<(OsString, Kind, Option<Info>)>> {
            let mut result = Vec::new();
            for name in self.names()? {
                match self.stat(&name) {
                    Ok(info) if info.directory() => result.push((name, Kind::Directory, None)),
                    Ok(info) => result.push((name, Kind::of(info), Some(info))),
                    Err(e) if missing(&e) => {}
                    Err(e) => return Err(e),
                }
            }
            Ok(result)
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
/// `walk` for callers that only need names, directories and links: entry types
/// come from the directory listing instead of one metadata call per entry.
pub fn walk_kinds(
    root: &Path,
    progress: &Progress,
    visit: &mut impl FnMut(&Path, &std::ffi::OsStr, Kind) -> Result<Visit>,
) -> Result<()> {
    progress.check()?;
    fn descend(
        rel: &Path,
        dir: Directory,
        progress: &Progress,
        visit: &mut impl FnMut(&Path, &std::ffi::OsStr, Kind) -> Result<Visit>,
        depth: usize,
    ) -> Result<()> {
        if depth > 512 {
            bail!("目录层级过深");
        }
        for (name, kind) in dir.entries()? {
            name.to_str()
                .context("目录含无法安全写入报告的文件名；保留")?;
            progress.check()?;
            let relative = rel.join(&name);
            if visit(&relative, &name, kind)? == Visit::Descend && kind == Kind::Directory {
                // A stale type cannot redirect the walk: the open refuses links.
                let child = dir.child(&name)?;
                if !child.info()?.directory() {
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
    fn listed_kinds_match_metadata_and_refuse_links() {
        let tmp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(tmp.path()).unwrap();
        fs::create_dir_all(root.join("tree/dir/inner")).unwrap();
        fs::create_dir(root.join("outside")).unwrap();
        fs::write(root.join("outside/secret"), "private").unwrap();
        fs::write(root.join("tree/file"), "x").unwrap();
        symlink("../outside", root.join("tree/link")).unwrap();
        let dir = Directory::open(&root.join("tree")).unwrap();
        let expected: Vec<_> = dir
            .names()
            .unwrap()
            .into_iter()
            .map(|n| {
                let kind = Kind::of(dir.stat(&n).unwrap());
                (n, kind)
            })
            .collect();
        // Repeated listings share one descriptor offset and must still be complete.
        assert_eq!(dir.entries().unwrap(), expected);
        assert_eq!(dir.entries().unwrap(), expected);
        assert_eq!(dir.names().unwrap().len(), 3);
        let mut entries = Vec::new();
        walk_kinds(&root.join("tree"), &Progress::default(), &mut |r, _, k| {
            entries.push((r.to_path_buf(), k));
            Ok(Visit::Descend)
        })
        .unwrap();
        assert_eq!(
            entries,
            vec![
                (PathBuf::from("dir"), Kind::Directory),
                (PathBuf::from("dir/inner"), Kind::Directory),
                (PathBuf::from("file"), Kind::Other),
                (PathBuf::from("link"), Kind::Symlink),
            ]
        );
        let progress = Progress::default();
        progress.cancel();
        assert!(walk_kinds(&root, &progress, &mut |_, _, _| Ok(Visit::Descend)).is_err());
    }
    fn listed(path: &Path) -> Vec<(OsString, Kind, Option<Info>)> {
        let dir = Directory::open(path).unwrap();
        let listing = dir.listing().unwrap();
        let expected: Vec<_> = dir
            .names()
            .unwrap()
            .into_iter()
            .map(|name| {
                let info = dir.stat(&name).unwrap();
                let kind = Kind::of(info);
                (name, kind, (!info.directory()).then_some(info))
            })
            .collect();
        assert_eq!(listing, expected, "{}", path.display());
        assert_eq!(dir.listing().unwrap(), expected);
        assert_eq!(dir.names().unwrap().len(), expected.len());
        listing
    }
    #[test]
    fn bulk_listing_matches_one_stat_per_entry() {
        use std::os::unix::fs::PermissionsExt;
        let tmp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(tmp.path()).unwrap();
        fs::create_dir(root.join("dir")).unwrap();
        fs::write(root.join("empty"), "").unwrap();
        fs::write(root.join("data"), vec![b'x'; 70_000]).unwrap();
        fs::hard_link(root.join("data"), root.join("hard")).unwrap();
        File::create(root.join("sparse"))
            .unwrap()
            .set_len(1 << 24)
            .unwrap();
        fs::write(root.join("mode"), "x").unwrap();
        fs::set_permissions(root.join("mode"), fs::Permissions::from_mode(0o4751)).unwrap();
        symlink("data", root.join("short")).unwrap();
        symlink("x".repeat(900), root.join("long")).unwrap();
        symlink("dir", root.join("to-dir")).unwrap();
        let fifo = std::ffi::CString::new(root.join("fifo").as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(fifo.as_ptr(), 0o600) }, 0);
        // A resource fork only exists on macOS volumes; elsewhere this is a no-op.
        let _ = fs::write(root.join("data/..namedfork/rsrc"), vec![b'r'; 9000]);
        for index in 0..700 {
            fs::write(
                root.join(format!("many-{index:04}-{}", "n".repeat(120))),
                "x",
            )
            .unwrap();
        }
        let listing = listed(&root);
        assert_eq!(listing.len(), 710);
        assert!(listing.iter().any(|e| e.0 == "dir" && e.2.is_none()));
        assert!(
            listing
                .iter()
                .any(|e| e.0 == "to-dir" && e.1 == Kind::Symlink)
        );
        assert!(listed(&root.join("dir")).is_empty());
        // Read-only system directories: compressed files, links and special modes.
        // Not /dev: terminal devices change while the test prints.
        for system in ["/usr/bin", "/usr/lib", "/etc", "/Applications"] {
            let Ok(path) = fs::canonicalize(system) else {
                continue;
            };
            if path.to_str().is_some() {
                listed(&path);
            }
        }
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
