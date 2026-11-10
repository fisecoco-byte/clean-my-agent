#![cfg(windows)]
use clean_my_agent::{fsutil, progress::Progress};
use std::{
    fs,
    io::{Seek, SeekFrom, Write},
    os::windows::io::AsRawHandle,
    process::Command,
};

#[test]
fn canonical_drive_paths_support_directory_and_metadata_reads() {
    let temp = tempfile::tempdir().unwrap();
    let root = fs::canonicalize(temp.path()).unwrap();
    let path = root.join("metadata.json");
    fs::write(&path, "fixture").unwrap();
    assert_eq!(fsutil::resolve(&root).unwrap(), root);
    assert_eq!(fsutil::resolve(&path).unwrap(), path);
    let directory = fsutil::Directory::open(&root).unwrap();
    assert!(directory.info().unwrap().directory());
    assert_eq!(fsutil::regular_text(&path, 100).unwrap(), "fixture");
}

#[test]
fn allocated_bytes_deduplicate_hardlinks_and_skip_junctions() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("scan");
    let outside = temp.path().join("outside");
    fs::create_dir(&root).unwrap();
    fs::create_dir(&outside).unwrap();
    let path = root.join("payload");
    fs::write(&path, vec![b'x'; 8193]).unwrap();
    fs::hard_link(&path, root.join("second-name")).unwrap();
    fs::write(outside.join("unrelated"), vec![b'y'; 1024 * 1024]).unwrap();
    let info = fsutil::stat(&path).unwrap();
    assert_ne!(info.ino, 0);
    assert!(info.links >= 2);
    assert_eq!(
        info.identity(),
        fsutil::stat(&root.join("second-name")).unwrap().identity()
    );
    let junction = root.join("junction");
    let output = Command::new("cmd")
        .args(["/C", "mklink", "/J"])
        .arg(&junction)
        .arg(&outside)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(fsutil::stat(&junction).unwrap().symlink());
    assert!(fsutil::Directory::open(&junction).is_err());
    assert_eq!(
        fsutil::group_size(&root, &Progress::default()).unwrap(),
        info.allocated.div_ceil(1024) * 1024
    );
    // Remove only the junction itself, never its target, before TempDir cleanup.
    fs::remove_dir(&junction).unwrap();
    assert!(outside.join("unrelated").exists());
}

#[test]
fn sparse_allocation_is_not_logical_length() {
    use windows_sys::Win32::System::{IO::DeviceIoControl, Ioctl::FSCTL_SET_SPARSE};
    let temp = tempfile::tempdir().unwrap();
    let mut parent = temp.path().to_path_buf();
    for n in 0..10 {
        parent.push(format!("long-component-{n}-abcdefghijklmno"));
    }
    fs::create_dir_all(&parent).unwrap();
    let path = parent.join("sparse-file");
    let mut file = fs::File::create(&path).unwrap();
    let mut returned = 0;
    assert_ne!(
        unsafe {
            DeviceIoControl(
                file.as_raw_handle(),
                FSCTL_SET_SPARSE,
                std::ptr::null(),
                0,
                std::ptr::null_mut(),
                0,
                &mut returned,
                std::ptr::null_mut(),
            )
        },
        0,
        "{}",
        std::io::Error::last_os_error()
    );
    file.set_len(64 * 1024 * 1024).unwrap();
    file.seek(SeekFrom::Start(1024 * 1024)).unwrap();
    file.write_all(b"fixture").unwrap();
    file.sync_all().unwrap();
    let info = fsutil::stat(&path).unwrap();
    assert_eq!(info.size, 64 * 1024 * 1024);
    assert!(info.allocated > 0 && info.allocated < info.size / 2);
}
