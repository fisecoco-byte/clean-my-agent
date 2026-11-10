use crate::{
    fsutil::{self, Directory, Info, Visit},
    progress::Progress,
};
use anyhow::{Result, bail};
use serde_json::{Value, json};
use std::{collections::HashMap, ffi::OsStr, path::Path};

#[derive(Default, Clone)]
pub struct DirectoryUsage {
    bytes: u64,
    links: HashMap<(u64, u64), u64>,
}
impl DirectoryUsage {
    pub fn observe(&mut self, s: Info) {
        if s.links > 1 && !s.directory() {
            self.links.insert(s.identity(), s.allocated);
        } else {
            self.bytes += s.allocated;
        }
    }
    pub fn merge(&mut self, other: &Self) {
        self.bytes += other.bytes;
        self.links.extend(other.links.iter().map(|(k, v)| (*k, *v)));
    }
    pub fn total(&self) -> u64 {
        (self.bytes + self.links.values().sum::<u64>()).div_ceil(1024) * 1024
    }
}
pub fn workspace_usage(
    path: &Path,
    measured: &std::collections::BTreeMap<std::path::PathBuf, (Value, DirectoryUsage)>,
    progress: &Progress,
) -> Result<u64> {
    let base = Directory::open(path)?.info()?;
    let mut usage = DirectoryUsage::default();
    usage.observe(base);
    let mut count = 0;
    fsutil::walk(path, progress, &mut |relative, _, _, s| {
        if s.dev != base.dev {
            return Ok(Visit::Skip);
        }
        count += 1;
        if count % 2048 == 0 {
            progress.update("统计工作树完整占用", None, None, fsutil::text(path))?;
        }
        if s.directory()
            && let Some((stamp, cached)) = measured.get(&path.join(relative))
            && s.stamp() == *stamp
        {
            usage.merge(cached);
            return Ok(Visit::Skip);
        }
        usage.observe(s);
        Ok(Visit::Descend)
    })?;
    if fsutil::stat(path)?.identity() != base.identity() {
        bail!("统计期间工作目录已替换");
    }
    Ok(usage.total())
}

#[derive(Clone, Copy, Debug)]
struct Attributes {
    private: u64,
    clone: Option<(u64, u64)>,
}
#[cfg(target_os = "macos")]
fn attributes(dir: &Directory, name: &OsStr, s: Info) -> Result<Option<Attributes>> {
    use std::{ffi::CString, os::unix::ffi::OsStrExt};
    // Fixed offsets match FSOPT_PACK_INVAL_ATTRS and returned attribute bitmaps.
    #[repr(C)]
    struct AttrList {
        count: u16,
        reserved: u16,
        common: u32,
        volume: u32,
        directory: u32,
        file: u32,
        extended: u32,
    }
    unsafe extern "C" {
        fn getattrlistat(
            fd: i32,
            path: *const libc::c_char,
            attrs: *const AttrList,
            buffer: *mut libc::c_void,
            size: usize,
            options: u64,
        ) -> i32;
    }
    let name = CString::new(name.as_bytes())?;
    let request = AttrList {
        count: 5,
        reserved: 0,
        common: 0x82000000,
        volume: 0,
        directory: 0,
        file: 0,
        extended: 0x1108 | 0x200,
    };
    let mut data = [0u8; 128];
    if unsafe {
        getattrlistat(
            dir.fd(),
            name.as_ptr(),
            &request,
            data.as_mut_ptr().cast(),
            data.len(),
            0x29,
        )
    } != 0
    {
        let error = std::io::Error::last_os_error();
        if matches!(
            error.raw_os_error(),
            Some(libc::ENOTSUP | libc::ENOSYS | libc::EINVAL)
        ) {
            return Ok(None);
        }
        return Err(error.into());
    }
    let u32at = |i| u32::from_ne_bytes(data[i..i + 4].try_into().unwrap());
    let u64at = |i| u64::from_ne_bytes(data[i..i + 8].try_into().unwrap());
    let returned = u32at(20);
    let private = i64::from_ne_bytes(data[32..40].try_into().unwrap());
    if u32at(0) < 60 || u32at(4) & 0x02000000 == 0 || returned & 8 == 0 {
        return Ok(None);
    }
    if u64at(24) != s.ino {
        bail!("测量期间文件已替换");
    }
    if private < 0 || private as u64 > s.allocated {
        return Ok(None);
    }
    let clone = if returned & request.extended == request.extended
        && u64at(48) & 0x40 != 0
        && u64at(40) != 0
        && u32at(56) > 1
    {
        Some((u64at(40), u32at(56) as u64))
    } else {
        None
    };
    Ok(Some(Attributes {
        private: private as u64,
        clone,
    }))
}
#[cfg(not(target_os = "macos"))]
fn attributes(_: &Directory, _: &OsStr, _: Info) -> Result<Option<Attributes>> {
    Ok(None)
}

type LinkAccounting = (u64, u64, u64, Option<Attributes>);
#[derive(Default)]
struct Accounting {
    allocated: u64,
    shared: u64,
    private: u64,
    unknown: u64,
    uncertain: u64,
    links: HashMap<(u64, u64), LinkAccounting>,
    clones: HashMap<u64, (u64, u64, u64, bool)>,
}
impl Accounting {
    fn add(&mut self, size: u64, attrs: Option<Attributes>, external: bool) -> Result<()> {
        self.allocated += size;
        if external {
            self.shared += size;
        } else if let Some(a) = attrs {
            self.shared += size - a.private;
            self.private += a.private;
            if a.clone.is_none() {
                self.uncertain += size - a.private;
            }
        } else {
            self.unknown += size;
        }
        if let Some(a) = attrs
            && let Some((cid, refs)) = a.clone
        {
            let group = self.clones.entry(cid).or_insert((0, refs, 0, false));
            if group.1 != refs {
                bail!("测量期间克隆引用数变化，请重新盘点");
            }
            group.0 += 1;
            group.2 = group.2.max(size - a.private);
            group.3 |= external;
        }
        Ok(())
    }
    fn observe(&mut self, dir: &Directory, name: &OsStr, s: Info) -> Result<()> {
        if s.allocated == 0 {
            return Ok(());
        }
        let hard = s.links > 1 && !s.directory();
        if hard && let Some(r) = self.links.get_mut(&s.identity()) {
            if (r.1, r.2) != (s.links, s.allocated) {
                bail!("测量期间硬链接变化，请重新盘点");
            }
            r.0 += 1;
            return Ok(());
        }
        let attrs = attributes(dir, name, s)?;
        if hard {
            self.links
                .insert(s.identity(), (1, s.links, s.allocated, attrs));
            Ok(())
        } else {
            self.add(s.allocated, attrs, false)
        }
    }
    fn finish(mut self) -> Result<Value> {
        for (_, (count, links, size, attrs)) in std::mem::take(&mut self.links) {
            if count > links {
                bail!("测量期间硬链接变化，请重新盘点");
            }
            self.add(size, attrs, count < links)?;
        }
        let mut upper = self.private + self.unknown + self.uncertain;
        for (count, refs, size, external) in self.clones.values() {
            if count > refs {
                bail!("测量期间克隆引用数变化，请重新盘点");
            }
            if count == refs && !*external {
                upper += size;
            }
        }
        Ok(
            json!({"directory_bytes":self.allocated,"known_shared_bytes":self.shared,
            "shared_bytes":if self.unknown==0{Some(self.shared)}else{None},"unknown_bytes":self.unknown,
            "reclaimable_min_bytes":self.private,"reclaimable_max_bytes":upper,
            "reclaimable_bytes":if self.private==upper{Some(self.private)}else{None}}),
        )
    }
}
pub fn measure(
    path: &Path,
    progress: &Progress,
    observer: &mut impl FnMut(&Path, Info) -> Result<()>,
) -> Result<(Value, i64, bool)> {
    let root = Directory::open(path)?;
    let initial = root.info()?;
    let mut totals = Accounting::default();
    let mut newest = initial.modified_ns;
    let mut nested = false;
    let mut visited = 0;
    totals.observe(&root, OsStr::new("."), initial)?;
    fsutil::walk(path, progress, &mut |relative, dir, name, s| {
        if s.dev != initial.dev {
            bail!("包含其他挂载卷，未测量");
        }
        newest = newest.max(s.modified_ns);
        nested |= name == ".git";
        observer(relative, s)?;
        totals.observe(dir, name, s)?;
        visited += 1;
        if visited % 2048 == 0 {
            progress.update(
                "测量依赖目录",
                None,
                None,
                format!("{visited} 项 · {}", path.display()),
            )?;
        }
        Ok(Visit::Descend)
    })?;
    if initial.identity() != fsutil::stat(path)?.identity() {
        bail!("测量期间依赖目录已替换");
    }
    Ok((totals.finish()?, newest, nested))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unknown_and_external_hardlink_bounds() {
        let mut a = Accounting::default();
        a.add(4096, None, false).unwrap();
        a.add(8192, None, true).unwrap();
        let result = a.finish().unwrap();
        assert_eq!(result["directory_bytes"], 12288);
        assert_eq!(result["known_shared_bytes"], 8192);
        assert_eq!(result["unknown_bytes"], 4096);
        assert_eq!(result["reclaimable_min_bytes"], 0);
        assert_eq!(result["reclaimable_max_bytes"], 4096);
        assert!(result["shared_bytes"].is_null());
    }
    #[test]
    fn complete_clones_allow_range_not_double_count() {
        let mut a = Accounting::default();
        for _ in 0..2 {
            a.add(
                8192,
                Some(Attributes {
                    private: 0,
                    clone: Some((100, 2)),
                }),
                false,
            )
            .unwrap();
        }
        let result = a.finish().unwrap();
        assert_eq!(result["directory_bytes"], 16384);
        assert_eq!(result["reclaimable_min_bytes"], 0);
        assert_eq!(result["reclaimable_max_bytes"], 8192);
        assert!(result["reclaimable_bytes"].is_null());
    }
    #[test]
    fn incomplete_clone_has_no_shared_reclaim() {
        let mut a = Accounting::default();
        a.add(
            8192,
            Some(Attributes {
                private: 4096,
                clone: Some((100, 2)),
            }),
            false,
        )
        .unwrap();
        let result = a.finish().unwrap();
        assert_eq!(result["reclaimable_bytes"], 4096);
    }
    #[cfg(unix)]
    #[test]
    fn workspace_summary_deduplicates_across_artifacts() {
        use std::fs;
        let temp = tempfile::tempdir().unwrap();
        let root = fs::canonicalize(temp.path()).unwrap();
        for name in ["first", "second"] {
            fs::create_dir(root.join(name)).unwrap();
        }
        fs::write(root.join("first/file"), vec![1u8; 10000]).unwrap();
        fs::hard_link(root.join("first/file"), root.join("second/linked")).unwrap();
        let mut measured = std::collections::BTreeMap::new();
        for name in ["first", "second"] {
            let path = root.join(name);
            let info = fsutil::stat(&path).unwrap();
            let mut usage = DirectoryUsage::default();
            usage.observe(info);
            fsutil::walk(&path, &Progress::default(), &mut |_, _, _, s| {
                usage.observe(s);
                Ok(Visit::Descend)
            })
            .unwrap();
            measured.insert(path, (info.stamp(), usage));
        }
        assert_eq!(
            workspace_usage(&root, &measured, &Progress::default()).unwrap(),
            fsutil::group_size(&root, &Progress::default()).unwrap()
        );
    }
}
