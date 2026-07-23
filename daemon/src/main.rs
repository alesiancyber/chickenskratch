//! tezcatd — the headless trigger (DEVPLAN Phase 8, DESIGN-processors.md §2).
//!
//! Deliberately dumb: watch one directory tree, wait for it to go quiet,
//! run one command (`uv run harness-watch`). Every ounce of engine
//! intelligence stays in Python — the pass lock over there serializes
//! concurrent triggers, so this daemon needs no coordination with the
//! web server's own poll loop. If this binary disappears, `harness-watch`
//! by hand or cron does the same job; nothing depends on it.

use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use std::{env, fs, io, process, thread};

/// Never scanned: vault-internal machinery and build junk whose mtimes
/// are noise (mirrors vault.py's IGNORED_DIRS plus the vault .gitignore
/// venv rule). Hidden directories are skipped wholesale.
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
    /// Decision surface (Review.md): a checkbox toggle is an atomic write,
    /// so it gets a fast lane — no settle, checked every `fast_interval`.
    fast_file: Option<PathBuf>,
    fast_cmd: Vec<String>,
    fast_interval: Duration,
}

const USAGE: &str = "\
tezcatd — watch the vault, wait for quiet, run one engine pass

USAGE: tezcatd --vault <path> [--repo <path>] [--cmd \"uv run harness-watch\"]
               [--interval-secs 5] [--settle-secs 3]
               [--fast-file <path>] [--fast-cmd \"...\"] [--fast-interval-ms 300]

  --vault             vault to watch (or OBSIDIAN_VAULT_PATH env)
  --repo              working directory for the pass command (default: cwd)
  --cmd               pass command, whitespace-split (default: uv run harness-watch)
  --interval-secs     seconds between tree scans (default 5)
  --settle-secs       quiet time required before a pass fires (default 3)
  --fast-file         decision surface checked every fast interval, no settle
  --fast-cmd          command for fast-file changes (default: cmd + --decisions-only)
  --fast-interval-ms  fast lane check period (default 300)";

impl Config {
    fn parse(mut args: impl Iterator<Item = String>) -> Result<Config, String> {
        let mut vault = env::var("OBSIDIAN_VAULT_PATH").ok().map(PathBuf::from);
        let mut repo = env::current_dir().map_err(|e| e.to_string())?;
        let mut cmd = "uv run harness-watch".to_string();
        let mut interval = 5u64;
        let mut settle = 3u64;
        let mut fast_file: Option<PathBuf> = None;
        let mut fast_cmd: Option<String> = None;
        let mut fast_interval = 300u64;
        while let Some(flag) = args.next() {
            let mut value = |name: &str| args.next().ok_or(format!("{name} needs a value"));
            match flag.as_str() {
                "--vault" => vault = Some(PathBuf::from(value("--vault")?)),
                "--repo" => repo = PathBuf::from(value("--repo")?),
                "--cmd" => cmd = value("--cmd")?,
                "--interval-secs" => interval = value("--interval-secs")?.parse().map_err(|_| "bad --interval-secs")?,
                "--settle-secs" => settle = value("--settle-secs")?.parse().map_err(|_| "bad --settle-secs")?,
                "--fast-file" => fast_file = Some(PathBuf::from(value("--fast-file")?)),
                "--fast-cmd" => fast_cmd = Some(value("--fast-cmd")?),
                "--fast-interval-ms" => fast_interval = value("--fast-interval-ms")?.parse().map_err(|_| "bad --fast-interval-ms")?,
                "--help" | "-h" => return Err(USAGE.to_string()),
                other => return Err(format!("unknown flag {other}\n\n{USAGE}")),
            }
        }
        let vault = vault.ok_or(format!("--vault (or OBSIDIAN_VAULT_PATH) is required\n\n{USAGE}"))?;
        if !vault.is_dir() {
            return Err(format!("vault directory not found: {}", vault.display()));
        }
        let cmd: Vec<String> = cmd.split_whitespace().map(str::to_string).collect();
        let fast_cmd = match fast_cmd {
            Some(s) => s.split_whitespace().map(str::to_string).collect(),
            None => {
                let mut c = cmd.clone();
                c.push("--decisions-only".to_string());
                c
            }
        };
        Ok(Config {
            vault,
            repo,
            cmd,
            interval: Duration::from_secs(interval),
            settle: Duration::from_secs(settle),
            fast_file,
            fast_cmd,
            fast_interval: Duration::from_millis(fast_interval.max(50)),
        })
    }
}

fn log(msg: &str) {
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
    println!("tezcatd[{secs}] {msg}");
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

fn mtime_of(p: &Path) -> Option<SystemTime> {
    std::fs::metadata(p).and_then(|m| m.modified()).ok()
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

    // The fast lane checks the decision surface every fast_interval; the
    // full scan keeps its own cadence on top of the same clock.
    let ticks_per_full = (cfg.interval.as_millis() / cfg.fast_interval.as_millis()).max(1);
    let mut last_fast = cfg.fast_file.as_deref().and_then(mtime_of);
    let mut tick: u128 = 0;
    loop {
        thread::sleep(cfg.fast_interval);
        if let Some(ff) = cfg.fast_file.as_deref() {
            let m = mtime_of(ff);
            if m != last_fast {
                log("decision surface changed — fast pass");
                run_cmd(&cfg.fast_cmd, &cfg.repo);
                last_fast = mtime_of(ff); // our own re-render isn't a decision
            } else {
                last_fast = m;
            }
        }
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
        run_pass(&cfg);
        // Re-baseline after the pass so its own writes (Review.md render,
        // snapshot commits) never re-trigger the next tick.
        baseline = scan(&cfg.vault).unwrap_or(cur);
        last_fast = cfg.fast_file.as_deref().and_then(mtime_of);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_vault(tag: &str) -> PathBuf {
        let dir = env::temp_dir().join(format!("tezcatd-test-{}-{tag}", process::id()));
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
    fn fast_cmd_defaults_to_decisions_only() {
        let dir = temp_vault("cfg");
        let cfg = Config::parse(
            vec!["--vault".to_string(), dir.to_string_lossy().into_owned(),
                 "--fast-file".to_string(), "Review.md".to_string()].into_iter(),
        ).unwrap();
        assert_eq!(cfg.fast_cmd.last().unwrap(), "--decisions-only");
        assert_eq!(cfg.cmd.join(" "), "uv run harness-watch");
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
