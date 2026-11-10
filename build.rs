use sha2::{Digest, Sha256};
use std::{
    fs,
    path::{Path, PathBuf},
    process::Command,
};
fn collect(root: &Path, dir: &Path, files: &mut Vec<PathBuf>) {
    for entry in fs::read_dir(dir).expect("build input directory") {
        let path = entry.unwrap().path();
        if path.is_dir() {
            collect(root, &path, files);
        } else {
            files.push(path.strip_prefix(root).unwrap().to_path_buf());
        }
    }
}
fn main() {
    let root = PathBuf::from(std::env::var_os("CARGO_MANIFEST_DIR").unwrap());
    // Commits can change without changing Rust inputs (for example, docs-only
    // releases). Track Git's resolved metadata paths, including linked worktrees.
    for name in ["HEAD", "refs", "packed-refs"] {
        if let Ok(output) = Command::new("git")
            .args(["rev-parse", "--git-path", name])
            .current_dir(&root)
            .output()
            && output.status.success()
        {
            let path = root.join(String::from_utf8_lossy(&output.stdout).trim());
            if path.exists() {
                println!("cargo:rerun-if-changed={}", path.display());
            }
        }
    }
    let mut files = vec![
        PathBuf::from("Cargo.toml"),
        PathBuf::from("Cargo.lock"),
        PathBuf::from("build.rs"),
        PathBuf::from("assets/inventory.html"),
    ];
    for dir in ["rust", "assets/loading"] {
        println!("cargo:rerun-if-changed={dir}");
        collect(&root, &root.join(dir), &mut files);
    }
    files.sort();
    let mut hash = Sha256::new();
    for path in files {
        println!("cargo:rerun-if-changed={}", path.display());
        hash.update(path.to_string_lossy().replace('\\', "/").as_bytes());
        hash.update([0]);
        hash.update(fs::read(root.join(&path)).unwrap());
        hash.update([0]);
    }
    let revision = Command::new("git")
        .args(["rev-parse", "--short=12", "HEAD"])
        .current_dir(&root)
        .output()
        .ok()
        .filter(|r| r.status.success())
        .map(|r| String::from_utf8_lossy(&r.stdout).trim().to_string())
        .unwrap_or_else(|| "unknown".into());
    let rustc = Command::new(std::env::var_os("RUSTC").unwrap())
        .arg("--version")
        .output()
        .unwrap();
    println!("cargo:rustc-env=CMA_SOURCE_SHA256={:x}", hash.finalize());
    println!("cargo:rustc-env=CMA_REVISION={revision}");
    println!(
        "cargo:rustc-env=CMA_TARGET={}",
        std::env::var("TARGET").unwrap()
    );
    println!(
        "cargo:rustc-env=CMA_RUSTC={}",
        String::from_utf8_lossy(&rustc.stdout).trim()
    );
}
