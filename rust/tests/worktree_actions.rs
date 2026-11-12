#![cfg(unix)]
use clean_my_agent::{
    fsutil,
    policy::{Layout, Policy},
    progress::{Progress, Update},
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
fn artifact_batch_checks_only_selected_sizes_and_rechecks_references_before_moves() {
    use clean_my_agent::{actions, dependency_report, vault};
    use std::{
        collections::BTreeSet,
        os::unix::fs::symlink,
        sync::mpsc,
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
    let layout = Layout::load().unwrap();
    let policy = Policy::load(None, None).unwrap();
    let root = &layout.roots["codex"];
    let repo = root.join("worktrees/batch");
    fs::create_dir_all(&repo).unwrap();
    fs::write(
        repo.join("Cargo.toml"),
        "[package]\nname='fixture'\nversion='0.1.0'\n",
    )
    .unwrap();
    fs::write(repo.join(".gitignore"), "target/\nbuild/\n").unwrap();
    git(&repo, &["init", "-q"]);
    commit(&repo);
    let paths: BTreeSet<_> = ["build", "target"].map(|n| repo.join(n)).into();
    for path in &paths {
        fs::create_dir(path).unwrap();
        fs::write(path.join("generated"), "fixture").unwrap();
        for p in [path.clone(), path.join("generated")] {
            fs::File::open(p)
                .unwrap()
                .set_times(
                    fs::FileTimes::new()
                        .set_modified(SystemTime::now() - Duration::from_secs(90 * 86400)),
                )
                .unwrap();
        }
    }
    let outside = home.join("outside");
    git(
        &repo,
        &[
            "worktree",
            "add",
            "-q",
            "--detach",
            outside.to_str().unwrap(),
        ],
    );
    // Enough source entries to exercise progress within a single checkout.
    for n in 0..2050 {
        fs::write(outside.join(format!("source-{n}")), "").unwrap();
    }
    let (tx, rx) = mpsc::channel();
    let progress = Progress::new(tx);
    let report =
        dependency_report::inspect_selected(root, &policy, &paths, "codex", &progress).unwrap();
    assert_eq!(report["projects"], json!([]));
    assert_eq!(report["errors"], json!([]));
    let selected = report["entries"].as_array().unwrap();
    assert_eq!(selected.len(), 2);
    assert!(
        selected.iter().all(|r| r["status"] == "candidate"),
        "{report}"
    );
    let updates: Vec<_> = rx.try_iter().collect();
    assert!(
        updates
            .iter()
            .any(|u| u.phase == "复核工作树引用" && u.detail.contains("已检查 2048 项"))
    );
    // The report does not depend on how many scan threads produced it.
    let workers = "CLEAN_MY_AGENT_SCAN_WORKERS";
    let configured = std::env::var_os(workers);
    environment.0.push((workers.into(), configured));
    for count in ["1", "4"] {
        unsafe {
            std::env::set_var(workers, count);
        }
        let again =
            dependency_report::inspect_selected(root, &policy, &paths, "codex", &progress).unwrap();
        assert_eq!(again, report, "{count} 个线程");
    }
    unsafe {
        std::env::remove_var(workers);
    }
    rx.try_iter().count();
    let result = actions::stage_artifacts(&layout, &policy, selected, &progress).unwrap();
    assert_eq!(result["count"], 2, "{result}");
    assert_eq!(result["errors"], json!([]));
    let updates: Vec<_> = rx.try_iter().collect();
    assert!(
        !updates
            .iter()
            .any(|u| u.phase.contains("统计工作树完整占用"))
    );
    // One shared preflight, then one review shared by both moves.
    let completed = |updates: &[Update]| -> Vec<String> {
        updates
            .iter()
            .filter(|u| {
                u.phase.contains("复核工作树引用") && u.total.is_some() && u.done == u.total
            })
            .map(|u| u.phase.clone())
            .collect()
    };
    let reviews = completed(&updates);
    assert_eq!(reviews.len(), 2, "{reviews:?}");
    assert!(reviews[0].starts_with("暂存前复核 codex（2 项）"));
    assert!(reviews[1].starts_with("暂存 1/2 · 移动前复核 codex（2 项）"));
    let restore = |batch: &str| {
        let restored =
            vault::process(&layout, batch, "restore", true, None, None, &progress).unwrap();
        assert_eq!(restored.count, 2, "{restored:?}");
    };
    restore(result["batch"].as_str().unwrap());

    // A review older than its limit is repeated before the next move.
    let items: Vec<_> = selected
        .iter()
        .map(|row| {
            let path = Path::new(row["path"].as_str().unwrap());
            json!({"source":"codex", "root":root, "path":path.strip_prefix(root).unwrap(),
                "category":"dependencies", "size":row["directory_bytes"],
                "stamp":row["stamp"], "project":row["project"]})
        })
        .collect();
    rx.try_iter().count();
    let saved = vault::save_within(&items, &layout, &policy, Duration::ZERO, &progress).unwrap();
    assert_eq!((saved.count, &saved.errors), (2, &vec![]));
    let reviews = completed(&rx.try_iter().collect::<Vec<_>>());
    assert_eq!(reviews.len(), 2, "{reviews:?}");
    assert!(reviews[0].starts_with("暂存 1/2 · 移动前复核 codex（2 项）"));
    assert!(reviews[1].starts_with("暂存 2/2"));
    restore(saved.batch.as_deref().unwrap());

    // A reference added after the preview must be caught even when vault::save
    // is called without the batch preflight (the final per-item guard).
    let referenced = paths.first().unwrap();
    symlink(referenced, outside.join("arbitrary-shared-output")).unwrap();
    assert!(actions::stage_artifacts(&layout, &policy, selected, &progress).is_err());
    assert!(paths.iter().all(|p| p.is_dir()));
    let row = selected
        .iter()
        .find(|r| r["path"].as_str() == referenced.to_str())
        .unwrap();
    let item = json!({"source":"codex", "root":root,
        "path":referenced.strip_prefix(root).unwrap(), "category":"dependencies",
        "size":row["directory_bytes"], "stamp":row["stamp"], "project":row["project"]});
    let saved = vault::save(&[item], &layout, &policy, &progress).unwrap();
    assert_eq!(saved.count, 0);
    assert!(saved.errors.iter().any(|e| e.contains("引用")), "{saved:?}");
    assert!(referenced.join("generated").is_file());
    fs::remove_file(outside.join("arbitrary-shared-output")).unwrap();
    symlink("cycle", outside.join("cycle")).unwrap();
    let incomplete =
        dependency_report::inspect_selected(root, &policy, &paths, "codex", &progress).unwrap();
    assert!(!incomplete["errors"].as_array().unwrap().is_empty());
    assert!(
        incomplete["entries"]
            .as_array()
            .unwrap()
            .iter()
            .all(|r| r["status"] == "retained")
    );

    // An incomplete shared review moves nothing, whichever directory it is about.
    let saved = vault::save(&items, &layout, &policy, &progress).unwrap();
    assert_eq!(saved.count, 0, "{saved:?}");
    assert_eq!(saved.errors.len(), 2, "{saved:?}");
    assert!(paths.iter().all(|p| p.join("generated").is_file()));
    fs::remove_file(outside.join("cycle")).unwrap();

    // Permanent deletion reviews references once for the preview and once for
    // the apply, and still retains an entry that gained a reference in between.
    let staged = actions::stage_artifacts(&layout, &policy, selected, &progress).unwrap();
    assert_eq!(staged["count"], 2, "{staged}");
    let listed = vault::list_entries(&layout, &progress).unwrap();
    let rows = listed["items"].as_array().unwrap();
    assert_eq!(rows.len(), 2, "{listed}");
    rx.try_iter().count();
    let preview = actions::quarantine_preview(&layout, "purge", rows, &progress).unwrap();
    assert_eq!(completed(&rx.try_iter().collect::<Vec<_>>()).len(), 1);
    symlink(referenced, outside.join("late-reference")).unwrap();
    let purged = actions::quarantine_apply(&layout, &preview, &progress).unwrap();
    assert_eq!(completed(&rx.try_iter().collect::<Vec<_>>()).len(), 1);
    assert_eq!(purged["count"], 1, "{purged}");
    let errors = purged["errors"].as_array().unwrap();
    assert!(
        errors.len() == 1 && errors[0].as_str().unwrap().contains("引用"),
        "{purged}"
    );
    let listed = vault::list_entries(&layout, &progress).unwrap();
    let kept = listed["items"].as_array().unwrap();
    assert_eq!(kept.len(), 1, "{listed}");
    assert_eq!(kept[0]["path"].as_str(), referenced.to_str());
    assert!(actions::quarantine_apply(&layout, &preview, &progress).is_err());
    fs::remove_file(outside.join("late-reference")).unwrap();
    let preview = actions::quarantine_preview(&layout, "purge", kept, &progress).unwrap();
    let purged = actions::quarantine_apply(&layout, &preview, &progress).unwrap();
    assert_eq!(
        (&purged["count"], &purged["errors"]),
        (&json!(1), &json!([]))
    );
    assert_eq!(
        vault::list_entries(&layout, &progress).unwrap()["items"],
        json!([])
    );
    assert!(paths.iter().all(|p| !p.exists()));
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
