use crate::{
    fsutil::{self, Info, Visit},
    progress::Progress,
};
use anyhow::{Context, Result, bail};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
};

pub const TARGETS: &[&str] = &[
    "node_modules",
    "target",
    "build",
    "dist",
    "venv",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    ".nox",
    ".ruff_cache",
    ".gradle",
    ".terragrunt-cache",
    "__pycache__",
    ".next",
    ".nuxt",
    ".output",
    "vendor",
    "bin",
    "obj",
    ".turbo",
    ".parcel-cache",
    ".dart_tool",
    ".zig-cache",
    "zig-out",
    ".angular",
    ".svelte-kit",
    ".astro",
    "coverage",
    "DerivedData",
    "Pods",
    ".cxx",
    ".expo",
    ".build",
];
pub const SWIFT_OUTPUTS: &[&str] = &[
    "out",
    "index-build",
    "arm64-apple-macosx",
    "x86_64-apple-macosx",
    "aarch64-unknown-linux-gnu",
    "x86_64-unknown-linux-gnu",
];
pub const PYTHON: &[&str] = &[
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "setup.cfg",
    "Pipfile",
];
pub const JVM: &[&str] = &["pom.xml", "build.gradle", "build.gradle.kts"];
pub const INDICATORS: &[&str] = &[
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "composer.json",
    "pubspec.yaml",
    "Package.swift",
    "Podfile",
    "build.zig",
    "build.zig.zon",
    "terragrunt.hcl",
    "CMakeLists.txt",
    "Makefile",
    "Gemfile",
];
pub fn tagged(path: &Path) -> bool {
    let marker = path.join("CACHEDIR.TAG");
    // Most directories have no marker. This negative-only probe avoids reopening
    // every ancestor for each miss. A positive result still requires the complete
    // no-follow, bounded and identity-checked read below; it never authorizes a move.
    if !fs::symlink_metadata(&marker).is_ok_and(|m| m.is_file() && m.len() <= 4096) {
        return false;
    }
    fsutil::regular_text(&marker, 4096)
        .is_ok_and(|s| s.lines().next() == Some("Signature: 8a477f597d28d172789f06886806bc55"))
}

pub fn target(path: &Path) -> bool {
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    TARGETS.contains(&name.as_ref())
        || (path
            .parent()
            .and_then(|p| p.file_name())
            .is_some_and(|p| p == ".build")
            && SWIFT_OUTPUTS.contains(&name.as_ref()))
        || tagged(path)
}
pub fn files(parent: &Path, patterns: &[&str]) -> Vec<PathBuf> {
    let mut result = BTreeSet::new();
    for pattern in patterns {
        if let Some((prefix, suffix)) = pattern.split_once('*') {
            if let Ok(entries) = fs::read_dir(parent) {
                for entry in entries.flatten() {
                    let name = entry.file_name().to_string_lossy().into_owned();
                    let (suffix, child) = suffix
                        .split_once('/')
                        .map_or((suffix, None), |(a, b)| (a, Some(b)));
                    if name.starts_with(prefix) && name.ends_with(suffix) {
                        let p = child.map_or(entry.path(), |child| entry.path().join(child));
                        if fsutil::stat(&p).is_ok_and(|s| s.regular()) {
                            result.insert(p);
                        }
                    }
                }
            }
        } else {
            let p = parent.join(pattern);
            if fsutil::stat(&p).is_ok_and(|s| s.regular()) {
                result.insert(p);
            }
        }
    }
    result.into_iter().collect()
}
pub fn context(path: &Path, repo: &Path) -> PathBuf {
    let patterns: Vec<_> = INDICATORS
        .iter()
        .copied()
        .chain([
            "*.csproj",
            "*.fsproj",
            "*.vbproj",
            "*.xcodeproj/project.pbxproj",
        ])
        .collect();
    for parent in path.parent().into_iter().flat_map(Path::ancestors) {
        if !parent.starts_with(repo) {
            break;
        }
        if !files(parent, &patterns).is_empty() {
            return parent.into();
        }
    }
    repo.into()
}
pub fn describe(path: &Path, project: &Path, original: &Path) -> Result<Value> {
    let name = original.file_name().unwrap_or_default().to_string_lossy();
    let have = |p: &[&str]| files(project, p);
    let (mut language, mut category, mut rebuild) = ("", "构建产物", "");
    let mut proof = Vec::new();
    match name.as_ref() {
        "node_modules" => {
            language = "JavaScript / TypeScript";
            category = "项目依赖";
            proof = have(&["package.json"]);
            rebuild = "按锁文件重新安装依赖";
        }
        "venv" | ".venv" | ".tox" | ".nox" | "__pycache__" | ".pytest_cache" | ".mypy_cache"
        | ".ruff_cache" => {
            language = "Python";
            proof = have(PYTHON);
            category = if ["venv", ".venv", ".tox", ".nox"].contains(&name.as_ref()) {
                "虚拟环境"
            } else {
                "工具缓存"
            };
            rebuild = if category == "虚拟环境" {
                "按项目依赖清单重建环境"
            } else {
                "下次运行工具时重建"
            };
            if ["venv", ".venv"].contains(&name.as_ref()) {
                fsutil::regular_text(&path.join("pyvenv.cfg"), 65536)?;
            }
        }
        "target" => {
            proof = have(&["Cargo.toml"]);
            if proof.is_empty() {
                proof = have(&["pom.xml"]);
            }
            language = if have(&["Cargo.toml"]).is_empty() {
                "Java / Maven"
            } else {
                "Rust"
            };
            rebuild = if language == "Rust" {
                "cargo build"
            } else {
                "mvn package"
            };
        }
        "bin" | "obj" => {
            proof = have(&["*.csproj", "*.fsproj", "*.vbproj"]);
            language = ".NET";
            rebuild = "dotnet build";
            if name == "bin" && !["Debug", "Release"].iter().any(|n| path.join(n).is_dir()) {
                bail!("bin 未发现 .NET Debug / Release 产物；保留");
            }
        }
        "vendor" => {
            language = "PHP / Composer";
            category = "项目依赖";
            proof = have(&["composer.json"]);
            rebuild = "composer install";
            if have(&["composer.lock"]).is_empty()
                || !path.join("composer/installed.json").is_file()
            {
                bail!("仅接受有锁文件和安装记录的 Composer vendor；Go / Ruby vendor 保留");
            }
            fsutil::regular_text(&project.join("composer.lock"), 8 * 1024 * 1024)?;
            let _: Value = serde_json::from_str(&fsutil::regular_text(
                &path.join("composer/installed.json"),
                8 * 1024 * 1024,
            )?)?;
        }
        "Pods" => {
            language = "CocoaPods";
            category = "项目依赖";
            proof = have(&["Podfile"]);
            if have(&["Podfile.lock"]).is_empty() || !path.join("Manifest.lock").is_file() {
                bail!("Pods 缺少 Podfile.lock / Manifest.lock，保留");
            }
            if fsutil::regular_text(&project.join("Podfile.lock"), 8 * 1024 * 1024)?
                != fsutil::regular_text(&path.join("Manifest.lock"), 8 * 1024 * 1024)?
            {
                bail!("Pods 安装记录与锁文件不一致，保留");
            }
            rebuild = "pod install";
        }
        ".gradle" => {
            language = "Java / Gradle";
            category = "工具缓存";
            proof = have(JVM);
            rebuild = "重新运行 Gradle 构建";
        }
        ".terragrunt-cache" => {
            language = "Terragrunt";
            category = "工具缓存";
            proof = have(&["terragrunt.hcl"]);
            rebuild = "重新运行 Terragrunt";
        }
        ".zig-cache" | "zig-out" => {
            language = "Zig";
            proof = have(&["build.zig", "build.zig.zon"]);
            rebuild = "zig build";
        }
        ".dart_tool" => {
            language = "Dart / Flutter";
            proof = have(&["pubspec.yaml"]);
            rebuild = "dart pub get / flutter pub get";
        }
        "DerivedData" => {
            language = "Xcode";
            proof = have(&["*.xcodeproj/project.pbxproj", "Package.swift"]);
            rebuild = "重新构建 Xcode 项目";
        }
        ".cxx" => {
            language = "Android / CMake";
            proof = have(&[
                "pom.xml",
                "build.gradle",
                "build.gradle.kts",
                "CMakeLists.txt",
            ]);
            rebuild = "重新执行 Android / NDK 构建";
        }
        "build" | "dist" | "coverage" => {
            let choices: Vec<(&str, &[&str])> = vec![
                ("JavaScript / TypeScript", &["package.json"]),
                ("Python", PYTHON),
                ("Java / Gradle", JVM),
                ("Dart / Flutter", &["pubspec.yaml"]),
                ("C / C++", &["CMakeLists.txt", "Makefile"]),
                ("Ruby", &["Gemfile"]),
                ("Go", &["go.mod"]),
                ("Rust", &["Cargo.toml"]),
                ("SwiftPM", &["Package.swift"]),
            ];
            for (lang, patterns) in choices {
                language = lang;
                proof = have(patterns);
                if !proof.is_empty() {
                    break;
                }
            }
            rebuild = "按项目构建或测试命令重新生成";
        }
        ".next" | ".nuxt" | ".output" | ".turbo" | ".parcel-cache" | ".angular" | ".svelte-kit"
        | ".astro" | ".expo" => {
            language = "JavaScript / TypeScript";
            proof = have(&["package.json"]);
            rebuild = "重新运行对应框架构建";
        }
        _ => {
            if name == ".build"
                || (original
                    .parent()
                    .and_then(|p| p.file_name())
                    .is_some_and(|p| p == ".build")
                    && SWIFT_OUTPUTS.contains(&name.as_ref()))
            {
                language = "SwiftPM";
                proof = have(&["Package.swift"]);
                rebuild = "swift build";
                if name == ".build"
                    && [
                        "checkouts",
                        "repositories",
                        "artifacts",
                        "workspace-state.json",
                    ]
                    .iter()
                    .chain(SWIFT_OUTPUTS)
                    .any(|p| path.join(p).exists())
                {
                    bail!("SwiftPM 依赖与状态保留；请选择展开的编译输出目录");
                }
            } else if tagged(path) {
                language = "CACHEDIR.TAG";
                category = "声明缓存";
                proof = have(INDICATORS);
                rebuild = "由生成该缓存的工具重建";
            }
        }
    }
    if proof.is_empty() {
        bail!("缺少匹配的项目清单，目录名称不足以证明可重建");
    }
    Ok(
        json!({"ecosystem":language,"artifact":name,"artifact_category":category,"project":project,"evidence":proof,"rebuild":rebuild}),
    )
}

struct Installation {
    path: PathBuf,
    project: PathBuf,
    evidence: Vec<(PathBuf, Value)>,
    changed: Option<String>,
    marker_ns: Option<i64>,
    result: Value,
}
impl Installation {
    fn new(path: &Path, project: &Path) -> Self {
        let mut this = Self {
            path: path.into(),
            project: project.into(),
            evidence: Vec::new(),
            changed: None,
            marker_ns: None,
            result: json!({"state":"unknown","manager":null,"marker":null,"reason":"缺少可验证的安装记录（Bun 等未支持的格式保留）"}),
        };
        if this.collect().is_err() {
            this.result["state"] = json!("unknown");
            this.result["reason"] = json!("安装记录、锁文件或项目清单无法完整验证");
        }
        this
    }
    fn read(&mut self, path: PathBuf) -> Result<(String, Info)> {
        let content = fsutil::regular_text(&path, 8 * 1024 * 1024)?;
        let s = fsutil::stat(&path)?;
        self.evidence.push((path, s.stamp()));
        Ok((content, s))
    }
    fn collect(&mut self) -> Result<()> {
        let (manifest, _) = self.read(self.project.join("package.json"))?;
        if !serde_json::from_str::<Value>(&manifest)?.is_object() {
            bail!("package.json 格式无效");
        }
        let pairs = [
            ("npm", "package-lock.json", ".package-lock.json"),
            ("npm", "npm-shrinkwrap.json", ".package-lock.json"),
            ("pnpm", "pnpm-lock.yaml", ".modules.yaml"),
            ("yarn", "yarn.lock", ".yarn-integrity"),
            ("yarn", "yarn.lock", ".yarn-state.yml"),
        ];
        let mut found = Vec::new();
        let modules = regex::Regex::new(r"(?m)^layoutVersion:\s*[1-9][0-9]*\s*$")?;
        let metadata = regex::Regex::new(r"(?m)^__metadata:\s*$")?;
        let yarn_version = regex::Regex::new(r"(?m)^\s+version:\s*[0-9]+\s*$")?;
        for (manager, lock, record) in pairs {
            let marker = self.path.join(record);
            let lock = self.project.join(lock);
            if !marker.exists() || !lock.exists() {
                continue;
            }
            let (text, s) = self.read(marker.clone())?;
            self.read(lock)?;
            let valid = match record {
                ".package-lock.json" => {
                    let v: Value = serde_json::from_str(&text)?;
                    matches!(v["lockfileVersion"].as_i64(), Some(2 | 3))
                        && v["packages"].is_object()
                }
                ".modules.yaml" => modules.is_match(&text),
                ".yarn-integrity" => {
                    serde_json::from_str::<Value>(&text)?["topLevelPatterns"].is_array()
                }
                _ => metadata.is_match(&text) && yarn_version.is_match(&text),
            };
            if !valid {
                bail!("安装记录格式不受支持");
            }
            found.push((s.modified_ns, manager, marker));
        }
        if found.is_empty() {
            return Ok(());
        }
        if found.iter().map(|x| x.1).collect::<BTreeSet<_>>().len() != 1 {
            bail!("存在多种包管理器安装记录");
        }
        found.sort();
        let (stamp, manager, marker) = &found[0];
        self.marker_ns = Some(*stamp);
        self.result = json!({"state":"verified","manager":manager,"marker":marker,"reason":"安装记录之后未发现更新的文件；未校验包内容哈希"});
        for (path, value) in &self.evidence {
            if path.parent() == Some(self.project.as_path())
                && value[3].as_i64().is_some_and(|v| v > *stamp)
            {
                self.changed = Some(fsutil::text(path));
                break;
            }
        }
        Ok(())
    }
    fn observe(&mut self, relative: &Path, s: Info) {
        if self.result["state"] != "verified"
            || self.changed.is_some()
            || relative.starts_with(".cache")
        {
            return;
        }
        if !s.directory() && self.marker_ns.is_some_and(|t| s.modified_ns > t) {
            self.changed = Some(fsutil::text(&self.path.join(relative)));
        }
    }
    fn finish(mut self) -> Value {
        if self.result["state"] == "verified" {
            if self.evidence.iter().any(|(p, s)| {
                fsutil::stat(p).map_or(true, |info| info.symlink() || info.stamp() != *s)
            }) {
                self.result["state"] = json!("unknown");
                self.result["reason"] = json!("检查期间安装证据已变化");
            } else if let Some(changed) = self.changed {
                self.result["state"] = json!("modified");
                self.result["reason"] = json!(format!("安装后存在改动：{changed}"));
            }
        }
        self.result
    }
}
pub struct Evidence {
    path: PathBuf,
    original: PathBuf,
    pub detail: Option<Value>,
    reason: Option<String>,
    stamps: Vec<(PathBuf, Value)>,
    js: Option<Installation>,
}
impl Evidence {
    pub fn new(path: &Path, project: &Path, original: &Path) -> Self {
        let mut result = Self {
            path: path.into(),
            original: original.into(),
            detail: None,
            reason: None,
            stamps: Vec::new(),
            js: None,
        };
        if original.file_name().is_some_and(|n| n == "node_modules") {
            result.js = Some(Installation::new(path, project));
            return result;
        }
        let detail = describe(path, project, original).and_then(|v| {
            for name in v["evidence"].as_array().context("缺少证据")? {
                let p = Path::new(name.as_str().context("无效证据路径")?);
                fsutil::regular_text(p, 8 * 1024 * 1024)?;
                result.stamps.push((p.into(), fsutil::stat(p)?.stamp()));
            }
            Ok(v)
        });
        match detail {
            Ok(value) => result.detail = Some(value),
            Err(e) => result.reason = Some(e.to_string()),
        }
        result
    }
    pub fn observe(&mut self, relative: &Path, s: Info) -> Result<()> {
        if let Some(js) = self.js.as_mut() {
            js.observe(relative, s);
            return Ok(());
        }
        let name = relative.file_name().unwrap_or_default().to_string_lossy();
        let extension = relative.extension().unwrap_or_default().to_string_lossy();
        let mut private_pem = extension == "pem";
        if private_pem
            && s.regular()
            && let Ok(text) = fsutil::regular_text(&self.path.join(relative), 8 * 1024 * 1024)
        {
            let pattern = regex::Regex::new(r"-----BEGIN ([A-Z0-9 ]+)-----")?;
            let blocks: Vec<_> = pattern.captures_iter(&text).collect();
            private_pem = blocks.is_empty() || blocks.iter().any(|c| &c[1] != "CERTIFICATE");
        }
        if private_pem
            || name == ".git"
            || ["-keypair.json", ".tfstate", ".tfstate.backup", ".key"]
                .iter()
                .any(|s| name.ends_with(s))
            || name.starts_with(".env")
            || ["id_rsa", "id_ed25519", "terraform.tfstate"].contains(&name.as_ref())
        {
            self.reason = Some(format!(
                "包含源码仓库、凭据或持久状态；保留：{}",
                relative.display()
            ));
        }
        if self
            .original
            .file_name()
            .is_some_and(|n| n == "__pycache__")
            && !s.directory()
            && !["pyc", "pyo"].contains(&extension.as_ref())
        {
            self.reason = Some("__pycache__ 中含非字节码文件；保留".into());
        }
        if !s.directory() && !s.regular() && !s.symlink() {
            self.reason = Some("包含特殊文件；保留".into());
        }
        Ok(())
    }
    pub fn finish(mut self) -> Value {
        if let Some(js) = self.js {
            return js.finish();
        }
        for (path, stamp) in self.stamps {
            match fsutil::stat(&path) {
                Ok(info) if info.stamp() == stamp => (),
                _ => self.reason = Some("盘点期间项目清单发生变化".into()),
            }
        }
        json!({"state":if self.reason.is_some(){"unknown"}else{"verified"},"manager":null,"marker":null,"reason":self.reason.unwrap_or_else(||"项目类型、Git 保护和生成目录规则已核对；不保证完整内容哈希一致".into())})
    }
}
pub fn require_evidence(
    path: &Path,
    original: &Path,
    project: &Path,
    progress: &Progress,
) -> Result<()> {
    let mut evidence = Evidence::new(path, project, original);
    let dev = fsutil::stat(path)?.dev;
    fsutil::walk(path, progress, &mut |rel, _, name, s| {
        if name == ".git" {
            bail!("包含嵌套 Git 仓库，保留");
        }
        if s.dev != dev {
            bail!("包含其他挂载卷，保留");
        }
        evidence.observe(rel, s)?;
        Ok(Visit::Descend)
    })?;
    let result = evidence.finish();
    if result["state"] != "verified" {
        bail!("{}", result["reason"].as_str().unwrap_or("证据不完整"));
    }
    Ok(())
}
fn python_repr(value: &str) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut result = quote.to_string();
    for c in value.chars() {
        match c {
            '\\' => result.push_str("\\\\"),
            '\n' => result.push_str("\\n"),
            '\r' => result.push_str("\\r"),
            '\t' => result.push_str("\\t"),
            c if c == quote => {
                result.push('\\');
                result.push(c)
            }
            c if !fsutil::printable(c) => {
                if c as u32 <= 255 {
                    result.push_str(&format!("\\x{:02x}", c as u32));
                } else {
                    if c as u32 <= 65535 {
                        result.push_str(&format!("\\u{:04x}", c as u32));
                    } else {
                        result.push_str(&format!("\\U{:08x}", c as u32));
                    }
                }
            }
            c => result.push(c),
        }
    }
    result.push(quote);
    result
}
pub fn tree_digest(path: &Path, progress: &Progress) -> Result<String> {
    let dev = fsutil::stat(path)?.dev;
    let mut digest = Sha256::new();
    fn walk(
        path: &Path,
        relative: &Path,
        dev: u64,
        digest: &mut Sha256,
        progress: &Progress,
    ) -> Result<()> {
        let dir = fsutil::Directory::open(path)?;
        let names = dir.names()?;
        let mut children = Vec::new();
        for name in names {
            progress.check()?;
            let s = dir.stat(&name)?;
            if s.dev != dev {
                bail!("包含其他挂载卷");
            }
            let rel = relative.join(&name);
            digest.update(
                format!(
                    "({}, {}, {}, {}, {})",
                    python_repr(&fsutil::text(&rel)),
                    s.mode,
                    s.ino,
                    s.size,
                    s.modified_ns
                )
                .as_bytes(),
            );
            if s.directory() {
                children.push((path.join(name), rel));
            }
        }
        for (child, rel) in children {
            walk(&child, &rel, dev, digest, progress)?;
        }
        Ok(())
    }
    walk(path, Path::new(""), dev, &mut digest, progress)?;
    Ok(format!("{:x}", digest.finalize()))
}

#[cfg(test)]
mod marker_tests {
    use super::*;
    const SIGNATURE: &str = "Signature: 8a477f597d28d172789f06886806bc55\n";

    #[test]
    fn cache_marker_requires_a_bounded_regular_file_with_signature() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        assert!(!tagged(&root));
        let marker = root.join("CACHEDIR.TAG");
        fs::write(&marker, SIGNATURE).unwrap();
        assert!(tagged(&root));
        fs::write(&marker, "unrecognized marker").unwrap();
        assert!(!tagged(&root));
        fs::write(&marker, format!("{SIGNATURE}{}", "x".repeat(4096))).unwrap();
        assert!(!tagged(&root));
    }

    #[test]
    #[cfg(unix)]
    fn cache_marker_probe_does_not_accept_linked_files_or_parents() {
        use std::os::unix::fs::symlink;
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().canonicalize().unwrap();
        let actual = root.join("actual");
        let linked_file = root.join("linked-file");
        fs::create_dir(&actual).unwrap();
        fs::create_dir(&linked_file).unwrap();
        fs::write(actual.join("CACHEDIR.TAG"), SIGNATURE).unwrap();
        symlink(
            actual.join("CACHEDIR.TAG"),
            linked_file.join("CACHEDIR.TAG"),
        )
        .unwrap();
        symlink(&actual, root.join("linked-parent")).unwrap();
        assert!(!tagged(&linked_file));
        assert!(!tagged(&root.join("linked-parent")));
        assert!(tagged(&actual));
    }
}
