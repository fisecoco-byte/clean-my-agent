//! Mutating primitives always use pinned directory descriptors and no-clobber moves.
#[cfg(unix)]
use crate::fsutil;
use crate::{fsutil::Directory, progress::Progress};
#[cfg(unix)]
use anyhow::Context;
use anyhow::{Result, bail};
use std::{ffi::OsStr, fs::File, path::Path};

#[cfg(unix)]
mod platform {
    use super::*;
    use std::{
        ffi::CString,
        io::Write,
        os::{
            fd::{AsRawFd, FromRawFd},
            unix::{ffi::OsStrExt, fs::MetadataExt},
        },
        path::Component,
    };
    fn name(name: &OsStr) -> Result<CString> {
        if name.is_empty() || name == "." || name == ".." || name.as_bytes().contains(&b'/') {
            bail!("无效的目录项名称");
        }
        Ok(CString::new(name.as_bytes())?)
    }
    fn checked(result: i32) -> Result<()> {
        if result == 0 {
            Ok(())
        } else {
            Err(std::io::Error::last_os_error().into())
        }
    }
    pub fn directory(path: &Path, create: bool) -> Result<Directory> {
        fsutil::absolute(path)?;
        let mut current = Directory::open(Path::new("/"))?;
        for component in path.components() {
            if let Component::Normal(part) = component {
                if create {
                    let part = name(part)?;
                    let result = unsafe { libc::mkdirat(current.fd(), part.as_ptr(), 0o700) };
                    if result != 0
                        && std::io::Error::last_os_error().raw_os_error() != Some(libc::EEXIST)
                    {
                        checked(result)?;
                    }
                }
                current = current.child(part)?;
            }
        }
        Ok(current)
    }
    pub fn private(dir: &Directory) -> Result<()> {
        let s = dir.0.metadata()?;
        if s.uid() != unsafe { libc::getuid() } || s.mode() & 0o077 != 0 {
            bail!("暂存目录必须归当前用户所有，且仅当前用户可访问 (0700)");
        }
        Ok(())
    }
    pub fn lock(dir: &Directory) -> Result<()> {
        checked(unsafe { libc::flock(dir.fd(), libc::LOCK_EX | libc::LOCK_NB) })
            .context("操作已在执行或无法取得批次锁")
    }
    pub fn lock_file(path: &Path) -> Result<File> {
        let parent = directory(path.parent().context("缺少锁文件父路径")?, false)?;
        let part = name(path.file_name().context("缺少锁文件名称")?)?;
        let fd = unsafe {
            libc::openat(
                parent.fd(),
                part.as_ptr(),
                libc::O_RDWR | libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK,
            )
        };
        if fd < 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        let file = unsafe { File::from_raw_fd(fd) };
        if !file.metadata()?.is_file() {
            bail!("安装锁不是普通文件");
        }
        checked(unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) })
            .context("安装更新正在进行，保留资源")?;
        Ok(file)
    }
    pub fn mkdir(parent: &Directory, part: &OsStr) -> Result<()> {
        checked(unsafe { libc::mkdirat(parent.fd(), name(part)?.as_ptr(), 0o700) })
    }
    pub fn sync(dir: &Directory) -> Result<()> {
        checked(unsafe { libc::fsync(dir.fd()) })
    }
    pub fn unlink(parent: &Directory, part: &OsStr, dir: bool) -> Result<()> {
        checked(unsafe {
            libc::unlinkat(
                parent.fd(),
                name(part)?.as_ptr(),
                if dir { libc::AT_REMOVEDIR } else { 0 },
            )
        })
    }
    pub fn replace(parent: &Directory, from: &OsStr, to: &OsStr) -> Result<()> {
        checked(unsafe {
            libc::renameat(
                parent.fd(),
                name(from)?.as_ptr(),
                parent.fd(),
                name(to)?.as_ptr(),
            )
        })
    }
    pub fn exclusive(src: &Directory, from: &OsStr, dst: &Directory, to: &OsStr) -> Result<()> {
        let from = name(from)?;
        let to = name(to)?;
        #[cfg(target_os = "macos")]
        let result = unsafe {
            libc::renameatx_np(
                src.fd(),
                from.as_ptr(),
                dst.fd(),
                to.as_ptr(),
                libc::RENAME_EXCL,
            )
        };
        #[cfg(target_os = "linux")]
        let result = unsafe {
            libc::renameat2(
                src.fd(),
                from.as_ptr(),
                dst.fd(),
                to.as_ptr(),
                libc::RENAME_NOREPLACE,
            )
        };
        #[cfg(not(any(target_os = "macos", target_os = "linux")))]
        {
            bail!("此系统不支持安全还原目录");
        }
        #[cfg(any(target_os = "macos", target_os = "linux"))]
        checked(result)
    }
    pub fn link(src: &Directory, from: &OsStr, dst: &Directory, to: &OsStr) -> Result<()> {
        checked(unsafe {
            libc::linkat(
                src.fd(),
                name(from)?.as_ptr(),
                dst.fd(),
                name(to)?.as_ptr(),
                0,
            )
        })
    }
    pub fn write_exclusive(dir: &Directory, part: &OsStr, bytes: &[u8]) -> Result<()> {
        let fd = unsafe {
            libc::openat(
                dir.fd(),
                name(part)?.as_ptr(),
                libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0o600,
            )
        };
        if fd < 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        let mut file = unsafe { File::from_raw_fd(fd) };
        file.write_all(bytes)?;
        file.sync_all()?;
        Ok(())
    }
    pub fn empty_tree(
        parent: &Directory,
        part: &OsStr,
        device: u64,
        progress: &Progress,
    ) -> Result<()> {
        fn remove(
            parent: &Directory,
            part: &OsStr,
            device: u64,
            progress: &Progress,
            depth: usize,
        ) -> Result<()> {
            progress.check()?;
            if depth > 512 {
                bail!("目录层级过深，已停止");
            }
            let expected = parent.stat(part)?;
            let dir = parent.child(part)?;
            let root = dir.info()?;
            if root.dev != device || root.identity() != expected.identity() {
                bail!("目录被替换或包含其他挂载卷，已停止");
            }
            for name in dir.names()? {
                let info = dir.stat(&name)?;
                if info.dev != device {
                    bail!("目录包含其他挂载卷，已停止");
                }
                if info.directory() {
                    remove(&dir, &name, device, progress, depth + 1)?;
                } else {
                    unlink(&dir, &name, false)?;
                }
            }
            if parent.stat(part)?.identity() != root.identity() {
                bail!("清除期间目录已被替换");
            }
            unlink(parent, part, true)
        }
        remove(parent, part, device, progress, 0)
    }
}
#[cfg(windows)]
mod platform {
    use super::*;
    pub fn directory(_: &Path, _: bool) -> Result<Directory> {
        bail!("Windows 原生版仅盘点")
    }
    pub fn private(_: &Directory) -> Result<()> {
        bail!("Windows 原生版仅盘点")
    }
    pub fn lock(_: &Directory) -> Result<()> {
        bail!("Windows 原生版仅盘点")
    }
    pub fn lock_file(_: &Path) -> Result<File> {
        bail!("Windows 原生版仅盘点")
    }
    pub fn mkdir(_: &Directory, _: &OsStr) -> Result<()> {
        bail!("Windows 原生版仅盘点")
    }
    pub fn sync(_: &Directory) -> Result<()> {
        bail!("Windows 原生版仅盘点")
    }
    pub fn unlink(_: &Directory, _: &OsStr, _: bool) -> Result<()> {
        bail!("Windows 原生版仅盘点")
    }
    pub fn replace(_: &Directory, _: &OsStr, _: &OsStr) -> Result<()> {
        bail!("Windows 原生版仅盘点")
    }
    pub fn exclusive(_: &Directory, _: &OsStr, _: &Directory, _: &OsStr) -> Result<()> {
        bail!("Windows 原生版仅盘点")
    }
    pub fn link(_: &Directory, _: &OsStr, _: &Directory, _: &OsStr) -> Result<()> {
        bail!("Windows 原生版仅盘点")
    }
    pub fn write_exclusive(_: &Directory, _: &OsStr, _: &[u8]) -> Result<()> {
        bail!("Windows 原生版仅盘点")
    }
    pub fn empty_tree(_: &Directory, _: &OsStr, _: u64, _: &Progress) -> Result<()> {
        bail!("Windows 原生版仅盘点")
    }
}
pub use platform::*;

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::{
        fs,
        os::unix::fs::{PermissionsExt, symlink},
    };
    #[test]
    fn exclusive_restore_preserves_destination() {
        let tmp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(tmp.path()).unwrap();
        fs::create_dir(root.join("source")).unwrap();
        fs::create_dir(root.join("destination")).unwrap();
        let dir = directory(&root, false).unwrap();
        assert!(exclusive(&dir, OsStr::new("source"), &dir, OsStr::new("destination")).is_err());
        assert!(root.join("source").is_dir());
        assert!(root.join("destination").is_dir());
    }
    #[test]
    fn recursive_purge_unlinks_link_without_touching_target() {
        let tmp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(tmp.path()).unwrap();
        fs::create_dir(root.join("tree")).unwrap();
        fs::write(root.join("keep"), "data").unwrap();
        symlink("../keep", root.join("tree/link")).unwrap();
        let dir = directory(&root, false).unwrap();
        empty_tree(
            &dir,
            OsStr::new("tree"),
            dir.info().unwrap().dev,
            &Progress::default(),
        )
        .unwrap();
        assert_eq!(fs::read_to_string(root.join("keep")).unwrap(), "data");
    }
    #[test]
    fn parent_creation_never_follows_links() {
        let tmp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(tmp.path()).unwrap();
        fs::create_dir(root.join("outside")).unwrap();
        symlink("outside", root.join("link")).unwrap();
        assert!(directory(&root.join("link/new"), true).is_err());
        assert!(!root.join("outside/new").exists());
    }
    #[test]
    fn private_state_and_lock_enforced() {
        let tmp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(tmp.path()).unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        let first = directory(&root, false).unwrap();
        private(&first).unwrap();
        lock(&first).unwrap();
        let second = directory(&root, false).unwrap();
        assert!(lock(&second).is_err());
        fs::set_permissions(&root, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(private(&first).is_err());
    }
}
