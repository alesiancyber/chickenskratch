//! chickenskratchd — the headless trigger.
//!
//! Deliberately dumb: watch one directory tree, wait for it to go quiet,
//! run one command (`uv run chickenskratch-watch`). Every ounce of engine
//! intelligence stays in Python — the pass lock over there serializes
//! concurrent triggers, so this daemon needs no coordination with anything.
//! If this binary disappears, `chickenskratch-watch` by hand or cron does the
//! same job; nothing depends on it.
//!
//! Trigger model: events as the trigger, fingerprint as the truth.
//! Filesystem events (FSEvents/inotify via the notify crate) are the
//! trigger — zero idle cost, latency = settle time — while the recursive
//! (path, mtime) fingerprint scan is the pre-fire check and post-pass
//! baseline, so noise events never fire a pass and the daemon's own pass
//! writes never re-trigger it. `--poll` forces the timer loop for
//! filesystems where events are unreliable (network mounts), and the
//! daemon falls back to it automatically if the watcher fails.

use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::mpsc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use std::{env, fs, io, process, thread};

use notify::{RecursiveMode, Watcher};

/// Never scanned or reacted to: vault-internal machinery and build junk
/// whose mtimes are noise (mirrors vault.py's dot-dir rule plus the vault
/// .gitignore venv rule). Hidden directories are skipped wholesale.
const SKIP_DIRS: &[&str] = &[".venv", "__pycache__", "target", "node_modules"];

struct Snapshot {
    fingerprint: u64,
    newest: Option<SystemTime>,
    files: usize,
}

/// One pass over the tree: hash every (relative path, mtime) pair into a
/// single fingerprint, tracking the newest mtime for the settle check.
/// Creates, edits, deletes, and renames all move the fingerprint.
fn scan(root: &Path) -> io::Result<Snapshot> {
    let mut hasher = DefaultHasher::new();
    let mut snap = Snapshot { fingerprint: 0, newest: None, files: 0 };
    walk(root, root, &mut hasher, &mut snap)?;
    snap.fingerprint = hasher.finish();
    Ok(snap)
}

fn walk(root: &Path, dir: &Path, hasher: &mut DefaultHasher, snap: &mut Snapshot) -> io::Result<()> {
    let mut entries: Vec<fs::DirEntry> = fs::read_dir(dir)?.collect::<Result<_, _>>()?;
    entries.sort_by_key(|e| e.path());
    for entry in entries {
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.starts_with('.') {
            continue;
        }
        let path = entry.path();
        let file_type = entry.file_type()?;
        if file_type.is_dir() {
            if !SKIP_DIRS.contains(&name.as_str()) {
                walk(root, &path, hasher, snap)?;
            }
        } else if file_type.is_file() {
            let mtime = entry.metadata()?.modified()?;
            path.strip_prefix(root).unwrap_or(&path).hash(hasher);
            let since_epoch = mtime.duration_since(UNIX_EPOCH).unwrap_or_default();
            (since_epoch.as_secs(), since_epoch.subsec_nanos()).hash(hasher);
            snap.newest = Some(snap.newest.map_or(mtime, |n: SystemTime| n.max(mtime)));
            snap.files += 1;
        }
    }
    Ok(())
}

/// Is an event path one the harness cares about? Everything under a hidden
/// or skip directory (git internals, editor machinery, build junk) is
/// noise; anything outside the vault root is someone else's business.
fn relevant(root: &Path, path: &Path) -> bool {
    let rel = match path.strip_prefix(root) {
        Ok(r) => r,
        Err(_) => return false,
    };
    !rel.components().any(|c| {
        let s = c.as_os_str().to_string_lossy();
        s.starts_with('.') || SKIP_DIRS.contains(&s.as_ref())
    })
}

/// Fire only when the tree changed AND has been quiet for `settle` —
/// never mid-keystroke (the engine would extract from a half-typed note).
fn should_fire(changed: bool, newest_age: Duration, settle: Duration) -> bool {
    changed && newest_age >= settle
}

struct Config {
    vault: PathBuf,
    repo: PathBuf,
    cmd: Vec<String>,
    interval: Duration,
    settle: Duration,
    /// Poll period for the fallback timer loop.
    tick: Duration,
    /// Force the fallback timer loop instead of filesystem events.
    poll: bool,
}

const USAGE: &str = "\
chickenskratchd — watch the vault, wait for quiet, run one engine pass

USAGE: chickenskratchd --vault <path> [--repo <path>] [--cmd \"uv run chickenskratch-watch\"]
               [--settle-secs 3] [--poll] [--interval-secs 5] [--tick-ms 300]

  --vault             vault to watch (or CHICKENSKRATCH_VAULT_PATH env)
  --repo              working directory for the pass command (default: cwd)
  --cmd               pass command, whitespace-split (default: uv run chickenskratch-watch)
  --settle-secs       quiet time required before a pass fires (default 3)
  --poll              use the fallback timer loop instead of filesystem events
  --interval-secs     [poll mode] seconds between tree scans (default 5)
  --tick-ms           [poll mode] sleep granularity (default 300)

Note: there is no --fast-file/--fast-cmd decision lane. Decisions arrive
through the dashboard or `chickenskratch decide`, neither of which needs a file
watcher.";

impl Config {
    fn parse(mut args: impl Iterator<Item = String>) -> Result<Config, String> {
        let mut vault = env::var("CHICKENSKRATCH_VAULT_PATH").ok().map(PathBuf::from);
        let mut repo = env::current_dir().map_err(|e| e.to_string())?;
        let mut cmd = "uv run chickenskratch-watch".to_string();
        let mut interval = 5u64;
        let mut settle = 3u64;
        let mut tick = 300u64;
        let mut poll = false;
        while let Some(flag) = args.next() {
            let mut value = |name: &str| args.next().ok_or(format!("{name} needs a value"));
            match flag.as_str() {
                "--vault" => vault = Some(PathBuf::from(value("--vault")?)),
                "--repo" => repo = PathBuf::from(value("--repo")?),
                "--cmd" => cmd = value("--cmd")?,
                "--interval-secs" => interval = value("--interval-secs")?.parse().map_err(|_| "bad --interval-secs")?,
                "--settle-secs" => settle = value("--settle-secs")?.parse().map_err(|_| "bad --settle-secs")?,
                "--tick-ms" => tick = value("--tick-ms")?.parse().map_err(|_| "bad --tick-ms")?,
                "--poll" => poll = true,
                "--help" | "-h" => return Err(USAGE.to_string()),
                other => return Err(format!("unknown flag {other}\n\n{USAGE}")),
            }
        }
        let vault = vault.ok_or(format!("--vault (or CHICKENSKRATCH_VAULT_PATH) is required\n\n{USAGE}"))?;
        if !vault.is_dir() {
            return Err(format!("vault directory not found: {}", vault.display()));
        }
        let cmd: Vec<String> = cmd.split_whitespace().map(str::to_string).collect();
        Ok(Config {
            vault,
            repo,
            cmd,
            interval: Duration::from_secs(interval),
            settle: Duration::from_secs(settle),
            tick: Duration::from_millis(tick.max(50)),
            poll,
        })
    }
}

fn log(msg: &str) {
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
    println!("chickenskratchd[{secs}] {msg}");
}

fn run_cmd(cmd: &[String], repo: &Path) {
    match Command::new(&cmd[0]).args(&cmd[1..]).current_dir(repo).status() {
        Ok(status) if status.success() => {}
        Ok(status) => log(&format!("pass command exited with {status}")),
        Err(e) => log(&format!("failed to spawn {:?}: {e}", cmd[0])),
    }
}

fn run_pass(cfg: &Config) {
    run_cmd(&cfg.cmd, &cfg.repo);
}

/// The event loop: block on filesystem events, fire a full pass once the
/// tree has been quiet for `settle` — verified against the fingerprint so
/// noise events (or our own drained-and-missed writes) never spawn a pass.
fn event_loop(cfg: &Config, baseline: &mut Snapshot) -> notify::Result<()> {
    let root = cfg.vault.canonicalize().unwrap_or_else(|_| cfg.vault.clone());

    let (tx, rx) = mpsc::channel::<bool>(); // tree_changed
    let root_cb = root.clone();
    let mut watcher = notify::recommended_watcher(move |res: notify::Result<notify::Event>| {
        let ev = match res {
            Ok(ev) => ev,
            // Watch errors (queue overflow etc.): assume the tree moved —
            // the fingerprint check makes a false positive free.
            Err(_) => {
                let _ = tx.send(true);
                return;
            }
        };
        if ev.kind.is_access() {
            return;
        }
        if ev.paths.iter().any(|p| relevant(&root_cb, p)) {
            let _ = tx.send(true);
        }
    })?;
    watcher.watch(&root, RecursiveMode::Recursive)?;
    log("event watch active (notify/FSEvents) — zero idle scans, latency = settle time");

    let mut pending: Option<Instant> = None; // last relevant tree event
    loop {
        // Wait for the next event, or only as long as the settle deadline.
        let received = if let Some(t0) = pending {
            let wait = cfg.settle.saturating_sub(t0.elapsed());
            if wait.is_zero() { None } else { rx.recv_timeout(wait).ok() }
        } else {
            match rx.recv() {
                Ok(ev) => Some(ev),
                Err(_) => return Ok(()), // watcher gone; caller falls back to polling
            }
        };
        if received.is_some() {
            pending = Some(Instant::now()); // every event resets the settle clock
        }
        // absorb the rest of the burst (editors write in flurries)
        while rx.try_recv().is_ok() {
            pending = Some(Instant::now());
        }

        if let Some(t0) = pending {
            if t0.elapsed() >= cfg.settle {
                pending = None;
                let cur = match scan(&cfg.vault) {
                    Ok(s) => s,
                    Err(e) => {
                        log(&format!("scan failed, waiting for next event: {e}"));
                        continue;
                    }
                };
                if cur.fingerprint != baseline.fingerprint {
                    log("vault changed and settled — running pass");
                    run_pass(cfg);
                    // Re-baseline after the pass so its own writes (renders,
                    // snapshot commits) never feed the next trigger.
                    *baseline = scan(&cfg.vault).unwrap_or(cur);
                    while rx.try_recv().is_ok() {}
                }
            }
        }
    }
}

/// The fallback timer loop (`--poll`, and the automatic fallback when the
/// event watcher can't start — e.g. network mounts without inotify).
fn poll_loop(cfg: &Config, mut baseline: Snapshot) -> ! {
    let ticks_per_full = (cfg.interval.as_millis() / cfg.tick.as_millis()).max(1);
    let mut tick: u128 = 0;
    loop {
        thread::sleep(cfg.tick);
        tick += 1;
        if tick % ticks_per_full != 0 {
            continue;
        }
        let cur = match scan(&cfg.vault) {
            Ok(s) => s,
            Err(e) => {
                log(&format!("scan failed, retrying next tick: {e}"));
                continue;
            }
        };
        let changed = cur.fingerprint != baseline.fingerprint;
        let newest_age = cur
            .newest
            .and_then(|t| t.elapsed().ok())
            .unwrap_or(Duration::MAX); // unknowable mtimes never block a fire
        if !should_fire(changed, newest_age, cfg.settle) {
            continue; // unchanged, or still being typed in — wait for quiet
        }
        log("vault changed and settled — running pass");
        run_pass(cfg);
        baseline = scan(&cfg.vault).unwrap_or(cur);
    }
}

fn main() {
    let cfg = Config::parse(env::args().skip(1)).unwrap_or_else(|msg| {
        eprintln!("{msg}");
        process::exit(2);
    });
    let mut baseline = scan(&cfg.vault).unwrap_or_else(|e| {
        eprintln!("cannot scan {}: {e}", cfg.vault.display());
        process::exit(1);
    });
    log(&format!(
        "watching {} ({} files), pass = {:?} in {}",
        cfg.vault.display(), baseline.files, cfg.cmd.join(" "), cfg.repo.display(),
    ));
    // One pass at startup: whatever changed while the daemon was down is
    // picked up immediately (the engine's own memo makes it free if nothing did).
    run_pass(&cfg);
    baseline = scan(&cfg.vault).unwrap_or(baseline);

    if !cfg.poll {
        match event_loop(&cfg, &mut baseline) {
            Ok(()) => log("event watcher stopped — falling back to polling"),
            Err(e) => log(&format!("event watch unavailable ({e}) — falling back to polling")),
        }
    }
    poll_loop(&cfg, baseline);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_vault(tag: &str) -> PathBuf {
        let dir = env::temp_dir().join(format!("chickenskratchd-test-{}-{tag}", process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("Daily")).unwrap();
        fs::create_dir_all(dir.join(".obsidian")).unwrap();
        fs::create_dir_all(dir.join("Scripts/.venv")).unwrap();
        fs::write(dir.join("Daily/note.md"), "hello").unwrap();
        fs::write(dir.join(".obsidian/workspace.json"), "{}").unwrap();
        fs::write(dir.join("Scripts/.venv/junk.py"), "x = 1").unwrap();
        dir
    }

    #[test]
    fn fingerprint_is_stable_and_moves_on_edit_create_delete() {
        let dir = temp_vault("edits");
        let a = scan(&dir).unwrap();
        let b = scan(&dir).unwrap();
        assert_eq!(a.fingerprint, b.fingerprint, "rescan of unchanged tree must match");
        assert_eq!(a.files, 1, "only the visible note counts");

        fs::write(dir.join("Daily/note.md"), "hello edited").unwrap();
        let edited = scan(&dir).unwrap();
        assert_ne!(a.fingerprint, edited.fingerprint, "edit must move the fingerprint");

        fs::write(dir.join("Daily/new.md"), "new").unwrap();
        let created = scan(&dir).unwrap();
        assert_ne!(edited.fingerprint, created.fingerprint, "create must move the fingerprint");

        fs::remove_file(dir.join("Daily/new.md")).unwrap();
        let deleted = scan(&dir).unwrap();
        assert_ne!(created.fingerprint, deleted.fingerprint, "delete must move the fingerprint");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn hidden_and_skip_dirs_are_invisible() {
        let dir = temp_vault("skips");
        let before = scan(&dir).unwrap();
        fs::write(dir.join(".obsidian/workspace.json"), "{\"changed\": true}").unwrap();
        fs::write(dir.join("Scripts/.venv/junk.py"), "x = 2").unwrap();
        let after = scan(&dir).unwrap();
        assert_eq!(before.fingerprint, after.fingerprint, "noise dirs must never trigger");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn event_relevance_mirrors_the_scan_rules() {
        let root = PathBuf::from("/v");
        assert!(relevant(&root, Path::new("/v/Daily/note.md")));
        assert!(relevant(&root, Path::new("/v/Tasks/detections.md")));
        assert!(!relevant(&root, Path::new("/v/.git/index")), "git churn is noise");
        assert!(!relevant(&root, Path::new("/v/.obsidian/workspace.json")));
        assert!(!relevant(&root, Path::new("/v/.foam/templates/t.md")));
        assert!(!relevant(&root, Path::new("/v/Scripts/.venv/x.py")));
        assert!(!relevant(&root, Path::new("/v/Scripts/__pycache__/x.pyc")));
        assert!(!relevant(&root, Path::new("/elsewhere/file.md")), "outside the vault");
    }

    #[test]
    fn defaults_are_events_and_one_pass_command() {
        let dir = temp_vault("cfg");
        let cfg = Config::parse(
            vec!["--vault".to_string(), dir.to_string_lossy().into_owned()].into_iter(),
        ).unwrap();
        assert_eq!(cfg.cmd.join(" "), "uv run chickenskratch-watch");
        assert!(!cfg.poll, "events are the default; --poll is the fallback");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_retired_decision_lane_is_rejected_not_ignored() {
        // A stale launchd plist or shell alias still passing --fast-file
        // must fail loudly: silently dropping it would leave someone
        // believing checkbox decisions still work.
        let dir = temp_vault("cfg-fast");
        let err = match Config::parse(
            vec!["--vault".to_string(), dir.to_string_lossy().into_owned(),
                 "--fast-file".to_string(), "Review.md".to_string()].into_iter(),
        ) {
            Err(e) => e,
            Ok(_) => panic!("--fast-file was accepted; it must be rejected"),
        };
        assert!(err.contains("unknown flag --fast-file"), "got: {err}");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn settle_gates_the_fire() {
        let settle = Duration::from_secs(3);
        assert!(!should_fire(false, Duration::from_secs(10), settle), "unchanged never fires");
        assert!(!should_fire(true, Duration::from_secs(1), settle), "still typing never fires");
        assert!(should_fire(true, Duration::from_secs(3), settle), "changed + quiet fires");
    }
}
