#![cfg(unix)]
use clean_my_agent::{
    fsutil,
    policy::{Layout, Policy},
    progress::Progress,
    worktrees,
};
use serde_json::json;
use std::{fs, path::Path, process::Command};
static ENVIRONMENT_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn git(path: &Path, args: &[&str]) {
    let output = Command::new("git")
        .arg("-C")
        .arg(path)
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
fn commit(repo: &Path) {
    git(repo, &["add", "."]);
    git(
        repo,
        &[
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "fixture",
        ],
    );
}
struct Environment(Vec<(String, Option<std::ffi::OsString>)>);
impl Drop for Environment {
    fn drop(&mut self) {
        for (k, v) in &self.0 {
            unsafe {
                if let Some(v) = v {
                    std::env::set_var(k, v);
                } else {
                    std::env::remove_var(k);
                }
            }
        }
    }
}

#[test]
fn worktree_checks_revalidate_and_preserve_user_changes() {
    let _guard = ENVIRONMENT_LOCK.lock().unwrap();
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path().canonicalize().unwrap();
    let mut environment = Environment(vec![]);
    for (key, value) in [
        ("HOME", home.clone()),
        ("CODEX_HOME", home.join(".codex")),
        ("CLAUDE_CONFIG_DIR", home.join(".claude")),
        ("T3_HOME", home.join(".t3")),
        ("XDG_CONFIG_HOME", home.join("config")),
        ("XDG_STATE_HOME", home.join("state")),
        ("XDG_CACHE_HOME", home.join("cache")),
    ] {
        environment.0.push((key.into(), std::env::var_os(key)));
        unsafe {
            std::env::set_var(key, value);
        }
    }
    let layout = Layout::load().unwrap();
    let policy = Policy::load(None, None).unwrap();
    let progress = Progress::default();
    let main = home.join("main");
    fs::create_dir(&main).unwrap();
    git(&main, &["init", "-q"]);
    fs::write(main.join("tracked"), "original").unwrap();
    commit(&main);
    let target = home.join(".codex/worktrees/linked");
    fs::create_dir_all(target.parent().unwrap()).unwrap();
    git(
        &main,
        &[
            "worktree",
            "add",
            "-q",
            "--detach",
            target.to_str().unwrap(),
        ],
    );
    let selected =
        json!({"path":target,"agent":"codex","stamp":fsutil::stat(&target).unwrap().stamp()});
    let initial = worktrees::inspect(&layout, &policy, &selected, &progress).unwrap();
    assert_eq!(initial["can_delete"], true, "{initial}");
    fs::write(target.join("tracked"), "unsaved edit").unwrap();
    let dirty = worktrees::inspect(&layout, &policy, &selected, &progress).unwrap();
    assert_eq!(dirty["can_delete"], false);
    assert!(worktrees::apply(&layout, &policy, &initial, false, &progress).is_err());
    assert_eq!(
        fs::read_to_string(target.join("tracked")).unwrap(),
        "unsaved edit"
    );
    git(&target, &["restore", "tracked"]);
    let state = layout.roots["codex"].join(".codex-global-state.json");
    fs::write(
        &state,
        json!({"active-workspace-roots":[target]}).to_string(),
    )
    .unwrap();
    assert_eq!(
        worktrees::inspect(&layout, &policy, &selected, &progress).unwrap()["can_delete"],
        false
    );
    fs::remove_file(state).unwrap();
    fs::write(target.join(".gitignore"), ".env\n").unwrap();
    commit(&target);
    fs::write(target.join(".env"), "fixture-secret").unwrap();
    assert_eq!(
        worktrees::inspect(&layout, &policy, &selected, &progress).unwrap()["can_delete"],
        false
    );
    fs::remove_file(target.join(".env")).unwrap();
    // Detached commits must be explicitly kept by a reference.
    let detached = worktrees::inspect(&layout, &policy, &selected, &progress).unwrap();
    assert_eq!(detached["can_delete"], false);
    git(&target, &["branch", "kept-fixture"]);
    let ready = worktrees::inspect(&layout, &policy, &selected, &progress).unwrap();
    assert_eq!(ready["can_delete"], true, "{ready}");
    let done = worktrees::apply(&layout, &policy, &ready, false, &progress).unwrap();
    assert_eq!(done["status"], "succeeded", "{done}");
    assert!(!target.exists());
    git(&main, &["rev-parse", "--verify", "kept-fixture"]);
    if cfg!(target_os = "macos") {
        let standalone = home.join(".codex/worktrees/standalone é");
        fs::create_dir(&standalone).unwrap();
        git(&standalone, &["init", "-q"]);
        fs::write(standalone.join("tracked"), "standalone").unwrap();
        commit(&standalone);
        let row = json!({"path":standalone,"agent":"codex"});
        let preview = worktrees::inspect(&layout, &policy, &row, &progress).unwrap();
        assert_eq!(preview["can_trash"], true, "{preview}");
        assert_eq!(preview["can_delete"], false);
        let done = worktrees::apply(&layout, &policy, &preview, true, &progress).unwrap();
        assert_eq!(done["status"], "succeeded", "{done}");
        let trashed = Path::new(done["trash_path"].as_str().unwrap());
        assert!(!standalone.exists());
        assert_eq!(
            fs::read_to_string(trashed.join("tracked")).unwrap(),
            "standalone"
        );
        fs::rename(trashed, &standalone).unwrap();
        git(&standalone, &["rev-parse", "--verify", "HEAD"]);
    }
}

#[test]
fn native_session_actions_verify_results_and_stop_after_uncertainty() {
    use clean_my_agent::{actions, inventory};
    use std::{
        os::unix::fs::PermissionsExt,
        time::{Duration, SystemTime},
    };
    let _guard = ENVIRONMENT_LOCK.lock().unwrap();
    let temp = tempfile::tempdir().unwrap();
    let home = temp.path().canonicalize().unwrap();
    let mut environment = Environment(vec![]);
    for (key, value) in [
        ("HOME", home.clone()),
        ("CODEX_HOME", home.join(".codex")),
        ("CLAUDE_CONFIG_DIR", home.join(".claude")),
        ("T3_HOME", home.join(".t3")),
        ("XDG_CONFIG_HOME", home.join("config")),
        ("XDG_STATE_HOME", home.join("state")),
        ("XDG_CACHE_HOME", home.join("cache")),
    ] {
        environment.0.push((key.into(), std::env::var_os(key)));
        unsafe {
            std::env::set_var(key, value);
        }
    }
    let bin = home.join("bin");
    fs::create_dir(&bin).unwrap();
    let old_path = std::env::var_os("PATH");
    let paths = std::iter::once(bin.clone()).chain(std::env::split_paths(
        old_path.as_deref().unwrap_or_default(),
    ));
    let path = std::env::join_paths(paths).unwrap();
    environment.0.push(("PATH".into(), old_path));
    unsafe {
        std::env::set_var("PATH", path);
    }
    let root = home.join(".codex");
    fs::create_dir_all(root.join("sessions")).unwrap();
    for n in 1..=2 {
        let file = root.join(format!(
            "sessions/rollout-01234567-89ab-cdef-9012-{n:012}.jsonl"
        ));
        fs::write(&file, "{}\n").unwrap();
        fs::File::open(file)
            .unwrap()
            .set_times(
                fs::FileTimes::new()
                    .set_modified(SystemTime::now() - Duration::from_secs(90 * 86400)),
            )
            .unwrap();
    }
    let codex = bin.join("codex");
    let script = |body: &str| {
        fs::write(&codex,format!("#!/bin/sh\nif [ \"$2\" = --help ]; then echo 'Usage: codex --force'; exit 0; fi\n{body}\n")).unwrap();
        fs::set_permissions(&codex, fs::Permissions::from_mode(0o700)).unwrap();
    };
    let layout = Layout::load().unwrap();
    let policy = Policy::load(None, None).unwrap();
    let progress = Progress::default();
    let rules = json!({"archive_days":10,"delete_days":20});
    let (_, rows) =
        inventory::inspect(&layout, &policy, "codex", None, None, false, 10, &progress).unwrap();
    let selected = rows["codex"].clone();
    assert_eq!(selected.len(), 2);
    script("exit 0");
    let result = actions::sessions(&layout, "archive", &selected, &rules, &progress).unwrap();
    assert_eq!(result["results"][0]["status"], "uncertain");
    assert_eq!(result["results"][1]["status"], "skipped");
    script("echo 'fixture failure' >&2; exit 3");
    let result = actions::sessions(&layout, "archive", &selected, &rules, &progress).unwrap();
    assert_eq!(result["results"][0]["status"], "failed");
    assert_eq!(result["results"][1]["status"], "skipped");
    script(
        "mkdir -p \"$CODEX_HOME/archived_sessions\"\nmv \"$CODEX_HOME/sessions/rollout-$2.jsonl\" \"$CODEX_HOME/archived_sessions/rollout-$2.jsonl\"",
    );
    let result = actions::sessions(&layout, "archive", &selected, &rules, &progress).unwrap();
    assert!(
        result["results"]
            .as_array()
            .unwrap()
            .iter()
            .all(|r| r["status"] == "succeeded"),
        "{result}"
    );
    let (_, rows) =
        inventory::inspect(&layout, &policy, "codex", None, None, false, 10, &progress).unwrap();
    assert!(rows["codex"].iter().all(|r| r["state"] == "archived"));
    script(
        "test \"$3\" = --force || exit 4\nrm \"$CODEX_HOME/archived_sessions/rollout-$2.jsonl\"",
    );
    let result = actions::sessions(&layout, "delete", &rows["codex"], &rules, &progress).unwrap();
    assert!(
        result["results"]
            .as_array()
            .unwrap()
            .iter()
            .all(|r| r["status"] == "succeeded"),
        "{result}"
    );
    assert!(
        inventory::inspect(&layout, &policy, "codex", None, None, false, 10, &progress)
            .unwrap()
            .1["codex"]
            .is_empty()
    );
}
